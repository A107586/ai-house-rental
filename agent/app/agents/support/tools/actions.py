"""可代办的操作——客服从「能查」走到「能做」。

参考通义千问 App、Siri 这类助手的交互：用户说一句「帮我把这个月房租交了」，
助手**不是直接执行**，而是先把要做的事摊开给用户看——交哪一笔、多少钱、
用什么方式——用户点确认才真正落库。

## 两阶段，一步都不能省

    ① 提议 propose()  →  返回一张确认卡片，**不产生任何副作用**
    ② 执行 execute()  →  只有拿到用户确认后才调后端写接口

`agent.run()` **永远只走到第一步**。第二步是独立的接口 `/agent/support/confirm`，
必须由前端在用户点了确认之后才调用。这条边界是硬的：
把执行塞进对话里，等于让一句话就能划走用户的钱。

## 为什么可以把待确认动作交给前端带回来

确认卡片会原样回到前端、再由前端回传。看起来像是可以被伪造，但**不构成
提权**：所有写操作都用用户自己的令牌调后端受控接口，用户本来就能在页面上
点同样的按钮。伪造一个 payload 能做的事，他打开网页也能做。
真正的权限判断在后端，与这里无关。

所以这里不做会话态、不存待确认队列——无状态实现少一整类
「确认的是上一条还是上上条」的竞态问题。

## 多轮槽位填充：助手问了问题，就得接得住答案

实测下来「不好用」的根源不是识别不准，是**每一轮都是孤立的**：

    用户：帮我约看房
    助手：想看哪一套？
    用户：周六下午两点        ← 上一轮的问题整个丢了，跑去查 FAQ

参考拼多多、美团那类客服的做法，缺三样东西：

1. **待办状态**（`pending`）：问了什么、已经填了哪些槽、还缺哪个，
   一路带着走。用户补一句就能接上，不必把整句话重说一遍。
2. **选项可点**（`options`）：「你有 6 条预约，要取消哪一条」后面跟一串
   纯文本，用户只能猜格式。改成结构化选项，前端渲染成按钮，
   点一下等于把值填进槽位。
3. **口头确认**：卡片出来以后用户说「好的」，那就是确认。
   只认按钮不认人话，是把交互做回了表单。

待办状态**不存在服务端**，随响应发给前端、下一轮再带回来——
与确认卡片同一个理由：无状态就没有「接的是上一条还是上上条」的竞态。

## 风险分级决定措辞，不决定要不要确认

    low     收藏、标记已读——做错了随手能撤
    medium  预约、报修、取消预约——有影响但可挽回
    high    缴费、续租、退租——涉及钱或不可逆

**三档都要确认**，分级只影响卡片上的措辞与提示强度。
「低风险就直接执行」听着效率高，但用户对助手的信任是一次性的：
只要有一次「我没让你做你就做了」，之后每一句都得提防。
"""

from app.agents.booking.tools.parse_datetime import parse_datetime
from app.backend import BackendError

RISK_LOW = "low"
RISK_MEDIUM = "medium"
RISK_HIGH = "high"

# 提议阶段的返回状态
CONFIRM = "confirm"        # 已生成确认卡片，等用户点确认
NEED_SLOT = "need_slot"    # 还缺关键信息，得先问用户
NEED_LOGIN = "need_login"
NOT_APPLICABLE = "n/a"     # 这句话不是在要求做事，交回给别的处理路径
FAILED = "failed"


def _rows(data):
    if isinstance(data, dict):
        return data.get("list") or []
    return data or []


def _money(value):
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return str(value)
    return ("%.0f" % amount) if amount == int(amount) else ("%.2f" % amount)


# ---------------------------------------------------------------- 意图识别

# 动作关键词。要求「动词 + 对象」都出现，**不能只靠对象词**：
# 「我的账单」是查询，「帮我交账单」才是操作。
# 少了动词这一层，用户每问一次余额都会被弹一张缴费确认卡。
_DO_VERBS = ["帮我", "给我", "替我", "去", "把", "请"]

