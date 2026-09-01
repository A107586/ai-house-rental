"""中文数字与金额、面积的解析工具。

自然语言找房里用户很少写阿拉伯数字：「三千以内」「两千五」「一万二」都要能解析。
本模块不依赖任何第三方库，便于脱离大模型单独做单元测试。
"""

import re

_DIGITS = {
    "零": 0, "〇": 0,
    "一": 1, "壹": 1, "幺": 1,
    "二": 2, "两": 2, "贰": 2, "俩": 2,
    "三": 3, "叁": 3,
    "四": 4, "肆": 4,
    "五": 5, "伍": 5,
    "六": 6, "陆": 6,
    "七": 7, "柒": 7,
    "八": 8, "捌": 8,
    "九": 9, "玖": 9,
}

_UNITS = {"十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000}
_BIG_UNITS = {"万": 10000, "亿": 100000000}

CN_NUM_CHARS = "".join(_DIGITS) + "".join(_UNITS) + "".join(_BIG_UNITS) + "点"


def cn_to_int(text):
    """把中文数字串转成整数，无法解析时返回 None。

    支持「三千」「两千五」「一万二」「十五」「三千五百」这类口语写法。
    其中「两千五」按口语习惯解析为 2500，而不是 2005。
    """
    if not text:
        return None
    text = text.strip()

    # 纯阿拉伯数字直接返回
    if re.fullmatch(r"\d+", text):
        return int(text)

    if any(ch not in CN_NUM_CHARS for ch in text):
        return None

    total = 0        # 已结算到「万/亿」的部分
    section = 0      # 当前小节（万以下）
    number = 0       # 当前待乘的数字
    last_unit = None  # 上一个用过的单位，用于处理「两千五」这种省略写法

    for ch in text:
        if ch in _DIGITS:
            number = _DIGITS[ch]
        elif ch in _UNITS:
            unit = _UNITS[ch]
            # 「十五」这种以十开头的写法，十前面默认是 1
            if number == 0 and unit == 10 and section == 0:
                number = 1
            section += number * unit
            number = 0
            last_unit = unit
        elif ch in _BIG_UNITS:
            unit = _BIG_UNITS[ch]
            if number == 0 and section == 0:
                return None
            section += number
            total += section * unit
            section = 0
            number = 0
            last_unit = unit
        else:
            return None

    # 处理「两千五」「一万二」这类结尾省略单位的口语写法：
    # 结尾的散数字按上一个单位的下一级计算
    if number:
        if last_unit and last_unit >= 10:
            section += number * (last_unit // 10)
        else:
            section += number

    result = total + section
    return result if result else None


_NUM_TOKEN = r"(?:\d+(?:\.\d+)?|[" + CN_NUM_CHARS + r"]+)"


def _token_to_number(token):
    """把一个数字片段（阿拉伯或中文）转成 float。"""
    if token is None:
        return None
    token = token.strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", token):
        return float(token)
    value = cn_to_int(token)
    return float(value) if value is not None else None


def parse_amount(token, unit_hint=None):
    """解析金额片段，返回元为单位的数值。

    `unit_hint` 用于处理「3.5万」「两万」这类以万为单位的表达。
    """
    value = _token_to_number(token)
    if value is None:
        return None
    if unit_hint == "万":
        value *= 10000
    elif unit_hint == "千":
        value *= 1000
    return value
