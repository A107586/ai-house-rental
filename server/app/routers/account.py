"""账号身份升级（K-13）。

`奇思妙想.docx` 第 2 条：「房东方和租客方两个身份应该是一个账号，
分区域而不是分两个账号，身份互换」。

现状是 `users.role` 单值、注册时定死、之后只有管理员能改。演示一条完整
链路要两个账号来回切换，而现实里房东自己也要租房、租客也可能转租。

**没有把 role 改成多角色。** 那会牵动全站每一处 `require_staff` 和
前端的 `role === 'landlord'` 判断，风险远大于收益，而收益主要在体验。
贝壳自己也是单账号：「发布房源」是一个入口，不是另一个账号。

所以做的是**升级**，一次单向的 role 变更：

    tenant ──升级──► landlord
              （收藏、足迹、租约、账单一条不丢）

数据本来就不会丢——它们全挂在 `user_id` 上，与 role 无关。这一点
值得写进测试，因为「升级会不会把我的收藏清空」是用户真正担心的事。

**升级要审核，退回不用（D1/D2）。**

升级原先是点一下当场生效——任何注册用户点一次就能发布房源、签合同、
收租。在一个要签合同收钱的平台上这不成立：房东是合同甲方，
平台至少要看一眼这个人是谁。所以改成申请制，管理员在后台通过或驳回。
**获批之前仍是租客**，找房收藏预约照常，只是发不了房源。

退回则不需要任何审核：那不是权限升级，是主动放弃权限，
没有需要把关的东西。唯一的前置是名下不能还有在租房源与生效合同——
那是数据完整性要求（不然那些房源就成了没有主人的数据），
不是审批。这一条会在接口里明确告诉用户还差哪几套。
"""

import secrets
from datetime import datetime

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_session
from app.deps import require_login
from app.leasing import (cancel_tenant_viewings, landlord_blockers, notify,
                         wind_down_delegations)
from app.models import (AgentCommission, AgentProfile, Bill, BrowsingHistory, ChatMessage,
                        Contract, Favorite, House, RoleRequest, SavedSearch, User)
from app.security import hash_password, verify_password

router = APIRouter()


@router.post("/api/auth/become-landlord")
def become_landlord(payload: dict = Body(default={}),
                    user: User = Depends(require_login),
                    session: Session = Depends(get_session)):
    """提交成为房东的申请（D1）。

    **不再当场生效。** 提交后进入待审，管理员在后台通过才变角色。
    在此之前用户仍是租客，找房、收藏、预约都不受影响。

    实名与手机号仍然是前置：等到管理员审的时候才发现姓名是空的，
    等于把校验推给了人。
    """
    if user.role == "admin":
        raise HTTPException(status_code=400,
                            detail="管理员账号本来就能管理房源")
    if user.role == "landlord":
        raise HTTPException(status_code=409, detail="你已经是房东了")
    if user.role == "agent":
        # **必须拦。** 中介的房源管理权来自「role == agent 且有生效委托」，
        # 一旦角色变成 landlord，他手上在托的房源立刻全部管不了，
        # 而页面上不会有任何提示——房东那边也不知道自己的委托已经失效。
        raise HTTPException(
            status_code=400,
            detail="经纪人账号不能直接转为房东。如需自己出租，"
                   "请先解除全部在托委托，再联系管理员变更身份")

    if not user.real_name.strip():
        raise HTTPException(
            status_code=400,
            detail="成为房东需要先实名。请在「账号设置」里填写真实姓名，"
                   "签合同时甲方要用它")
    if not user.phone.strip():
        raise HTTPException(
            status_code=400,
            detail="成为房东需要先填手机号，租客要靠它联系你")

    # 已经有一条待审的就别重复提交。返回 200 而不是报错——
    # 用户想做的事（成为房东）已经在办了，告诉他进度即可
    exist = session.scalar(select(RoleRequest).where(
        RoleRequest.user_id == user.id,
        RoleRequest.target_role == "landlord",
        RoleRequest.status == "pending"))
    if exist:
        return {"ok": True, "pending": True, "request": exist.to_dict(),
                "message": "你的房东申请正在审核中，通过后会站内通知你。"}

    req = RoleRequest(user_id=user.id, target_role="landlord",
                      reason=str(payload.get("reason") or "").strip()[:500])
    session.add(req)
    session.commit()
    session.refresh(req)

    return {"ok": True, "pending": True, "request": req.to_dict(),
            "message": "已提交房东申请，管理员审核通过后即可发布房源。"
                       "在此之前你仍可以正常找房、收藏和预约。"}


