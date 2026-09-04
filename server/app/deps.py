"""鉴权依赖。

从 `main.py` 搬出来，让按业务拆分的路由模块能直接用，而不必回头 import
`main`——那会形成 `main → routers → main` 的循环导入。

三个依赖是分层的：`current_user` 允许匿名（返回 None），
`require_login` 要求登录；`require_staff` 再要求是房东或管理员；
`require_house_manager` 放行中介，但它只管角色，具体哪一套房
还要过 `app.permissions` 里的房源级判断。
接口按自己的需要挑一个，**不要在接口体内手写角色判断**——
散落的权限检查是最容易漏掉一处的写法。
"""

from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app.auth import decode_token
from app.db import get_session
from app.models import User


def current_user(authorization: str = Header(default=""),
                 session: Session = Depends(get_session)):
    """从 Authorization 头解出当前用户。未登录或令牌无效返回 None。"""
    if not authorization.lower().startswith("bearer "):
        return None
    payload = decode_token(authorization[7:].strip())
    if not payload:
        return None
    user = session.get(User, payload["uid"])
    if not user or user.status != "active":
        return None

    # 令牌吊销（B-2）：签发时刻早于失效基线的一律作废。
    #
    # 少了这一步，用户改完密码、管理员禁用账号之后，**旧令牌还能继续用
    # 满 7 天**——「改密码」在用户心里等于「把别人踢下去」，
    # 而实际上什么都没发生。
    #
    # 两边都用带小数的时间戳比较。iat 若只精确到秒，「同一秒内签发又
    # 吊销」就会漏掉——改完密码立刻拿旧令牌访问仍然通得过。
    #
    # 老令牌没有 iat 字段，一律当作「早于任何基线」：加这个字段之前
    # 签发的令牌本来就该在第一次吊销时失效。
    if user.token_valid_after:
        issued = payload.get("iat")
        if issued is None or issued < user.token_valid_after.timestamp():
            return None
    return user


def require_login(user: User = Depends(current_user)):
    if not user:
        raise HTTPException(status_code=401, detail="请先登录")
    return user


def require_staff(user: User = Depends(require_login)):
    """房东或管理员才能操作。

    **这一个曾经同时承担两件事**：「能发房源、能签约」和「能管某套房」。
    加中介（J-02）时才发现这两件事必须分开——中介管得了受托的房源，
    但绝不能凭空发房源、也不能代替房东签约。
    所以现在它只保留前一个含义，后一个见 `require_house_manager`。
    """
    if user.role not in ("landlord", "admin"):
        raise HTTPException(status_code=403, detail="没有操作权限")
    return user


def require_house_manager(user: User = Depends(require_login)):
    """可能管得了某套房的角色：房东、中介、管理员。

    **这只是第一道门，管的是角色。** 具体「这个人管不管得了这一套」
    还要过 `app.permissions.require_can_manage_house`——
    否则中介能改别人家的房源。两道门缺一不可：
    只有角色门，中介能碰所有房；只有房源门，租客也能来试。
    """
    if user.role not in ("landlord", "agent", "admin"):
        raise HTTPException(status_code=403, detail="没有操作权限")
    return user
