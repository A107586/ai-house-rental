# -*- coding: utf-8 -*-
"""把日期写成人关心的样子。

起因是一次长对话测试：用户问「我的合同**还有几天**到期」，
系统答「租到 2027-09-01」。内容没错，但答非所问——
问的是天数，给的是日期，用户还得自己掰手指头算。

这类「答案正确但没回答问题」是本地小模型最容易漏掉的一种，
因为它看不出「2027-09-01」和「还有 344 天」在语义上差了一步换算。
所以不交给模型，在这里算好再给它。

只做两件事，都刻意做得很保守：

1. **只认 ISO 日期。** 后端 `end_date` 一律是 `YYYY-MM-DD`，
   解析不了就返回 None，由调用方退回原来的说法，绝不猜。
2. **天数和日期一起说。** 只说「还有 344 天」用户没法核对，
   只说日期又不回答问题。两个都给，一句话里说完。
"""

import datetime


def _parse(value):
    """`'2027-09-01'` → `date`；认不出返回 None。"""
    if isinstance(value, datetime.date):
        return value
    text = (value or "")
    if not isinstance(text, str):
        return None
    text = text.strip()[:10]
    try:
        return datetime.datetime.strptime(text, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def days_left(end_date, today=None):
    """距 `end_date` 还有几天。已过期返回负数，认不出日期返回 None。"""
    end = _parse(end_date)
    if end is None:
        return None
    return (end - (today or datetime.date.today())).days


def until_text(end_date, today=None):
    """『2027-09-01（还有 344 天）』。认不出日期时只回日期本身。

    边界上的说法单独处理过：「还有 0 天」是句病句，
    而「已经过期 -5 天」更糟。今天到期就说今天到期，
    过期就说过期了几天——用户看到这句话时最需要知道的正是这个。
    """
    end = _parse(end_date)
    if end is None:
        return str(end_date or "")
    days = days_left(end, today)
    if days > 1:
        tail = "还有 %d 天" % days
    elif days == 1:
        tail = "还有 1 天，明天"
    elif days == 0:
        tail = "就是今天"
    else:
        tail = "已经过期 %d 天" % (-days)
    return "%s（%s）" % (end.isoformat(), tail)
