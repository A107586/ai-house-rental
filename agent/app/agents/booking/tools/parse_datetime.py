"""工具：把中文相对时间表达解析成具体日期与时刻（AI-06 的第一步）。

租客约看房几乎不会写完整日期，说的都是「这周六下午两点」「明天上午」
「最近三天都行」。本模块负责把这些口语表达落成可比较的 `datetime`。

**「现在」通过参数注入**，不在函数内部调 `datetime.now()`——
否则测试结果会随运行日期漂移，无法写确定性断言。
"""

import re
from datetime import date, datetime, time, timedelta

from app.cn_number import CN_NUM_CHARS, _token_to_number

# ---------------------------------------------------------------- 星期

_WEEKDAY_NAMES = {
    "一": 0, "1": 0,
    "二": 1, "2": 1,
    "三": 2, "3": 2,
    "四": 3, "4": 3,
    "五": 4, "5": 4,
    "六": 5, "6": 5,
    "日": 6, "天": 6, "7": 6,
}
_WEEKDAY = re.compile(r"(这|本|下|下个|上)?\s*(?:周|星期|礼拜)\s*([一二三四五六日天1-7])")

# ---------------------------------------------------------------- 相对日

# 顺序有讲究：长的写在前面，否则「大后天」会先被「后天」吃掉。
# 「明早」「明晚」这类合成词也要列出来——口语里说「明早九点」的
# 比说「明天早上九点」的多，只认后者等于漏掉一半说法。
_RELATIVE_DAYS = [
    ("大后天", 3),
    ("后天", 2),
    ("明天", 1),
    ("明早", 1),
    ("明晚", 1),
    ("明儿", 1),
    ("今天", 0),
    ("今晚", 0),
    ("今儿", 0),
]

# 合成词里隐含的时段：「明早」= 明天 + 早上
_DAY_PERIOD_HINT = {"明早": "早上", "明晚": "晚上", "今晚": "晚上"}

# ---------------------------------------------------------------- 绝对日期

_ABS_DATE = re.compile(r"(?:(\d{1,2})\s*月)?\s*(\d{1,2})\s*[日号]")
_SLASH_DATE = re.compile(r"(\d{1,2})\s*[/\-]\s*(\d{1,2})")

# ---------------------------------------------------------------- 时刻

_PERIODS = [
    ("凌晨", 4), ("早上", 8), ("早晨", 8), ("上午", 10),
    ("中午", 12), ("下午", 14), ("傍晚", 17), ("晚上", 19), ("晚间", 19),
]
_CLOCK = re.compile(r"([" + CN_NUM_CHARS + r"\d]{1,3})\s*[点时](\s*半|\s*[\d一二三四五]{1,2}\s*分)?")
# 「14:00」「9:30」这种写法。手机上打冒号比打「点」快，实际用得不少，
# 而只认「点」的正则完全匹配不到。
# 不能用 \b 做边界：Python 的 \b 认的是「单词字符与非单词字符的交界」，
# 而中文汉字也算单词字符，「明天14:00」里「天」和「1」之间根本没有边界，
# 整条正则就永远匹配不上。改用「前面不是数字」这种显式断言。
_COLON_CLOCK = re.compile(r"(?<!\d)([01]?\d|2[0-3])\s*[:：]\s*([0-5]\d)(?!\d)")

# ---------------------------------------------------------------- 时间范围

_RANGE_DAYS = re.compile(r"(?:最近|未来|这)\s*([" + CN_NUM_CHARS + r"\d]{1,3})\s*[天日]")
_THIS_WEEKEND = re.compile(r"(这|本|下)?\s*(?:个)?\s*周末")
_ANYTIME = re.compile(r"随时|都行|都可以|都有空|什么时候都|随便")

# 平台可预约的时段：09:00–18:00
BUSINESS_START = time(9, 0)
BUSINESS_END = time(18, 0)


def _next_weekday(base, target, modifier):
    """求相对于 `base` 的目标星期几。

    带「这/本/下/上」修饰时按**周边界**计算，不能用「下一次出现」再偏移：
    周二说「下周一」指的是下周那个周一，若先取「下一个周一」（已经是下周了）
    再加七天就会多跳一周。

    不带修饰的「周六」则按下一次出现算，这符合口语习惯。
    """
    week_monday = base - timedelta(days=base.weekday())
    if modifier in ("下", "下个"):
        return week_monday + timedelta(days=7 + target)
    if modifier == "上":
        return week_monday + timedelta(days=target - 7)
    if modifier in ("这", "本"):
        this_week = week_monday + timedelta(days=target)
        # **「这周六」在周日说，指的是下一个周六，不是昨天那个。**
        #
        # 按 ISO 周算，周日属于「这一周」的最后一天，于是「这周六」
        # 算出来是昨天——用户拿它去约看房，后端一句「不能预约过去的时间」
        # 把他挡回来，而他根本不知道自己说错了什么。
        # 这个 bug 只在周日（以及本周那天已经过去时）冒出来，
        # 平时跑测试撞不到；今天恰好是周日，才被 AI-06 那条验收用例逮住。
        if this_week < base:
            return this_week + timedelta(days=7)
        return this_week

    delta = (target - base.weekday()) % 7
    if delta == 0:
        delta = 7  # 「周六」在周六当天说，通常指下一个周六
    return base + timedelta(days=delta)


