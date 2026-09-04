"""中介：档案、委托、带看、佣金与工作台（J 系列）。

设计依据见 docs/中介功能设计方案.md。这一条服务链解决的是一个
系统当前完全没有出口的需求：**房东不想自己带看。**

    房东委托 ──> 中介带看 ──> 促成签约 ──> 结算佣金

统计口径的三条规矩（方案 8.5，这里逐条落实）：

1. **一个数字都不许存**，全部由明细实时聚合（R-05）；
2. **一个页面一个接口**，工作台一次取全，不让首屏打十次请求；
3. **比率必须带分母**——「成交率 25%」在样本只有 4 次带看时是误导，
   所以一律返回 `{rate, hit, total}` 三件套。
"""

from datetime import datetime, timedelta

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import leasing
from app.db import get_session
from app.deps import require_login
from app.models import (AgentCommission, AgentProfile, Appointment, Contract,
                        House, HouseDelegation, Review, User, ViewingRecord)
from app.permissions import active_delegation

router = APIRouter()

# 近 N 天。用 30 天滚动窗口而不是自然月——自然月在月初会突然清零，
# 中介早上打开工作台看到带看量归零，会以为数据丢了
RECENT_DAYS = 30


def _recent_since():
    return datetime.now() - timedelta(days=RECENT_DAYS)


def _ratio(hit, total):
    """比率一律带分母。

    只给一个百分数，样本小的时候就是误导：3 次带看成交 1 单是 33%，
    30 次带看成交 10 单也是 33%，但这两个数字的可信度差得远。
    """
    return {
        "rate": round(hit / total * 100, 1) if total else 0.0,
        "hit": hit,
        "total": total,
    }


def _profile(session, user_id, create=False):
    row = session.scalar(
        select(AgentProfile).where(AgentProfile.user_id == user_id))
    if row is None and create:
        row = AgentProfile(user_id=user_id)
        session.add(row)
        session.flush()
    return row


def require_agent(user: User = Depends(require_login)):
    if user.role != "agent":
        raise HTTPException(status_code=403, detail="只有中介可以操作")
    return user


# ---------------------------------------------------------------- 档案

class ProfileUpdate(BaseModel):
    service_districts: list[str] | None = None
    intro: str | None = Field(default=None, max_length=300)
    years: int | None = Field(default=None, ge=0, le=60)
    accepting: bool | None = None


class LicenseSubmit(BaseModel):
    license_no: str = Field(min_length=4, max_length=40)
    license_images: list[str] = Field(default_factory=list)


@router.get("/api/agent/profile")
def my_profile(user: User = Depends(require_agent),
               session: Session = Depends(get_session)):
    profile = _profile(session, user.id, create=True)
    session.commit()
    return {"ok": True, "profile": profile.to_dict()}


@router.put("/api/agent/profile")
def update_profile(payload: ProfileUpdate,
                   user: User = Depends(require_agent),
                   session: Session = Depends(get_session)):
    """改主营区域、简介、从业年限、接单开关。

    **改不了资质状态和任何统计字段**——那些不在这个模型里。
    统计要么是聚合出来的，要么由管理员审核决定，中介自己说了不算。
    """
    profile = _profile(session, user.id, create=True)
    if payload.service_districts is not None:
        profile.service_districts = ",".join(
            d.strip() for d in payload.service_districts[:6] if d.strip())
    if payload.intro is not None:
        profile.intro = payload.intro.strip()
    if payload.years is not None:
        profile.years = payload.years
    if payload.accepting is not None:
        profile.accepting = payload.accepting
    session.commit()
    session.refresh(profile)
    return {"ok": True, "profile": profile.to_dict()}


@router.post("/api/agent/license")
def submit_license(payload: LicenseSubmit,
                   user: User = Depends(require_agent),
                   session: Session = Depends(get_session)):
    """提交从业资质，转入待审核。

    已经通过的不允许重复提交——那等于让中介在被投诉后自己重置状态。
    """
    profile = _profile(session, user.id, create=True)
    if profile.license_status == "approved":
        raise HTTPException(status_code=409, detail="资质已通过审核，无需重复提交")
    profile.license_no = payload.license_no.strip()
    profile.license_images = ",".join(payload.license_images[:6])
    profile.license_status = "pending"
    profile.reject_reason = ""
    session.commit()
    session.refresh(profile)
    return {"ok": True, "profile": profile.to_dict()}