@router.post("/api/auth/revert-to-tenant")
def revert_to_tenant(user: User = Depends(require_login),
                     session: Session = Depends(get_session)):
    """房东退回租客（D2）。**不需要审核。**

    放弃权限不需要别人批准。但要先确认名下没有还在运转的东西——
    在租房源和生效合同一旦失去主人，租客那边的合同就指向一个
    没有房东权限的账号，报修找不到人、收租收不上来。

    所以这里拦的是**数据完整性**，不是资格：把还差哪几套明确列出来，
    用户自己去处理，处理完随时可以再来。
    """
    if user.role != "landlord":
        raise HTTPException(status_code=400, detail="当前账号不是房东")

    blockers = landlord_blockers(session, user)
    if blockers:
        raise HTTPException(
            status_code=400,
            detail="退回租客前需要先处理：" + "；".join(blockers)
                   + "。下架房源、结清合同之后再来即可。")

    user.role = "tenant"
    # 下架的房子还托在中介手上的话，中介能替一个已经不是房东的人重新上架
    wind_down_delegations(session, "房东已退回租客身份", landlord_id=user.id)
    session.commit()
    session.refresh(user)
    return {"ok": True, "user": user.to_dict(),
            "message": "已退回租客身份。收藏、足迹、租约与账单都还在，"
                       "以后想再出租可以重新申请。"}


# ---------------------------------------------------------------- 管理端审核

@router.get("/api/admin/role-requests")
def list_role_requests(status: str = "pending",
                       admin: User = Depends(require_login),
                       session: Session = Depends(get_session)):
    """待审的角色申请。默认只给 pending——管理员打开就是来处理待办的。"""
    if admin.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    stmt = select(RoleRequest).order_by(RoleRequest.created_at.desc())
    if status:
        stmt = stmt.where(RoleRequest.status == status)
    rows = session.scalars(stmt.limit(200)).all()
    return {"list": [r.to_dict() for r in rows]}


@router.post("/api/admin/role-requests/{request_id}")
def review_role_request(request_id: int,
                        payload: dict = Body(default={}),
                        admin: User = Depends(require_login),
                        session: Session = Depends(get_session)):
    """通过或驳回。**驳回必须填理由**——只说「不通过」等于让用户去猜。"""
    if admin.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    req = session.get(RoleRequest, request_id)
    if not req:
        raise HTTPException(status_code=404, detail="申请不存在")
    if req.status != "pending":
        raise HTTPException(status_code=409, detail="这条申请已经处理过了")

    action = str(payload.get("action") or "").strip()
    note = str(payload.get("note") or "").strip()[:500]
    if action not in ("approve", "reject"):
        raise HTTPException(status_code=422, detail="action 只能是 approve 或 reject")
    if action == "reject" and not note:
        raise HTTPException(status_code=422, detail="驳回必须填写理由")

    target = session.get(User, req.user_id)
    if not target:
        raise HTTPException(status_code=404, detail="申请人已不存在")

    req.status = "approved" if action == "approve" else "rejected"
    req.review_note = note
    req.reviewer_id = admin.id
    req.reviewed_at = datetime.now()

    if action == "approve":
        # 审核期间他可能已经通过别的途径变了角色，这里只在仍是租客时才改
        if target.role == "tenant":
            target.role = req.target_role
        notify(session, target.id, "system", "房东申请已通过",
               "你的%s申请已通过审核，现在可以发布房源了。"
               % req.to_dict()["target_role_label"])
    else:
        notify(session, target.id, "system", "房东申请未通过",
               "未通过原因：%s。补充材料后可以再次提交。" % note)

    session.commit()
    session.refresh(req)
    return {"ok": True, "request": req.to_dict()}


@router.get("/api/auth/landlord-eligibility")
def landlord_eligibility(user: User = Depends(require_login),
                         session: Session = Depends(get_session)):
    """能不能升级、还差什么。

    前端据此决定按钮是可点还是带一句「还差手机号」——
    让用户点下去才被告知缺什么，是多余的一次往返。
    """
    missing = []
    if not user.real_name.strip():
        missing.append("真实姓名")
    if not user.phone.strip():
        missing.append("手机号")

    # 最近一次申请。原先这里不说，用户提交过申请再打开页面，看到的还是那个「成为房东」按钮，
    # 被驳回了也只能去翻站内信才知道为什么
    latest = session.scalar(select(RoleRequest).where(
        RoleRequest.user_id == user.id, RoleRequest.target_role == "landlord")
        .order_by(RoleRequest.created_at.desc(), RoleRequest.id.desc()).limit(1))
    waiting = bool(latest and latest.status == "pending")

    return {
        "role": user.role,
        "can_upgrade": user.role == "tenant" and not missing and not waiting,
        "request": latest.to_dict() if latest else None,
        "already_landlord": user.role in ("landlord", "admin"),
        # 经纪人既不是「已经是房东」也不是「可以升级」，
        # 前端据此把升级入口整个藏掉，而不是给一个点了会报错的按钮
        "is_agent": user.role == "agent",
        "missing": missing,
        # 升级后这些数据都还在，前端可以直接摆出来打消顾虑
        "kept": {
            "houses": len(session.scalars(select(House).where(
                House.landlord_id == user.id)).all()),
        },
    }


