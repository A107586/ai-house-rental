"""预约看房智能体（BookingAgent）：完成 AI-06。

编排三步：解析时间 → 冲突检测 → 生成预约。

对话往往需要多轮（用户先说「这周六」，再补「下午两点」），
因此本智能体不假设一次就能拿全信息，而是明确区分四种结果状态：

* ``created``      预约已生成
* ``need_time``    日期清楚但缺时刻，附可选时段
* ``need_choice``  用户给的是模糊范围，附候选时段
* ``conflict``     时段被占或不在营业时间，附替代时段
"""

from datetime import datetime, timedelta

from app.agents.booking.tools.parse_datetime import default_slots, parse_datetime
from app.backend import BackendError
from app.llm.base import LLMUnavailable
from app.agents.booking.tools.slots import (
    check_slot, format_slot, slots_in_range, suggest_alternatives)


def _format_list(slots):
    return "、".join(format_slot(s) for s in slots)


_LLM_SYSTEM = "你把用户说的看房时间换算成具体日期和时刻，只输出 JSON，不要解释。"
_LLM_PROMPT = """今天是 {today}（星期{weekday}）。用户说：「{text}」
把用户想看房的时间换算出来，输出 {{"date": "YYYY-MM-DD", "time": "HH:MM"}}。
用户没说日期就 date 写空串，没说具体几点就 time 写空串，不要自己编一个。"""


def _llm_fill(text, parsed, now, provider):
    """规则没读出日期或时刻时，让大模型换算一次。

    规则认得「这周六下午两点」「明天上午」，换个说法（「下礼拜头一天傍晚」
    「后儿个五点多」）就读不出来，只能回一句「没听清」。大模型只补**规则留空**
    的那一项，读出来的日期要落在今天到 90 天内，时刻要是合法的 HH:MM。
    """
    if provider is None or not getattr(provider, "available", False):
        return parsed
    if parsed["date"] and parsed["time"]:
        return parsed
    try:
        data = provider.complete_json(_LLM_SYSTEM, _LLM_PROMPT.format(
            today=now.date().isoformat(), weekday="一二三四五六日"[now.weekday()],
            text=text[:200]))
    except LLMUnavailable:
        return parsed
    if not isinstance(data, dict):
        return parsed
    filled = dict(parsed)
    try:
        day = datetime.strptime(str(data.get("date") or ""), "%Y-%m-%d").date()
    except ValueError:
        day = None
    if day and not (now.date() <= day <= now.date() + timedelta(days=90)):
        day = None
    try:
        moment = datetime.strptime(str(data.get("time") or ""), "%H:%M").time()
    except ValueError:
        moment = None
    if not filled["date"] and day and not filled["date_range"]:
        filled["date"] = day
    if filled["date"] and not filled["time"] and moment:
        filled["time"] = moment
    if filled != parsed:
        filled["matched"] = list(parsed.get("matched") or []) + ["大模型换算"]
    return filled