# ---------------------------------------------------------------- 委托

class DelegationCreate(BaseModel):
    house_no: str
    # 房东邀请时填中介用户名；中介申请接单时不填
    agent_username: str = ""
    commission_rate: float = Field(default=50, ge=0, le=100)


def _one_active(session, house_id):
    return active_delegation(session, house_id) is not None


@router.post("/api/delegations")
def create_delegation(payload: DelegationCreate,
                      user: User = Depends(require_login),
                      session: Session = Depends(get_session)):
    """发起委托。房东邀请中介，或中介申请接单——两条路进同一张表。

    **无论谁发起都只到 pending**，要另一方确认才生效。单方面就能建立
    委托，等于允许中介给自己派活。
    """
    house = session.scalar(
        select(House).where(House.house_no == payload.house_no))
    if not house:
        raise HTTPException(status_code=404, detail="房源不存在")

    if user.role == "agent":
        profile = _profile(session, user.id)
        if not profile or not profile.can_take_orders:
            raise HTTPException(
                status_code=403,
                detail="资质未通过审核或已关闭接单，暂时不能申请接单")
        agent_id, landlord_id, initiated_by = user.id, house.landlord_id, "agent"
    elif house.landlord_id == user.id or user.role == "admin":
        target = session.scalar(
            select(User).where(User.username == payload.agent_username.strip()))
        if not target or target.role != "agent":
            raise HTTPException(status_code=404, detail="中介不存在")
        profile = _profile(session, target.id)
        if not profile or not profile.can_take_orders:
            raise HTTPException(status_code=400,
                                detail="该中介资质未通过或未开启接单")
        agent_id, landlord_id, initiated_by = target.id, house.landlord_id, "landlord"
    else:
        raise HTTPException(status_code=403, detail="只能委托自己的房源")

    if _one_active(session, house.id):
        raise HTTPException(status_code=409, detail="这套房已经有生效中的委托")
    exists = session.scalar(select(HouseDelegation).where(
        HouseDelegation.house_id == house.id,
        HouseDelegation.agent_id == agent_id,
        HouseDelegation.status == "pending"))
    if exists:
        raise HTTPException(status_code=409, detail="已经有一条待确认的委托")

    row = HouseDelegation(house_id=house.id, landlord_id=landlord_id,
                          agent_id=agent_id, initiated_by=initiated_by,
                          commission_rate=payload.commission_rate)
    session.add(row)
    session.flush()

    # 通知需要确认的那一方
    target_id = landlord_id if initiated_by == "agent" else agent_id
    leasing.notify(session, target_id, "system", "有一条委托待确认",
                   "房源 %s 的带看委托等待你确认。" % house.house_no,
                   house.house_no)
    session.commit()
    session.refresh(row)
    return {"ok": True, "delegation": row.to_dict()}


@router.patch("/api/delegations/{delegation_id}")
def respond_delegation(delegation_id: int,
                       accept: bool = Body(embed=True),
                       user: User = Depends(require_login),
                       session: Session = Depends(get_session)):
    """确认或拒绝一条委托。**只有被邀请的那一方能点。**

    发起人自己点同意就是单方面成立，那正是双向确认要防的事。
    """
    row = session.get(HouseDelegation, delegation_id)
    if not row:
        raise HTTPException(status_code=404, detail="委托不存在")
    if row.status != "pending":
        raise HTTPException(status_code=409, detail="这条委托已经处理过了")

    approver = row.landlord_id if row.initiated_by == "agent" else row.agent_id
    if user.id != approver and user.role != "admin":
        raise HTTPException(status_code=403, detail="只有被邀请的一方可以确认")

    if not accept:
        row.status = "rejected"
        row.end_at = datetime.now()
    else:
        if _one_active(session, row.house_id):
            raise HTTPException(status_code=409, detail="这套房已经有生效中的委托")
        row.status = "active"
        row.start_at = datetime.now()
        leasing.notify(session, row.agent_id, "system", "委托已生效",
                       "房源 %s 的带看委托已生效，可以开始接待看房了。"
                       % (row.house.house_no if row.house else ""),
                       row.house.house_no if row.house else "")
    session.commit()
    session.refresh(row)
    return {"ok": True, "delegation": row.to_dict()}


