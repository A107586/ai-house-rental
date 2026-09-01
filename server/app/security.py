"""密码哈希。

落实需求 R-01：密码必须加盐哈希存储，任何情况下不得明文落库。

优先使用 bcrypt；未安装时退回标准库的 PBKDF2-HMAC-SHA256——
它同样是加盐、可配置迭代次数的慢哈希，安全性足以满足课程要求，
且不引入任何第三方依赖，组员零配置就能跑通。
"""

import base64
import hashlib
import hmac
import os

_PBKDF2_ROUNDS = 260000
_PREFIX = "pbkdf2_sha256"

try:  # pragma: no cover - 取决于本机是否装了 bcrypt
    import bcrypt as _bcrypt
except ImportError:  # pragma: no cover
    _bcrypt = None


def hash_password(password):
    """生成密码哈希。返回值自带算法标识、盐与迭代次数。"""
    if not password:
        raise ValueError("密码不能为空")

    if _bcrypt is not None:
        return _bcrypt.hashpw(password.encode("utf-8"), _bcrypt.gensalt()).decode("ascii")

    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 salt, _PBKDF2_ROUNDS)
    return "%s$%d$%s$%s" % (
        _PREFIX, _PBKDF2_ROUNDS,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_password(password, stored):
    """校验密码。用 `compare_digest` 避免计时侧信道。"""
    if not password or not stored:
        return False

    if stored.startswith(_PREFIX + "$"):
        try:
            _, rounds, salt_b64, digest_b64 = stored.split("$")
            salt = base64.b64decode(salt_b64)
            expected = base64.b64decode(digest_b64)
        except (ValueError, TypeError):
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                     salt, int(rounds))
        return hmac.compare_digest(actual, expected)

    if _bcrypt is not None:
        try:
            return _bcrypt.checkpw(password.encode("utf-8"), stored.encode("utf-8"))
        except (ValueError, TypeError):
            return False
    return False