ACTION_KEYWORDS = [
    ("pay_bill", ["交房租", "交租", "缴费", "把房租交", "付房租", "交账单",
                  "把账单交", "缴租金", "交一下房租"]),
    ("cancel_viewing", ["取消预约", "取消看房", "别去看了", "不看了",
                        "把预约取消"]),
    ("book_viewing", ["约看房", "预约看房", "帮我约", "安排看房", "想去看"]),
    ("submit_repair", ["报修", "报个修", "提交报修", "报一下修", "叫维修",
                       "叫师傅"]),
    ("renew_contract", ["续租", "续签", "接着租", "再租"]),
    ("end_contract", ["退租", "解约", "不租了", "退房"]),
    ("unfavorite_house", ["取消收藏", "不收藏了"]),
    ("favorite_house", ["收藏这套", "收藏一下", "加收藏", "收藏它"]),
    ("read_all_messages", ["消息全部已读", "全部已读", "标记已读",
                           "消息都已读"]),
    ("clear_history", ["清空浏览", "清除足迹", "清空足迹", "删掉浏览记录"]),
    ("submit_review", ["评价一下", "给个好评", "打分", "评个", "写评价",
                       "提交评价"]),
    ("handle_appointment", ["同意预约", "确认预约", "通过预约", "拒绝预约",
                            "驳回预约"]),
    ("update_repair", ["工单完成", "维修完成", "修好了", "关闭工单",
                       "处理完了"]),
    ("toggle_house", ["上架", "下架", "把房源上", "把房源下"]),
]

# 问句词。带这些的句子是在**问**而不是在**要求做**，
# 「怎么退租」是咨询流程，「帮我退租」才是要执行。
#
# 这张表宁可写长也不能写短。漏掉一个词的代价是不对称的：
# 漏了，用户咨询一次就被弹一张确认卡，重则手滑点确认；
# 多了，无非是把一句指令当成了提问，再说一遍就是。
#
# 后两组是被测试逼出来的——「我的报修到哪一步了」原先命中「报修」
# 触发了提交报修，「续租需要什么手续」原先命中「续租」触发了续租。
_ASKING = [
    # 疑问词
    "怎么", "如何", "能不能", "可以吗", "多久", "什么时候", "要不要",
    "是不是", "有没有", "为什么", "多长时间", "几天", "哪些",
    # 咨询流程
    "流程", "规定", "一般", "需要什么", "手续", "条件", "材料", "注意什么",
    # 问进度（这是查询，不是指令）
    "到哪一步", "处理了吗", "处理好了吗", "回复了吗", "进度", "怎么样了",
    "有结果", "通过了吗", "审核过了吗",
]

# **软否决词**：只在句子里**没有祈使语气**时才一票否决。
#
# 「吗」和「什么」是最强的疑问信号，但不能无条件否决——
# 「能帮我交下房租吗」是客气的请求，不是提问，否决了就等于逼用户说话生硬。
# 判断标准是有没有「帮我」这类祈使词：
#
#   退租要打扫干净吗    没有祈使词 → 是提问，不执行
#   能帮我交下房租吗    有「帮我」 → 是请求，可以执行
#
# 这一层同样是被验收集逼出来的：「什么情况算紧急报修」曾触发提交报修、
# 「退租要打扫干净吗」曾触发退租、「可以用公积金付房租吗」曾触发缴费。
# 三条都是知识型问题，却因为句子里有业务动词而被当成了指令。
# 「哪」一个字覆盖哪种／哪个／哪些／哪儿，不必逐个列举
_SOFT_ASKING = ["吗", "什么", "哪", "呢", "算不算", "属于"]


# 「动词 × 对象」共现规则。固定短语覆盖不了中文的语序自由：
# 「帮我把**这个月**房租交了」里，动词和宾语之间插了三个字，
# 任何写死的短语都对不上。实测就是被这一句打回来的。
#
# 共现只要求两类词都出现在同一句里，不管顺序、不管中间隔多远，
# 这才是中文口语的常态。
ACTION_PAIRS = [
    ("pay_bill", ["交", "缴", "付", "支付", "结清"],
     ["房租", "租金", "账单", "费用", "这个月"]),
    ("cancel_viewing", ["取消", "退掉", "撤销"], ["预约", "看房"]),
    ("book_viewing", ["约", "预约", "安排"], ["看房", "看一下", "去看"]),
    ("submit_repair", ["报", "叫", "提交"], ["修", "维修", "师傅"]),
    ("renew_contract", ["续", "接着", "再租"], ["租", "约", "合同"]),
    ("end_contract", ["退", "解", "不租"], ["租", "约", "房"]),
    ("toggle_house", ["上", "下"], ["架"]),
]