@router.post("/api/delegations/{delegation_id}/end")
def end_delegation(delegation_id: int,
                   reason: str = Body(default="", embed=True),
                   user: User = Depends(require_login),
                   session: Session = Depends(get_session)):
    """解除委托。任一方都可以，但必须填原因。

    **不阻止**——即使还有未完成的预约。委托关系不该变成绑定关系；
    界面上给提示就够了，剩下的是他们两个人的事。
    """
    row = session.get(HouseDelegation, delegation_id)
    if not row:
        raise HTTPException(status_code=404, detail="委托不存在")
    if row.status != "active":
        raise HTTPException(status_code=409, detail="只有生效中的委托可以解除")
    if user.id not in (row.landlord_id, row.agent_id) and user.role != "admin":
        raise HTTPException(status_code=403, detail="不是这条委托的当事方")
    if not reason.strip():
        raise HTTPException(status_code=400, detail="请填写解除原因")

    row.status = "ended"
    row.end_at = datetime.now()
    row.end_reason = reason.strip()[:100]

    other = row.agent_id if user.id == row.landlord_id else row.landlord_id
    leasing.notify(session, other, "system", "委托已解除",
                   "房源 %s 的带看委托已解除：%s"
                   % (row.house.house_no if row.house else "", row.end_reason),
                   row.house.house_no if row.house else "")
    session.commit()
    session.refresh(row)
    return {"ok": True, "delegation": row.to_dict()}


@router.get("/api/delegations")
def list_delegations(status: str = "", role: str = "",
                     user: User = Depends(require_login),
                     session: Session = Depends(get_session)):
    """我的委托。按「我是不是当事一方」取，不按角色二选一——

    这是 K-13 那次修复留下的教训：按角色分支的查询，在一个人身兼两职时
    会把他自己的数据挡在外面。
    """
    stmt = select(HouseDelegation)
    if user.role != "admin" or role:
        if role == "agent":
            stmt = stmt.where(HouseDelegation.agent_id == user.id)
        elif role == "landlord":
            stmt = stmt.where(HouseDelegation.landlord_id == user.id)
        else:
            stmt = stmt.where(
                (HouseDelegation.agent_id == user.id)
                | (HouseDelegation.landlord_id == user.id))
    if status:
        stmt = stmt.where(HouseDelegation.status == status)
    rows = session.scalars(
        stmt.order_by(HouseDelegation.created_at.desc())).all()
    return {"total": len(rows), "list": [r.to_dict() for r in rows]}


@router.get("/api/agent/open-houses")
def open_houses(limit: int = Query(20, ge=1, le=100),
                user: User = Depends(require_agent),
                session: Session = Depends(get_session)):
    """可以申请接单的房源：在我主营区域内、当前没有生效委托的。

    主营区域为空时不做区域过滤——新中介还没填区域，
    给他一片空白等于让他无从下手。
    """
    profile = _profile(session, user.id, create=True)
    session.commit()

    taken = select(HouseDelegation.house_id).where(
        HouseDelegation.status.in_(("active", "pending")))
    stmt = select(House).where(House.status == "available",
                               House.id.notin_(taken))
    if profile.districts:
        stmt = stmt.where(House.district.in_(profile.districts))
    rows = session.scalars(
        stmt.order_by(House.published_at.desc()).limit(limit)).all()
    return {"total": len(rows), "list": [h.to_card_dict() for h in rows]}


# ---------------------------------------------------------------- 带看

class ViewingCreate(BaseModel):
    house_no: str
    tenant_username: str
    appointment_id: int | None = None
    showed_up: bool = True
    duration_minutes: int = Field(default=0, ge=0, le=600)
    feedback: str = Field(default="", max_length=1000)
    concerns: list[str] = Field(default_factory=list)
    intention: str = "medium"


