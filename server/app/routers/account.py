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

**不提供反向降级。** 名下还有在租房源、生效合同的房东降回租客，
那些房源就成了没有主人的数据。真要退出出租业务，该走的是
「下架全部房源 + 结清合同」，那是另一件事，不是一个开关。
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_session
from app.deps import require_login
from app.models import House, User

router = APIRouter()


@router.post("/api/auth/become-landlord")
def become_landlord(user: User = Depends(require_login),
                    session: Session = Depends(get_session)):
    """把当前账号升级为房东。

    **要求先实名。** 房东要签合同，而合同上的甲方必须是真实姓名
    （K-03 已经在签约那一步拦了一道）。放在这里再拦一次不是重复——
    等到发起签约时才发现不能签，房东已经把房源发布、约看房都做完了。
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
        # 要转做房东就先把委托解除干净，这是一次需要他自己确认的迁移，
        # 不该由一个「升级为房东」按钮顺手完成。
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

    user.role = "landlord"
    session.commit()
    session.refresh(user)

    # **不吊销令牌。** 角色变了但还是同一个人，没有安全上的理由把他踢下线；
    # 而踢下线之后他得重新登录才能看到「发布房源」，白白多一步。
    # 令牌里带的角色由前端用返回的 user 覆盖即可。
    return {"ok": True, "user": user.to_dict(),
            "message": "已成为房东，现在可以发布房源了。"
                       "原来的收藏、足迹和租约都还在。"}


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
