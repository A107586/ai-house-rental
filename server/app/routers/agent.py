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

import re
from datetime import datetime, timedelta

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app import leasing, moderation
from app.db import get_session
from app.security import hash_password
from app.deps import require_login
from app.models import (AgentCommission, AgentProfile, Appointment, Contract,
                        House, HouseDelegation, Review, User, ViewingRecord,
                        mask_name)
from app.permissions import active_delegation, can_see_tenant_contact, tenant_side_block

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
        intro = payload.intro.strip()
        # **简介是公开文案，和房源描述一个性质。**
        #
        # 它原样出现在「找中介」名录里，任何人不登录都看得到。
        # 原先只审房源不审这里——同一句「加微信 wxid_xxx」换个入口
        # 就发出去了，而且还挂在一个「平台已核验」的中介名下。
        verdict = moderation.check(intro, scene="profile")
        if verdict["level"] == moderation.LEVEL_BLOCK:
            raise HTTPException(
                status_code=400,
                detail="简介未通过审核：" + (verdict["reasons"][0]
                                     if verdict["reasons"] else "内容不合规"))
        profile.intro = intro
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

    # **先看归属，再看角色。** 顺序反过来会出一个很难发现的错：
    # 一个名下有房、同时又是中介身份的账号（管理员改过角色的就是这样，
    # 房源不会跟着角色走），对自己的房源发起委托时会走进「中介申请接单」
    # 那一支，建出一条 `agent_id == landlord_id` 的记录——
    # **自己委托自己**，而且接口返回 200，看起来一切正常。
    #
    # 这与 K-13 是同一类错误：**按角色分支，而不是按他在这件事里
    # 站在哪个位置。** 房主对自己的房子永远是委托方，与他的 role 无关。
    if house.landlord_id == user.id or user.role == "admin":
        target = session.scalar(
            select(User).where(User.username == payload.agent_username.strip()))
        if not target or target.role != "agent":
            raise HTTPException(status_code=404, detail="中介不存在")
        if target.status != "active":
            # 名录里早就不列停用的中介了，手填用户名却照样能邀请——对方登不上来，
            # 这条邀请永远等不到确认
            raise HTTPException(status_code=400, detail="该中介账号已停用，不能委托")
        profile = _profile(session, target.id)
        if not profile or not profile.can_take_orders:
            raise HTTPException(status_code=400,
                                detail="该中介资质未通过或未开启接单")
        if target.id == house.landlord_id:
            raise HTTPException(status_code=400,
                                detail="不能把房源委托给房主自己")
        agent_id, landlord_id, initiated_by = target.id, house.landlord_id, "landlord"
    elif user.role == "agent":
        profile = _profile(session, user.id)
        if not profile or not profile.can_take_orders:
            raise HTTPException(
                status_code=403,
                detail="资质未通过审核或已关闭接单，暂时不能申请接单")
        agent_id, landlord_id, initiated_by = user.id, house.landlord_id, "agent"
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
        # 同一套房上别的中介还在等的申请：一套房只能有一条生效委托，它们已经不可能
        # 再被同意（会 409）。原先一直挂着「待确认」，那几位中介永远等不到回音
        others = session.scalars(select(HouseDelegation).where(
            HouseDelegation.house_id == row.house_id,
            HouseDelegation.status == "pending",
            HouseDelegation.id != row.id)).all()
        for other in others:
            other.status = "rejected"
            other.end_at = datetime.now()
            other.end_reason = "房东已委托给其他中介"
            leasing.notify(session, other.agent_id, "system", "委托未成",
                           "房源 %s 已委托给其他中介，你的申请自动结束。"
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
    # 看房的得是能以租客身份看这套房的人：房东本人、中介、管理员都不是（2026-10-23）
    if tenant_side_block(session, tenant, house):
        raise HTTPException(status_code=400, detail="这个账号不能作为这套房的看房人")
    # 关联的预约必须就是这套房、这个人的。原先 appointment_id 不查，
    # 随手填一个别的房子的预约号也能挂上
    if payload.appointment_id is not None:
        appointment = session.get(Appointment, payload.appointment_id)
        if (not appointment or appointment.house_id != house.id
                or appointment.tenant_id != tenant.id):
            raise HTTPException(status_code=400,
                                detail="关联的预约不是这位租客在这套房上的预约")

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
    """带看流水。中介看自己带的，房东看自己房子被带的，管理员看全部。

    **按「是不是当事一方」取，不按角色分支**——这是 K-13 留下的教训，
    我在这里又犯了一次。原来写的是「role 是 agent 就只看他带的，
    否则看他名下房源的」：一个由房东被管理员改成中介的账号，名下房源
    仍然在，但这套房被谁带看过他从此看不到了。**数据一直在库里，
    是查询把它挡住的**，页面上完全看不出来，和 K-13 那次一模一样。
    """
    stmt = select(ViewingRecord)
    if user.role != "admin":
        mine = select(House.id).where(House.landlord_id == user.id)
        stmt = stmt.where(or_(ViewingRecord.agent_id == user.id,
                              ViewingRecord.house_id.in_(mine)))
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


@router.get("/api/agent/commission-proposals")
def commission_proposals(user: User = Depends(require_agent),
                         session: Session = Depends(get_session)):
    """等我表态的佣金调低：受托房源上、还没生效、比例比委托时低的合同。

    中介不是合同一方，看不到合同本身（_visible_contract 只放当事双方），
    所以这里把表态需要的几样直接带出来。
    """
    rows = session.scalars(
        select(Contract).join(HouseDelegation, HouseDelegation.house_id == Contract.house_id)
        .where(HouseDelegation.agent_id == user.id, HouseDelegation.status == "active",
               Contract.status == "pending", Contract.commission_state == "pending")
        .order_by(Contract.created_at.desc())).all()
    out = []
    for c in rows:
        delegation = active_delegation(session, c.house_id)
        out.append({
            "contract_no": c.contract_no,
            "house_no": c.house.house_no if c.house else "",
            "house_title": c.house.title if c.house else "",
            "rent": float(c.rent),
            "proposed_rate": float(c.commission_rate),
            "delegation_rate": float(delegation.commission_rate) if delegation else None,
            "landlord_name": mask_name(session.get(User, c.landlord_id)),
            "created_at": c.created_at.strftime("%Y-%m-%d %H:%M") if c.created_at else "",
        })
    return {"total": len(out), "list": out}


@router.post("/api/contracts/{contract_no}/commission")
def respond_commission(contract_no: str,
                       accept: bool = Body(embed=True),
                       user: User = Depends(require_agent),
                       session: Session = Depends(get_session)):
    """受托中介同意或不同意房东调低的佣金比例（2026-10-23 定）。"""
    contract = session.scalar(select(Contract).where(Contract.contract_no == contract_no))
    if not contract:
        raise HTTPException(status_code=404, detail="合同不存在")
    try:
        leasing.respond_commission(session, contract, user, accept)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    session.commit()
    return {"ok": True, "contract_no": contract.contract_no,
            "commission_state": contract.commission_state}


@router.get("/api/landlord/commissions")
def owed_commissions(status: str = "",
                     user: User = Depends(require_login),
                     session: Session = Depends(get_session)):
    """我（房东）应付给中介的佣金。管理员看全部。"""
    stmt = select(AgentCommission).join(
        Contract, AgentCommission.contract_id == Contract.id)
    if user.role != "admin":
        stmt = stmt.where(Contract.landlord_id == user.id)
    if status:
        stmt = stmt.where(AgentCommission.status == status)
    rows = session.scalars(stmt.order_by(AgentCommission.created_at.desc())).all()
    return {"total": len(rows),
            "pending_amount": sum(float(r.amount) for r in rows if r.status == "pending"),
            "settled_amount": sum(float(r.amount) for r in rows if r.status == "settled"),
            "list": [dict(r.to_dict(),
                          agent_name=mask_name(session.get(User, r.agent_id)),
                          house_no=(r.contract.house.house_no
                                    if r.contract and r.contract.house else ""),
                          contract_no=r.contract.contract_no if r.contract else "")
                     for r in rows]}


def _commission_or_404(session, commission_id):
    row = session.get(AgentCommission, commission_id)
    if not row:
        raise HTTPException(status_code=404, detail="佣金记录不存在")
    return row


@router.post("/api/commissions/{commission_id}/settle")
def settle_commission(commission_id: int,
                      user: User = Depends(require_login),
                      session: Session = Depends(get_session)):
    """房东付清佣金后点「已结清」。钱线下给，这里记下状态与时间、通知中介。

    原先佣金只有「成交那一刻记一笔 pending」，**此后没有任何一处会改它的状态**：
    中介工作台上「已结算」永远是 0，每一笔都永远「待结算」（2026-10-22）。
    只有欠这笔钱的那位房东（或管理员）能点——中介自己点「已结清」不算数。
    """
    row = _commission_or_404(session, commission_id)
    landlord_id = row.contract.landlord_id if row.contract else None
    if user.role != "admin" and user.id != landlord_id:
        raise HTTPException(status_code=403, detail="只有付佣金的房东可以确认结清")
    if row.status != "pending":
        raise HTTPException(status_code=409, detail="这笔佣金已经%s" % (
            "结清" if row.status == "settled" else "取消"))
    row.status = "settled"
    row.settled_at = datetime.now()
    leasing.notify(session, row.agent_id, "system", "佣金已结清",
                   "合同 %s 的佣金 %s 元，房东已确认结清。" % (
                       row.contract.contract_no if row.contract else "",
                       leasing._money(row.amount)),
                   row.contract.contract_no if row.contract else "")
    session.commit()
    session.refresh(row)
    return {"ok": True, "commission": row.to_dict()}


@router.post("/api/commissions/{commission_id}/cancel")
def cancel_commission(commission_id: int,
                      reason: str = Body(default="", embed=True),
                      user: User = Depends(require_login),
                      session: Session = Depends(get_session)):
    """取消一笔佣金：**只有管理员**，用于纠纷处理（比如成交后很快因中介的问题退租）。
    房东不能单方面取消自己欠的钱。"""
    _require_admin(user)
    row = _commission_or_404(session, commission_id)
    if row.status != "pending":
        raise HTTPException(status_code=409, detail="只有待结算的佣金可以取消")
    if not reason.strip():
        raise HTTPException(status_code=400, detail="请填写取消原因")
    row.status = "cancelled"
    leasing.notify(session, row.agent_id, "system", "佣金已取消",
                   "合同 %s 的佣金 %s 元已由平台取消：%s" % (
                       row.contract.contract_no if row.contract else "",
                       leasing._money(row.amount), reason.strip()[:100]),
                   row.contract.contract_no if row.contract else "")
    session.commit()
    session.refresh(row)
    return {"ok": True, "commission": row.to_dict()}


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
    contract_ids = [c.id for c in deals]
    reviews = []
    if contract_ids:
        # **过滤必须落在 SQL 里。** 原来是把整张 reviews 表读进来再用
        # Python 筛——管理端的中介档案页每个中介调一次 agent_stats，
        # 十个中介就是十遍全表扫描。N-01 要求接口 < 1s，这种写法在
        # 演示数据上看不出问题，评价一多就直接崩掉。
        reviews = session.scalars(select(Review).where(
            Review.contract_id.in_(contract_ids),
            Review.deleted_at.is_(None))).all()

    # **好评率按「评中介的那一栏」算，不再拿房源总体评分顶替。**
    #
    # 原来用的是 `r.rating`——那是对这套房的总体评价。房子采光差、
    # 隔音烂，中介带看准时、协调修东西很上心，他照样被算成差评；
    # 反过来房子好中介失约，他也能白得一个高分。**这个数字原本
    # 度量的根本不是他的服务。**
    #
    # 只统计真的评了中介的那些（rating_agent > 0）。没评的不能当 0 分：
    # 那是「没说」，不是「很差」。
    rated = [r for r in reviews if (r.rating_agent or 0) > 0]
    good = len([r for r in rated if r.rating_agent >= 4])
    agent_scores = [r.rating_agent for r in rated]

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
        "praise": _ratio(good, len(rated)),
        # 评过他的有几个人。**好评率必须和样本量一起给**——
        # 「100% 好评（1 条）」和「92% 好评（48 条）」不是一回事，
        # 只给百分比会让前者看起来更强
        "rated_count": len(rated),
        "rating": (round(sum(agent_scores) / len(agent_scores), 1)
                   if agent_scores else 0),
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

    # 今天要带看的：我受托房源上、今天的、**已确认**的预约。
    #
    # status 这个条件不能省。省掉的话，一条房东还没点确认的预约也会被算进
    # 「今日带看 1 场」，中介照着这个数去安排一天的行程，到了地方才发现
    # 这场约根本没成立。卡片上的数字必须是他真的要去做的事。
    house_ids = [d.house_id for d in active_rows] or [-1]
    houses_by_id = {d.house.id: d.house for d in active_rows if d.house}
    day_start = datetime.now().replace(hour=0, minute=0, second=0,
                                       microsecond=0)
    today = session.scalars(select(Appointment).where(
        Appointment.house_id.in_(house_ids),
        Appointment.status == "confirmed",
        Appointment.viewing_time >= day_start,
        Appointment.viewing_time < day_start + timedelta(days=1)).order_by(
            Appointment.viewing_time)).all()

    # 待我确认的预约单独一档。上面把未确认的滤掉了，但它们不能就此消失——
    # 受托期间这些预约本来就该由中介来处理（J-05），落在待办里才有人管。
    # 取今天之后的全部，而不只是今天的：一条明天的预约今天不确认，
    # 明天就来不及了。
    pending_appointments = session.scalars(select(Appointment).where(
        Appointment.house_id.in_(house_ids),
        Appointment.status == "pending",
        # 今天早上那条已经过了点，确认不了了，不再算待办
        Appointment.viewing_time > datetime.now()).order_by(
            Appointment.viewing_time).limit(20)).all()

    return {
        "profile": profile.to_dict(),
        "stats": stats,
        "todo": {
            # 待我确认的邀请
            "invitations": [d.to_dict() for d in pending],
            # 今天的带看在窗口内，联系方式给；待确认的那些还没到日子，
            # 号码此刻对中介没有用处（J-07）
            "today_viewings": [
                a.to_dict(show_contact=can_see_tenant_contact(
                    session, user, a, houses_by_id.get(a.house_id)))
                for a in today],
            "pending_appointments": [
                a.to_dict(show_contact=False) for a in pending_appointments],
            # 房东调低了佣金比例、等我表态的合同（2026-10-23）
            "commission_proposals": commission_proposals(user, session)["list"],
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


@router.post("/api/admin/agents")
def create_agent(payload: dict = Body(default={}),
                 user: User = Depends(require_login),
                 session: Session = Depends(get_session)):
    """管理员直接创建一个中介账号（F2）。

    **为什么需要这个入口。** 原先要一个经纪人上线得走三步：
    他自己注册 → 提交资质 → 管理员审核。可现实里中介多是平台
    先签下来、再开账号的——人已经在办公室里坐着了，还要他自己
    去注册页填一遍，然后等自己人审自己人。

    所以给管理员一条直通路径：填姓名手机号、指定主营区域，
    账号建出来就是 `role=agent` 且资质已过审（`reviewed_by` 记的是
    这位管理员，谁开的账号查得到）。

    **初始密码由管理员设定并当面告知，接口不回显。**
    返回里带明文密码等于把它写进浏览器历史与日志；
    真要找回走的是重置流程，不是翻记录。
    """
    _require_admin(user)

    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "").strip()
    real_name = str(payload.get("real_name") or "").strip()
    phone = str(payload.get("phone") or "").strip()
    districts = payload.get("service_districts") or []
    intro = str(payload.get("intro") or "").strip()[:300]
    years = payload.get("years")

    if not re.fullmatch(r"[A-Za-z0-9_]{3,20}", username):
        raise HTTPException(status_code=422,
                            detail="用户名需为 3–20 位字母、数字或下划线")
    if not 6 <= len(password) <= 64 or any(c.isspace() for c in password):
        raise HTTPException(status_code=422,
                            detail="初始密码需 6–64 位且不能包含空格")
    if not re.fullmatch(r"[A-Za-z一-鿿· ]{2,20}", real_name):
        raise HTTPException(status_code=422, detail="姓名需为 2–20 位中英文")
    if not re.fullmatch(r"\d{11}", phone):
        raise HTTPException(status_code=422, detail="请填写 11 位手机号")

    if session.scalar(select(User).where(User.username == username)):
        raise HTTPException(status_code=409, detail="该用户名已被占用")

    agent = User(username=username, password_hash=hash_password(password),
                 real_name=real_name, phone=phone, role="agent",
                 status="active")
    session.add(agent)
    session.flush()

    profile = AgentProfile(
        user_id=agent.id,
        # 管理员开的账号资质直接算过审——**是他把人签进来的**，
        # 再让他去审一遍自己刚建的账号只是走形式
        license_status="approved",
        reviewed_by=user.id,
        reviewed_at=datetime.now(),
        service_districts=",".join(
            str(d).strip() for d in list(districts)[:6] if str(d).strip()),
        intro=intro,
        years=int(years) if str(years).isdigit() else 0,
        accepting=True,
    )
    session.add(profile)
    session.commit()
    session.refresh(agent)

    leasing.notify(session, agent.id, "system", "经纪人账号已开通",
                   "管理员已为你开通经纪人账号，登录后可在中介台接受委托、"
                   "登记带看。初始密码请向管理员索取。")
    session.commit()

    return {"ok": True, "user": agent.to_dict(),
            "message": "已创建经纪人账号 %s，请把初始密码当面告知本人。"
                       % username}


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

    spans = session.scalars(select(HouseDelegation).where(
        HouseDelegation.agent_id == agent_id,
        HouseDelegation.start_at.is_not(None))).all()
    if not spans:
        return {"total": 0, "accepted": 0}

    # 只取他托过的那几套房的举报，而不是把整张举报表读进来。
    # 「时间落在哪一段委托里」这一步仍在 Python 做——那是区间比对，
    # 用 SQL 表达要么写成一串 OR，要么按委托条数发多次查询，
    # 两种都不比现在清楚。**先用房源把量缩小，再在小集合上比对**。
    rows = session.scalars(select(HouseReport).where(
        HouseReport.house_id.in_({d.house_id for d in spans}))).all()
    hit = [r for r in rows for d in spans
           if d.house_id == r.house_id
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

    delegated_ids = set(session.scalars(select(HouseDelegation.house_id).where(
        HouseDelegation.status == "active")).all())
    available_total = session.scalar(select(func.count()).select_from(
        House).where(House.status == "available")) or 0
    delegated_available = session.scalar(select(func.count()).select_from(
        House).where(House.status == "available",
                     House.id.in_(delegated_ids or {-1}))) or 0

    viewings = session.scalar(select(func.count()).select_from(
        select(ViewingRecord.id).where(
            ViewingRecord.created_at >= since).subquery())) or 0

    # **委托房源 vs 自管房源的成交率对比。**
    #
    # 这是唯一能回答「中介到底有没有用」的数字。做了这个功能就该敢于
    # 把它摆出来——如果委托的反而更低，那说明功能没起作用，应当如实
    # 显示。看板不是用来证明功能有价值的，是用来发现它没价值的。
    # 数数就用 count，不要把整张合同表读进内存——
    # 这一页只需要两个数字
    deal_total = session.scalar(
        select(func.count()).select_from(Contract)) or 0
    with_agent = session.scalar(select(func.count()).select_from(
        Contract).where(Contract.agent_id.is_not(None))) or 0
    without_agent = deal_total - with_agent

    return {
        "agent_total": len(profiles),
        "agent_approved": len([p for p in profiles
                               if p.license_status == "approved"]),
        "license_pending": pending_license,
        "delegated_houses": len(delegated_ids),
        "delegated_share": _ratio(delegated_available, available_total),
        "recent_viewings": viewings,
        "recent_days": RECENT_DAYS,
        "deals_with_agent": with_agent,
        "deals_self_managed": without_agent,
    }


# ---------------------------------------------------------- J-09-b 智能派单

# 四个因子的权重，加起来 100。**权重写在这里而不是散在算式里**，
# 是为了让「为什么这么排」可以当场解释——答辩时被问「凭什么他排第一」，
# 要能指着这张表说话。
WEIGHTS = {"activity": 30, "praise": 30, "load": 25, "deals": 15}

# 活跃度与负载的封顶。超过这个数再多也不加分/不扣分了：
# 近 30 天带看 40 次和 20 次，对「他还接不接得过来」没有区别
ACTIVITY_CAP = 20
LOAD_CAP = 10
DEALS_CAP = 10

# 好评率样本不足时按这个中性值算（与 RatioCell 的 MIN_SAMPLE 同一条理由）。
# **不能按 0 分算**——那等于新中介永远排在最后，
# 而「没人给过差评」和「大家都给差评」是两回事
NEUTRAL_PRAISE = 50.0
MIN_PRAISE_SAMPLE = 5


def _score_agent(stats):
    """给一个中介打分，返回 ``(分数, [理由])``。

    **全部由规则算，不调大模型。** 派单结果直接决定谁能接到这单生意，
    它必须是可复算、可申诉的：中介问「为什么不派给我」，
    要能拿出一行一行的算式，而不是「模型觉得他更合适」。

    方案里写的「大模型可选地在同分时给一句说明」没有做，理由是
    规则本来就能给出精确到分数的理由（下面 `reasons` 里每一条都带数字），
    再让大模型改写一遍只会引入不确定，换不来任何新信息。
    """
    reasons = []

    recent = min(stats["recent_viewings"], ACTIVITY_CAP)
    activity = recent / ACTIVITY_CAP * WEIGHTS["activity"]
    if stats["recent_viewings"]:
        reasons.append("近 %d 天带看 %d 次"
                       % (stats["recent_days"], stats["recent_viewings"]))

    praise = stats["praise"]
    if praise["total"] >= MIN_PRAISE_SAMPLE:
        rate = praise["rate"]
        reasons.append("好评率 %.0f%%（%d 条评价）" % (rate, praise["total"]))
    else:
        rate = NEUTRAL_PRAISE
        reasons.append("评价样本不足，好评率按中性计")
    quality = rate / 100 * WEIGHTS["praise"]

    active = stats["active_delegations"]
    load = (1 - min(active, LOAD_CAP) / LOAD_CAP) * WEIGHTS["load"]
    reasons.append("当前在托 %d 套" % active)

    deals = min(stats["deal_total"], DEALS_CAP) / DEALS_CAP * WEIGHTS["deals"]
    if stats["deal_total"]:
        reasons.append("累计成交 %d 单" % stats["deal_total"])

    return round(activity + quality + load + deals, 1), reasons


@router.get("/api/agents/directory")
def agent_directory(keyword: str = "", district: str = "",
                    limit: int = Query(20, ge=1, le=50),
                    user: User = Depends(require_login),
                    session: Session = Depends(get_session)):
    """可接单的中介名录（F1）。

    与 `/api/delegations/suggest` 的区别是**不需要先选房源**。
    委托面板原先的流程是：先选一套房 → 点「帮我推荐中介」→ 从结果里
    「选他」→ 用户名被填进一个输入框 → 再点提交。四步，而且中间那个
    输入框让人以为**必须知道对方的登录名才能委托**。

    现在改成一进面板就列出可接单的中介，可按姓名或主营区域搜，
    点一下直接发邀请。名录本身不涉及某一套房，所以不限房主——
    但仅限已登录用户，且只出**资质过审、开着接单开关**的人：
    没过审的出现在这里，房东委托过去只会白等。

    返回里不给手机号。要联系先建立委托关系，这是 J-07 定下的规矩。
    """
    stmt = (select(User, AgentProfile)
            .join(AgentProfile, AgentProfile.user_id == User.id)
            .where(User.role == "agent",
                   User.status == "active",
                   AgentProfile.license_status == "approved",
                   AgentProfile.accepting.is_(True)))

    kw = keyword.strip()
    if kw:
        like = "%%%s%%" % kw
        stmt = stmt.where(or_(User.real_name.like(like),
                              User.username.like(like),
                              AgentProfile.service_districts.like(like),
                              AgentProfile.intro.like(like)))
    if district.strip():
        stmt = stmt.where(
            AgentProfile.service_districts.like("%%%s%%" % district.strip()))

    rows = session.execute(stmt.limit(limit)).all()

    out = []
    for u, prof in rows:
        stats = agent_stats(session, u.id)
        score, reasons = _score_agent(stats)
        out.append({
            "user_id": u.id,
            "username": u.username,
            "name": u.real_name or u.username,
            "districts": [d for d in (prof.service_districts or "").split(",") if d],
            "years": prof.years or 0,
            "intro": prof.intro or "",
            "score": score,
            "reasons": reasons,
            "in_progress": stats["active_delegations"],
            "deal_total": stats["deal_total"],
            # 租客给他打的分，与撮合评分 score 分开列。
            # **score 是平台算的（活跃度、成交量、负载），rating 是人评的**，
            # 混成一个数字，房东就分不清「平台推荐他」和「租客喜欢他」
            "rating": stats["rating"],
            "rated_count": stats["rated_count"],
        })

    # 按评分降序。同分按 user_id，保证顺序稳定——
    # 每次刷新顺序都变的话，用户会以为名单在动
    out.sort(key=lambda a: (-a["score"], a["user_id"]))
    return {"list": out, "total": len(out)}


@router.get("/api/delegations/suggest")
def suggest_agents(house_no: str, limit: int = Query(5, ge=1, le=20),
                   user: User = Depends(require_login),
                   session: Session = Depends(get_session)):
    """给这套房推荐几个中介（J-09-b）。

    只有**房主本人和管理员**能问——这是「我该把房子委托给谁」的答案，
    不是一份可以随便拉取的中介通讯录。

    两个硬条件先把人筛掉，不参与打分：主营区域不覆盖这个区的、
    以及关掉了接单开关或资质没过审的。硬条件就是硬条件，
    分数再高也不该出现在结果里。
    """
    house = session.scalar(select(House).where(House.house_no == house_no))
    if not house:
        raise HTTPException(status_code=404, detail="房源不存在")
    if user.role != "admin" and house.landlord_id != user.id:
        raise HTTPException(status_code=403, detail="只能为自己的房源找中介")

    if _one_active(session, house.id):
        raise HTTPException(status_code=409, detail="这套房已经有生效中的委托")

    profiles = session.scalars(select(AgentProfile).where(
        AgentProfile.license_status == "approved",
        AgentProfile.accepting.is_(True))).all()

    ranked = []
    for profile in profiles:
        # 主营区域为空表示不限区域——新中介还没填，不该因此被排除
        if profile.districts and house.district not in profile.districts:
            continue
        stats = agent_stats(session, profile.user_id)
        score, reasons = _score_agent(stats)
        ranked.append({
            "user_id": profile.user_id,
            "username": profile.user.username if profile.user else "",
            "name": mask_name(profile.user) if profile.user else "",
            "districts": profile.districts,
            "years": profile.years,
            "intro": profile.intro,
            "score": score,
            "reasons": reasons,
            "stats": stats,
        })

    ranked.sort(key=lambda r: (-r["score"], r["username"]))
    return {
        "total": len(ranked),
        "house_no": house.house_no,
        "district": house.district,
        # 权重一并返回，前端可以把它显示出来。**排序规则不该是黑箱**：
        # 中介问「为什么不派给我」，房东问「为什么推荐他」，
        # 答案得在同一个页面上找得到
        "weights": WEIGHTS,
        "list": ranked[:limit],
        "source": "rules",
    }
