"""登录令牌的签发与校验。

用 HMAC 签名的无状态令牌，不引入 JWT 库——签名算法用标准库
`hmac` + `hashlib`，逻辑不到五十行，答辩时也讲得清楚。

令牌形如 ``base64(payload).base64(signature)``，payload 里带用户 id、
角色与过期时间。服务端不保存会话，重启也不会踢人下线。

**签名密钥必须来自环境变量**，代码里只有一个仅供本地开发的默认值，
部署前必须设置 `AUTH_SECRET`（对应非功能需求 N-05）。
"""

import base64
import hashlib
import hmac
import json
import os
import time

_DEV_SECRET = "dev-only-secret-change-me"
SECRET = os.environ.get("AUTH_SECRET") or _DEV_SECRET

# 令牌有效期，默认 7 天
TTL_SECONDS = int(os.environ.get("AUTH_TTL_SECONDS") or 7 * 24 * 3600)


def using_dev_secret():
    """是否还在用开发默认密钥。健康检查会据此给出告警。"""
    return SECRET == _DEV_SECRET


def _b64encode(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(text):
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _sign(payload_b64):
    digest = hmac.new(SECRET.encode("utf-8"),
                      payload_b64.encode("ascii"), hashlib.sha256).digest()
    return _b64encode(digest)


def create_token(user):
    """为用户签发令牌。"""
    payload = {
        "uid": user.id,
        "username": user.username,
        "role": user.role,
        "exp": int(time.time()) + TTL_SECONDS,
    }
    payload_b64 = _b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return payload_b64 + "." + _sign(payload_b64)


def decode_token(token):
    """校验并解出令牌内容。任何异常一律返回 None，不向调用方泄漏原因。"""
    if not token or "." not in token:
        return None

    payload_b64, _, signature = token.rpartition(".")
    # 先比签名再看内容，避免有人伪造 payload 探测行为差异
    if not hmac.compare_digest(signature, _sign(payload_b64)):
        return None

    try:
        payload = json.loads(_b64decode(payload_b64).decode("utf-8"))
    except (ValueError, TypeError):
        return None

    if not isinstance(payload, dict) or payload.get("exp", 0) < time.time():
        return None
    return payload
