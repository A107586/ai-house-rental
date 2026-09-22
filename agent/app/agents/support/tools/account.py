"""事务型问题的取数：直接查业务后端，不经过检索。

智能体不连数据库，也不持有身份：令牌由前端带来、原样转发给后端
（非功能需求 N-06，与 AI-07 智能推荐的做法一致）。
后端按令牌决定这个人能看到哪些数据，越权判断不在智能体这一层做——
放在这里做等于把鉴权逻辑复制了一份，两份迟早会不一致。

**每一个查询函数都对应一个已有的后端接口，一个都不新造。**
客服能查到的，恰好是用户自己在页面上点得开的那些，不多也不少。

角色差异同样交给后端：租客问「我的房源」会拿到 403，
这里把后端给的原因如实转述，而不是自己判断谁能看什么。

每个查询函数返回 ``(message, data)``：
`message` 是给用户看的一句话（也是大模型不可用时的最终回答），
`data` 是结构化结果，既供前端渲染，也作为大模型组织回答的唯一依据。
"""

from app.agents.support.tools import progress
from app.backend import BackendError


from app.agents.support.tools.dates import until_text
from app.agents.support.tools.money import money_text as _fmt_money


def _rows(data):
    """后端的列表接口有 ``{"list": [...]}`` 和裸列表两种形状，统一取出来。"""
    if isinstance(data, dict):
        return data.get("list") or []
    return data or []


# ---------------------------------------------------------------- 租客侧

def bills(backend, token):
    """账单：未缴金额与最近一期。"""
    rows = _rows(backend.get("/api/bills/mine", token=token))
    if not rows:
        return "你名下还没有账单记录。", {"total": 0, "unpaid": []}

    # **作废的账单不是欠款。** 退租时未到期的租金账单会被置成
    # cancelled（见 leasing.end_contract），拿 `!= "paid"` 去数，
    # 它们全算进「你还欠多少」里——退过一次租的人会被告知
    # 欠着一笔早就作废的钱，而那笔钱他既不用付、也付不了
    # （缴费接口对已结束的合同有拦截）。
    unpaid = [b for b in rows if b.get("status") == "unpaid"]
    if not unpaid:
        return "你的账单都结清了，没有待缴费用。", {"total": len(rows), "unpaid": []}

    total = sum(float(b.get("amount") or 0) for b in unpaid)
    first = unpaid[0]
    message = ("你还有 %d 笔待缴账单，合计 %s 元。最近一笔是%s，%s 元%s。"
               % (len(unpaid), _fmt_money(total),
                  bill_type_text(first),
                  _fmt_money(first.get("amount")),
                  # 逾期几天是催缴时用户最该先知道的事，`until_text`
                  # 在过期时会直接写成「已经过期 N 天」。
                  "，%s 到期" % until_text(first["due_date"])
                  if first.get("due_date") else ""))
    return message, {"total": len(rows), "unpaid": unpaid[:5]}


def contracts(backend, token):
    """租约：当前生效的那一份与到期时间。"""
    rows = _rows(backend.get("/api/contracts/mine", token=token))
    if not rows:
        return "你名下还没有租约。", {"list": []}

    active = [c for c in rows if c.get("status") in ("active", "生效中")]
    current = active[0] if active else rows[0]
    # 先说总数再说详情。「我租了几套房」问的就是数量，
    # 只报一份合同号答非所问。
    head = "你名下共 %d 份租约" % len(rows)
    if active:
        head += "，其中 %d 份在租期内" % len(active)
    ended = len(rows) - len(active)
    if ended:
        head += "、%d 份已结束" % ended
    # 结束日期带上天数：「还有几天到期」是这个接口最常被问到的一句，
    # 只给日期等于让用户自己去数日历。
    message = ("%s。当前生效的是 %s（%s），租期 %s 至 %s。"
               % (head, current.get("house_no") or current.get("contract_no", ""),
                  current.get("contract_no", ""),
                  current.get("start_date", ""),
                  until_text(current.get("end_date"))))
    return message, {"list": rows[:5], "current": current, "total": len(rows)}


# 后端存的是状态码，用户看的得是中文。
# 「当前状态：pending」这种句子出现在客服回答里，比不回答更露怯。
STATUS_TEXT = {
    "pending": "待处理", "processing": "处理中", "done": "已完成",
    "closed": "已关闭", "active": "生效中", "ended": "已结束",
    "terminated": "已终止", "confirmed": "已确认", "cancelled": "已取消",
    "rejected": "已拒绝", "paid": "已缴清", "unpaid": "待缴",
    "available": "在租中", "rented": "已出租", "offline": "已下架",
    "replied": "已回复",
}


