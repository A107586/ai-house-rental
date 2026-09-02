"""身份证号的校验、掩码与哈希（对应需求 R-02、N-04）。

**设计选择：存不可逆哈希 + 掩码，而不是可逆加密。**

需求原文写的是「身份证号加密存储」。这里做得比它更严格，理由是身份证号
在本系统里只有两个用途：

1. 合同与档案上展示 —— 脱敏形式（`110***********1234`）就够了；
2. 判断是否已被别人登记过 —— 比对哈希就够了。

**两者都不需要还原原文。** 既然不需要还原，就不该保留还原的可能：
可逆加密意味着密钥一旦泄露，整库身份证号全部暴露；不可逆哈希则没有
这个风险。原文在服务端算完这两个字段后立即丢弃，从不落库。

哈希用 HMAC-SHA256 加盐（盐取自 `AUTH_SECRET`）。裸 SHA-256 挡不住彩虹表
——身份证号的空间比密码小得多，前六位是行政区划、中间八位是生日，
真正随机的只有后四位，穷举成本极低。加盐之后拿不到密钥就无从穷举。
"""

import hashlib
import hmac
import re

from app.auth import SECRET

# 十八位：六位地址码 + 八位生日 + 三位顺序码 + 一位校验位（可能是 X）
_PATTERN = re.compile(r"^\d{17}[\dXx]$")

# 校验位算法（GB 11643-1999）的两张常量表
_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_CHECK_CODES = "10X98765432"


def is_valid(number):
    """校验身份证号，含**校验位**。

    只判长度和字符是不够的：随手编一串十八位数字也能通过，
    而校验位能挡掉绝大多数手滑与瞎填。
    """
    if not number:
        return False
    number = str(number).strip().upper()
    if not _PATTERN.match(number):
        return False

    total = sum(int(number[i]) * _WEIGHTS[i] for i in range(17))
    return number[17] == _CHECK_CODES[total % 11]


def mask(number):
    """脱敏为 ``110***********1234``：留前三位与后四位。

    前三位是省级行政区划，能看出籍贯范围但定位不到人；后四位用于核对
    「是不是我这张」。中间的生日与顺序码是识别个人的关键，必须遮掉。
    """
    number = str(number or "").strip()
    if len(number) != 18:
        return ""
    return number[:3] + "*" * 11 + number[-4:]


def fingerprint(number):
    """算出用于比对的哈希。同一个号码永远得到同一个值。"""
    number = str(number or "").strip().upper()
    if not number:
        return ""
    return hmac.new(SECRET.encode("utf-8"), number.encode("utf-8"),
                    hashlib.sha256).hexdigest()


def prepare(number):
    """一次算出要落库的两个字段。

    返回 ``(是否合法, {"hash": ..., "masked": ...} 或错误说明)``。
    **调用方拿到结果后就该把原文丢掉**，不要再往下传。
    """
    number = str(number or "").strip().upper()
    if not number:
        return False, "身份证号不能为空"
    if not is_valid(number):
        return False, "身份证号格式不正确，请检查是否输错"
    return True, {"hash": fingerprint(number), "masked": mask(number)}
