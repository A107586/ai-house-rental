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

from datetime import datetime

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_session
from app.deps import require_login
from app.leasing import landlord_blockers, notify, wind_down_delegations
from app.models import House, RoleRequest, User

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

    return {
        "role": user.role,
        "can_upgrade": user.role == "tenant" and not missing,
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
