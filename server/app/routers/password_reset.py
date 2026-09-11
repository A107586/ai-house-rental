"""找回密码（K-11）。

`奇思妙想.docx` 第 1 条：「忘记密码如何处理」。答案原先是「处理不了」——
`/api/auth/password` 要求先登录且必须验原密码，**密码忘了就是进不去**，
只能找管理员改。

真实平台走短信验证码，我们没有短信通道（《待完善清单》C-2 说明了原因）；
但邮件通道是现成的（`app/notifier.py` 的 `EmailNotifier`），
`users.email` 字段也一直都在。所以做成「邮箱收一次性重置链接」，
零新增依赖。

三条安全约束，每一条都有具体的攻击场景：

**一、不透露账号是否存在。** 无论用户名有没有、绑没绑邮箱，
接口一律返回同一句话。区别对待的话，这个接口就成了一个免费的
「账号是否注册」查询器——撞库的第一步正是筛出哪些账号真实存在。

**二、只存令牌哈希，不存原文。** 原文只在发出去的那封邮件里存在。
库被读到也不能拿它去改别人的密码。这和 `users.password_hash`
是同一条原则（R-01）：一次性令牌同样是能直接夺号的凭据。

**三、重置成功即吊销全部旧令牌。** 用户来重置密码，通常正是因为
怀疑账号被人拿了。不吊销的话，攻击者手上那条七天有效的令牌照样能用，
这次重置等于什么都没做（与 `/api/auth/password` 的处理一致，B-2）。
"""

import hashlib
import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import notifier, ratelimit
from app.db import get_session
from app.deps import require_login
from app.models import PasswordReset, User
from app.security import hash_password

router = APIRouter()

# 链接有效期。短一点更安全，但太短会让「去邮箱翻一下」这个动作变成
# 一场赛跑——邮件本身就可能延迟几分钟
TOKEN_TTL_MINUTES = 30

# 无论成功失败都是这一句。**不能因为「用户不存在」就换个说法**，
# 见模块开头第一条
SAME_ANSWER = "如果该账号存在且绑定了邮箱，重置链接已经发出，请查收邮件。"

# 同一 IP 十分钟内最多申请 5 次。真人忘密码不会一分钟点五遍，
# 而这个额度足够覆盖「邮件没收到，再来一次」的正常反复。
FORGOT_LIMIT = 5
FORGOT_WINDOW = 600


class ForgotRequest(BaseModel):
    username: str = Field(default="", max_length=50)


class ResetRequest(BaseModel):
    token: str = Field(default="", max_length=128)
    new_password: str = Field(default="", max_length=64)