@router.post("/api/viewings")
def create_viewing(payload: ViewingCreate,
                   user: User = Depends(require_agent),
                   session: Session = Depends(get_session)):
    """记一次带看。只能记自己受托房源的。"""
    if payload.intention not in ViewingRecord.INTENTIONS:
        raise HTTPException(status_code=422, detail="意向取值不正确")
    house = session.scalar(
        select(House).where(House.house_no == payload.house_no))
    if not house:
        raise HTTPException(status_code=404, detail="房源不存在")
    if not active_delegation(session, house.id, user.id):
        raise HTTPException(status_code=403, detail="这套房不在你的受托范围内")

    tenant = session.scalar(
        select(User).where(User.username == payload.tenant_username.strip()))
    if not tenant:
        raise HTTPException(status_code=404, detail="租客不存在")

    row = ViewingRecord(
        appointment_id=payload.appointment_id, house_id=house.id,
        tenant_id=tenant.id, agent_id=user.id,
        showed_up=payload.showed_up,
        duration_minutes=payload.duration_minutes,
        feedback=payload.feedback.strip(),
        concerns=",".join(c.strip() for c in payload.concerns[:8] if c.strip()),
        intention=payload.intention)
    session.add(row)

    # 带看纪要回流给房东——这是他把带看委托出去之后唯一想知道的事
    leasing.notify(
        session, house.landlord_id, "system",
        "房源 %s 有一次带看" % house.house_no,
        "%s；意向：%s。%s" % (
            "租客已到场" if payload.showed_up else "租客未到场",
            ViewingRecord.INTENTIONS.get(payload.intention, ""),
            payload.feedback.strip()[:80]),
        house.house_no)
    session.commit()
    session.refresh(row)
    return {"ok": True, "viewing": row.to_dict()}


@router.get("/api/viewings")
def list_viewings(house_no: str = "", limit: int = Query(50, ge=1, le=200),
                  user: User = Depends(require_login),
                  session: Session = Depends(get_session)):
    """带看流水。中介看自己带的，房东看自己房子被带的，管理员看全部。"""
    stmt = select(ViewingRecord)
    if user.role == "agent":
        stmt = stmt.where(ViewingRecord.agent_id == user.id)
    elif user.role != "admin":
        mine = select(House.id).where(House.landlord_id == user.id)
        stmt = stmt.where(ViewingRecord.house_id.in_(mine))
    if house_no:
        house = session.scalar(select(House).where(House.house_no == house_no))
        stmt = stmt.where(ViewingRecord.house_id == (house.id if house else -1))
    rows = session.scalars(
        stmt.order_by(ViewingRecord.created_at.desc()).limit(limit)).all()
    return {"total": len(rows), "list": [r.to_dict() for r in rows]}


# ---------------------------------------------------------------- 佣金

@router.get("/api/agent/commissions")
def my_commissions(status: str = "",
                   user: User = Depends(require_agent),
                   session: Session = Depends(get_session)):
    stmt = select(AgentCommission).where(AgentCommission.agent_id == user.id)
    if status:
        stmt = stmt.where(AgentCommission.status == status)
    rows = session.scalars(
        stmt.order_by(AgentCommission.created_at.desc())).all()
    pending = sum(float(r.amount) for r in rows if r.status == "pending")
    settled = sum(float(r.amount) for r in rows if r.status == "settled")
    return {"total": len(rows), "pending_amount": pending,
            "settled_amount": settled,
            "list": [r.to_dict() for r in rows]}


# ---------------------------------------------------------------- 工作台

def agent_stats(session, agent_id):
    """一个中介的全部统计。**没有一个数是存下来的**（R-05）。

    管理端的中介档案页也调它，两处口径因此不可能不一致——
    要是各算各的，答辩时被问「工作台说 12 次、后台说 9 次，哪个对」
    就没法回答了。
    """
    since = _recent_since()

    active = session.scalar(select(func.count()).select_from(
        select(HouseDelegation.id).where(
            HouseDelegation.agent_id == agent_id,
            HouseDelegation.status == "active").subquery())) or 0

    viewings = session.scalars(select(ViewingRecord).where(
        ViewingRecord.agent_id == agent_id,
        ViewingRecord.created_at >= since)).all()
    recent_viewings = len(viewings)
    no_show = len([v for v in viewings if not v.showed_up])

    deals = session.scalars(select(Contract).where(
        Contract.agent_id == agent_id)).all()
    month_start = datetime.now().replace(day=1, hour=0, minute=0, second=0,
                                         microsecond=0)
    month_deals = len([c for c in deals
                       if c.created_at and c.created_at >= month_start])

    commissions = session.scalars(select(AgentCommission).where(
        AgentCommission.agent_id == agent_id)).all()

    # 好评率：4 分及以上算好评。评价挂在合同上，取这个中介促成的那些
    contract_ids = {c.id for c in deals}
    reviews = []
    if contract_ids:
        reviews = [r for r in session.scalars(select(Review)).all()
                   if r.contract_id in contract_ids and not r.deleted_at]
    good = len([r for r in reviews if r.rating >= 4])

    return {
        "active_delegations": active,
        "recent_viewings": recent_viewings,
        "recent_days": RECENT_DAYS,
        "deal_total": len(deals),
        "month_deals": month_deals,
        # 带看成交率：中介自己最该看的一个数
        "conversion": _ratio(len(deals), recent_viewings + len(deals)),
        # 放鸽子率要单列。这个数高通常说明预约确认流程有问题，
        # 不是中介的服务质量问题——两者必须能分开看
        "no_show": _ratio(no_show, recent_viewings),
        "praise": _ratio(good, len(reviews)),
        "commission_pending": sum(float(c.amount) for c in commissions
                                  if c.status == "pending"),
        "commission_settled": sum(float(c.amount) for c in commissions
                                  if c.status == "settled"),
    }