def detect(text):
    """判断这句话是不是在要求代办一件事，返回动作名或空串。

    三道过滤：

    1. **问句词一票否决**。「怎么退租」问的是流程，不是让你现在就退。
       没有这一层，用户咨询一次退租政策就会收到一张退租确认卡——
       吓人，而且会让人再也不敢问。
    2. **固定短语**：说法明确的直接命中，比如「取消预约」「全部已读」。
    3. **动词 × 对象共现**：兜住语序被打散的口语说法。
       这一层要求句子里带祈使语气（「帮我」「把」之类），
       否则「房租什么时候交」这种陈述也会被当成要交钱。
    """
    raw = (text or "").strip()
    if not raw:
        return ""
    if any(word in raw for word in _ASKING):
        return ""

    imperative = any(verb in raw for verb in _DO_VERBS)
    if not imperative and any(word in raw for word in _SOFT_ASKING):
        return ""

    for name, keywords in ACTION_KEYWORDS:
        if any(kw in raw for kw in keywords):
            return name

    # 共现规则需要祈使语气托底。少了这个约束，「账单还有 2 笔没交」
    # 这种陈述句也会被判成「要交钱」——那是查询，不是操作。
    if not any(verb in raw for verb in _DO_VERBS):
        return ""

    for name, verbs, objects in ACTION_PAIRS:
        if any(v in raw for v in verbs) and any(o in raw for o in objects):
            return name
    return ""



# ---------------------------------------------------------------- 多轮槽位

# 肯定与否定。用户对着确认卡片说「好的」，那就是确认——
# 只认按钮不认人话，等于把对话做回了表单。
AFFIRM = ["好", "好的", "行", "可以", "确认", "对", "是的", "嗯", "没问题",
          "就这个", "就它", "同意", "ok", "OK", "yes", "执行", "去吧", "麻烦了"]
DENY = ["不", "不用", "算了", "取消", "先不", "再想想", "不要", "no", "等等"]


def is_affirm(text):
    """用户是不是在肯定上一张确认卡片。

    要求整句话短——「好的」是确认，「好的，那退租要打扫吗」是新问题。
    长句里出现「好」大概率只是语气词，不能当确认用。
    """
    raw = (text or "").strip().strip("，。！,.!~ ")
    if not raw or len(raw) > 8:
        return False
    return any(raw == word or raw.startswith(word) for word in AFFIRM)


def is_deny(text):
    raw = (text or "").strip().strip("，。！,.!~ ")
    if not raw or len(raw) > 8:
        return False
    return any(raw == word or raw.startswith(word) for word in DENY)


# 序数词 -> 下标。用户看到一串选项，最自然的回答就是「第一个」。
_ORDINALS = {
    "第一": 0, "第1": 0, "第一个": 0, "第一条": 0, "1": 0, "一": 0,
    "第二": 1, "第2": 1, "第二个": 1, "第二条": 1, "2": 1, "二": 1,
    "第三": 2, "第3": 2, "第三个": 2, "第三条": 2, "3": 2, "三": 2,
    "第四": 3, "第4": 3, "第四个": 3, "4": 3, "四": 3,
    "第五": 4, "第5": 4, "第五个": 4, "5": 4, "五": 4,
}


def pick_option(text, options):
    """把用户这句话对应到某个选项，返回选项的 value 或空串。

    三种命中方式，按可靠性排序：

    1. 直接说出了值（房源编号、合同号）——最可靠
    2. 说了标签里的一段（「桂林街那套」）
    3. 说了序数（「第一个」）——最省事，也最容易错位，所以放最后
    """
    raw = (text or "").strip()
    if not raw or not options:
        return ""

    for option in options:
        if option["value"] and option["value"] in raw:
            return option["value"]

    for option in options:
        label = option.get("label") or ""
        # 标签里挑一段有区分度的来比对，整条标签太长不可能被原样说出来
        for piece in label.replace("　", " ").split():
            if len(piece) >= 3 and piece in raw:
                return option["value"]

    compact = raw.strip("，。 ")
    for word, index in _ORDINALS.items():
        if compact == word or compact.startswith(word):
            if index < len(options):
                return options[index]["value"]
    return ""


def _option(value, label):
    return {"value": value, "label": label}


def _ask(slot, question, options=None):
    """一次追问：缺哪个槽、问什么、有哪些可点的选项。"""
    return {"slot": slot, "question": question, "options": options or []}


# ---------------------------------------------------------------- 提议

def _card(name, label, summary, risk, params, detail=None, warning=""):
    """一张确认卡片。

    `summary` 是给用户看的一句话，**必须把关键事实写全**——
    交哪一笔、多少钱、哪一天。用户点确认时看到的就这一句，
    写得含糊等于骗签字。
    """
    return {
        "name": name,
        "label": label,
        "summary": summary,
        "risk": risk,
        "params": params,
        "detail": detail or {},
        "warning": warning,
    }