def _hash(token):
    """令牌哈希。这里用 SHA-256 而不是 BCrypt：

    令牌是 32 字节的随机串，本身就没有「弱口令」可猜，不需要 BCrypt
    那种刻意放慢的算法；而重置接口要按哈希查库，BCrypt 每行盐不同、
    没法直接等值匹配。
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@router.post("/api/auth/forgot")
def forgot_password(payload: ForgotRequest, request: Request,
                    session: Session = Depends(get_session)):
    """申请重置链接。"""
    # 按 IP 限流。不限的话，这个接口可以被用来给任意用户的邮箱刷垃圾邮件。
    #
    # **用的是独立的计数，不是登录那套失败计数。** 登录的计数
    # 「成功即清零」，攻击者只要登录一下自己的账号就能把这里的计数抹掉。
    ip = ratelimit.client_ip(request)
    allowed, wait = ratelimit.hit("forgot", ip, limit=FORGOT_LIMIT,
                                 window=FORGOT_WINDOW)
    if not allowed:
        raise HTTPException(status_code=429,
                            detail="操作过于频繁，请 %d 秒后再试" % wait)

    username = payload.username.strip()
    user = session.scalar(select(User).where(User.username == username)) \
        if username else None

    # 用户不存在、没绑邮箱、账号被禁用——三种情况都走同一个出口。
    # **不要在这里加日志说「谁不存在」**，那等于把差异换个地方泄露
    if user and user.status == "active" and "@" in (user.email or ""):
        token = secrets.token_urlsafe(32)
        session.add(PasswordReset(
            user_id=user.id,
            token_hash=_hash(token),
            expires_at=datetime.now() + timedelta(minutes=TOKEN_TTL_MINUTES),
            request_ip=ip,
        ))
        session.commit()

        notifier.notify_external(
            user, "重置你的登录密码",
            "你申请了重置密码。请在 %d 分钟内使用下面的验证码完成重置：\n\n"
            "%s\n\n"
            "如果不是你本人操作，忽略这封邮件即可，密码不会有任何变化。"
            % (TOKEN_TTL_MINUTES, token))

    return {"ok": True, "message": SAME_ANSWER}


@router.post("/api/auth/reset")
def reset_password(payload: ResetRequest,
                   session: Session = Depends(get_session)):
    """用链接里的令牌设置新密码。"""
    if not 6 <= len(payload.new_password) <= 64:
        raise HTTPException(status_code=422, detail="密码长度需为 6–64 位")
    if any(char.isspace() for char in payload.new_password):
        raise HTTPException(status_code=422, detail="密码不能包含空格")

    row = session.scalar(select(PasswordReset).where(
        PasswordReset.token_hash == _hash(payload.token.strip())))

    # 「用过了」和「从来不存在」要分开提示：前者该告诉用户重新申请一次，
    # 笼统一句「无效」会让他反复点同一个失效链接
    if row and row.used_at is not None:
        raise HTTPException(status_code=400,
                            detail="这个重置链接已经用过了，请重新申请")
    if not row or row.expires_at <= datetime.now():
        raise HTTPException(status_code=400,
                            detail="重置链接无效或已过期，请重新申请")

    user = session.get(User, row.user_id)
    if not user or user.status != "active":
        raise HTTPException(status_code=400, detail="账号不可用")

    user.password_hash = hash_password(payload.new_password)
    # 重置密码即吊销此前签发的全部令牌。用户来重置，通常正是因为
    # 怀疑账号被人拿了；不吊销的话攻击者手上那条令牌照样能用
    user.token_valid_after = datetime.now()

    row.used_at = datetime.now()

    # 同一个人可能连点几次「忘记密码」，手上有好几个有效链接。
    # 用掉一个，其余的一并作废——否则邮箱里那几封旧邮件仍然是活的钥匙
    others = session.scalars(select(PasswordReset).where(
        PasswordReset.user_id == user.id,
        PasswordReset.used_at.is_(None))).all()
    for other in others:
        other.used_at = datetime.now()

    session.commit()
    return {"ok": True, "relogin": True,
            "message": "密码已重置，请用新密码登录。"}


@router.post("/api/admin/users/{user_id}/reset-code")
def issue_reset_code(user_id: int, request: Request,
                     user: User = Depends(require_login),
                     session: Session = Depends(get_session)):
    """管理员为某个用户签发一次性重置码，当场返回给管理员。

    ## 为什么要有这条

    邮箱通道是「尽力而为」的：平台没配 SMTP、用户压根没绑邮箱，
    `/api/auth/forgot` 就只能返回那句「如果该账号存在……」然后什么也
    不发生。**对忘了密码的人来说，这等于没有找回密码功能**——
    他做完了所有该做的动作，然后卡在原地，还不知道是哪一步出了问题。

    真实平台在这种时候走的是人工客服：核实身份，然后由客服发起重置。
    这条接口就是那个路径。

    ## 它不是「管理员改密码」

    签发的是**一次性码**，不是新密码。差别在于：

    - 管理员全程不知道、也定不了用户最后设成什么密码；
    - 码只能用一次、30 分钟过期，和邮件里那条走完全同一套校验；
    - 用掉之后该用户此前签发的全部令牌一并吊销。

    管理员本来就能改库里的任何东西，这条接口不会放大他的权限；
    它的意义是**把这件事变成一个有记录、有时效、可审计的动作**，
    而不是让他直接去改 password_hash。

    ## 必须提醒的事

    响应里那句 warning 不是客套：这串码等于一把临时钥匙，
    **只能交给已经核实过身份的账号本人**。
    """
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")

    target = session.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="用户不存在")
    if target.status != "active":
        raise HTTPException(status_code=409, detail="该账号已被禁用，请先启用")
    if target.role == "admin" and target.id != user.id:
        # 管理员之间不能互相签发。**这是防内鬼最基本的一条**——
        # 否则任何一个管理员都能悄悄接管另一个管理员的账号
        raise HTTPException(status_code=403,
                            detail="不能为其他管理员签发重置码")

    token = secrets.token_urlsafe(32)
    session.add(PasswordReset(
        user_id=target.id,
        token_hash=_hash(token),
        expires_at=datetime.now() + timedelta(minutes=TOKEN_TTL_MINUTES),
        request_ip=ratelimit.client_ip(request),
    ))

    # 绑了邮箱的照样发一份。**管理员手上那份是兜底，不是替代**——
    # 能直接送到本人邮箱，就不该经过第三个人的手
    if "@" in (target.email or ""):
        notifier.notify_external(
            target, "管理员为你签发了密码重置码",
            "客服为你签发了一次性重置码，%d 分钟内有效：\n\n%s\n\n"
            "如果不是你本人申请，请立即联系平台。"
            % (TOKEN_TTL_MINUTES, token))

    session.commit()
    return {
        "ok": True,
        "code": token,
        "expires_in_minutes": TOKEN_TTL_MINUTES,
        "emailed": "@" in (target.email or ""),
        "warning": "这串码等于一把临时钥匙，只能交给已核实身份的账号本人。"
                   "用户在登录页点「忘记密码 → 我有验证码」填入即可设置新密码。",
    }