@router.get("/api/agent/dashboard")
def dashboard(user: User = Depends(require_agent),
              session: Session = Depends(get_session)):
    """中介工作台，一次取全（方案 8.5 规矩二）。

    十个卡片打十次请求是最容易写、也最容易把首屏拖慢的做法，
    照 `/api/landlord/dashboard` 的先例一次返回。
    """
    profile = _profile(session, user.id, create=True)
    session.commit()

    stats = agent_stats(session, user.id)

    pending = session.scalars(select(HouseDelegation).where(
        HouseDelegation.agent_id == user.id,
        HouseDelegation.status == "pending",
        HouseDelegation.initiated_by == "landlord").order_by(
            HouseDelegation.created_at.desc())).all()

    active_rows = session.scalars(select(HouseDelegation).where(
        HouseDelegation.agent_id == user.id,
        HouseDelegation.status == "active").order_by(
            HouseDelegation.start_at.desc())).all()

    # 今天要带看的：我受托房源上、今天的、已确认的预约
    house_ids = [d.house_id for d in active_rows] or [-1]
    day_start = datetime.now().replace(hour=0, minute=0, second=0,
                                       microsecond=0)
    today = session.scalars(select(Appointment).where(
        Appointment.house_id.in_(house_ids),
        Appointment.viewing_time >= day_start,
        Appointment.viewing_time < day_start + timedelta(days=1)).order_by(
            Appointment.viewing_time)).all()

    return {
        "profile": profile.to_dict(),
        "stats": stats,
        "todo": {
            # 待我确认的邀请
            "invitations": [d.to_dict() for d in pending],
            "today_viewings": [a.to_dict() for a in today],
        },
        "delegations": [d.to_dict() for d in active_rows[:20]],
    }


# ---------------------------------------------------------------- 管理端

class LicenseReview(BaseModel):
    approved: bool
    reason: str = Field(default="", max_length=200)


def _require_admin(user):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")


@router.get("/api/admin/agent-queue")
def license_queue(user: User = Depends(require_login),
                  session: Session = Depends(get_session)):
    """待审资质队列（对标已有的「房源审核」）。"""
    _require_admin(user)
    rows = session.scalars(select(AgentProfile).where(
        AgentProfile.license_status == "pending").order_by(
            AgentProfile.created_at)).all()
    return {
        "total": len(rows),
        "list": [dict(r.to_dict(contact=True),
                      license_images=[i for i in
                                      (r.license_images or "").split(",") if i])
                 for r in rows],
    }


@router.patch("/api/admin/agents/{user_id}")
def review_license(user_id: int, payload: LicenseReview,
                   user: User = Depends(require_login),
                   session: Session = Depends(get_session)):
    """审核中介资质。

    **驳回必须填理由。** 理由同 K-08：「未通过」三个字对被驳回的人
    毫无意义，他不知道该补什么，只能反复提交。
    """
    _require_admin(user)
    profile = _profile(session, user_id)
    if not profile:
        raise HTTPException(status_code=404, detail="中介档案不存在")
    if profile.license_status != "pending":
        raise HTTPException(status_code=409, detail="这份资质不在待审状态")
    if not payload.approved and not payload.reason.strip():
        raise HTTPException(status_code=400, detail="驳回必须填写理由")

    profile.license_status = "approved" if payload.approved else "rejected"
    profile.reject_reason = "" if payload.approved else payload.reason.strip()
    profile.reviewed_by = user.id
    profile.reviewed_at = datetime.now()

    leasing.notify(
        session, profile.user_id, "system",
        "资质审核%s" % ("通过" if payload.approved else "未通过"),
        "你的从业资质已通过审核，现在可以接单了。" if payload.approved
        else "资质未通过：%s" % profile.reject_reason, "")
    session.commit()
    session.refresh(profile)
    return {"ok": True, "profile": profile.to_dict()}


