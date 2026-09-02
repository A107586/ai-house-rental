"""事务型问题的取数：直接查业务后端，不经过检索。

智能体不连数据库，也不持有身份：令牌由前端带来、原样转发给后端
（非功能需求 N-06，与 AI-07 智能推荐的做法一致）。
后端按令牌决定这个人能看到哪些数据，越权判断不在智能体这一层做——
放在这里做等于把鉴权逻辑复制了一份，两份迟早会不一致。

每个查询函数返回 ``(message, data)``：
`message` 是给用户看的一句话，`data` 是结构化结果，供前端渲染卡片。
"""

from app.backend import BackendError


def _fmt_money(value):
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return str(value)
    return ("%.0f" % amount) if amount == int(amount) else ("%.2f" % amount)


def bills(backend, token):
    """账单：未缴金额与最近一期。"""
    data = backend.get("/api/bills/mine", token=token)
    rows = data.get("list", data if isinstance(data, list) else []) or []

    unpaid = [b for b in rows if b.get("status") != "paid"]
    if not rows:
        return "你名下还没有账单记录。", {"total": 0, "unpaid": []}

    if not unpaid:
        return "你的账单都结清了，没有待缴费用。", {"total": len(rows), "unpaid": []}

    total = sum(float(b.get("amount") or 0) for b in unpaid)
    first = unpaid[0]
    message = ("你还有 %d 笔待缴账单，合计 %s 元。最近一笔是%s，%s 元%s。"
               % (len(unpaid), _fmt_money(total),
                  first.get("bill_type") or "租金",
                  _fmt_money(first.get("amount")),
                  "，%s 到期" % first["due_date"] if first.get("due_date") else ""))
    return message, {"total": len(rows), "unpaid": unpaid[:5]}


def contracts(backend, token):
    """租约：当前生效的那一份与到期时间。"""
    data = backend.get("/api/contracts/mine", token=token)
    rows = data.get("list", data if isinstance(data, list) else []) or []
    if not rows:
        return "你名下还没有租约。", {"list": []}

    active = [c for c in rows if c.get("status") in ("active", "生效中")]
    current = active[0] if active else rows[0]
    message = ("你当前的租约是 %s，租期 %s 至 %s，状态：%s。"
               % (current.get("contract_no", ""),
                  current.get("start_date", ""), current.get("end_date", ""),
                  current.get("status", "")))
    return message, {"list": rows[:5], "current": current}


def repairs(backend, token):
    """报修：进行中的工单。"""
    data = backend.get("/api/repairs", token=token)
    rows = data.get("list", data if isinstance(data, list) else []) or []
    if not rows:
        return "你还没有提交过报修。", {"list": []}

    open_rows = [r for r in rows if r.get("status") not in ("done", "已完成", "closed")]
    if not open_rows:
        return "你的报修都已处理完成。", {"list": rows[:5]}

    first = open_rows[0]
    message = ("你有 %d 条报修在处理中。最近一条是「%s」，当前状态：%s。"
               % (len(open_rows),
                  (first.get("content") or "")[:20],
                  first.get("status", "待受理")))
    return message, {"list": open_rows[:5]}


def bookings(backend, token):
    """看房预约：待确认与已确认的。"""
    data = backend.get("/api/appointments/mine", token=token)
    rows = data.get("list", data if isinstance(data, list) else []) or []
    if not rows:
        return "你还没有预约过看房。", {"list": []}

    pending = [r for r in rows if r.get("status") in ("pending", "待审核")]
    if pending:
        first = pending[0]
        message = ("你有 %d 条预约还在等房东确认，最近一条是 %s。"
                   % (len(pending), first.get("viewing_time", "")))
    else:
        first = rows[0]
        message = ("你最近一条预约是 %s，状态：%s。"
                   % (first.get("viewing_time", ""), first.get("status", "")))
    return message, {"list": rows[:5]}


HANDLERS = {
    "bill": bills,
    "contract": contracts,
    "repair": repairs,
    "booking": bookings,
}

TOPIC_LABELS = {
    "bill": "账单",
    "contract": "租约",
    "repair": "报修",
    "booking": "预约",
}


def lookup(backend, token, topic):
    """按事务类型取数。返回 ``(status, message, data)``。

    `status` 取值：``ok`` / ``need_login`` / ``error``。
    后端不可用时**不抛异常**——客服问答不该因为一个子查询失败就整个 500，
    给出一句人话并引导到对应页面即可。
    """
    if not token:
        return ("need_login",
                "这个要看你自己的记录，先登录一下我才查得到。", {})

    handler = HANDLERS.get(topic)
    if handler is None:
        return "error", "暂时查不了这类记录。", {}

    try:
        message, data = handler(backend, token)
    except BackendError:
        label = TOPIC_LABELS.get(topic, "")
        return ("error",
                "%s数据暂时取不到，你可以稍后再问我，或者直接到「我的租约」里看。"
                % label, {})
    except Exception:
        return "error", "查询时出了点问题，稍后再试试。", {}

    return "ok", message, data
