# -*- coding: utf-8 -*-
"""执行选中的工具（G1）。

这一层是**翻译，不是实现**：把 `{tool, params}` 翻译成对已有子智能体
和已有接口的调用。刻意没有在这里新写任何业务逻辑——

- 找房仍是 `property_finder.run`
- 讲解、对比仍是 `pitch.describe` / `pitch.compare`
- 评价仍是 `house.reviews` / `house.landlord_reviews`
- 查我的数据仍是后端那几个 `/mine` 接口
- **写操作一步都不执行**，只交给 `actions.propose` 出确认卡片

新写一份的话，就会出现「对话里改了、页面上没改」这种两套逻辑各自漂移的
经典毛病。这一层唯一新增的东西是「序号 → 房源编号」的还原，
因为只有它知道模型说的 `ordinal: 3` 对应屏幕上的哪一套。
"""

import re

from app import memory
from app.agents.support.tools import house as house_tool
from app.agents.support.tools import pitch
from app.agents.support.tools.dates import days_left, until_text
from app.agents.support.tools.money import money_text
from app.agents.support.tools.account import bill_type_text as _bill_type
from app.agents.support.tools.account import lookup as account_lookup

# 找房一次给几套。和 main.PAGE_SIZE 必须一致——
# 界面列几条、助手数到几、这里取几条，是同一个数
PAGE_SIZE = 10


def _house_from(params, last_house_nos, current):
    """把 house_no / ordinal 还原成一个真实编号。

    优先级：模型直接给的编号 → 序号 → 用户正打开的那套 → 列表第一套。

    **序号越界返回 `OUT_OF_RANGE` 而不是悄悄取第一套。** 用户说
    「第二十套」而列表只有十套，答第一套的信息比说「只有 10 套」糟得多：
    他会以为第二十套长这样。
    """
    no = str(params.get("house_no") or "").strip().upper()
    if no:
        return no
    ordinal = params.get("ordinal")
    if ordinal:
        index = int(ordinal) - 1
        if index < 0 or index >= len(last_house_nos or []):
            return memory.OUT_OF_RANGE
        return last_house_nos[index]
    return current or (last_house_nos[0] if last_house_nos else "")


def _houses_from(params, last_house_nos):
    """对比要两三套。返回编号列表，不足两套时返回空。"""
    nos = [str(x).strip().upper()
           for x in (params.get("house_nos") or []) if str(x).strip()]
    for ordinal in (params.get("ordinals") or []):
        index = int(ordinal) - 1
        if 0 <= index < len(last_house_nos or []):
            no = last_house_nos[index]
            if no not in nos:
                nos.append(no)
    return nos[:3]


# 工具参数名 → 系统内部的条件键名。
#
# **内部键是拼音**（`zujia_max`、`suozaidiqu`…），不是后端接口的
# `rent_price_max`。两套名字之间的翻译在
# `search_properties.build_params` 里，这一层不该越过它。
#
# 第一版我按后端键名写，于是 build_params 一个都认不出来，
# 条件被静默丢掉——「租金3000以内」返回了全部 95 套，租价上到 4650，
# 而 `context` 里明明白白显示条件已经解析出来了。**这类错不报异常**，
# 界面上看起来一切正常，只是答案是错的。
#
# 更糟的是我给它写的单元测试断言的也是后端键名，于是测试绿着、
# 功能坏着。**翻译层的测试必须拿被调用方真正认识的键去断言**，
# 不能拿自己以为的。
_FILTER_KEYS = {
    "district": "suozaidiqu",
    "layout": "fangwuhuxing",
    "keyword": "guanjianci",
    "rent_min": "zujia_min",
    "rent_max": "zujia_max",
    "area_min": "mianji_min",
    "area_max": "mianji_max",
    "rent_type": "fangyuanleixing",
}


def _filters_from(params):
    """把工具参数翻译成 property_finder 认识的筛选键。"""
    out = {}
    for key, internal in _FILTER_KEYS.items():
        value = params.get(key)
        # **用 is not None 而不是真值判断**：0 是个有效的下限，
        # 「租金 0 起」和「没填」是两件事
        if value is not None and value != "":
            out[internal] = value
    return out


# 查「我的数据」的工具 → 后端接口。
#
# 这几条本可以走 support 层的 toolcall，但那条路会再调一次大模型
# 来「照着数据组织回答」。**这里的数据本身就是答案**——
# 「你有 3 笔待缴、共 5400 元」不需要润色，多一次往返只是多等两秒。
ACCOUNT_TOOLS = {
    "my_bills": ("/api/bills/mine", "账单"),
    "my_contracts": ("/api/contracts/mine", "租约"),
    "my_appointments": ("/api/appointments/mine", "看房预约"),
    "my_repairs": ("/api/repairs", "报修"),
    "my_favorites": ("/api/favorites", "收藏"),
    "my_history": ("/api/history", "浏览记录"),
    "landlord_appointments": ("/api/appointments/mine", "别人约我的看房"),
    "landlord_houses": ("/api/landlord/houses", "我名下的房源"),
    "landlord_tenants": ("/api/landlord/tenants", "我的租客"),
    "landlord_repairs": ("/api/repairs", "租客报修"),
    "landlord_contracts": ("/api/contracts/mine", "出租合同"),
    "agent_commissions": ("/api/agent/commissions", "佣金"),
    "agent_delegations": ("/api/delegations", "我代理的房源"),
    "agent_viewings": ("/api/viewings", "带看记录"),
}