# ---------------------------------------------------------------- 注销账号

class DeactivateRequest(BaseModel):
    password: str = Field(default="", max_length=64)


def deactivation_blockers(session, user):
    """注销之前还没了结的事。**钱和合同没了结的不能注销**：注销之后对方就找不到人了。"""
    from sqlalchemy import func, or_

    blockers = []
    open_contracts = session.scalar(select(func.count()).select_from(Contract).where(
        or_(Contract.tenant_id == user.id, Contract.landlord_id == user.id),
        Contract.status.in_(("pending", "active"))))
    if open_contracts:
        blockers.append("%d 份合同还没结束（待确认的可以撤回 / 拒签，生效中的先退租）" % open_contracts)
    unpaid = session.scalar(select(func.count()).select_from(Bill).join(
        Contract, Bill.contract_id == Contract.id).where(
        Contract.tenant_id == user.id, Bill.status == "unpaid"))
    if unpaid:
        blockers.append("%d 笔账单还没结清" % unpaid)
    listed = session.scalar(select(func.count()).select_from(House).where(
        House.landlord_id == user.id, House.status.in_(("available", "rented", "pending"))))
    if listed:
        blockers.append("%d 套房源还在架或审核中" % listed)
    owed = session.scalar(select(func.count()).select_from(AgentCommission).join(
        Contract, AgentCommission.contract_id == Contract.id).where(
        AgentCommission.status == "pending",
        or_(AgentCommission.agent_id == user.id, Contract.landlord_id == user.id)))
    if owed:
        blockers.append("%d 笔中介佣金还没结算" % owed)
    return blockers


@router.post("/api/auth/deactivate")
def deactivate_account(payload: DeactivateRequest,
                       user: User = Depends(require_login),
                       session: Session = Depends(get_session)):
    """注销自己的账号（2026-10-23 补）。

    个人信息保护法第 47 条：用户注销账号，平台应当删除其个人信息。原先只有管理员能「停用」，
    用户自己没有任何办法让平台忘掉他。

    - **要输一次密码**：令牌可能被别人拿到，注销不可逆，不能凭一个令牌就办；
    - **合同、账单、佣金没了结的不让注销**，并列出还差什么——对方还要找他；
    - 注销之后：姓名、手机、邮箱、身份证摘要清空，用户名改成 deleted_编号（原来那个可以被重新注册），
      收藏、足迹、保存的搜索、聊天记录删除，未到的看房取消、委托收尾，所有设备登出；
    - **合同与收支流水保留**：那是双方的交易凭证，不是他一个人的数据，合同正文里的姓名也不改。
    """
    if user.role == "admin":
        raise HTTPException(status_code=400, detail="管理员账号不能自助注销，请由其他管理员停用")
    if not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=403, detail="密码不正确")
    blockers = deactivation_blockers(session, user)
    if blockers:
        raise HTTPException(status_code=409,
                            detail="注销前需要先处理：" + "；".join(blockers) + "。")

    cancel_tenant_viewings(session, user, "租客已注销账号")
    wind_down_delegations(session, "对方已注销账号", landlord_id=user.id)
    wind_down_delegations(session, "对方已注销账号", agent_id=user.id)
    for model in (Favorite, BrowsingHistory, SavedSearch, ChatMessage):
        for row in session.scalars(select(model).where(model.user_id == user.id)).all():
            session.delete(row)
    profile = session.scalar(select(AgentProfile).where(AgentProfile.user_id == user.id))
    if profile:
        profile.accepting = False
        profile.intro = ""
        profile.license_images = ""

    user.status = "deleted"
    user.username = "deleted_%d" % user.id
    user.real_name = "已注销用户"
    user.phone = ""
    user.email = ""
    user.id_card_hash = ""
    user.id_card_masked = ""
    user.accept_contact = False
    user.password_hash = hash_password(secrets.token_urlsafe(32))
    user.token_valid_after = datetime.now()
    session.commit()
    return {"ok": True, "message": "账号已注销。合同与缴费记录作为交易凭证保留，其余个人信息已删除。"}
