"""工具：看房时段的冲突检测与可用时段推荐（AI-06 的第二步）。

刻意只跟 `datetime` 对象打交道，不关心后端把时间存成什么类型——
持久化细节由后端客户端负责。这样无论数据库用哪一版表结构，
本模块和它的单元测试都不用改。
"""

from datetime import datetime, timedelta

from app.agents.booking.tools.parse_datetime import (
    BUSINESS_END, BUSINESS_START, default_slots, within_business_hours)

# 一次看房占用的时长
VIEWING_MINUTES = 60


def _overlaps(a_start, b_start, minutes=VIEWING_MINUTES):
    """两次看房是否时间重叠。"""
    delta = abs((a_start - b_start).total_seconds()) / 60.0
    return delta < minutes


def find_conflicts(moment, taken):
    """返回与 `moment` 冲突的已有预约列表。"""
    return [t for t in taken if _overlaps(moment, t)]


def suggest_alternatives(day, taken, limit=3):
    """给出某天仍然空闲的整点时段。"""
    candidates = []
    cursor = datetime.combine(day, BUSINESS_START)
    end = datetime.combine(day, BUSINESS_END)
    while cursor <= end and len(candidates) < limit:
        if not find_conflicts(cursor, taken):
            candidates.append(cursor)
        cursor += timedelta(minutes=VIEWING_MINUTES)
    return candidates


def check_slot(moment, taken, now=None):
    """校验一个具体时刻能否预约。

    返回 ``{"ok":bool, "reason":str|None, "alternatives":[datetime]}``。
    """
    now = now or datetime.now()

    if moment <= now:
        return {"ok": False, "reason": "past",
                "alternatives": suggest_alternatives(
                    (now + timedelta(days=1)).date(), taken)}

    if not within_business_hours(moment.time()):
        return {"ok": False, "reason": "closed",
                "alternatives": suggest_alternatives(moment.date(), taken)}

    if find_conflicts(moment, taken):
        return {"ok": False, "reason": "taken",
                "alternatives": suggest_alternatives(moment.date(), taken)}

    return {"ok": True, "reason": None, "alternatives": []}


def slots_in_range(start_day, end_day, taken, limit=6):
    """在日期范围内挑出可用时段，用于「最近三天都行」这类模糊表达。"""
    result = []
    day = start_day
    while day <= end_day and len(result) < limit:
        for candidate in default_slots(day):
            if len(result) >= limit:
                break
            if not find_conflicts(candidate, taken):
                result.append(candidate)
        day += timedelta(days=1)
    return result


def format_slot(moment):
    """把时刻格式化成中文，用于回复用户。"""
    weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][moment.weekday()]
    return "%d月%d日（%s）%02d:%02d" % (
        moment.month, moment.day, weekday, moment.hour, moment.minute)
