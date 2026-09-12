"""智能客服的知识库、问答日志与转人工工单（AI-03）。

从 `main.py` 拆出来，见 `app/routers/__init__.py` 的说明。
这一组的请求模型也放在这里——它们只被这一组用，搬到公共文件反而要在
两个文件之间来回跳。
"""

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app import leasing
from app.db import get_session
from app.deps import current_user, require_login
from app.models import Faq, SupportHandoff, SupportLog, User

router = APIRouter()


#
# 三块：知识条目（faqs）的增删改查、问答日志与质量分析、转人工工单。
# 智能体服务只通过这些受控接口读写，不直接连库（非功能需求 N-06）。


class FaqPayload(BaseModel):
    code: str = ""
    question: str
    answer: str
    category: str = "其他"
    aliases: str = ""
    is_published: bool = True
    sort_order: int = 0


class SupportLogPayload(BaseModel):
    """智能体回写一次问答。字段与它返回的 retrieval 结构对应。"""

    question: str
    status: str = ""
    mode: str = ""
    session_id: str = ""
    cited: list[str] = []
    top_id: str = ""
    cover: float = 0.0
    answer_source: str = ""
    gate_reason: str = ""
    latency_ms: int = 0


class FeedbackPayload(BaseModel):
    log_id: int
    value: int = 0
    # 未登录用户拿这个证明「这条问答是我自己的」，见 support_feedback
    session_id: str = ""


class HandoffPayload(BaseModel):
    question: str
    reason: str = ""
    context: str = ""


class HandoffReplyPayload(BaseModel):
    reply: str


@router.get("/api/faqs")
def list_faqs(category: str = "", session: Session = Depends(get_session)):
    """已发布的知识条目。智能体启动与定时刷新时拉的就是这个。

    公开接口：帮助中心页面也用它，未登录用户同样能查平台规则。
    """
    stmt = select(Faq).where(Faq.is_published.is_(True))
    if category:
        stmt = stmt.where(Faq.category == category)
    rows = session.scalars(stmt.order_by(Faq.sort_order, Faq.id)).all()
    return {"total": len(rows), "list": [f.to_dict() for f in rows]}


@router.get("/api/admin/faqs")
def admin_faqs(keyword: str = "", user: User = Depends(require_login),
               session: Session = Depends(get_session)):
    """管理端列表，含未发布的条目。"""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    stmt = select(Faq)
    if keyword:
        like = "%" + keyword + "%"
        stmt = stmt.where(or_(Faq.question.like(like), Faq.answer.like(like),
                              Faq.aliases.like(like)))
    rows = session.scalars(stmt.order_by(Faq.sort_order, Faq.id)).all()
    return {"total": len(rows), "list": [f.to_dict() for f in rows]}


def _faq_code(session, given=""):
    """没填编码就生成一个。

    编码要进提示词和前端引用卡片，所以用 `faq-12` 这种短的，
    不要把中文问题塞进 id——中文 id 在日志里难对齐，也容易被模型改写。
    """
    if given.strip():
        return given.strip()
    largest = session.scalar(select(func.max(Faq.id))) or 0
    return "faq-%d" % (largest + 1)


@router.post("/api/faqs")
def create_faq(payload: FaqPayload, user: User = Depends(require_login),
               session: Session = Depends(get_session)):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    question = payload.question.strip()
    answer = payload.answer.strip()
    if not question or not answer:
        raise HTTPException(status_code=400, detail="问题与答案都不能为空")

    code = _faq_code(session, payload.code)
    if session.scalar(select(Faq).where(Faq.code == code)):
        raise HTTPException(status_code=400, detail="编码 %s 已存在" % code)

    row = Faq(code=code, question=question, answer=answer,
              category=payload.category or "其他",
              aliases=payload.aliases.strip(),
              is_published=payload.is_published,
              sort_order=payload.sort_order)
    session.add(row)
    session.commit()
    return {"ok": True, "faq": row.to_dict()}


@router.put("/api/faqs/{faq_id}")
def update_faq(faq_id: int, payload: FaqPayload,
               user: User = Depends(require_login),
               session: Session = Depends(get_session)):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    row = session.get(Faq, faq_id)
    if not row:
        raise HTTPException(status_code=404, detail="知识条目不存在")

    row.question = payload.question.strip() or row.question
    row.answer = payload.answer.strip() or row.answer
    row.category = payload.category or row.category
    row.aliases = payload.aliases.strip()
    row.is_published = payload.is_published
    row.sort_order = payload.sort_order
    session.commit()
    return {"ok": True, "faq": row.to_dict()}


