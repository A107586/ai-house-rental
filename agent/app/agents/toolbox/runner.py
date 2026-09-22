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

from app import memory
from app.agents.support.tools import house as house_tool
from app.agents.support.tools import pitch
from app.agents.support.tools.dates import until_text
from app.agents.support.tools.money import money_text

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
    "landlord_appointments": ("/api/appointments/mine", "别人约我的看房"),
    "landlord_houses": ("/api/landlord/houses", "我名下的房源"),
    "landlord_tenants": ("/api/landlord/tenants", "我的租客"),
    "agent_commissions": ("/api/agent/commissions", "佣金"),
    "agent_delegations": ("/api/delegations", "我代理的房源"),
    "agent_viewings": ("/api/viewings", "带看记录"),
}

# 各自的查询参数。my_* 与 landlord_* 打的是同一个接口，
# 靠 side 分开——这正是 D3 加那个参数的用处
ACCOUNT_QUERY = {
    "my_appointments": {"side": "tenant"},
    "my_contracts": {"side": "tenant"},
    "my_repairs": {"side": "tenant"},
    "landlord_appointments": {"side": "landlord"},
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
        return _account(name, ctx)
    return None


def _search(params, ctx, more=False):
    filters = dict(ctx["slots"] or {})
    filters.update(_filters_from(params))
    features = list(ctx["features"] or [])
    for feat in (params.get("features") or []):
        if feat not in features:
            features.append(feat)

    if not filters and not features and not more:
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
    return result


def _one_house(name, params, ctx):
    target = _house_from(params, ctx["last_house_nos"], ctx["house_no"])
    if target is memory.OUT_OF_RANGE:
        return {"_out_of_range": True}
    if not target:
        return None
    rows = ctx["fetch_houses"]([target])
    if not rows:
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
    if len(nos) < 2:
        nos = (ctx["last_house_nos"] or [])[:2]
    rows = ctx["fetch_houses"](nos[:3])
    if len(rows) < 2:
        return None
    message, data = pitch.compare(ctx["backend"], rows)
    return {"message": message, "compare": data,
            "options": ["介绍下第一套", "介绍下第二套", "帮我约看房"]}


def _account(name, ctx):
    """查我自己的数据。**必须带令牌**，未登录时说清楚要先登录。"""
    if not ctx["token"]:
        return {"message": "这个要先登录才看得到。右上角登录之后再问我一次。",
                "need_login": True}
    path, label = ACCOUNT_TOOLS[name]
    try:
        data = ctx["backend"].get(path, params=ACCOUNT_QUERY.get(name),
                                  token=ctx["token"])
    except Exception:                                        # noqa: BLE001
        return {"message": "查%s的时候没连上，稍后再问我一次。" % label}

    rows = (data or {}).get("list") or []
    if not rows:
        return {"message": "没有查到%s记录。" % label, "account": {"rows": []}}

    out = {"message": _summarize(name, data, rows, label),
           "account": {"tool": name, "rows": rows[:20],
                       "total": (data or {}).get("total") or len(rows)}}
    # **列的是房子，就把编号按屏幕顺序带出去。**
    #
    # 不带的话，下一句「第一套」没有可对应的列表：房东刚看完
    # 「你名下有 9 套房源」，接着说「把第一套下架」，系统却回
    # 「要操作哪一套？」——他明明刚数过。序号是双方唯一的共同坐标，
    # 只要屏幕上有编号的列表，这个坐标就该建立起来。
    if name in ("landlord_houses", "my_favorites"):
        nos = [{"house_no": r.get("house_no")} for r in rows[:20]
               if isinstance(r, dict) and r.get("house_no")]
        if nos:
            out["items"] = nos
    return out


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
            return ("你有 %d 份生效中的租约。%s 租到 %s，月租 %s 元。"
                    % (len(active), first.get("house_no") or "",
                       until_text(first.get("end_date")),
                       money_text(first.get("rent"))))
        return "你有 %d 份租约记录，目前没有生效中的。" % len(rows)

    if name == "my_repairs":
        open_rows = [r for r in rows if r.get("status") != "done"]
        return ("你提交过 %d 张报修单，其中 %d 张还没处理完。"
                % (len(rows), len(open_rows)))

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

    return "查到 %d 条%s记录。" % (len(rows), label)