def _propose_pay_bill(text, token, backend, context):
    rows = _rows(backend.get("/api/bills/mine", token=token))
    unpaid = [b for b in rows if b.get("status") != "paid"]
    if not unpaid:
        return NOT_APPLICABLE, "你的账单都结清了，没有需要缴的。", None, None

    # 多笔待缴时选最早到期的那一笔，并在卡片上说明还有几笔。
    # 不自作主张一次全交——那是一笔大得多的钱，必须由用户逐笔决定。
    unpaid.sort(key=lambda b: b.get("due_date") or "")
    target = unpaid[0]
    amount = _money(target.get("amount"))
    kind = target.get("bill_type") or "租金"
    due = target.get("due_date") or ""

    summary = "缴纳%s %s 元" % (kind, amount)
    if due:
        summary += "（%s 到期）" % due
    warning = ""
    if len(unpaid) > 1:
        warning = "你还有另外 %d 笔待缴账单，这次只交这一笔。" % (len(unpaid) - 1)

    return CONFIRM, "", _card(
        "pay_bill", "缴费", summary, RISK_HIGH,
        {"bill_no": target.get("bill_no"), "method": "alipay"},
        detail={"amount": target.get("amount"), "bill_type": kind,
                "due_date": due, "unpaid_total": len(unpaid)},
        warning=warning)


def _propose_cancel_viewing(text, token, backend, context):
    rows = _rows(backend.get("/api/appointments/mine", token=token))
    pending = [r for r in rows if r.get("status") in ("pending", "confirmed",
                                                      "待审核", "已确认")]
    if not pending:
        return NOT_APPLICABLE, "你没有可以取消的预约。", None, None

    options = [_option(str(r.get("id")),
                       "%s %s" % (r.get("viewing_time", ""), r.get("house_no", "")))
               for r in pending]
    chosen = context.get("appointment_id") or ""
    if not chosen and len(pending) > 1:
        return (NEED_SLOT, "你有 %d 条预约，要取消哪一条？" % len(pending), None,
                _ask("appointment_id", "要取消哪一条预约？", options))

    target = pending[0]
    if chosen:
        for row in pending:
            if str(row.get("id")) == str(chosen):
                target = row
                break

    return CONFIRM, "", _card(
        "cancel_viewing", "取消预约",
        "取消 %s 对 %s 的看房预约"
        % (target.get("viewing_time", ""), target.get("house_no", "")),
        RISK_MEDIUM,
        {"appointment_id": target.get("id"), "status": "cancelled"},
        detail=target), None


def _propose_book_viewing(text, token, backend, context):
    house_no = context.get("house_no") or ""
    if not house_no:
        # 没有指定房源时，从收藏与浏览足迹里挑几套给用户点。
        #
        # 原先直接回一句「在房源详情页里跟我说」，等于把用户赶出对话框——
        # 他本来就是不想翻页面才来问的。收藏和最近看过的，
        # 多半就是他想约的那几套。
        options = []
        for path in ("/api/favorites", "/api/history"):
            for row in _rows(backend.get(path, token=token)):
                no = row.get("house_no")
                if no and not any(o["value"] == no for o in options):
                    options.append(_option(
                        no, "%s %s" % (no, (row.get("title") or "")[:18])))
            if len(options) >= 5:
                break
        question = ("想看哪一套？" if options
                    else "想看哪一套？先去房源列表挑一套，或者直接告诉我房源编号。")
        return NEED_SLOT, question, None, _ask("house_no", question, options[:5])

    # `parse_datetime` 返回的是 dict 而不是 datetime——它要同时表达
    # 「明确的某一天某一刻」和「最近三天都行」这类模糊范围，
    # 一个 datetime 装不下。这里只接受明确到时刻的表达。
    parsed = parse_datetime(context.get("viewing_time") or text) or {}
    day, clock = parsed.get("date"), parsed.get("time")
    if not day:
        return (NEED_SLOT, "你想什么时候去看？比如「这周六下午两点」。", None,
                _ask("viewing_time", "你想什么时候去看？", [
                    _option("明天下午三点", "明天下午三点"),
                    _option("这周六上午十点", "本周六上午十点"),
                    _option("这周六下午两点", "本周六下午两点"),
                    _option("这周日下午三点", "本周日下午三点")]))
    if not clock:
        # 只说了哪天没说几点。平台可约时段是 9:00–18:00，
        # 与其替用户猜一个，不如给几个常用时段让他点。
        stamp = day.strftime("%Y-%m-%d")
        return (NEED_SLOT,
                "%s 哪个时间点方便？平台可约时段是 9:00 到 18:00。"
                % day.strftime("%m 月 %d 日"), None,
                _ask("viewing_time", "哪个时间点方便？", [
                    _option("%s 10:00" % stamp, "上午 10:00"),
                    _option("%s 14:00" % stamp, "下午 14:00"),
                    _option("%s 16:00" % stamp, "下午 16:00")]))

    when = "%s %s" % (day.strftime("%Y-%m-%d"), clock.strftime("%H:%M"))
    return CONFIRM, "", _card(
        "book_viewing", "预约看房",
        "预约 %s 看房源 %s" % (when, house_no), RISK_MEDIUM,
        {"house_no": house_no, "viewing_time": when},
        detail={"house_no": house_no, "viewing_time": when},
        warning="提交后要等房东确认，结果会通过站内消息通知你。"), None