# 账单类型也是英文枚举。库里迁移成 rent / deposit 之后，
# 缴费确认卡上显示的是「缴纳rent 2200 元」——**在一张要用户点确认付钱的
# 卡片上露出英文枚举**，是最不该省的一处翻译。
BILL_TYPE_TEXT = {
    "rent": "租金", "deposit": "押金", "utility": "水电费",
    "property": "物业费", "penalty": "违约金", "other": "其他费用",
}


def bill_type_text(bill, default="租金"):
    """一笔账单的中文类型名。字段名与取值都做过迁移，两种都认。"""
    raw = (bill or {}).get("bill_type") or (bill or {}).get("type") or ""
    if not raw:
        return default
    return BILL_TYPE_TEXT.get(raw, raw)


def status_text(value, default=""):
    """状态码翻成中文。认不出的原样返回——总比丢掉信息强。"""
    if not value:
        return default
    return STATUS_TEXT.get(value, value)


def repairs(backend, token):
    """报修：进行中的工单。"""
    rows = _rows(backend.get("/api/repairs", token=token))
    if not rows:
        return "你还没有提交过报修。", {"list": []}

    open_rows = [r for r in rows
                 if r.get("status") not in ("done", "已完成", "closed")]
    if not open_rows:
        return "你的报修都已处理完成。", {"list": rows[:5], "focus": rows[0]}

    first = open_rows[0]
    message = ("你有 %d 条报修在处理中。最近一条是「%s」，当前状态：%s。"
               % (len(open_rows), (first.get("content") or "")[:20],
                  status_text(first.get("status"), "待受理")))
    return message, {"list": open_rows[:5], "focus": first}


def bookings(backend, token):
    """看房预约：待确认与已确认的。"""
    rows = _rows(backend.get("/api/appointments/mine", token=token))
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
                   % (first.get("viewing_time", ""),
                      status_text(first.get("status"))))
    # `focus` 标出这句话说的是哪一条。轨迹要跟着它走，
    # 不然会出现文字说「等房东确认」、轨迹却画着「已取消」——
    # **两个说法摆在同一张卡片上打架，比不给轨迹更糟。**
    return message, {"list": rows[:5], "focus": first}


def favorites(backend, token):
    """我的收藏。"""
    rows = _rows(backend.get("/api/favorites", token=token))
    if not rows:
        return "你还没有收藏任何房源。", {"list": []}
    names = "、".join((h.get("title") or h.get("community") or "")[:14]
                     for h in rows[:3])
    return ("你收藏了 %d 套房源：%s%s。"
            % (len(rows), names, " 等" if len(rows) > 3 else ""),
            {"list": rows[:8]})


def history(backend, token):
    """浏览足迹。"""
    rows = _rows(backend.get("/api/history", token=token))
    if not rows:
        return "你还没有浏览记录。", {"list": []}
    first = rows[0]
    return ("你最近看过 %d 套房源，最近一套是「%s」。"
            % (len(rows), (first.get("title") or first.get("community") or "")[:20]),
            {"list": rows[:8]})


def messages(backend, token):
    """站内消息。"""
    data = backend.get("/api/messages", token=token)
    rows = _rows(data)
    unread = data.get("unread", 0) if isinstance(data, dict) else 0
    if not rows:
        return "你还没有收到站内消息。", {"list": [], "unread": 0}
    if unread:
        first = rows[0]
        return ("你有 %d 条未读消息，最新一条是「%s」。"
                % (unread, (first.get("title") or "")[:24]),
                {"list": rows[:5], "unread": unread})
    return ("你的消息都读过了，共 %d 条。" % len(rows),
            {"list": rows[:5], "unread": 0})


def pending_reviews(backend, token):
    """待评价的租约。"""
    rows = _rows(backend.get("/api/reviews/pending", token=token))
    if not rows:
        return "你没有待评价的租约。", {"list": []}
    return ("你有 %d 份租约还没评价。" % len(rows), {"list": rows[:5]})


def handoffs(backend, token):
    """我提交的转人工工单。"""
    rows = _rows(backend.get("/api/support/handoffs/mine", token=token))
    if not rows:
        return "你还没有提交过转人工。", {"list": []}
    pending = [r for r in rows if r.get("status") == "pending"]
    first = rows[0]
    if pending:
        return ("你有 %d 条转人工还在等管理员处理，最近一条问的是「%s」。"
                % (len(pending), (first.get("question") or "")[:20]),
                {"list": rows[:5]})
    return ("你最近一条转人工已回复：%s"
            % (first.get("reply") or "")[:40], {"list": rows[:5]})


def profile(backend, token):
    """我的账号信息。**不返回任何敏感字段**——身份证号后端本来就只存哈希与掩码。"""
    data = backend.get("/api/auth/me", token=token)
    if not isinstance(data, dict):
        return "账号信息暂时取不到。", {}
    # **四种角色一个都不能漏。** 漏一种的代价不是报错，是这句话直接
    # 说成「身份：agent」——库里确实有 agent 这个角色（中介，J-02）。
    role = {"tenant": "租客", "landlord": "房东",
            "agent": "中介", "admin": "管理员"}.get(
        data.get("role"), data.get("role", ""))
    return ("你的账号是 %s，身份：%s。"
            % (data.get("username", ""), role), {"profile": data})