# 各自的查询参数。my_* 与 landlord_* 打的是同一个接口，
# 靠 side 分开——这正是 D3 加那个参数的用处
ACCOUNT_QUERY = {
    "my_bills": {"side": "tenant"},
    "my_appointments": {"side": "tenant"},
    "my_contracts": {"side": "tenant"},
    "my_repairs": {"side": "tenant"},
    "landlord_appointments": {"side": "landlord"},
    "landlord_repairs": {"side": "landlord"},
    "landlord_contracts": {"side": "landlord"},
    # 委托表里一个人可能既是房东又是中介（K-13 的教训），
    # 不指明 role 会把他作为房东收到的委托也算成「我代理的房」
    "agent_delegations": {"role": "agent"},
}


def run_read(name, params, ctx):
    """执行一个读工具。返回结果 dict，接不住时返回 None。

    返回 None 表示「这一层没处理」，调用方会退回规则链路——
    和选择器返回 none 是同一种含义。
    """
    if name == "search_houses":
        return _search(params, ctx)
    if name == "more_houses":
        return _search({}, ctx, more=True)
    if name in ("house_detail", "house_reviews", "landlord_reputation"):
        return _one_house(name, params, ctx)
    if name == "compare_houses":
        return _compare(params, ctx)
    if name in ACCOUNT_TOOLS:
        return _account(name, ctx, params)
    if name == "my_todos":
        return _todos(ctx)
    if name == "landlord_income":
        return _income(ctx)
    if name == "pricing_suggest":
        return _pricing(params, ctx)
    if name == "admin_todos":
        return _admin_todos(ctx)
    return None


# (接口, 查询参数, 说法)。顺序即重要性：有人在等的排前面
# 最后一项是「问到了就算是 0 也要说」的关键词：问「多少房源待审核」
# 却只听到「24 套待核验」，他会以为待审核那项没查
_ADMIN_QUEUES = (
    ("/api/admin/handoffs", {"status": "pending"}, "%d 张转人工工单没回复", "转人工"),
    ("/api/admin/houses", {"status": "pending"}, "%d 套房源待审核", "审核"),
    ("/api/admin/verify-queue", None, "%d 套房源待核验", "核验"),
    ("/api/admin/reports", {"status": "pending"}, "%d 条举报待处理", "举报"),
    ("/api/admin/flags", {"status": "pending"}, "%d 条内容待复核", "复核"),
    ("/api/admin/role-requests", {"status": "pending"}, "%d 个角色申请待审", "角色"),
    ("/api/admin/agent-queue", None, "%d 份中介资质待审", "资质"),
)


def _admin_todos(ctx):
    """管理员待办。逐个问已有的管理接口，某一项查失败就跳过并说一声。"""
    if not ctx["token"]:
        return {"message": "这个要先登录才看得到。", "need_login": True}
    import datetime
    parts, missed, today_handoffs = [], 0, None
    asked = ctx.get("text") or ""
    for path, query, phrase, word in _ADMIN_QUEUES:
        try:
            data = ctx["backend"].get(path, params=query, token=ctx["token"]) or {}
        except Exception:                                    # noqa: BLE001
            missed += 1
            continue
        rows = data.get("list") if isinstance(data, dict) else data
        count = data.get("total") if isinstance(data, dict) else None
        count = count if isinstance(count, int) else len(rows or [])
        if path == "/api/admin/handoffs":
            today = datetime.date.today().isoformat()
            today_handoffs = sum(1 for r in rows or []
                                 if str(r.get("created_at") or "").startswith(today))
        if count or word in asked:
            parts.append(phrase % count)
    if missed == len(_ADMIN_QUEUES):
        return {"message": "这些是管理员的数据，你当前的身份看不到，或者这会儿没连上。"}
    text = ("平台待办：" + "；".join(parts) + "。") if parts else "平台现在没有待处理的事项。"
    if today_handoffs is not None:
        text += "今天新提交的转人工有 %d 张（还没回复的）。" % today_handoffs
    if missed:
        text += "另有 %d 项没查到，稍后再问我一次。" % missed
    return {"message": text, "status": "answered"}


_AREA_SAID = re.compile(r"(\d+(?:\.\d+)?)\s*(?:平米|平方米|平方|平|㎡)")


def _pricing(params, ctx):
    """定价参考。数字全部来自后端 /api/pricing/suggest（同区同户型的在租房源）。

    区、户型、面积：模型给了就用，没给就从原话里认——和找房同一个解析器，
    「西岗」「两室」这些省略写法它已经会了。
    """
    if not ctx["token"]:
        return {"message": "这个要先登录才看得到。右上角登录之后再问我一次。",
                "need_login": True}
    text = ctx.get("text") or ""
    parsed = memory.parse_query(text)["filters"]
    district = memory.known_district(params.get("district") or parsed.get("suozaidiqu"))
    layout = str(params.get("layout") or parsed.get("fangwuhuxing") or "")
    if layout:
        layout = memory.parse_query(layout)["filters"].get("fangwuhuxing") or layout
    area = params.get("area")
    if not area:
        said = _AREA_SAID.search(text)
        area = float(said.group(1)) if said else 0
    if not district:
        return {"message": "定价参考是按区算的，你的房子在哪个区？再说一下户型和面积，"
                           "比如「西岗区两室一厅 70 平」。"}
    try:
        data = ctx["backend"].get("/api/pricing/suggest", params={
            "district": district, "layout": layout, "area": area or 0},
            token=ctx["token"]) or {}
    except Exception:                                        # noqa: BLE001
        return {"message": "定价参考只对房东开放，或者这会儿没连上，稍后再问我一次。"}
    message = data.get("message") or "暂时给不出参考。"
    if data.get("ok"):
        message += "这是参考区间，最终定价由你自己决定；定得明显高于区间的，出租会慢一些。"
    return {"message": message, "status": "answered"}