def _propose_submit_repair(text, token, backend, context):
    house_no = (context or {}).get("house_no") or ""
    if not house_no:
        # 报修的对象一般是自己正在租的房子，从生效租约里取
        rows = _rows(backend.get("/api/contracts/mine", token=token))
        active = [c for c in rows if c.get("status") in ("active", "生效中")]
        if not active:
            return (NOT_APPLICABLE,
                    "你名下没有生效中的租约，报修要先有在租的房子。", None)
        house_no = active[0].get("house_no") or ""

    # 把祈使语摘掉，只留故障描述本身。
    # 不摘的话工单内容会变成「厨房水管漏水了帮我报修」——多出来的三个字
    # 会跟着进 AI-04 的故障分类，也会显示在房东的工单列表里。
    content = (text or "").strip()
    for word in ("帮我报修", "帮我报个修", "帮我叫维修", "帮我叫师傅",
                 "提交报修", "报一下修", "帮我", "给我", "请", "报修",
                 "报个修", "叫维修", "叫师傅"):
        content = content.replace(word, "")
    content = content.strip("，。,. ")
    if len(content) < 4:
        return NEED_SLOT, "哪里出问题了？描述一下，比如「厨房水管漏水」。", None, None

    return CONFIRM, "", _card(
        "submit_repair", "提交报修",
        "为 %s 提交报修：%s" % (house_no, content[:40]), RISK_MEDIUM,
        {"house_no": house_no, "content": content},
        detail={"house_no": house_no, "content": content},
        warning="提交后系统会自动判断故障类型与紧急程度并派单。")


def _propose_renew_contract(text, token, backend, context):
    rows = _rows(backend.get("/api/contracts/mine", token=token))
    active = [c for c in rows if c.get("status") in ("active", "生效中")]
    if not active:
        return NOT_APPLICABLE, "你名下没有生效中的租约。", None, None

    target = active[0]
    months = 12
    for candidate in (24, 18, 12, 6, 3, 1):
        if "%d个月" % candidate in text or "%d 个月" % candidate in text:
            months = candidate
            break
    if "一年" in text:
        months = 12
    elif "半年" in text:
        months = 6

    return CONFIRM, "", _card(
        "renew_contract", "续租",
        "把租约 %s 续 %d 个月" % (target.get("contract_no", ""), months),
        RISK_HIGH,
        {"contract_no": target.get("contract_no"), "months": months},
        detail={"contract_no": target.get("contract_no"), "months": months,
                "end_date": target.get("end_date")},
        warning="续租会生成新的合同与账单，租金以房东确认为准。")


def _propose_end_contract(text, token, backend, context):
    rows = _rows(backend.get("/api/contracts/mine", token=token))
    active = [c for c in rows if c.get("status") in ("active", "生效中")]
    if not active:
        return NOT_APPLICABLE, "你名下没有生效中的租约。", None, None

    target = active[0]
    return CONFIRM, "", _card(
        "end_contract", "退租",
        "发起租约 %s 的退租" % target.get("contract_no", ""), RISK_HIGH,
        {"contract_no": target.get("contract_no"), "reason": "租客通过智能客服发起"},
        detail=target,
        warning="退租不可撤销。提前解约需提前 30 日通知并支付一个月租金的"
                "违约金，未到期账单会作废，押金在结清后 7 个工作日内退还。")


def _propose_favorite(text, token, backend, context):
    house_no = (context or {}).get("house_no") or ""
    if not house_no:
        return NEED_SLOT, "要收藏哪一套？在房源详情页里跟我说。", None, None
    return CONFIRM, "", _card(
        "favorite_house", "收藏房源",
        "把房源 %s 加入收藏" % house_no, RISK_LOW, {"house_no": house_no})


def _propose_unfavorite(text, token, backend, context):
    house_no = (context or {}).get("house_no") or ""
    if not house_no:
        return NEED_SLOT, "要取消收藏哪一套？", None, None
    return CONFIRM, "", _card(
        "unfavorite_house", "取消收藏",
        "把房源 %s 移出收藏" % house_no, RISK_LOW, {"house_no": house_no})