# ---------------------------------------------------------------- 房东侧

def my_houses(backend, token):
    """我的房源（房东）。"""
    rows = _rows(backend.get("/api/landlord/houses", token=token))
    if not rows:
        return "你名下还没有发布房源。", {"list": []}

    by_status = {}
    for row in rows:
        by_status[row.get("status", "")] = by_status.get(row.get("status", ""), 0) + 1
    parts = []
    for key, label in (("available", "可租"), ("rented", "已出租"),
                       ("pending", "待审核"), ("offline", "已下架")):
        if by_status.get(key):
            parts.append("%s %d 套" % (label, by_status[key]))
    return ("你名下共 %d 套房源%s。"
            % (len(rows), "，" + "、".join(parts) if parts else ""),
            {"total": len(rows), "list": rows[:8], "by_status": by_status})


def income(backend, token):
    """经营概览（房东）：收入、出租率、待办。

    「预期收入」「这个月能收多少」这类问题问的就是它。
    数字全部取自后端统计接口，**不由大模型推算**——
    与 AI-05 经营报告「不允许出现模型编造的数据」同一条规则。
    """
    data = backend.get("/api/landlord/dashboard", token=token)
    if not isinstance(data, dict):
        return "经营数据暂时取不到。", {}

    message = ("你名下 %s 套房源，已出租 %s 套，出租率 %s%%。"
               "本月已收租金 %s 元，还有 %s 元待收。"
               % (data.get("house_total", 0), data.get("house_rented", 0),
                  data.get("occupancy_rate", 0),
                  _fmt_money(data.get("month_income", 0)),
                  _fmt_money(data.get("income_pending", 0))))
    overdue = data.get("overdue_count") or 0
    if overdue:
        message += "其中 %d 笔已逾期。" % overdue
    return message, data


def my_tenants(backend, token):
    """我的租客（房东）。"""
    rows = _rows(backend.get("/api/landlord/tenants", token=token))
    if not rows:
        return "你名下还没有租客。", {"list": []}
    overdue = [r for r in rows if r.get("overdue_count")]
    message = "你名下有 %d 位租客。" % len(rows)
    if overdue:
        message += "其中 %d 位有逾期账单。" % len(overdue)
    return message, {"total": len(rows), "list": rows[:8]}


# 事务类型 -> 取数函数。加一类事务只需在这里、`classify_ask.TOPICS`
# 与 `toolcall.TOOL_SPECS` 各加一行。
HANDLERS = {
    "bill": bills,
    "contract": contracts,
    "repair": repairs,
    "booking": bookings,
    "favorite": favorites,
    "history": history,
    "message": messages,
    "review": pending_reviews,
    "handoff": handoffs,
    "profile": profile,
    "house": my_houses,
    "income": income,
    "tenant": my_tenants,
}

TOPIC_LABELS = {
    "bill": "账单",
    "contract": "租约",
    "repair": "报修",
    "booking": "预约",
    "favorite": "收藏",
    "history": "浏览记录",
    "message": "站内消息",
    "review": "待评价",
    "handoff": "转人工工单",
    "profile": "账号信息",
    "house": "我的房源",
    "income": "经营数据",
    "tenant": "我的租客",
}

# 只有房东与管理员看得到的几类。这里不做拦截，仅用于把后端的 403
# 翻译成一句人话——**权限判断始终在后端**。
LANDLORD_ONLY = {"house", "income", "tenant"}


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

    label = TOPIC_LABELS.get(topic, "")
    try:
        message, data = handler(backend, token)
    except BackendError as exc:
        detail = str(exc)
        # 后端说「需要房东权限」时如实转述，别让租客对着一句
        # 「数据取不到」反复试——他不是网络有问题，是这类数据本来就不属于他
        if topic in LANDLORD_ONLY and ("权限" in detail or "403" in detail):
            return ("error",
                    "「%s」是房东端的数据，你当前的身份看不到。" % label, {})
        return ("error",
                "%s数据暂时取不到，你可以稍后再问我，或者直接到对应页面里看。"
                % label, {})
    except Exception:
        return "error", "查询时出了点问题，稍后再试试。", {}

    # 报修与预约再补一条办事轨迹。一句「当前状态：待处理」说的是对的，
    # 但用户看完仍然不知道「还要等什么、下一步该谁动」——
    # 京东客服把物流整条轨迹摆出来，就是为了省掉这一次追问。
    progress.attach(topic, data)
    return "ok", message, data