def _income(ctx):
    """经营数据。复用客服层那个取数函数——同一个接口、同一句话，
    不在这里再写一份口径。数字全部来自后端统计接口。"""
    if not ctx["token"]:
        return {"message": "这个要先登录才看得到。右上角登录之后再问我一次。",
                "need_login": True}
    status, message, data = account_lookup(ctx["backend"], ctx["token"], "income")
    out = {"message": message, "status": "answered" if status == "ok" else status}
    if status == "ok" and data:
        # 网页有专门的经营卡片（ChatPanel 的 IncomeCard），主题名就是 income
        out["account"] = {"tool": "income", "data": data}
    return out


def _search(params, ctx, more=False):
    # **本轮的条件也要过 apply_turn，和规则那一路同一套规矩。**
    #
    # 原先这里把模型给的参数直接拼到历史条件上，于是本轮的
    # 相对调整（「便宜点的呢」）、取消（「不限租金」「不用两室一厅」）
    # 全部被跳过——实测「便宜点的呢」原样返回了同一批房。
    #
    # 本轮条件取「规则解析 ∪ 模型参数」，冲突时听模型的：模型漏掉的
    # 规则常能补上（「单间」规则读成一室，模型要么漏掉、要么原样填
    # 「单间」一套也筛不出）；户型交给规则解析器规范一遍写法。
    text = ctx.get("text") or ""
    ruled = memory.parse_query(memory.strip_negated(text))
    now = dict(ruled.get("filters") or {})
    llm = _filters_from(params)
    layout = llm.get("fangwuhuxing")
    if layout:
        canon = memory.parse_query(str(layout))["filters"].get("fangwuhuxing")
        llm["fangwuhuxing"] = canon or layout
    now.update(llm)
    feats_now = list(ruled.get("features") or [])
    for feat in (params.get("features") or []):
        if feat not in feats_now:
            feats_now.append(feat)
    filters, features, notes = memory.apply_turn(
        ctx["slots"] or {}, ctx["features"] or [], text,
        filters=now, feats=feats_now)

    if not filters and not features and not more:
        if notes:
            # 唯一的条件是个认不出的区名——把原因说出来，别静默退回规则
            return {"message": " ".join(notes), "notes": notes,
                    "status": "unparsed", "intent": "search", "items": []}
        # 一个条件都没解析出来。**别硬查**——不带条件的查询会返回
        # 全部房源，而用户说的显然是别的什么
        return None

    page = 1
    if more:
        page = max(1, int(ctx.get("seen") or 0) // PAGE_SIZE + 1)

    result = ctx["property_finder"].run(
        ctx["text"], provider=ctx["provider"], filters=filters,
        features=features, source="tool", page=page, limit=PAGE_SIZE)
    result["page"] = page
    if more and page > 1:
        result["append"] = True
        start = int(ctx.get("seen") or 0) + 1
        count = len(result.get("items") or [])
        result["message"] = (
            "接着往下，第 %d–%d 套（共 %d 套）。"
            % (start, start + count - 1, result.get("total") or 0)
            if count else
            "没有更多了，这 %d 套就是全部符合条件的。" % (result.get("total") or 0))
    result["context"] = filters
    result["context_text"] = memory.describe(filters, features)
    if notes:
        result["notes"] = notes
        # 认不出区名这一条要说在正文里：它决定了用户看到的这批房
        # 为什么不在他以为的那个地方
        unknown = [n for n in notes if memory.speaks_up(n)]
        if unknown:
            result["message"] = " ".join(unknown) + " " + (result.get("message") or "")
    return result


def _one_house(name, params, ctx):
    target = _house_from(params, ctx["last_house_nos"], ctx["house_no"])
    if target is memory.OUT_OF_RANGE:
        return {"_out_of_range": True}
    if not target:
        return None
    rows = ctx["fetch_houses"]([target])
    if not rows:
        # 用户**亲口报了编号**却查不到，就说查不到。原先退回规则链路，
        # 回的是「我这边还没有可以介绍的房源，先说说你的条件」——
        # 像是没听见他说的 DL9999。
        if str(params.get("house_no") or "").strip():
            return {"message": "没有找到编号 %s 的房源，可能输错了，或者这套已经下架。"
                               "可以把编号再核对一下，或者告诉我条件我帮你找。" % target}
        return None

    house = rows[0]
    label = "%s %s" % (house.get("community") or "", house.get("layout") or "")
    backend = ctx["backend"]
    # 「这套」是猜出来的时候，先说清楚猜的是哪一套。
    # 不说的话，用户刚看完 A 的讲解、接着问「周边有什么」，
    # 拿到的是 B 的周边——他没有任何线索能发现指代跑偏了。
    hint = ("（你说的是 %s）" % house.get("house_no")
            if memory.is_weak_reference(ctx.get("text"),
                                        ctx["last_house_nos"],
                                        ctx["house_no"]) else "")

    if name == "house_reviews":
        return {"message": hint + house_tool.reviews(backend, house["house_no"],
                                                    label.strip()),
                "house_no": house["house_no"],
                "options": ["介绍下这套", "帮我约看房", "看看类似的"]}

    if name == "landlord_reputation":
        return {"message": hint + house_tool.landlord_reviews(
                    backend, house["house_no"], label.strip()),
                "house_no": house["house_no"],
                "options": ["这套评价怎么样", "帮我约看房"]}

    # 先看这句话是不是在问某个**具体条件**（能养宠物吗、有电梯吗、
    # 周边有什么）。是就回答那一条——`pitch.describe()` 只看房源、
    # 不看问题，所以这类是非题会统统拿到同一段推销词。用户看不出
    # 自己的问题有没有被回答，只能再问一遍：实测「这套能养宠物吗」
    # 连点三次，三次都是同一段户型介绍。
    direct = pitch.answer_attribute(house, ctx.get("text"))
    if ctx.get("agent"):
        # **agent 调用时把房源资料整份交回去，不在这里再问一次模型。**
        #
        # answer_by_llm 只拿得到这一句话。实测用户先说「公司在星海广场附近」，
        # 再问「第二套离公司远吗」——那套就在星海广场巷，内层模型却答
        # 「没写明离你公司的距离」，因为它根本不知道公司在哪。agent 自己带着
        # 整段对话，把字段给它，它就能把两头对上；还省掉一次模型往返。
        result = {"house_no": house["house_no"], "house": house,
                  "options": ["介绍下这套", "帮我约看房", "看看类似的"]}
        if direct:
            result["message"] = hint + direct
        else:
            message, data = pitch.describe(backend, house)
            result.update(message=hint + message, pitch=data)
        return result
    if not direct:
        # 关键词表列不全。没命中的交给模型按字段回答——
        # 它拿不到字段以外的东西，答不上来会说「没写明」。
        direct = pitch.answer_by_llm(house, ctx.get("text"),
                                     ctx.get("provider"))
    if direct:
        # 答「没有标注」时给条出路，不然用户只知道这套不行，
        # 不知道下一步该干什么。
        if "没有标注" in direct:
            direct += " 想看有这个条件的房子，跟我说一声我帮你筛。"
        return {"message": hint + direct, "house_no": house["house_no"],
                "options": ["介绍下这套", "帮我约看房", "看看类似的"]}

    message, data = pitch.describe(backend, house)
    return {"message": hint + message, "house_no": house["house_no"], "pitch": data,
            "options": ["帮我约看房", "收藏这套", "看看类似的"]}


def _compare(params, ctx):
    nos = _houses_from(params, ctx["last_house_nos"])
    direct = direct_list_answer(ctx.get("text"), nos, ctx["last_house_nos"],
                                ctx["fetch_houses"])
    if direct:
        return direct
    if len(nos) < 2:
        nos = (ctx["last_house_nos"] or [])[:2]
    rows = ctx["fetch_houses"](nos[:3])
    if len(rows) < 2:
        return None
    message, data = pitch.compare(ctx["backend"], rows)
    message = distance_note(ctx.get("text"), rows) + message
    return {"message": message, "compare": data,
            "options": ["介绍下第一套", "介绍下第二套", "帮我约看房"]}


# 「刚才那几套里哪个最便宜」——原先拿前两套做对比，另外三套压根没看。
# 「第一套和第二套哪个采光好」——对比讲的是价钱和面积，采光一个字没提。
# 这两类问题有确定答案，直接从字段里算，不必绕一段对比。
_SUPERLATIVES = (
    (re.compile(r"最便宜|价格最低|租金最低|最省钱"), "rent_price", min, "最便宜", "%s 元/月"),
    (re.compile(r"最贵|租金最高|价格最高"), "rent_price", max, "最贵", "%s 元/月"),
    (re.compile(r"面积最大|最大|最宽敞"), "area", max, "面积最大", "%s 平"),
    (re.compile(r"面积最小|最小"), "area", min, "面积最小", "%s 平"),
)
_ASKS_LIGHT = re.compile(r"采光|朝向|朝南|向阳|阳光")
_ASKS_FLOOR = re.compile(r"电梯|楼层|几楼|爬楼")


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def direct_list_answer(text, picked, listed, fetch):
    """有确定答案的比较问题，直接从字段里答。答不了返回 None。

    `picked` 是用户点名的几套（「第一套和第二套」），没点名时看整份列表——
    问「这几套里哪个最便宜」的人说的是屏幕上所有的房，不是前两套。
    """
    text = text or ""
    listed = [n for n in (listed or []) if n]
    rule = next((r for r in _SUPERLATIVES if r[0].search(text)), None)
    light, floor = _ASKS_LIGHT.search(text), _ASKS_FLOOR.search(text)
    if not (rule or light or floor):
        return None
    nos = picked if len(picked) >= 2 else listed[:10]
    if len(nos) < 2:
        return None
    rows = fetch(nos)
    if len(rows) < 2:
        return None

    def label(row):
        no = row.get("house_no") or ""
        where = ("第 %d 套 " % (listed.index(no) + 1)) if no in listed else ""
        return "%s%s（%s）" % (where, row.get("title") or row.get("community") or "", no)

    if rule:
        _, field, pick, word, unit = rule
        valued = [r for r in rows if _num(r.get(field)) is not None]
        if len(valued) < 2:
            return None
        best = pick(valued, key=lambda r: _num(r.get(field)))
        value = _num(best.get(field))
        message = "这 %d 套里%s的是%s，%s。" % (
            len(valued), word, label(best), unit % ("%g" % value))
        return {"message": message, "house_no": best.get("house_no"),
                "options": ["介绍下这套", "帮我约看房"]}

    parts = []
    for row in rows:
        if light:
            parts.append("%s %s" % (label(row), row.get("orientation") or "没标朝向"))
        else:
            lift = row.get("elevator")
            parts.append("%s %s，%s" % (label(row), row.get("floor") or "没标楼层",
                                       "有电梯" if lift else ("没电梯" if lift is False
                                                              else "没标电梯")))
    head = ("采光主要看朝向，朝南、南北通透的一般更好：" if light
            else "楼层和电梯：")
    return {"message": head + "；".join(parts) + "。房源信息只写到这一层，"
                       "实际亮不亮、吵不吵，看房时当面看最准。",
            "options": ["介绍下第一套", "帮我约看房"]}


# 问的是远近 / 通勤。平台只有房源自己的位置，没有到用户某个目的地的距离。
_ASKS_DISTANCE = re.compile(r"离.{0,8}(最近|最远|近|远|多远)|通勤|多久能到|到.{0,6}(方便|多远)")


def distance_note(text, rows):
    """「这几套里哪个离我公司最近」——挑中的是对比，对比讲的是价钱和面积。

    平台没有到某个地点的通勤数据，**先把这一点说清楚**，再给能给的：
    不说的话，用户会以为下面那段对比就是按远近排的。
    规则链路和工具层各有一个对比入口，两边都用这一句。
    """
    if not _ASKS_DISTANCE.search(text or ""):
        return ""
    subway = [r.get("house_no") for r in rows if "地铁" in str(r.get("tags") or "")]
    note = "我这边没有这几套到你说的地方的距离或通勤时间，没法按远近排。"
    if subway:
        note += "标了近地铁的是 %s，通勤可以优先看。" % "、".join(subway)
    return note + " 下面是它们其他方面的差别："

_HOUSE_STATUS = {"available": "在架还空着", "rented": "已经租出去",
                 "offline": "已下架"}


def _account(name, ctx, params=None):
    """查我自己的数据。**必须带令牌**，未登录时说清楚要先登录。"""
    if not ctx["token"]:
        return {"message": "这个要先登录才看得到。右上角登录之后再问我一次。",
                "need_login": True}
    params = params or {}
    path, label = ACCOUNT_TOOLS[name]
    try:
        data = ctx["backend"].get(path, params=ACCOUNT_QUERY.get(name),
                                  token=ctx["token"])
    except Exception:                                        # noqa: BLE001
        return {"message": "查%s的时候没连上，稍后再问我一次。" % label}

    rows = (data or {}).get("list") or []

    # 用户问的是其中一部分时，**回答那一部分**。「哪几笔逾期了」
    # 「哪几套还空着」原先拿到的是和总问题一字不差的回答。
    if name == "my_bills" and params.get("overdue_only"):
        late = [r for r in rows if r.get("status") == "unpaid" and r.get("overdue")]
        if not late:
            return {"message": "你没有逾期的账单。"}
        total = sum(float(r.get("amount") or 0) for r in late)
        late.sort(key=lambda r: r.get("due_date") or "")
        first = late[0]
        return {"message": ("你有 %d 笔逾期账单，合计 %s 元。最早一笔是%s %s 元，%s 到期。"
                            % (len(late), money_text(total),
                               _bill_type(first), money_text(first.get("amount")),
                               first.get("due_date") or "")),
                "account": {"tool": "bill",
                            "data": {"unpaid": late[:5], "total": len(late)}},
                "options": ["帮我把最早逾期的那笔交了"]}
    if name == "landlord_tenants" and params.get("overdue_only"):
        owing = [r for r in rows if (r.get("overdue_count") or 0) > 0]
        if not owing:
            return {"message": "你的租客都没有逾期账单。"}
        # **逾期金额用 overdue_total。** 原先拿 unpaid_total 说「一共还欠」——
        # 那里头大半是合同排好、还没到期的账单，实测把逾期 2 笔说成了欠 39650 元
        total = sum(float(r.get("overdue_total") or 0) for r in owing)
        return {"message": "有 %d 位租客有逾期账单：%s，逾期未缴合计 %s 元。" % (
                    len(owing),
                    "、".join("%s（逾期 %d 笔）" % (
                        r.get("real_name") or r.get("username") or r.get("name") or "",
                        r.get("overdue_count") or 0) for r in owing[:5]),
                    money_text(total)),
                "account": {"tool": "tenant",
                            "data": {"list": owing[:8], "total": len(owing)}}}
    if name == "landlord_contracts" and params.get("expiring_days"):
        try:
            within = int(float(params["expiring_days"]))
        except (TypeError, ValueError):
            within = 0
        soon = [r for r in rows if r.get("status") == "active"
                and 0 <= (days_left(r.get("end_date")) or 0) <= within
                and days_left(r.get("end_date")) is not None]
        if within > 0:
            if not soon:
                text = "未来 %d 天内没有到期的出租合同。" % within
                active = [r for r in rows if r.get("status") == "active"]
                if active:
                    first = min(active, key=lambda r: r.get("end_date") or "9")
                    text += "在租的 %d 份里，最早到期的是 %s，租到 %s。" % (
                        len(active), first.get("house_no") or "",
                        until_text(first.get("end_date")))
                return {"message": text}
            soon.sort(key=lambda r: r.get("end_date") or "")
            return {"message": "未来 %d 天内有 %d 份出租合同到期：%s。" % (
                        within, len(soon),
                        "；".join("%s 到 %s" % (r.get("house_no") or "",
                                               until_text(r.get("end_date")))
                                 for r in soon[:5])),
                    "account": {"tool": "contract",
                                "data": {"list": soon[:8], "total": len(soon)}},
                    "options": ["帮我给第一份续约"]}
    if name == "landlord_houses" and params.get("by_rating") in ("worst", "best"):
        return _houses_by_rating(rows, worst=params["by_rating"] == "worst")
    if name == "landlord_houses" and params.get("status") in _HOUSE_STATUS:
        want = params["status"]
        picked = [r for r in rows if r.get("status") == want]
        text = _HOUSE_STATUS[want]
        if not picked:
            return {"message": "你名下没有%s的房源。" % text}
        return {"message": "你名下%s的有 %d 套：%s。" % (
                    text, len(picked),
                    "、".join(r.get("house_no") or "" for r in picked[:8])),
                "items": picked[:20]}
    if not rows:
        # 「浏览记录」「带看记录」本身带着「记录」，别再接一个
        return {"message": "没有查到%s。" % (label if label.endswith("记录")
                                            else label + "记录")}

    out = {"message": _summarize(name, data, rows, label)}
    card = _card(name, rows)
    if card:
        out["account"] = card
    # **列的是房子，就把整行房源按屏幕顺序带出去。**
    #
    # 不带的话，下一句「第一套」没有可对应的列表：房东刚看完
    # 「你名下有 9 套房源」，接着说「把第一套下架」，系统却回
    # 「要操作哪一套？」——他明明刚数过。序号是双方唯一的共同坐标，
    # 只要屏幕上有编号的列表，这个坐标就该建立起来。
    #
    # 原来只带 {"house_no"}。网页把 items 画成房源卡片，于是收藏列表
    # 每一行都是空标题、「NaN 元」。这两个接口返回的本来就是整行房源，
    # 原样给出去即可，不用再查一遍。
    if name in ("landlord_houses", "my_favorites", "my_history"):
        houses = [r for r in rows[:20]
                  if isinstance(r, dict) and r.get("house_no")]
        if houses:
            out["items"] = houses
            # 有了房源卡片就不再叠一张同内容的查库卡片
            out.pop("account", None)
    return out


def _houses_by_rating(rows, worst):
    """名下房源按评价排。没人评过的单独说，不混进排名里——
    「暂无评价」排在最差或最好都是误导。"""
    rated = [r for r in rows if (r.get("review_count") or 0) > 0]
    unrated = len(rows) - len(rated)
    if not rated:
        return {"message": "你名下的房源都还没有评价。"}
    rated.sort(key=lambda r: float(r.get("rating") or 0), reverse=not worst)
    first = rated[0]
    text = "评价%s的是 %s %s：均分 %.1f，%d 条评价。" % (
        "最差" if worst else "最好", first.get("house_no") or "",
        (first.get("title") or "")[:16], float(first.get("rating") or 0),
        first.get("review_count") or 0)
    if len(rated) > 1:
        text += "有评价的 %d 套，从%s到%s%s：%s。" % (
            len(rated), "低" if worst else "高", "高" if worst else "低",
            "依次是" if len(rated) <= 6 else "前 6 套是",
            "、".join("%s（%.1f）" % (r.get("house_no") or "",
                                     float(r.get("rating") or 0))
                     for r in rated[:6]))
    if unrated:
        text += "另有 %d 套还没有人评价。" % unrated
    return {"message": text, "items": rated[:20],
            "options": ["%s 的评价具体怎么说" % (first.get("house_no") or "第一套")]}


# 工具名 → 网页查库卡片的主题名（web/src/components/ChatPanel.jsx 的
# ACCOUNT_ROWS）。**规则链路给的是 {tool: 主题, data: {...}}**，
# 这一层原来给的是 {tool: 工具名, rows: [...]}——网页只认前一种，
# 于是凡是由大模型选中的查库（账单、租约、预约……）都只剩一句话，
# 卡片一张都画不出来；同一句「我有哪些待缴费」，走规则有明细，走模型没有。
_CARD_TOPIC = {
    "my_bills": "bill",
    "my_contracts": "contract",
    "my_appointments": "booking",
    "landlord_appointments": "booking",
    "my_repairs": "repair",
    "landlord_houses": "house",
    "landlord_tenants": "tenant",
    "landlord_repairs": "repair",
    "landlord_contracts": "contract",
}


def bill_split(unpaid):
    """待缴账单按「已逾期 / 还没到期」分开算好——**按全部账单算**。

    卡片只摆前 5 条（按到期日排，正好是逾期的那几笔）。agent 原先拿这 5 条
    去算，用户问「是不是把没到期的也算进去了」，它答「未到期 0 笔」——
    实际 36 笔里有 31 笔还没到期。这句话只能由手里有全部账单的这一层来算。
    """
    late = [r for r in unpaid if r.get("overdue")]
    later = [r for r in unpaid if not r.get("overdue")]
    total = lambda part: round(sum(float(r.get("amount") or 0) for r in part), 2)  # noqa: E731
    out = {"已逾期": {"笔数": len(late), "合计": total(late)},
           "还没到期": {"笔数": len(later), "合计": total(later)}}
    dues = sorted(str(r.get("due_date")) for r in later if r.get("due_date"))
    if dues:
        out["还没到期"]["最近一笔到期日"] = dues[0]
        out["还没到期"]["最晚一笔到期日"] = dues[-1]
    return out


def _card(name, rows):
    topic = _CARD_TOPIC.get(name)
    if not topic:
        return None
    if topic == "bill":
        unpaid = [r for r in rows if r.get("status") == "unpaid"]
        return {"tool": topic, "data": {"unpaid": unpaid[:5], "total": len(rows),
                                        "待缴拆分": bill_split(unpaid)}}
    if topic == "contract":
        # 生效中的排前面：卡片只摆四行，「我租了哪些房」的用户要先看见在租的
        order = {"active": 0, "pending": 1}
        rows = sorted(rows, key=lambda r: order.get(r.get("status"), 2))
    return {"tool": topic, "data": {"list": rows[:8], "total": len(rows)}}


def _summarize(name, data, rows, label):
    """一句话把查到的东西说清楚。

    **数字全部从返回体里取，不经过大模型。** 这几句话正是用户会照着
    去交钱、去赴约的依据，错一个数就是实实在在的麻烦。
    """
    if name == "my_bills":
        unpaid = data.get("unpaid_count") or 0
        amount = data.get("unpaid_amount") or 0
        overdue = data.get("overdue_count") or 0
        if not unpaid:
            return "你没有待缴账单，都结清了。"
        text = "你有 %d 笔待缴账单，合计 %s 元。" % (unpaid, money_text(amount))
        if overdue:
            text += "其中 %d 笔已经逾期，建议先处理这几笔。" % overdue
        return text

    if name in ("my_appointments", "landlord_appointments"):
        pending = [r for r in rows if r.get("status") == "pending"]
        confirmed = [r for r in rows if r.get("status") == "confirmed"]
        who = "你约的看房" if name == "my_appointments" else "别人约你的房"
        return ("%s共 %d 条：%d 条待确认，%d 条已确认。"
                % (who, len(rows), len(pending), len(confirmed)))

    if name == "my_contracts":
        active = [r for r in rows if r.get("status") == "active"]
        if active:
            first = active[0]
            # 「还有几天到期」问的是天数。原先只回日期，内容对但答非所问，
            # 用户还得自己算——`until_text` 把换算在这里做掉。
            # 月租同理：`first["rent"]` 直接 %s 会写成「1250.0 元」。
            # 「我租了哪些房」问的是哪几套。原来说「3 份生效中」却只点名一份，
            # 另外两套是哪儿，用户得自己去卡片上找。
            if len(active) > 1:
                return ("你有 %d 份生效中的租约：%s。最早到期的是 %s，租到 %s。"
                        % (len(active),
                           "、".join(r.get("house_no") or "" for r in active[:5]),
                           min(active, key=lambda r: r.get("end_date") or "9")
                           .get("house_no") or "",
                           until_text(min(active, key=lambda r: r.get("end_date")
                                          or "9").get("end_date"))))
            return ("你有 %d 份生效中的租约。%s 租到 %s，月租 %s 元。"
                    % (len(active), first.get("house_no") or "",
                       until_text(first.get("end_date")),
                       money_text(first.get("rent"))))
        return "你有 %d 份租约记录，目前没有生效中的。" % len(rows)

    if name == "my_repairs":
        open_rows = [r for r in rows if r.get("status") != "done"]
        return ("你提交过 %d 张报修单，其中 %d 张还没处理完。"
                % (len(rows), len(open_rows)))

    if name == "landlord_repairs":
        waiting = [r for r in rows if r.get("status") == "pending"]
        doing = [r for r in rows if r.get("status") == "processing"]
        text = ("你名下的房源共有 %d 张报修单：%d 张还没受理，%d 张在修。"
                % (len(rows), len(waiting), len(doing)))
        if waiting:
            first = waiting[0]
            text += "最新一张没受理的是 %s：%s。" % (
                first.get("house_no") or "", (first.get("content") or "")[:30])
        return text

    if name == "landlord_contracts":
        active = [r for r in rows if r.get("status") == "active"]
        pending = [r for r in rows if r.get("status") == "pending"]
        text = "你出租的合同共 %d 份：%d 份生效中，%d 份等租客确认。" % (
            len(rows), len(active), len(pending))
        if active:
            first = min(active, key=lambda r: r.get("end_date") or "9")
            text += "最早到期的是 %s，租到 %s。" % (
                first.get("house_no") or "", until_text(first.get("end_date")))
        return text

    if name == "landlord_houses":
        listed = [r for r in rows if r.get("status") == "available"]
        rented = [r for r in rows if r.get("status") == "rented"]
        return ("你名下有 %d 套房源：%d 套在架，%d 套已出租。"
                % (len(rows), len(listed), len(rented)))

    if name == "agent_commissions":
        # **金额直接取返回体里算好的那两个数。**
        # 自己再遍历一遍 list 求和，等于在客户端复制一份口径，
        # 哪天后端改了「作废的佣金算不算」，两边就对不上账了。
        # 这是这个人要照着去对工资的数字。
        pending = money_text(data.get("pending_amount") or 0)
        settled = money_text(data.get("settled_amount") or 0)
        return ("你共有 %d 笔佣金记录：待结算 %s 元，已结算 %s 元。"
                % (len(rows), pending, settled))

    if name == "agent_delegations":
        active = [r for r in rows if r.get("status") == "active"]
        return ("你受托代理 %d 套房源，其中 %d 套委托还在生效。"
                % (len(rows), len(active)))

    if name == "agent_viewings":
        houses = {r.get("house_no") for r in rows if r.get("house_no")}
        return ("你带看过 %d 次，涉及 %d 套房源。" % (len(rows), len(houses)))

    if name in ("my_favorites", "my_history"):
        first = rows[0]
        title = (first.get("title") or first.get("community") or "")[:20]
        verb = "收藏了" if name == "my_favorites" else "最近看过"
        return ("你%s %d 套房源，第 1 套是「%s」。下面列出来了，"
                "可以直接说「第二套评价怎么样」「介绍下第一套」。"
                % (verb, (data or {}).get("total") or len(rows), title))

    return "查到 %d 条%s记录。" % (len(rows), label)


def _todos(ctx):
    """我还有哪些事没处理完。

    **不新造接口**：逐个问已有的那几个，各自只取「还没完」的部分。
    某一项查失败就跳过、在末尾说一声，别让一个子查询把整句拖垮——
    用户问的是「还有什么要办」，少一项比整句报错有用。
    数字全部取自返回体，不经过大模型。
    """
    if not ctx["token"]:
        return {"message": "这个要先登录才看得到。右上角登录之后再问我一次。",
                "need_login": True}
    backend, token = ctx["backend"], ctx["token"]

    def rows_of(path, params=None):
        data = backend.get(path, params=params, token=token) or {}
        return data, (data.get("list") if isinstance(data, dict) else data) or []

    parts, options, missed = [], [], []

    try:
        bills, _ = rows_of("/api/bills/mine", {"side": "tenant"})
        unpaid = bills.get("unpaid_count") or 0
        if unpaid:
            text = "%d 笔待缴账单共 %s 元" % (
                unpaid, money_text(bills.get("unpaid_amount") or 0))
            if bills.get("overdue_count"):
                text += "（%d 笔已逾期）" % bills["overdue_count"]
            parts.append(text)
            options.append("帮我交房租")
    except Exception:                                        # noqa: BLE001
        missed.append("账单")

    try:
        _, rows = rows_of("/api/appointments/mine", {"side": "tenant"})
        waiting = [r for r in rows if r.get("status") == "pending"]
        if waiting:
            parts.append("%d 个看房预约等房东确认" % len(waiting))
            options.append("我约的看房")
    except Exception:                                        # noqa: BLE001
        missed.append("看房预约")

    try:
        _, rows = rows_of("/api/appointments/mine", {"side": "landlord"})
        waiting = [r for r in rows if r.get("status") == "pending"]
        if waiting:
            parts.append("%d 个别人约你的看房等你确认" % len(waiting))
    except Exception:                                        # noqa: BLE001
        pass  # 租客问这个接口可能被拒，不算「没查到」

    try:
        _, rows = rows_of("/api/contracts/mine", {"side": "tenant"})
        pending = [r for r in rows if r.get("status") == "pending"]
        if pending:
            parts.append("%d 份租约待处理（%s）" % (
                len(pending), "、".join(r.get("house_no") or "" for r in pending[:3])))
            options.append("我租了哪些房")
    except Exception:                                        # noqa: BLE001
        missed.append("租约")

    try:
        _, rows = rows_of("/api/repairs", {"side": "tenant"})
        doing = [r for r in rows if r.get("status") not in ("done", "closed")]
        if doing:
            parts.append("%d 张报修还没处理完" % len(doing))
            options.append("我的报修进度")
    except Exception:                                        # noqa: BLE001
        missed.append("报修")

    # 房东侧。实测房东问「帮我总结一下我现在该处理哪些事」拿到一句
    # 「暂时没有查到相关规定」——汇总里原先只有租客那几项。
    # 租客的令牌打这两个接口会被拒，拒了就当没有，不算「没查到」。
    try:
        _, rows = rows_of("/api/landlord/tenants")
        owing = [r for r in rows if (r.get("overdue_count") or 0) > 0]
        if owing:
            parts.append("%d 位租客有逾期账单（%s）" % (
                len(owing), "、".join((r.get("username") or r.get("name") or "")
                                      for r in owing[:3])))
            options.append("我的租客都有谁")
    except Exception:                                        # noqa: BLE001
        pass
    try:
        _, rows = rows_of("/api/repairs", {"side": "landlord"})
        doing = [r for r in rows if r.get("status") not in ("done", "closed")]
        if doing:
            parts.append("名下房源有 %d 张报修待处理" % len(doing))
    except Exception:                                        # noqa: BLE001
        pass

    try:
        _, rows = rows_of("/api/reviews/pending")
        if rows:
            parts.append("%d 份租约待评价" % len(rows))
    except Exception:                                        # noqa: BLE001
        missed.append("评价")

    try:
        msgs, _ = rows_of("/api/messages")
        unread = msgs.get("unread") if isinstance(msgs, dict) else 0
        if unread:
            parts.append("%d 条未读消息" % unread)
    except Exception:                                        # noqa: BLE001
        missed.append("消息")

    if parts:
        message = "你还有这些没处理完：" + "；".join(parts) + "。"
    else:
        message = "你手上没有待处理的事，账单、预约、报修都清了。"
    if missed:
        message += "（%s暂时没查到，稍后可以再问我。）" % "、".join(missed)
    return {"message": message, "options": options[:3],
            "intent": "support", "status": "answered"}