def _propose_read_all(text, token, backend, context):
    data = backend.get("/api/messages", token=token)
    unread = data.get("unread", 0) if isinstance(data, dict) else 0
    if not unread:
        return NOT_APPLICABLE, "你没有未读消息。", None, None
    return CONFIRM, "", _card(
        "read_all_messages", "消息全部已读",
        "把 %d 条未读消息标记为已读" % unread, RISK_LOW, {})


def _propose_clear_history(text, token, backend, context):
    return CONFIRM, "", _card(
        "clear_history", "清空浏览足迹",
        "清空你的全部浏览记录", RISK_MEDIUM, {},
        warning="清空后无法恢复，智能推荐也会因为缺少足迹而变得不准。")


def _propose_toggle_house(text, token, backend, context):
    house_no = (context or {}).get("house_no") or ""
    if not house_no:
        rows = _rows(backend.get("/api/landlord/houses", token=token))
        if not rows:
            return NOT_APPLICABLE, "你名下还没有房源。", None, None
        if len(rows) > 1:
            return (NEED_SLOT,
                    "你名下有 %d 套房源，要操作哪一套？告诉我房源编号。"
                    % len(rows), None)
        house_no = rows[0].get("house_no")

    offline = "下架" in text
    status = "offline" if offline else "available"
    return CONFIRM, "", _card(
        "toggle_house", "房源上下架",
        "把房源 %s %s" % (house_no, "下架" if offline else "上架"),
        RISK_MEDIUM, {"house_no": house_no, "status": status},
        warning="下架后房源不再对外展示，已有的预约不受影响。"
                if offline else "")


def _propose_submit_review(text, token, backend, context):
    rows = _rows(backend.get("/api/reviews/pending", token=token))
    if not rows:
        return NOT_APPLICABLE, "你没有待评价的租约。", None, None
    if len(rows) > 1:
        options = "；".join(r.get("contract_no", "") for r in rows[:5])
        return (NEED_SLOT,
                "你有 %d 份租约待评价，要评哪一份？%s" % (len(rows), options), None)

    target = rows[0]
    rating = 5
    for score in (5, 4, 3, 2, 1):
        if "%d星" % score in text or "%d 星" % score in text or "%d分" % score in text:
            rating = score
            break
    if "好评" in text:
        rating = 5
    elif "差评" in text:
        rating = 1

    return CONFIRM, "", _card(
        "submit_review", "提交评价",
        "给租约 %s 打 %d 星" % (target.get("contract_no", ""), rating),
        RISK_MEDIUM,
        {"contract_no": target.get("contract_no"), "rating": rating,
         "content": text[:200]},
        detail={"contract_no": target.get("contract_no"), "rating": rating},
        warning="评价提交后不能修改，房东会看到。")


def _propose_handle_appointment(text, token, backend, context):
    """房东确认或拒绝看房预约。"""
    rows = _rows(backend.get("/api/appointments", token=token))
    pending = [r for r in rows if r.get("status") in ("pending", "待审核")]
    if not pending:
        return NOT_APPLICABLE, "没有待处理的看房预约。", None, None

    reject = "拒绝" in text or "驳回" in text
    if len(pending) > 1:
        options = "；".join(
            "%s %s" % (r.get("viewing_time", ""), r.get("house_no", ""))
            for r in pending[:5])
        return (NEED_SLOT,
                "有 %d 条待处理预约，要处理哪一条？%s" % (len(pending), options),
                None)

    target = pending[0]
    status = "rejected" if reject else "confirmed"
    return CONFIRM, "", _card(
        "handle_appointment", "处理看房预约",
        "%s %s 对 %s 的看房预约"
        % ("拒绝" if reject else "确认", target.get("viewing_time", ""),
           target.get("house_no", "")),
        RISK_MEDIUM,
        {"appointment_id": target.get("id"), "status": status},
        detail=target,
        warning="结果会通过站内消息通知租客。")


def _propose_update_repair(text, token, backend, context):
    """房东把工单标记为已完成。"""
    rows = _rows(backend.get("/api/repairs", token=token))
    open_rows = [r for r in rows
                 if r.get("status") not in ("done", "已完成", "closed")]
    if not open_rows:
        return NOT_APPLICABLE, "没有处理中的工单。", None, None
    if len(open_rows) > 1:
        options = "；".join(
            "#%s %s" % (r.get("id"), (r.get("content") or "")[:12])
            for r in open_rows[:5])
        return (NEED_SLOT,
                "有 %d 条工单在处理中，要关闭哪一条？%s"
                % (len(open_rows), options), None)

    target = open_rows[0]
    return CONFIRM, "", _card(
        "update_repair", "关闭工单",
        "把工单「%s」标记为已完成" % (target.get("content") or "")[:24],
        RISK_MEDIUM,
        {"repair_id": target.get("id"), "status": "done"},
        detail=target)