def run(text, house_no=None, applicant=None, now=None, backend=None, taken=None,
        token=None, provider=None):
    """预约看房智能体入口。

    :param taken: 该房源已有的预约时刻列表；传入 ``None`` 时向后端查询。
    """
    now = now or datetime.now()

    if taken is None:
        taken = _load_taken(house_no, backend)
        if taken is None:
            return _fail("预约服务暂时不可用，请稍后再试。")

    parsed = _llm_fill(text, parse_datetime(text, now=now), now, provider)

    # ---- 情况一：日期时刻都清楚 ----
    if parsed["date"] and parsed["time"]:
        moment = datetime.combine(parsed["date"], parsed["time"])
        verdict = check_slot(moment, taken, now=now)
        if verdict["ok"]:
            return _create(moment, house_no, applicant, backend, token=token)

        message = {
            "past": "%s 已经过去了，请选择将来的时间。" % format_slot(moment),
            "closed": "看房时间为每天 09:00–18:00，%s 不在可预约范围内。" % format_slot(moment),
            "taken": "%s 已经被预约了。" % format_slot(moment),
        }[verdict["reason"]]
        if verdict["alternatives"]:
            message += "这些时段还空着：%s。" % _format_list(verdict["alternatives"])
        return {
            "intent": "booking", "status": "conflict", "ok": False,
            "message": message, "parsed": _dump(parsed),
            "alternatives": [s.isoformat() for s in verdict["alternatives"]],
        }

    # ---- 情况二：只有日期，缺时刻 ----
    if parsed["date"]:
        available = [s for s in default_slots(parsed["date"])
                     if check_slot(s, taken, now=now)["ok"]]
        if not available:
            available = suggest_alternatives(parsed["date"], taken)
        if not available:
            return {
                "intent": "booking", "status": "conflict", "ok": False,
                "message": "%d月%d日已经约满了，换一天看看？" % (
                    parsed["date"].month, parsed["date"].day),
                "parsed": _dump(parsed), "alternatives": [],
            }
        return {
            "intent": "booking", "status": "need_time", "ok": False,
            "message": "%d月%d日这几个时段可以看房：%s。你想约哪个？" % (
                parsed["date"].month, parsed["date"].day, _format_list(available)),
            "parsed": _dump(parsed),
            "alternatives": [s.isoformat() for s in available],
        }

    # ---- 情况三：只给了模糊范围 ----
    if parsed["date_range"]:
        start, end = parsed["date_range"]
        available = slots_in_range(start, end, taken)
        available = [s for s in available if s > now]
        if not available:
            return {
                "intent": "booking", "status": "conflict", "ok": False,
                "message": "这段时间已经约满了，换个时间段看看？",
                "parsed": _dump(parsed), "alternatives": [],
            }
        return {
            "intent": "booking", "status": "need_choice", "ok": False,
            "message": "这几个时段可以看房：%s。你想约哪个？" % _format_list(available),
            "parsed": _dump(parsed),
            "alternatives": [s.isoformat() for s in available],
        }

    # ---- 情况四：完全没解析出时间 ----
    return {
        "intent": "booking", "status": "need_time", "ok": False,
        "message": "没听清你想什么时候看房，可以说「这周六下午两点」或「明天上午」。",
        "parsed": _dump(parsed), "alternatives": [],
    }


def _create(moment, house_no, applicant, backend, token=None):
    """写入看房申请。"""
    applicant = applicant or {}
    if backend is None or not house_no:
        # 统一聊天入口没带房源编号，此时只能确认时间，不能落库
        return {
            "intent": "booking", "status": "need_house", "ok": False,
            "message": "时间没问题（%s）。请到具体房源页面点「预约看房」完成提交。"
                       % format_slot(moment),
            "appointment_time": moment.isoformat(), "alternatives": [],
        }

    try:
        backend.create_viewing(
            house_no=house_no,
            moment=moment,
            applicant=applicant,
            token=token,
        )
    except BackendError as exc:
        reason = str(exc)
        if reason.startswith(("无法连接", "测试模式", "后端返回 HTTP 5")):
            return _fail("预约提交失败，请稍后再试。")
        # 后端拒收的原因原样给用户（「你已经约过这套房……」「看房时间需在 9:00–18:00 之间」），
        # 一句「稍后再试」会让他一遍遍重试一件注定不成的事
        return _fail("预约没能提交：%s" % reason)
    except Exception:
        return _fail("预约提交失败，请稍后再试。")

    return {
        "intent": "booking", "status": "created", "ok": True,
        "message": "已为你预约 %s 看房，等待管理员确认。" % format_slot(moment),
        "appointment_time": moment.isoformat(),
        "alternatives": [],
    }


def _load_taken(house_no, backend):
    """查询该房源已有的预约时刻。后端不可用时返回 None。"""
    if backend is None:
        return []
    try:
        return backend.list_viewings(house_no)
    except Exception:
        return None


def _dump(parsed):
    """把解析结果转成可序列化的形式。"""
    return {
        "date": parsed["date"].isoformat() if parsed["date"] else None,
        "time": parsed["time"].strftime("%H:%M") if parsed["time"] else None,
        "date_range": [d.isoformat() for d in parsed["date_range"]]
                      if parsed["date_range"] else None,
        "confidence": parsed["confidence"],
        "matched": parsed["matched"],
    }


def _fail(message):
    return {"intent": "booking", "status": "error", "ok": False,
            "message": message, "alternatives": []}
