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
from app.models import (ChatLine, ContentFlag, Conversation, House,
                        HouseDelegation, User, mask_name)
from app.permissions import can_manage_house, responsible_user_id

router = APIRouter()



class ConversationStart(BaseModel):
    house_no: str


class LineCreate(BaseModel):
    content: str = Field(min_length=1, max_length=500)


def _my_conversation(session, conversation_id, user):
    """取出会话并校验：当事双方、受托中介与管理员能碰。

    **每个会话接口都必须过这一关。** 少一处，改个 id 就能读走别人的
    私聊——这类越权本项目已经修过两次（工单列表、对话记录），
    是同一类错误。

    受托中介之所以能进来，是因为委托期间**由他来答**（J-03）。
    注意这里问的是 `can_manage_house`，不是「他是不是 agent」——
    只有在托的那几套房的会话对他开放，别的房源的私聊他一样读不到。
    """
    conversation = session.get(Conversation, conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="会话不存在")
    if user.role == "admin":
        return conversation
    if user.id in (conversation.tenant_id, conversation.landlord_id):
        return conversation
    if conversation.house and can_manage_house(session, user,
                                               conversation.house):
        return conversation
    raise HTTPException(status_code=403, detail="无权查看该会话")


def _responder(session, conversation):
    """当前负责答这个会话的人。委托中是受托中介，否则是房东本人。

    只有租客那一侧需要它（用来显示「对方」是房东还是经纪人）。
    列表接口会对每个会话调一次——委托表上有 (house_id, status) 索引，
    这是那条索引存在的主要理由之一。
    """
    if not conversation.house:
        return conversation.landlord
    uid = responsible_user_id(session, conversation.house)
    if uid == conversation.landlord_id:
        return conversation.landlord
    return session.get(User, uid)


def _sender_label(session, conversation, line):
    """同一侧内部，这条是谁说的。

    只有房东一侧会出现两个人（房东本人 + 受托中介），租客侧永远是一个人，
    所以租客侧一律返回空。
    """
    if line.sender_id == conversation.tenant_id:
        return ""
    if line.sender_id == conversation.landlord_id:
        return "房东"
    sender = session.get(User, line.sender_id)
    return "经纪人 " + mask_name(sender) if sender else "经纪人"


def _landlord_side(conversation, user):
    """这个人是不是站在「房东一侧」。

    房东本人、受托中介都算。会话是两方的，中介是**替房东那一侧说话**，
    不是凭空多出来的第三方——所以未读数、消息气泡的左右、
    「对方是谁」这些判断全都沿用房东侧那一套，不必为中介另开一路。
    """
    return user.id != conversation.tenant_id


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
        return {"ok": True,
                "conversation": existing.to_dict(
                    user.id, _responder(session, existing))}

    conversation = Conversation(house_id=house.id, tenant_id=user.id,
                                landlord_id=house.landlord_id)
    session.add(conversation)
    session.commit()
    session.refresh(conversation)
    return {"ok": True,
            "conversation": conversation.to_dict(
                user.id, _responder(session, conversation))}


@router.get("/api/conversations")
def list_conversations(user: User = Depends(require_login),
                       session: Session = Depends(get_session)):
    """我的会话列表，最近有消息的排前面。"""
    stmt = select(Conversation).where(
        or_(Conversation.tenant_id == user.id,
            Conversation.landlord_id == user.id))
    if user.role == "agent":
        # 中介看到的是他在托房源上的会话（J-03）。
        # 委托解除后这些会话就不再出现在他的列表里，但**消息不删**——
        # 已经发生的对话是问责的依据。
        mine = select(HouseDelegation.house_id).where(
            HouseDelegation.agent_id == user.id,
            HouseDelegation.status == "active")
        stmt = select(Conversation).where(
            or_(Conversation.tenant_id == user.id,
                Conversation.landlord_id == user.id,
                Conversation.house_id.in_(mine)))
    rows = session.scalars(
        stmt.order_by(Conversation.last_at.desc())).all()
    unread = sum((c.tenant_unread if c.tenant_id == user.id
                  else c.landlord_unread) for c in rows)
    return {"total": len(rows), "unread": unread,
            "list": [c.to_dict(user.id, _responder(session, c))
                     for c in rows]}


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
            "list": [line.to_dict(user.id, conversation.tenant_id,
                                  _sender_label(session, conversation, line))
                     for line in rows]}


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
        # 租客的问题发给**当前负责答的人**：委托中就是受托中介，
        # 否则是房东本人（J-03）
        peer_id = responsible_user_id(session, conversation.house)             if conversation.house else conversation.landlord_id
    else:
        conversation.tenant_unread += 1
        peer_id = conversation.tenant_id

    # 顺手写一条站内通知：对方没开着会话面板时，靠消息中心的红点提醒。
    # 会话与通知是两件事——通知要的是「未读清零」，会话要的是
    # 「对方读到哪了」——所以各存各的，只在这里联动一次。
    house_no = conversation.house.house_no if conversation.house else ""
    leasing.notify(
        session, peer_id, "system", "收到一条新消息",
        "%s：%s" % (house_no, payload.content.strip()[:40]), house_no)

    # 由中介代答时给房东抄送一条。**他委托出去的是带看，不是知情权**——
    # 自己的房子被问了什么、中介怎么答的，他有权看到。
    # 抄送只是提示，会话未读数不重复计（那是「谁该回」的信号，
    # 而该回的人只有一个）。
    if peer_id != conversation.landlord_id and             user.id == conversation.tenant_id:
        leasing.notify(
            session, conversation.landlord_id, "system",
            "租客咨询（由中介代答）",
            "%s：%s" % (house_no, payload.content.strip()[:40]), house_no)

    session.commit()
    session.refresh(line)
    return {"ok": True,
            "line": line.to_dict(user.id, conversation.tenant_id,
                                 _sender_label(session, conversation, line))}


@router.post("/api/conversations/{conversation_id}/read")
def mark_conversation_read(conversation_id: int,
                           user: User = Depends(require_login),
                           session: Session = Depends(get_session)):
    """把自己这一侧的未读清零。"""
    conversation = _my_conversation(session, conversation_id, user)
    if user.id == conversation.tenant_id:
        conversation.tenant_unread = 0
    else:
        # 房东与受托中介共用房东侧那个未读数。
        # 分开存会立刻带出一个没有答案的问题：中介读了、房东没读，
        # 这条到底算不算已读？会话只需要一个「该回的人读到没有」。
        conversation.landlord_unread = 0
    session.commit()
    return {"ok": True}