@router.patch("/api/faqs/{faq_id}")
def toggle_faq(faq_id: int, published: bool,
               user: User = Depends(require_login),
               session: Session = Depends(get_session)):
    """上下架。下架后智能体下一次刷新就不再检索到它。"""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    row = session.get(Faq, faq_id)
    if not row:
        raise HTTPException(status_code=404, detail="知识条目不存在")
    row.is_published = published
    session.commit()
    return {"ok": True, "faq": row.to_dict()}


@router.delete("/api/faqs/{faq_id}")
def delete_faq(faq_id: int, user: User = Depends(require_login),
               session: Session = Depends(get_session)):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    row = session.get(Faq, faq_id)
    if not row:
        raise HTTPException(status_code=404, detail="知识条目不存在")
    code = row.code
    session.delete(row)
    session.commit()
    return {"ok": True}


@router.post("/api/support/logs")
def log_support(payload: SupportLogPayload,
                user: User = Depends(current_user),
                session: Session = Depends(get_session)):
    """记录一次客服问答，并给被引用的条目累加命中数。

    不要求登录：未登录用户的提问同样是「哪些问题没人答得上来」的证据，
    而这正是这张表最主要的用途。
    """
    row = SupportLog(
        user_id=user.id if user else None,
        session_id=payload.session_id[:64],
        question=payload.question[:500],
        status=payload.status, mode=payload.mode,
        cited=",".join(payload.cited)[:255],
        top_id=payload.top_id[:60], cover=payload.cover,
        answer_source=payload.answer_source,
        gate_reason=payload.gate_reason[:200],
        latency_ms=payload.latency_ms)
    session.add(row)

    # 命中数同样交给数据库加，不要读出来加完再写回去——
    # 两个人同时问到同一条，两次命中会只记一次。
    # 「最常被引用的条目」那张表就是拿这个数排序的
    for code in payload.cited:
        session.execute(update(Faq).where(Faq.code == code)
                        .values(hit_count=Faq.hit_count + 1))

    session.commit()
    return {"ok": True, "log_id": row.id}


def _owns_support_log(user, row, session_id):
    """这条问答是不是这个人的。

    登录时认 user_id；未登录时认 session_id——前端会话建立时生成一个，
    每次提问都带着，所以未登录用户也能给自己的回答打分。

    两样都没记的老数据一律不让改：宁可少一条反馈，也不要让一条
    来路不明的反馈混进补语料的清单里。
    """
    if row.user_id:
        return bool(user) and user.id == row.user_id
    if row.session_id:
        return bool(session_id) and session_id == row.session_id
    return False


@router.post("/api/support/feedback")
def support_feedback(payload: FeedbackPayload,
                     user: User = Depends(current_user),
                     session: Session = Depends(get_session)):
    """用户对一条回答点有用 / 没用。

    「没用」比「有用」值钱得多：它直接指出哪条 FAQ 写得不对或答非所问，
    是补语料时第一个该看的清单。

    **只能给自己的那条打分。** 这里原先只按 log_id 找行就改，谁都能改：
    log_id 是自增的连号，`for i in range(1, 10000)` 挨个点一遍「没用」，
    管理端那张「补语料清单」就全是假的——而它恰恰是拿来决定
    下一批写哪些 FAQ 的，被污染之后是在指挥人去修没坏的东西。
    """
    row = session.get(SupportLog, payload.log_id)
    if not row or not _owns_support_log(user, row, payload.session_id):
        # 不是你的也报「不存在」：log_id 连号，回一句「这条不是你的」
        # 就等于确认了这个号存在，逐个试一遍能问出总量和分布
        raise HTTPException(status_code=404, detail="记录不存在")
    row.feedback = 1 if payload.value > 0 else (-1 if payload.value < 0 else 0)
    session.commit()
    return {"ok": True}


@router.post("/api/support/handoff")
def create_handoff(payload: HandoffPayload,
                   user: User = Depends(require_login),
                   session: Session = Depends(get_session)):
    """转人工：把答不上来的问题留给管理员。

    要求登录——管理员回复时得知道回给谁。未登录用户在前端会先被引导登录。
    """
    question = payload.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="问题不能为空")

    row = SupportHandoff(
        handoff_no=leasing._serial("HD"), user_id=user.id,
        question=question[:500], reason=payload.reason[:200],
        context=payload.context[:2000])
    session.add(row)
    session.flush()

    # 通知每一位管理员。数量很少，直接全发，不做分配策略——
    # 课设规模下引入排班与分派只会增加没人用的复杂度。
    admins = session.scalars(select(User).where(User.role == "admin")).all()
    for admin in admins:
        leasing.notify(session, admin.id, "system", "客服转人工",
                       "用户「%s」的问题：%s" % (user.username, question[:80]),
                       link_no=row.handoff_no)
    session.commit()
    return {"ok": True, "handoff": row.to_dict()}