PROPOSERS = {
    "pay_bill": _propose_pay_bill,
    "cancel_viewing": _propose_cancel_viewing,
    "book_viewing": _propose_book_viewing,
    "submit_repair": _propose_submit_repair,
    "renew_contract": _propose_renew_contract,
    "end_contract": _propose_end_contract,
    "favorite_house": _propose_favorite,
    "unfavorite_house": _propose_unfavorite,
    "read_all_messages": _propose_read_all,
    "clear_history": _propose_clear_history,
    "submit_review": _propose_submit_review,
    "handle_appointment": _propose_handle_appointment,
    "update_repair": _propose_update_repair,
    "toggle_house": _propose_toggle_house,
}


def propose(name, text, token, backend, context=None):
    """生成确认卡片。**保证不产生任何副作用**——只读后端，不写。

    返回 ``(status, message, card, ask)``：

    - `card` 是确认卡片，只有 `status == CONFIRM` 时非空
    - `ask` 是一次追问，只有 `status == NEED_SLOT` 时非空，
      形如 ``{"slot", "question", "options"}``。`options` 让前端渲染成
      可点按钮——用户点一下就等于把值填进槽位，不用猜该怎么打字。

    `context` 里既有页面上下文（当前打开的房源），也有前几轮已经填好的槽位，
    两者合流。对 proposer 来说「这个值哪来的」不重要，有就行。

    各个 proposer 返回三元组或四元组都可以，这里统一补齐——
    多数操作不需要追问，让它们为此多写一个 `None` 是噪声。
    """
    if not token:
        return NEED_LOGIN, "这个操作要用你的账号来做，先登录一下。", None, None
    proposer = PROPOSERS.get(name)
    if proposer is None:
        return NOT_APPLICABLE, "", None, None
    try:
        result = proposer(text, token, backend, context or {})
    except BackendError:
        return FAILED, "查你的数据时没连上后端，稍后再试。", None, None
    except Exception:
        return FAILED, "准备这个操作时出了点问题，稍后再试。", None, None

    return tuple(result) + (None,) if len(result) == 3 else tuple(result)



# ---------------------------------------------------------------- 执行

def _exec_pay_bill(params, token, backend):
    data = backend.post("/api/bills/%s/pay" % params["bill_no"],
                        {"method": params.get("method", "alipay")}, token=token)
    return "缴费成功。", data


def _exec_cancel_viewing(params, token, backend):
    data = backend._request(
        "PATCH", "/api/appointments/%s" % params["appointment_id"],
        payload={"status": "cancelled"}, token=token)
    return "预约已取消，该时段重新释放。", data


def _exec_book_viewing(params, token, backend):
    data = backend.post("/api/appointments", {
        "house_no": params["house_no"],
        "viewing_time": params["viewing_time"],
    }, token=token)
    return "预约已提交，等房东确认，结果会通过站内消息通知你。", data


def _exec_submit_repair(params, token, backend):
    data = backend.post("/api/repairs", {
        "house_no": params["house_no"],
        "content": params["content"],
    }, token=token)
    return "报修已提交，可以在「我的租约 → 报修」里看进度。", data


def _exec_renew_contract(params, token, backend):
    data = backend.post("/api/contracts/%s/renew" % params["contract_no"],
                        {"months": params["months"]}, token=token)
    return "续租已发起。", data


def _exec_end_contract(params, token, backend):
    data = backend.post("/api/contracts/%s/end" % params["contract_no"],
                        {"reason": params.get("reason", "")}, token=token)
    return "退租已发起，接下来请结清账单并与房东验房。", data


def _exec_favorite(params, token, backend):
    data = backend.post("/api/favorites/%s" % params["house_no"], {},
                        token=token)
    return "已收藏。", data


def _exec_unfavorite(params, token, backend):
    data = backend._request("DELETE", "/api/favorites/%s" % params["house_no"],
                            token=token)
    return "已取消收藏。", data


def _exec_read_all(params, token, backend):
    data = backend.post("/api/messages/read-all", {}, token=token)
    return "消息已全部标记为已读。", data


def _exec_clear_history(params, token, backend):
    data = backend._request("DELETE", "/api/history", token=token)
    return "浏览足迹已清空。", data


def _exec_toggle_house(params, token, backend):
    data = backend._request(
        "PATCH", "/api/houses/%s/status" % params["house_no"],
        params={"status": params["status"]}, token=token)
    return ("房源已%s。" % ("下架" if params["status"] == "offline" else "上架"),
            data)