@router.get("/api/admin/agents")
def admin_agents(status: str = "", keyword: str = "",
                 user: User = Depends(require_login),
                 session: Session = Depends(get_session)):
    """中介档案（对标已有的「户主档案」）。

    这一页要回答的是「这个中介靠不靠谱」，所以**被举报记录必须在这里**，
    而不是只躺在举报页签里——管理员不该为了看一个人的底细去翻两个页面。

    统计走 `agent_stats()`，和中介自己工作台上看到的是同一份计算。
    各算各的话，答辩时被问「工作台说 12 次、后台说 9 次，哪个对」
    就没法回答了。
    """
    _require_admin(user)
    stmt = select(AgentProfile)
    if status:
        stmt = stmt.where(AgentProfile.license_status == status)
    rows = session.scalars(stmt.order_by(AgentProfile.created_at.desc())).all()

    if keyword:
        key = keyword.strip()
        rows = [r for r in rows if r.user and
                (key in r.user.username or key in (r.user.real_name or ""))]

    out = []
    for row in rows:
        data = row.to_dict(contact=True)
        data["stats"] = agent_stats(session, row.user_id)
        # 被举报次数。中介被举报走的是同一张 house_reports 表，
        # 按被举报房源当时的受托人归集
        data["reported"] = _reported_count(session, row.user_id)
        out.append(data)
    return {"total": len(out), "list": out}


def _reported_count(session, agent_id):
    """这个中介受托期间、名下房源被举报了多少次。

    按「举报发生时这套房是不是他在托」来算，而不是按当前受托人——
    否则委托一解除，历史举报就全都不算在他头上了。
    """
    from app.models import HouseReport

    rows = session.scalars(select(HouseReport)).all()
    if not rows:
        return {"total": 0, "accepted": 0}
    spans = session.scalars(select(HouseDelegation).where(
        HouseDelegation.agent_id == agent_id)).all()
    hit = [r for r in rows for d in spans
           if d.house_id == r.house_id and d.start_at
           and d.start_at <= r.created_at
           and (d.end_at is None or r.created_at <= d.end_at)]
    return {"total": len(hit),
            "accepted": len([r for r in hit if r.status == "accepted"])}


@router.get("/api/admin/agent-stats")
def platform_agent_stats(user: User = Depends(require_login),
                         session: Session = Depends(get_session)):
    """平台层的中介口径，并进管理后台的数据看板。"""
    _require_admin(user)
    since = _recent_since()

    profiles = session.scalars(select(AgentProfile)).all()
    pending_license = len([p for p in profiles
                           if p.license_status == "pending"])

    available = session.scalars(select(House).where(
        House.status == "available")).all()
    delegated_ids = {d.house_id for d in session.scalars(select(
        HouseDelegation).where(HouseDelegation.status == "active")).all()}
    delegated_available = len([h for h in available if h.id in delegated_ids])

    viewings = session.scalar(select(func.count()).select_from(
        select(ViewingRecord.id).where(
            ViewingRecord.created_at >= since).subquery())) or 0

    # **委托房源 vs 自管房源的成交率对比。**
    #
    # 这是唯一能回答「中介到底有没有用」的数字。做了这个功能就该敢于
    # 把它摆出来——如果委托的反而更低，那说明功能没起作用，应当如实
    # 显示。看板不是用来证明功能有价值的，是用来发现它没价值的。
    contracts = session.scalars(select(Contract)).all()
    with_agent = len([c for c in contracts if c.agent_id])
    without_agent = len(contracts) - with_agent

    return {
        "agent_total": len(profiles),
        "agent_approved": len([p for p in profiles
                               if p.license_status == "approved"]),
        "license_pending": pending_license,
        "delegated_houses": len(delegated_ids),
        "delegated_share": _ratio(delegated_available, len(available)),
        "recent_viewings": viewings,
        "recent_days": RECENT_DAYS,
        "deals_with_agent": with_agent,
        "deals_self_managed": without_agent,
    }