@router.get("/api/support/handoffs/mine")
def my_handoffs(user: User = Depends(require_login),
                session: Session = Depends(get_session)):
    rows = session.scalars(
        select(SupportHandoff).where(SupportHandoff.user_id == user.id)
        .order_by(SupportHandoff.created_at.desc()).limit(50)).all()
    return {"total": len(rows), "list": [h.to_dict() for h in rows]}


@router.get("/api/admin/handoffs")
def admin_handoffs(status: str = "", user: User = Depends(require_login),
                   session: Session = Depends(get_session)):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    stmt = select(SupportHandoff)
    if status:
        stmt = stmt.where(SupportHandoff.status == status)
    rows = session.scalars(
        stmt.order_by(SupportHandoff.created_at.desc()).limit(200)).all()
    return {"total": len(rows), "list": [h.to_dict() for h in rows]}


@router.post("/api/admin/handoffs/{handoff_no}/reply")
def reply_handoff(handoff_no: str, payload: HandoffReplyPayload,
                  user: User = Depends(require_login),
                  session: Session = Depends(get_session)):
    """管理员回复转人工工单，回复通过站内消息送达用户。"""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    row = session.scalar(
        select(SupportHandoff).where(SupportHandoff.handoff_no == handoff_no))
    if not row:
        raise HTTPException(status_code=404, detail="工单不存在")

    reply = payload.reply.strip()
    if not reply:
        raise HTTPException(status_code=400, detail="回复内容不能为空")

    row.reply = reply
    row.status = "replied"
    row.handler_id = user.id
    row.replied_at = datetime.now()
    leasing.notify(session, row.user_id, "system", "客服回复了你的问题",
                   reply[:200], link_no=row.handoff_no)
    session.commit()
    return {"ok": True, "handoff": row.to_dict()}


@router.get("/api/admin/support/analytics")
def support_analytics(days: int = 30, user: User = Depends(require_login),
                      session: Session = Depends(get_session)):
    """客服质量分析。

    回答的是四个运营问题，不是技术指标：

    1. **拒答率高不高** —— 高就是语料不够，不是模型不行
    2. **哪些问题反复被拒** —— 这张清单直接就是「下一批该写哪些 FAQ」
    3. **哪些条目最常被引用** —— 说明用户最关心什么
    4. **哪些回答被点了没用** —— 写了但写得不对的条目
    """
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")

    since = datetime.now() - timedelta(days=max(days, 1))
    rows = session.scalars(
        select(SupportLog).where(SupportLog.created_at >= since)).all()

    total = len(rows)
    by_status = {}
    for row in rows:
        by_status[row.status] = by_status.get(row.status, 0) + 1

    refused = [r for r in rows if r.status == "refused"]
    # 被拒问题按出现次数排序——问一次可能是意外，问五次就是语料缺口
    gaps = {}
    for row in refused:
        gaps[row.question] = gaps.get(row.question, 0) + 1
    top_gaps = sorted(gaps.items(), key=lambda kv: -kv[1])[:20]

    faqs = session.scalars(
        select(Faq).order_by(Faq.hit_count.desc()).limit(10)).all()
    unused = session.scalars(
        select(Faq).where(Faq.hit_count == 0, Faq.is_published.is_(True))).all()

    disliked = [r.to_dict() for r in rows if r.feedback < 0][:20]
    answered = by_status.get("answered", 0)
    latencies = sorted(r.latency_ms for r in rows) or [0]

    return {
        "days": days,
        "total": total,
        "by_status": by_status,
        "answer_rate": round(answered / total, 3) if total else 0,
        "refuse_rate": round(len(refused) / total, 3) if total else 0,
        "median_latency_ms": latencies[len(latencies) // 2],
        "top_gaps": [{"question": q, "count": c} for q, c in top_gaps],
        "top_faqs": [{"code": f.code, "question": f.question,
                      "hit_count": f.hit_count} for f in faqs],
        "unused_faqs": [{"code": f.code, "question": f.question}
                        for f in unused],
        "disliked": disliked,
        "pending_handoffs": session.scalar(
            select(func.count()).select_from(SupportHandoff)
            .where(SupportHandoff.status == "pending")) or 0,
    }