def _exec_submit_review(params, token, backend):
    data = backend.post("/api/reviews", {
        "contract_no": params["contract_no"],
        "rating": params["rating"],
        "content": params.get("content", ""),
    }, token=token)
    return "评价已提交，谢谢反馈。", data


def _exec_handle_appointment(params, token, backend):
    data = backend._request(
        "PATCH", "/api/appointments/%s" % params["appointment_id"],
        payload={"status": params["status"]}, token=token)
    return ("预约已%s，租客会收到站内消息。"
            % ("拒绝" if params["status"] == "rejected" else "确认"), data)


def _exec_update_repair(params, token, backend):
    data = backend._request(
        "PATCH", "/api/repairs/%s" % params["repair_id"],
        payload={"status": params.get("status", "done")}, token=token)
    return "工单已标记为完成。", data


EXECUTORS = {
    "pay_bill": _exec_pay_bill,
    "cancel_viewing": _exec_cancel_viewing,
    "book_viewing": _exec_book_viewing,
    "submit_repair": _exec_submit_repair,
    "renew_contract": _exec_renew_contract,
    "end_contract": _exec_end_contract,
    "favorite_house": _exec_favorite,
    "unfavorite_house": _exec_unfavorite,
    "read_all_messages": _exec_read_all,
    "clear_history": _exec_clear_history,
    "submit_review": _exec_submit_review,
    "handle_appointment": _exec_handle_appointment,
    "update_repair": _exec_update_repair,
    "toggle_house": _exec_toggle_house,
}

# 每个动作允许出现的参数名。**白名单，不是黑名单**：
# 确认卡片是从前端回传的，多一个没见过的字段就应当拒绝，
# 而不是原样透传给后端。
ALLOWED_PARAMS = {
    "pay_bill": {"bill_no", "method"},
    "cancel_viewing": {"appointment_id", "status"},
    "book_viewing": {"house_no", "viewing_time"},
    "submit_repair": {"house_no", "content"},
    "renew_contract": {"contract_no", "months"},
    "end_contract": {"contract_no", "reason"},
    "favorite_house": {"house_no"},
    "unfavorite_house": {"house_no"},
    "read_all_messages": set(),
    "clear_history": set(),
    "submit_review": {"contract_no", "rating", "content"},
    "handle_appointment": {"appointment_id", "status"},
    "update_repair": {"repair_id", "status"},
    "toggle_house": {"house_no", "status"},
}


def execute(name, params, token, backend):
    """执行一个**已被用户确认**的动作。返回 ``(ok, message, data)``。

    只有 `/agent/support/confirm` 这一个入口会调到这里，
    对话主流程永远走不到——这条边界是刻意的。
    """
    if not token:
        return False, "登录状态已失效，请重新登录后再试。", {}

    executor = EXECUTORS.get(name)
    if executor is None:
        return False, "不支持这个操作。", {}

    params = params or {}
    allowed = ALLOWED_PARAMS.get(name, set())
    unexpected = set(params) - allowed
    if unexpected:
        return False, "操作参数不合法：%s" % "、".join(sorted(unexpected)), {}
    missing = allowed - set(params)
    # method / reason 有默认值，缺了不影响
    missing -= {"method", "reason", "status"}
    if missing:
        return False, "操作缺少参数：%s" % "、".join(sorted(missing)), {}

    try:
        message, data = executor(params, token, backend)
    except BackendError as exc:
        # 后端的拒绝原因要如实转述：可能是账单已被支付、预约已过期、
        # 或者根本不是他的数据。含糊成「操作失败」会让用户反复重试。
        return False, "没能完成：%s" % str(exc), {}
    except Exception:
        return False, "执行时出了点问题，请到对应页面手动操作。", {}

    return True, message, data if isinstance(data, dict) else {}


# ---------------------------------------------------------------- 刻意不接的

# 以下操作**故意不做成可代办**，不是漏了：
#
# - **改密码**（`/api/auth/password`）：口令不该经过对话框，也不该出现在
#   任何一条日志里。用户在「账号设置」里自己改，一步都不多。
# - **填身份证号**（`ProfileUpdate.id_card`）：证件原文属于最敏感的一类
#   个人信息，平台本身都只存哈希与掩码（R-02），没有理由让它先在
#   聊天记录里走一遭。
# - **发布房源**（`/api/houses`）：十几个必填字段，靠一句话对话填完只会
#   反复追问，体验比表单差得多。发布页已经有 AI 定价建议与描述生成，
#   那才是这个场景该用 AI 的地方。
#
# 判断标准是同一条：**代办要让事情更省，而不是把表单改造成审问。**
# 涉及凭据与证件的，无论多省都不做。
NOT_EXPOSED = ("change_password", "set_id_card", "publish_house")
