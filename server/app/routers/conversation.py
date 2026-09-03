"""租客与房东围绕某套房源的私聊（C-1）。

从 `main.py` 拆出来，见 `app/routers/__init__.py` 的说明。
这一组的请求模型也放在这里——它们只被这一组用，搬到公共文件反而要在
两个文件之间来回跳。
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app import leasing, moderation
from app.db import get_session
from app.deps import require_login
from app.models import ChatLine, ContentFlag, Conversation, House, User

router = APIRouter()



class ConversationStart(BaseModel):
    house_no: str


class LineCreate(BaseModel):
    content: str = Field(min_length=1, max_length=500)


def _my_conversation(session, conversation_id, user):
    """取出会话并校验：只有当事双方与管理员能碰。

    **每个会话接口都必须过这一关。** 少一处，改个 id 就能读走别人的
    私聊——这类越权本项目已经修过两次（工单列表、对话记录），
    是同一类错误。
    """
    conversation = session.get(Conversation, conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="会话不存在")
    if user.role != "admin" and user.id not in (conversation.tenant_id,
                                                conversation.landlord_id):
        raise HTTPException(status_code=403, detail="无权查看该会话")
    return conversation


@router.post("/api/conversations")
def start_conversation(payload: ConversationStart,
                       user: User = Depends(require_login),
                       session: Session = Depends(get_session)):
    """开启与房东的会话。

    **幂等**：同一租客同一房源只有一个会话，重复点「咨询房东」返回原有的。
    数据库上有 UNIQUE(house_id, tenant_id) 兜底。
    """
    house = session.scalar(
        select(House).where(House.house_no == payload.house_no))
    if not house:
        raise HTTPException(status_code=404, detail="房源不存在")
    if house.landlord_id == user.id:
        raise HTTPException(status_code=400, detail="不能和自己的房源发起咨询")

    existing = session.scalar(select(Conversation).where(
        Conversation.house_id == house.id,
        Conversation.tenant_id == user.id))
    if existing:
        return {"ok": True, "conversation": existing.to_dict(user.id)}

    conversation = Conversation(house_id=house.id, tenant_id=user.id,
                                landlord_id=house.landlord_id)
    session.add(conversation)
    session.commit()
    session.refresh(conversation)
    return {"ok": True, "conversation": conversation.to_dict(user.id)}


@router.get("/api/conversations")
def list_conversations(user: User = Depends(require_login),
                       session: Session = Depends(get_session)):
    """我的会话列表，最近有消息的排前面。"""
    rows = session.scalars(select(Conversation).where(
        or_(Conversation.tenant_id == user.id,
            Conversation.landlord_id == user.id)
    ).order_by(Conversation.last_at.desc())).all()
    unread = sum((c.landlord_unread if c.landlord_id == user.id
                  else c.tenant_unread) for c in rows)
    return {"total": len(rows), "unread": unread,
            "list": [c.to_dict(user.id) for c in rows]}


@router.get("/api/conversations/{conversation_id}/lines")
def list_lines(conversation_id: int, after_id: int = 0,
               limit: int = Query(50, ge=1, le=200),
               user: User = Depends(require_login),
               session: Session = Depends(get_session)):
    """消息明细。

    `after_id` 用于轮询增量：面板打开时每十秒问一次「比这条新的有吗」，
    而不是每次把整段对话重拉一遍。
    """
    conversation = _my_conversation(session, conversation_id, user)
    stmt = select(ChatLine).where(
        ChatLine.conversation_id == conversation.id)
    if after_id:
        stmt = stmt.where(ChatLine.id > after_id)
    rows = session.scalars(
        stmt.order_by(ChatLine.id).limit(limit)).all()
    return {"total": len(rows),
            "list": [line.to_dict(user.id) for line in rows]}


@router.post("/api/conversations/{conversation_id}/lines")
def send_line(conversation_id: int, payload: LineCreate,
              user: User = Depends(require_login),
              session: Session = Depends(get_session)):
    """发一条消息。

    **发出去之前先过内容审核。** 平台上线后第一件事就是有人在聊天里发
    微信号绕过平台，或者骗子发链接。M-08 的审核模块本来只审房源与评价，
    而私聊才是这类内容真正高发的地方。
    """
    conversation = _my_conversation(session, conversation_id, user)
    if conversation.status != "active":
        raise HTTPException(status_code=409, detail="该会话已关闭")

    verdict = moderation.check(payload.content, scene="chat")
    if verdict["level"] == moderation.LEVEL_BLOCK:
        raise HTTPException(
            status_code=400,
            detail="为保障双方权益，请在平台内沟通：" + (
                verdict["reasons"][0] if verdict["reasons"] else "内容不合规"))
    # flag 级别放行但留痕，交人工复核——存疑的内容直接拒发误伤太大
    if verdict["level"] == moderation.LEVEL_FLAG:
        session.add(ContentFlag(
            scene="chat", target_id=conversation.id,
            target_key=(conversation.house.house_no
                        if conversation.house else ""),
            author_id=user.id,
            author_name=user.real_name or user.username,
            excerpt=payload.content.strip()[:300],
            categories="、".join(verdict["categories"]),
            reasons="；".join(verdict["reasons"])))

    line = ChatLine(conversation_id=conversation.id, sender_id=user.id,
                    content=payload.content.strip())
    session.add(line)

    # 冗余字段同步更新，会话列表才不用回查明细表
    conversation.last_message = payload.content.strip()[:100]
    conversation.last_at = datetime.now()
    if user.id == conversation.tenant_id:
        conversation.landlord_unread += 1
        peer_id = conversation.landlord_id
    else:
        conversation.tenant_unread += 1
        peer_id = conversation.tenant_id

    # 顺手写一条站内通知：对方没开着会话面板时，靠消息中心的红点提醒。
    # 会话与通知是两件事——通知要的是「未读清零」，会话要的是
    # 「对方读到哪了」——所以各存各的，只在这里联动一次。
    leasing.notify(
        session, peer_id, "system", "收到一条新消息",
        "%s：%s" % (conversation.house.house_no if conversation.house else "",
                    payload.content.strip()[:40]),
        conversation.house.house_no if conversation.house else "")

    session.commit()
    session.refresh(line)
    return {"ok": True, "line": line.to_dict(user.id)}


@router.post("/api/conversations/{conversation_id}/read")
def mark_conversation_read(conversation_id: int,
                           user: User = Depends(require_login),
                           session: Session = Depends(get_session)):
    """把自己这一侧的未读清零。"""
    conversation = _my_conversation(session, conversation_id, user)
    if user.id == conversation.tenant_id:
        conversation.tenant_unread = 0
    elif user.id == conversation.landlord_id:
        conversation.landlord_unread = 0
    session.commit()
    return {"ok": True}
