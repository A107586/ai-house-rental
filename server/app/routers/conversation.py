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
from app.models import (Appointment, ChatLine, ContentFlag, Conversation,
                        House, HouseDelegation, User, ViewingRecord,
                        mask_name)
from app.permissions import active_delegation, can_manage_house, responsible_user_id

router = APIRouter()



class ConversationStart(BaseModel):
    house_no: str
    # 由房源负责人发起时填：要联系哪位租客（J-07）。
    # 租客自己发起时留空——他就是那个租客
    tenant_username: str = ""
    # 需求摘要（J-09-a）。由智能体整理好之后随咨询一并带过来，
    # 落成会话的第一条消息，中介一进来就看得到
    demand_summary: str = Field(default="", max_length=600)


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
    单条会话用这个；**列表一律走 `_responders`**，见下面的说明。
    """
    return _responders(session, [conversation])[conversation.id]


def _responders(session, conversations):
    """一批会话各自的答复人，`{conversation_id: User}`。

    列表接口原来是对每个会话调一次 `_responder`——一个手上有五十个咨询
    的房东，打开会话列表就是五十次委托查询。**这正是 K-09 之后反复强调
    的那类问题：单条看着没毛病，放进列表里就是 N+1。**

    这里改成两次查询：一次把涉及房源的生效委托全取回来，一次把用到的
    中介取回来，然后在内存里配对。会话数再多，查询次数都是常数。
    """
    if not conversations:
        return {}

    house_ids = {c.house_id for c in conversations}
    active = {d.house_id: d.agent_id for d in session.scalars(
        select(HouseDelegation).where(
            HouseDelegation.house_id.in_(house_ids),
            HouseDelegation.status == "active")).all()}

    agent_ids = set(active.values())
    agents = {}
    if agent_ids:
        agents = {u.id: u for u in session.scalars(
            select(User).where(User.id.in_(agent_ids))).all()}

    out = {}
    for c in conversations:
        agent_id = active.get(c.house_id)
        # 委托给的就是房东自己，或者中介账号已被删——都退回房东。
        # 退回而不是返回 None，是因为调用方只想知道「该显示谁」，
        # 让它去处理一个空值只会把判断散出去
        out[c.id] = agents.get(agent_id) if agent_id else None
        if out[c.id] is None:
            out[c.id] = c.landlord
    return out


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

    tenant_id = user.id
    if payload.tenant_username.strip():
        # ---- 由房源负责人（房东或受托中介）主动联系某位租客 ----
        tenant = session.scalar(select(User).where(
            User.username == payload.tenant_username.strip()))
        if not tenant:
            raise HTTPException(status_code=404, detail="租客不存在")
        if not can_manage_house(session, user, house):
            raise HTTPException(status_code=403, detail="这套房不在你的管理范围内")

        # **不能对任意人发起会话。** 否则「主动联系」就成了一个站内群发
        # 入口——中介手上有全站房源的浏览记录，挨个私信是稳赚的。
        # 必须是这套房上已经有往来的人：约过看房，或者被带看过。
        related = session.scalar(select(Appointment.id).where(
            Appointment.house_id == house.id,
            Appointment.tenant_id == tenant.id)) or session.scalar(
            select(ViewingRecord.id).where(
                ViewingRecord.house_id == house.id,
                ViewingRecord.tenant_id == tenant.id))
        if not related:
            raise HTTPException(
                status_code=403,
                detail="只能联系在这套房上约过看房或被带看过的租客")

        # **免打扰**（J-07）。放在这里而不是只把前端按钮置灰：
        # 按钮是提示，接口才是边界——灰掉的按钮拦不住直接调接口的人
        if not tenant.accept_contact:
            raise HTTPException(
                status_code=403,
                detail="对方已设置不接受主动联系。你可以等他先来问，"
                       "预约与带看的通知照常送达")
        tenant_id = tenant.id
    elif house.landlord_id == user.id:
        raise HTTPException(status_code=400, detail="不能和自己的房源发起咨询")
    elif house.status not in ("available", "rented") and not can_manage_house(session, user, house):
        # 待审、已下架的房源对外不可见；知道房源编号也不该能从这里绕进去问
        raise HTTPException(status_code=404, detail="房源不存在")

    existing = session.scalar(select(Conversation).where(
        Conversation.house_id == house.id,
        Conversation.tenant_id == tenant_id))
    if existing:
        return {"ok": True,
                "conversation": existing.to_dict(
                    user.id, _responder(session, existing))}

    conversation = Conversation(house_id=house.id, tenant_id=tenant_id,
                                landlord_id=house.landlord_id)
    session.add(conversation)
    session.flush()

    # 需求摘要落成第一条消息（J-09-a）。
    #
    # **做成一条消息，而不是给会话加一列。** 加列要改表、要在每一处
    # 会话展示里加渲染分支；做成消息则天然带时间、天然进历史、
    # 天然在中介手机上也看得到——会话本来就是「按时间排列的一串内容」，
    # 摘要没有理由是个例外。
    #
    # `kind="summary"` 让前端能把它渲染成一张卡片而不是聊天气泡；
    # 发送人记成租客本人，因为那确实是他的需求，不是系统凭空说的。
    if payload.demand_summary.strip() and tenant_id == user.id:
        session.add(ChatLine(conversation_id=conversation.id,
                             sender_id=tenant_id, kind="summary",
                             content=payload.demand_summary.strip()))
        conversation.last_message = "【需求摘要】"
        conversation.landlord_unread += 1

    session.commit()
    session.refresh(conversation)
    return {"ok": True,
            "conversation": conversation.to_dict(
                user.id, _responder(session, conversation))}


@router.get("/api/conversations")
def list_conversations(user: User = Depends(require_login),
                       session: Session = Depends(get_session)):
    """我的会话列表，最近有消息的排前面。"""
    # 三个身份并成一个 or：我是租客、我是房东、或者这套房现在委托给我。
    #
    # **不按角色分支**（K-13 的教训）：写成「如果 role 是 agent 就……」
    # 的话，一个身兼两职的账号会有一侧的数据被挡在外面。这里三个条件
    # 是并列的，谁占了哪个位置就看得到哪一份。
    #
    # 委托解除后这些会话不再出现在中介的列表里，但**消息不删**——
    # 已经发生的对话是问责的依据。
    delegated = select(HouseDelegation.house_id).where(
        HouseDelegation.agent_id == user.id,
        HouseDelegation.status == "active")
    rows = session.scalars(select(Conversation).where(
        or_(Conversation.tenant_id == user.id,
            Conversation.landlord_id == user.id,
            Conversation.house_id.in_(delegated))
    ).order_by(Conversation.last_at.desc())).all()
    unread = sum((c.tenant_unread if c.tenant_id == user.id
                  else c.landlord_unread) for c in rows)
    responders = _responders(session, rows)
    return {"total": len(rows), "unread": unread,
            "list": [c.to_dict(user.id, responders.get(c.id)) for c in rows]}


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
    if (user.role == "admin" and user.id not in (conversation.tenant_id, conversation.landlord_id)
            and not (conversation.house and active_delegation(session, conversation.house_id, user.id))):
        # 管理员能看（纠纷要有人查），但不能以任一方的名义说话：原先发得出去，
        # 而且会被标成「经纪人 管*」——房东一侧非房东本人的一律当成受托中介（2026-10-22）
        raise HTTPException(status_code=403, detail="管理员只能查看会话，不能代任何一方发言")

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
        peer_id = (responsible_user_id(session, conversation.house)
                   if conversation.house else conversation.landlord_id)
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
    if peer_id != conversation.landlord_id and user.id == conversation.tenant_id:
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