def _parse_clock(text):
    """解析「两点」「下午三点半」「14:00」，返回 `time` 或 None。"""
    # 冒号写法是无歧义的 24 小时制，先判掉，不用再考虑上下午换算
    colon = _COLON_CLOCK.search(text)
    if colon:
        return time(int(colon.group(1)), int(colon.group(2)))

    period_hour = None
    period_name = None
    for name, hour in _PERIODS:
        if name in text:
            period_hour = hour
            period_name = name
            break

    # 「明早九点」里的时段藏在合成词里，单独找一遍
    if period_name is None:
        for word, period in _DAY_PERIOD_HINT.items():
            if word in text:
                period_name = period
                period_hour = dict(_PERIODS)[period]
                break

    match = _CLOCK.search(text)
    if not match:
        # 只说了「下午」没说几点，用该时段的默认钟点
        return time(period_hour, 0) if period_hour is not None else None

    value = _token_to_number(match.group(1))
    if value is None:
        return time(period_hour, 0) if period_hour is not None else None
    hour = int(value)
    if not 0 <= hour <= 24:
        return None

    minute = 0
    tail = (match.group(2) or "").strip()
    if tail == "半":
        minute = 30
    elif tail:
        digits = _token_to_number(re.sub(r"\s*分", "", tail))
        if digits is not None and 0 <= int(digits) < 60:
            minute = int(digits)

    # 「下午两点」要换算成 14 点；「下午 14 点」则原样保留
    if period_name in ("下午", "傍晚", "晚上", "晚间") and hour < 12:
        hour += 12
    elif period_name == "中午" and hour < 12 and hour != 12:
        hour += 12
    elif period_name in ("凌晨", "早上", "早晨", "上午") and hour == 12:
        hour = 0

    if hour >= 24:
        return None
    return time(hour, minute)


def parse_datetime(text, now=None):
    """解析看房时间表达。

    返回 dict::

        {
          "date": date|None,          # 明确的单个日期
          "time": time|None,          # 明确的时刻
          "date_range": (d1, d2)|None,# 模糊范围，如「最近三天」
          "confidence": 0.0-1.0,
          "matched": [...],
        }

    `now` 用于注入「当前时间」，缺省取系统时间。
    """
    now = now or datetime.now()
    today = now.date()
    result = {"date": None, "time": None, "date_range": None,
              "confidence": 0.0, "matched": []}
    if not text or not text.strip():
        return result

    raw = text.strip()
    matched = []

    # ---------- 日期 ----------
    target_date = None

    weekday_match = _WEEKDAY.search(raw)
    if weekday_match:
        target = _WEEKDAY_NAMES.get(weekday_match.group(2))
        if target is not None:
            target_date = _next_weekday(today, target, weekday_match.group(1))
            matched.append("星期")

    if target_date is None:
        for word, offset in _RELATIVE_DAYS:
            if word in raw:
                target_date = today + timedelta(days=offset)
                matched.append("相对日")
                break

    if target_date is None:
        abs_match = _ABS_DATE.search(raw)
        slash_match = _SLASH_DATE.search(raw)
        if abs_match:
            month = int(abs_match.group(1)) if abs_match.group(1) else today.month
            day = int(abs_match.group(2))
            target_date = _safe_date(today, month, day)
            if target_date:
                matched.append("具体日期")
        elif slash_match:
            target_date = _safe_date(today, int(slash_match.group(1)),
                                     int(slash_match.group(2)))
            if target_date:
                matched.append("具体日期")

    # ---------- 模糊范围 ----------
    if target_date is None:
        range_match = _RANGE_DAYS.search(raw)
        weekend_match = _THIS_WEEKEND.search(raw)
        if range_match:
            days = _token_to_number(range_match.group(1))
            if days and 1 <= int(days) <= 30:
                result["date_range"] = (today, today + timedelta(days=int(days) - 1))
                matched.append("日期范围")
        elif weekend_match:
            saturday = _next_weekday(today, 5, weekend_match.group(1))
            result["date_range"] = (saturday, saturday + timedelta(days=1))
            matched.append("周末")
        elif _ANYTIME.search(raw):
            result["date_range"] = (today, today + timedelta(days=6))
            matched.append("随时")

    result["date"] = target_date

    # ---------- 时刻 ----------
    clock = _parse_clock(raw)
    if clock:
        result["time"] = clock
        matched.append("时刻")

        # **今天这个点已经过了，就顺延到下一周。**
        #
        # 「这周日上午十点」在周日下午说，日期算出来是今天——日期本身没过期，
        # 过期的是时刻。不管的话用户拿到一个已经过去的时间点，
        # 后端一句「不能预约过去的时间」把他挡回来，他还不知道错在哪。
        #
        # 只对**星期表达**顺延：「今天下午三点」说的时候已经四点了，
        # 那是用户自己说错，顺延成明天反而是替他做主。
        if ("星期" in matched and result.get("date") == today
                and clock <= now.time()):
            result["date"] = today + timedelta(days=7)

    result["matched"] = matched
    result["confidence"] = _confidence(result)
    return result


def _safe_date(today, month, day):
    """把「9月6日」这类表达落到最近的合理年份。

    若该日期在今年已经过去，顺延到明年——用户说「3月5号」时
    通常指的是下一个 3 月 5 号，而不是已经过去的那个。
    """
    for year in (today.year, today.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            return None
        if candidate >= today:
            return candidate
    return None


def _confidence(result):
    score = 0.0
    if result["date"]:
        score += 0.6
    elif result["date_range"]:
        score += 0.35
    if result["time"]:
        score += 0.4
    return round(min(score, 1.0), 2)


def within_business_hours(moment):
    """判断时刻是否落在可预约时段内。"""
    return BUSINESS_START <= moment <= BUSINESS_END


def default_slots(day):
    """某一天的默认可选时段，用于用户没说具体钟点时给建议。"""
    return [datetime.combine(day, time(hour, 0)) for hour in (10, 14, 16)]
