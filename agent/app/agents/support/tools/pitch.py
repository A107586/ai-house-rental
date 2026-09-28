# -*- coding: utf-8 -*-
"""房源讲解与对比——**让助手能替用户拿主意，而不是把清单再念一遍。**

实测里这三句话全都失败了：

    「第一套第二套哪个好」  → 把上一轮的搜索结果原样又列了一遍
    「对比第一套和第二套」  → 同上
    「帮我介绍下第一套」    → 同上

用户已经看到列表了。他再开口，要的是**判断**：这套到底好在哪、
两套差在哪、我该选哪个。把清单重复一遍等于没听懂。

## 关于「推销」

这是个租房平台，讲解本来就带着推销的性质，问题只在**怎么推销**。
这里的做法是：

- **亮点必须落在字段上。** 「采光好」是因为标签里写了、朝向是南；
  「单价划算」是因为和同区同户型比过。凭空一句「性价比超高」，
  用户到现场就知道被骗了，而看房是要花半天时间的。
- **短板照说不误。** 一层、没电梯、朝北、还没通过核验——这些
  藏起来最多骗到一次带看，说出来反而让「亮点」那部分可信。
  售楼处只报喜的话术在这里行不通：租客当天就能看见房子。
- **最后落到「适合谁」。** 情绪价值不是形容词堆砌，是替他把
  「这套适不适合我」这一步想完——一个人住还是两个人住、
  在意通勤还是在意安静。

金额、面积、评分一律由代码从字段里取，不经过大模型改写：
这些数字错一个，整段话就从「懂行」变成「不可信」。

## 关于「问什么答什么」

讲解之外还有一类：用户指着这套房问**一个具体条件**——能养宠物吗、
有电梯吗、房租多少、一个人住合适吗。这类是非题不能拿整段讲解去答：
他问的是一件事，拿到的是一段介绍，**他看不出自己的问题有没有被回答**，
只能再问一遍。实测里同一个用户连问三句不同的话，三次拿到同一段推销词，
最后的反馈是「怎么回复都一样」。

两条路：`answer_attribute()` 是关键词表，命中就即答、零延迟、结果确定；
没命中的交给 `answer_by_llm()`，让模型按字段回答，但**字段是白名单、
没有的一律说「没写明」**——猜一个条件出来，用户会照着它去看房。
"""

import json

_TAG_PITCH = {
    "地铁": "出门就是地铁，通勤不用挤公交",
    "近学校": "旁边就是学校，带孩子的省心",
    "近医院": "离医院近，家里有老人会踏实很多",
    "电梯": "有电梯，搬家和买菜都不受罪",
    "采光好": "采光好，白天基本不用开灯",
    "有阳台": "带阳台，晒被子和养花都有地方",
    "可做饭": "能开火，天天点外卖的钱省下来了",
    "可养宠物": "可以养宠物，带毛孩子的不用再找了",
    "有家具": "家具齐全，拎包就能住",
    "近商圈": "挨着商圈，吃饭逛街不用跑远",
}

_SUNNY = ("朝南", "南", "东南", "西南", "南北通透")
_DIM = ("朝北", "北")


def _num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _money(value):
    amount = _num(value)
    return "%d" % round(amount)


def _floor_parts(house):
    """把「1/30层」拆成 (所在层, 总层数)。拆不出来返回 (0, 0)。"""
    raw = str(house.get("floor") or "")
    digits = ""
    for ch in raw:
        digits += ch if ch.isdigit() or ch == "/" else " "
    parts = [p for p in digits.replace("/", " ").split() if p.isdigit()]
    if len(parts) >= 2:
        return int(parts[0]), int(parts[1])
    if len(parts) == 1:
        return int(parts[0]), 0
    return 0, 0


def price_context(backend, house):
    """这套房在**同区同户型**里贵还是便宜。取不到就返回空字典。

    比的是每平米单价而不是总租金：同样是两室一厅，78 平和 62 平的
    总价本来就该差一截，拿总价说「便宜 1500」是在误导。
    """
    district = house.get("district") or ""
    layout = house.get("layout") or ""
    if not district or not layout:
        return {}
    try:
        data = backend.get("/api/houses", params={
            "district": district, "layout": layout, "page_size": 50})
    except Exception:                                        # noqa: BLE001
        return {}
    rows = (data or {}).get("list") or []
    prices = []
    for row in rows:
        area = _num(row.get("area"))
        rent = _num(row.get("rent_price"))
        if area > 0 and rent > 0:
            prices.append(rent / area)
    if len(prices) < 2:
        return {}

    prices.sort()
    middle = len(prices) // 2
    median = (prices[middle] if len(prices) % 2
              else (prices[middle - 1] + prices[middle]) / 2.0)
    area = _num(house.get("area"))
    rent = _num(house.get("rent_price"))
    if area <= 0 or rent <= 0 or median <= 0:
        return {}
    unit = rent / area
    return {
        "unit": round(unit, 1),
        "median": round(median, 1),
        "diff": round((unit - median) / median * 100),
        "sample": len(prices),
        "scope": "%s%s" % (district, layout),
    }


def highlights(house, price=None):
    """这套房**值得说**的地方。每一条都指得出是哪个字段。"""
    found = []
    tags = house.get("tags") or []
    for tag in tags:
        if tag in _TAG_PITCH:
            found.append(_TAG_PITCH[tag])

    orientation = str(house.get("orientation") or "")
    if any(word in orientation for word in _SUNNY) and "采光好" not in tags:
        found.append("%s，一天里有大半天是亮的" % orientation)

    floor, total = _floor_parts(house)
    if house.get("elevator") and floor >= 6:
        found.append("%d 层带电梯，视野好又不用爬" % floor)

    rating = _num(house.get("rating"))
    reviews = int(_num(house.get("review_count")))
    if rating >= 4.5 and reviews >= 1:
        found.append("住过的%s给了 %.1f 分" % (
            "%d 位租客" % reviews if reviews > 1 else "租客", rating))

    if price and price.get("diff", 0) <= -8:
        found.append("每平米 %.1f 元，比%s的中位价低 %d%%"
                     % (price["unit"], price["scope"], abs(price["diff"])))

    views = int(_num(house.get("view_count")))
    if views >= 200:
        found.append("最近 %d 人看过，热度不低" % views)

    if _num(house.get("service_fee")) == 0:
        found.append("不收服务费")

    return found[:5]


def cautions(house, price=None):
    """**照实说的短板。**

    藏起来最多骗到一次带看。租客当天就能看见房子，看见了就再也不信
    这个助手说的任何一句话——包括那些是真的。

    价格那一条排在最前面：只有它是**能拿去谈**的，其余几条要么
    改不了（楼层、朝向），要么只是提醒他去核实。说话时最多说两条，
    第一条得是最有用的那条。
    """
    warned = []
    if price and price.get("diff", 0) >= 12:
        warned.append("每平米 %.1f 元，比%s的中位价高 %d%%，可以拿这个去和房东谈"
                      % (price["unit"], price["scope"], price["diff"]))

    floor, total = _floor_parts(house)
    if floor >= 5 and not house.get("elevator"):
        warned.append("%d 层没有电梯，搬家和买大件会比较费劲" % floor)
    if floor == 1:
        warned.append("是一层，采光和私密性要现场看一眼")

    orientation = str(house.get("orientation") or "")
    if any(word in orientation for word in _DIM) and "南" not in orientation:
        warned.append("朝北，冬天会偏冷、白天也暗一些")

    if not (house.get("tags") or []):
        warned.append("房源标签写得很少，配套情况建议问问房东")
    elif "有家具" not in (house.get("tags") or []):
        warned.append("没标「有家具」，家电家具要问清楚带不带")

    if str(house.get("verify_status") or "") != "verified":
        warned.append("还没通过平台核验，签约前建议先实地看房")

    return warned[:3]


def suits(house):
    """一句「适合谁」。判断依据只用户型和租金，不编造生活方式。"""
    layout = str(house.get("layout") or "")
    rent = _num(house.get("rent_price"))
    if layout.startswith("一室") or layout == "单间":
        who = "一个人住或者情侣"
    elif layout.startswith("两室"):
        who = "小两口，或者要个书房、留个客房的"
    elif layout.startswith("三室") or layout.startswith("四室"):
        who = "一家三口，也适合几个人合租分摊"
    else:
        who = "对空间要求不高的"
    return "月租 %s 元这个价位，%s住着比较合适。" % (_money(rent), who)


# 用户问某个具体条件时，问的词 -> (房源里怎么判断, 有的话怎么说, 没有怎么说)
#
# **「没有」必须说成「没有标注」而不是「不可以」。** 房东没勾这个标签，
# 可能是真不允许，也可能只是没填——把缺失说成禁止是在替房东做决定，
# 而用户会拿着这句话去退租、去投诉。
_ASK_TAG = {
    "宠物": ("可养宠物", "这套标了可养宠物，带毛孩子没问题。",
             "这套没有标注可养宠物。"),
    "养猫": ("可养宠物", "这套标了可养宠物，猫可以带。",
             "这套没有标注可养宠物。"),
    "养狗": ("可养宠物", "这套标了可养宠物，狗可以带。",
             "这套没有标注可养宠物。"),
    "做饭": ("可做饭", "这套能开火做饭。", "这套没有标注可做饭。"),
    "开火": ("可做饭", "这套能开火做饭。", "这套没有标注可做饭。"),
    "家具": ("有家具", "家具齐全，拎包就能住。", "这套没有标注带家具。"),
    "家electric": ("有家具", "", ""),
    "阳台": ("有阳台", "带阳台。", "这套没有标注阳台。"),
    "地铁": ("地铁", "离地铁近。", "这套没有标注近地铁。"),
    "学校": ("近学校", "旁边就是学校。", "这套没有标注近学校。"),
    "医院": ("近医院", "离医院近。", "这套没有标注近医院。"),
    "商圈": ("近商圈", "挨着商圈。", "这套没有标注近商圈。"),
}


def answer_attribute(house, question):
    """用户问这套房的某个具体条件时，直接回答那一条。

    返回一句话，或者 `None` 表示「这句话不是在问某个条件」，
    由调用方退回整段讲解。

    **为什么要单独一条路**：`describe()` 只看房源、不看问题，所以
    「能养宠物吗」「有电梯吗」「周边有什么」会拿到同一段推销词。
    用户问的是一个是非题，拿到的是一段介绍——他看不出自己的问题
    有没有被回答，只能再问一遍。实测里这三句连点三次拿到同一段话。
    """
    q = (question or "").strip()
    if not q:
        return None
    tags = [str(t) for t in (house.get("tags") or [])]

    for word, (tag, yes, no) in _ASK_TAG.items():
        if word and word in q and yes:
            return yes if tag in tags else no

    # 「第一套房东是谁，能联系吗」——原先交给模型按字段答，字段表里没有房东，
    # 于是回「房源信息里没写房东」。名字只给姓（和房东口碑那句同一个口径），
    # 联系方式平台在看房确认后才给，照实说，并把能做的那一步递过去。
    if "房东" in q and any(w in q for w in ("谁", "联系", "电话", "手机", "微信", "姓什么")):
        name = str(house.get("landlord_name") or "").strip()
        who = ("这套的房东是%s**。" % name[0]) if name else "这套没有写明房东是谁。"
        return (who + "平台不在看房前给联系方式：预约看房被房东确认后，"
                "联系方式会通过站内消息发给你。要我帮你约个时间吗？")

    # 结构化字段：这些不是标签，房源表里有确切值
    if "电梯" in q:
        lift = house.get("elevator")
        if lift is None and "电梯" not in tags:
            return "这套没有写明有没有电梯。"
        return "这套有电梯。" if (lift or "电梯" in tags) else "这套没有电梯。"
    if "朝向" in q or "朝南" in q or "采光" in q:
        ori = house.get("orientation")
        if not ori:
            return "这套没有写明朝向。"
        extra = "，采光好" if ori in _SUNNY else ""
        return "这套朝%s%s。" % (ori, extra)
    if "暖气" in q or "供暖" in q:
        h = house.get("heating")
        return ("供暖方式是%s。" % h) if h else "这套没有写明供暖方式。"
    if "多久" in q or "最短" in q or "几个月" in q:
        m = house.get("min_months")
        return ("最短租%s个月。" % m) if m else "这套没有写明最短租期。"
    if "什么时候" in q and ("入住" in q or "看房" in q or "能租" in q):
        d = house.get("available_from")
        return ("%s 起可入住。" % d) if d else "这套没有写明可入住时间。"
    if "周边" in q or "附近" in q:
        near = [t for t in tags if t in ("地铁", "近学校", "近医院", "近商圈")]
        where = "%s%s" % (house.get("district") or "",
                          house.get("business_area") or house.get("community") or "")
        if near:
            return "在%s，周边：%s。" % (where, "、".join(near))
        return "在%s，房源没有标注周边设施。" % where
    return None


# 「介绍一下」这类**要的就是整段讲解**，不该被当成具体问题拦下来。
_WHOLE_PITCH = ("介绍", "怎么样", "值不值", "讲讲", "说说", "详情", "情况")

# 给模型看的字段。**白名单而不是黑名单**：房源表里有 lat/lng、
# verify_no、view_count 这类不该出现在回答里的东西，漏掉一个
# 就可能被复述给用户。
_ASK_FIELDS = ("title", "district", "community", "business_area", "layout",
               "area", "rent_price", "deposit", "pay_plan", "service_fee",
               "rent_type", "min_months", "orientation", "floor", "floor_level",
               "elevator", "decoration", "heating", "tags", "available_from",
               "rating", "review_count", "status")

_ASK_SYSTEM = ("你是租房平台的房源客服。只依据给你的房源字段回答，"
               "一到两句话，不要复述整套房源介绍。")

_ASK_PROMPT = """房源字段（JSON）：
{fields}

用户问：{question}

规则：
1. **事实只能来自上面的字段。** 房租、押金、面积、朝向、租期这些
   有确切值的，直接引用，不要自己算。
2. 字段里没有的，说「房源信息里没写明」——但**别就这么停住**。
   接着做一件有用的事：用已有字段给个有依据的判断（比如问「一个人住
   合适吗」，可以说没写明，但两室一厅 70 平通常够住），
   或者告诉他下一步能怎么办（看房时当面确认、让我帮你问房东）。
   **只说「没写明」三个字，读起来像搜索引擎没查到，而不像有人在帮他。**
3. 推断要和事实分开说，让他看得出哪句是房源写的、哪句是你的判断。
   **别把判断说成事实**——他会照着去看房、去签约。
4. 标签里没有某一项，是「没有标注」，**不等于「不可以」**。
   房东可能只是没填，不要替房东做决定。
5. 只答他问的那件事，两三句话。不要顺带把整套房再夸一遍。"""


def answer_by_llm(house, question, provider, deadline=None):
    """关键词表没覆盖到的具体问题，交给模型按字段回答。

    返回一句话，或 `None`（模型不可用 / 这句话本来就是要整段讲解）。

    **为什么需要它**：`answer_attribute()` 是关键词表，列不全。
    实测用户问「房租多少钱」「一个人住合适吗」「有空调吗」——
    三个都不在表里，于是三次拿到同一段推销词。用户的反馈是
    「怎么回复都一样」，而他看不出是自己的问题没被理解，
    还是这套房真的只有这些信息。
    """
    q = (question or "").strip()
    if not q or provider is None or not getattr(provider, "available", False):
        return None
    if any(w in q for w in _WHOLE_PITCH):
        return None

    fields = {k: house[k] for k in _ASK_FIELDS if house.get(k) not in (None, "")}
    try:
        text = provider.complete(
            _ASK_SYSTEM,
            _ASK_PROMPT.format(fields=json.dumps(fields, ensure_ascii=False),
                               question=q[:120]),
            max_tokens=220, **({"deadline": deadline} if deadline else {}))
    except Exception:                                        # noqa: BLE001
        # 模型不可用不该让这一轮失败——退回整段讲解，那条路一直是通的
        return None
    text = (text or "").strip()
    return text[:200] if text else None


def describe(backend, house):
    """讲解一套房。返回 ``(message, data)``。"""
    price = price_context(backend, house)
    good = highlights(house, price)
    bad = cautions(house, price)

    # 标题里通常已经写着「中山区青泥洼桥 两室一厅 78平」，再把区、
    # 小区、面积复述一遍就成了「中山区青泥洼桥 两室一厅 78平，
    # 中山区青泥洼桥，79 平米」——**而且 78.6 四舍五入成 79，
    # 和标题里的 78 自相矛盾**。所以标题带过的就不再重复。
    title = house.get("title") or house.get("house_no") or "这套房"
    where = "%s%s" % (house.get("district") or "", house.get("community") or "")
    tail = ["月租 %s 元" % _money(house.get("rent_price"))]
    if where and where not in title:
        tail.insert(0, where)
    lead = "%s，%s。" % (title, "，".join(tail))

    parts = [lead]
    if good:
        parts.append("好的地方是：%s。" % "；".join(good[:3]))
    if bad:
        parts.append("要留意的是：%s。" % "；".join(bad[:2]))
    parts.append(suits(house))

    return " ".join(parts), {
        "house": house,
        "highlights": good,
        "cautions": bad,
        "price": price,
        "suits": suits(house),
    }


# ---------------------------------------------------------------- 对比

# 拿来对比的项。顺序就是用户关心的顺序：先钱，再空间，最后配套。
# `better` 说明**哪一头算好**，`None` 表示这一项没有高下之分。
COMPARE_ROWS = [
    ("月租", "rent_price", "元", "low"),
    ("押金", "deposit", "元", "low"),
    ("面积", "area", "平米", "high"),
    ("每平米", "_unit", "元", "low"),
    ("户型", "layout", "", None),
    ("楼层", "floor", "", None),
    ("朝向", "orientation", "", None),
    ("位置", "_where", "", None),
    ("评分", "rating", "分", "high"),
]


def _cell(house, field):
    if field == "_unit":
        area = _num(house.get("area"))
        rent = _num(house.get("rent_price"))
        return round(rent / area, 1) if area > 0 else ""
    if field == "_where":
        return "%s%s" % (house.get("district") or "",
                         house.get("community") or "")
    if field == "rating":
        # **没人评过不等于评了 0 分。**
        # 照原样填会在对比表里写出「5 分 / 0 分」，还把另一套标成更优——
        # 这是拿「没有数据」当成了「数据很差」，对新上架的房源不公平，
        # 对用户也是误导。返回空串，下面的 `_fmt` 会渲染成「—」，
        # 也就不会参与「哪边更好」的判定。
        if int(_num(house.get("review_count"))) <= 0:
            return ""
    return house.get(field)


def _fmt(value, unit):
    if value in (None, ""):
        return "—"
    if isinstance(value, float) and value == int(value):
        value = int(value)
    return "%s%s" % (value, (" " + unit) if unit else "")


def compare(backend, houses):
    """对比两套（或更多）房源。返回 ``(message, data)``。

    **不评「哪套绝对更好」。** 一套便宜一套大，好坏取决于这个人
    更缺钱还是更缺地方——替他下这个结论是越界。能做的是把差在哪里
    摆清楚，再按各自最突出的那一项说一句「什么情况下选它」。
    """
    houses = [h for h in houses if h][:3]
    if len(houses) < 2:
        return "", {}

    rows = []
    for label, field, unit, better in COMPARE_ROWS:
        values = [_cell(h, field) for h in houses]
        numbers = [_num(v, None) if isinstance(v, (int, float)) else None
                   for v in values]
        best = None
        if better and all(n is not None for n in numbers):
            pick = min(numbers) if better == "low" else max(numbers)
            # 完全相等就不标——两边都打上「更优」等于没说
            if numbers.count(pick) == 1:
                best = numbers.index(pick)
        rows.append({
            "label": label,
            "values": [_fmt(v, unit) for v in values],
            "best": best,
        })

    # **对比正文里一律不写序号，只写名字。**
    #
    # `houses` 是被挑出来对比的那两三套，它们在这个列表里的位次
    # （1、2）和用户屏幕上搜索结果里的位次（可能是 3、5）不是一回事。
    # 写「第 1 套更便宜」，而用户问的是「第三套和第五套哪个好」——
    # 又一次把同一套房换了个号码。序号只在搜索结果那一屏里有意义。
    lines = []
    cheap = min(houses, key=lambda h: _num(h.get("rent_price"), 1e9))
    big = max(houses, key=lambda h: _num(h.get("area")))
    if cheap is big:
        lines.append("%s 又便宜又大，这一组里它占优。" % _name(cheap))
    else:
        gap = _num(big.get("rent_price")) - _num(cheap.get("rent_price"))
        area_gap = _num(big.get("area")) - _num(cheap.get("area"))
        lines.append(
            "%s 每月便宜 %s 元，%s 多出 %s 平米——多花的钱买的就是这点空间。"
            % (_name(cheap), _money(gap), _name(big), _money(area_gap)))
        lines.append("预算紧就看%s，两个人住、东西多就看%s。"
                     % (_name(cheap), _name(big)))

    # 各自最拿得出手的一条
    for house in houses:
        good = highlights(house)
        if good:
            lines.append("%s 的长处：%s。" % (_name(house), good[0]))

    return " ".join(lines), {"rows": rows, "houses": houses}


# ---------------------------------------------------------------- 主动推荐

# 打分项：(权重, 说明的写法)。**每一项都必须指得出是哪个字段**——
# 这段话是要写进「我最推荐第几套」后面的理由里的，编一句就全砸了。
#
# 权重之间的相对大小是有讲究的：**单价压倒一切**。租房这件事上，
# 用户嘴上说的偏好再多，最后决定他签不签的还是每月要掏多少钱；
# 而「便宜」是这一组里可以客观比出来的，不用猜。
W_CHEAPEST = 4.0        # 每平米最便宜
W_WANTED_TAG = 2.0      # 命中一条他明确说过的偏好
W_RATED = 2.0           # 有口碑（**要够多人评过**，见下）
W_RATED_THIN = 1.0      # 只有一两条评价
W_BIGGEST = 0.5         # 面积最大
W_VERIFIED = 1.0        # 已通过平台核验
W_TAG = 0.4             # 配套多一条

# 「有口碑」的门槛。**一条五星评价不是口碑，是一个人的看法。**
# 不设这道门槛的话，一套刚上架、被房东的朋友评了一次五分的房子，
# 分数就能压过每月便宜一千五的那套——而用户真正在意的是后者。
ENOUGH_REVIEWS = 3

# 面积那一项的权重压得很低是有意的：这一组里面积最大的，
# 十有八九也是最贵的那套。给它高权重等于变相奖励「贵」。

# 领先优势不到这个数就不推。
#
# **「差不多」的时候硬挑一套出来，是这类功能最容易失信的地方**：
# 用户点进去发现和第二套没什么区别，下一次就不会再看这行字了。
# 宁可说一句「这几套各有各的好」，那也是有用的信息。
MIN_MARGIN = 1.5


def rank(houses, features=None):
    """给这一组房源打分，返回 ``[(分数, 理由列表, 房源), …]``，高分在前。"""
    houses = [h for h in houses if h]
    if not houses:
        return []

    wanted = [f for f in (features or []) if f]
    units = {}
    for house in houses:
        area = _num(house.get("area"))
        rent = _num(house.get("rent_price"))
        units[id(house)] = (rent / area) if area > 0 and rent > 0 else None

    live = [u for u in units.values() if u]
    cheapest = min(live) if live else None
    areas = [_num(h.get("area")) for h in houses]
    biggest = max(areas) if areas else 0

    scored = []
    for house in houses:
        score = 0.0
        why = []
        tags = house.get("tags") or []

        unit = units[id(house)]
        if unit is not None and cheapest is not None and unit <= cheapest + 0.01:
            score += W_CHEAPEST
            why.append("每平米 %.1f 元，是这几套里最便宜的" % unit)

        hit = [tag for tag in wanted if tag in tags]
        if hit:
            score += W_WANTED_TAG * len(hit)
            why.append("你要的「%s」它都有" % "」「".join(hit))

        rating = _num(house.get("rating"))
        reviews = int(_num(house.get("review_count")))
        if reviews >= 1 and rating >= 4.5:
            enough = reviews >= ENOUGH_REVIEWS
            score += W_RATED if enough else W_RATED_THIN
            why.append("%d 位住过的租客给了 %.1f 分" % (reviews, rating)
                       if enough else "住过的租客给了 %.1f 分" % rating)

        if biggest > 0 and _num(house.get("area")) >= biggest:
            score += W_BIGGEST
            why.append("面积也是这几套里最大的")

        if str(house.get("verify_status") or "") == "verified":
            score += W_VERIFIED
            why.append("已经通过平台核验")

        score += min(len(tags), 5) * W_TAG
        # 理由太单薄不好使——「最便宜」四个字说完就没了，用户还是不知道
        # 这套房好在哪。补一两条最拿得出手的配套，仍然只取标签字段。
        extra = [tag for tag in ("地铁", "电梯", "有家具", "可养宠物", "近学校")
                 if tag in tags and tag not in hit]
        if extra and len(why) < 3:
            why.append("还带%s" % "、".join(extra[:2]))
        scored.append((score, why, house))

    scored.sort(key=lambda row: -row[0])
    return scored


def best_of(houses, features=None):
    """从结果里挑**一套**最值得先看的。返回 ``(房源, 一句理由)``。

    挑不出来时返回 ``(None, "")``——这不是失败，是诚实：

    - 只有一套，没什么可挑的；
    - 头两名分差不到 `MIN_MARGIN`，说明**这几套确实差不多**。
      硬推一个，用户点进去发现和第二套没区别，这行字从此就白写了。
    """
    scored = rank(houses, features)
    if len(scored) < 2:
        return None, ""
    if scored[0][0] - scored[1][0] < MIN_MARGIN:
        return None, ""

    score, why, house = scored[0]
    if not why:
        return None, ""
    return house, "；".join(why[:2])


def _order_of(houses, house):
    for index, item in enumerate(houses, start=1):
        if item is house:
            return index
    return 0


def _name(house):
    """一套房在对话里怎么称呼。

    **用户看得见的是小区名，不是编号。** 列表里写着「沙河口区黑石礁
    一室一厅 46平」，对比表里却只写 DL0041，同一套房换了个叫法，
    用户得自己在两者之间连线。编号仍然给——报给客服、发起预约时要用它——
    但放进括号里，让人先认出是哪一套。
    """
    title = (house.get("title") or "").strip()
    no = (house.get("house_no") or "").strip()
    if title and no:
        return "%s（%s）" % (title, no)
    return title or no


def _where(houses, house, offset=0):
    """「第 N 套 小区名（编号）」。三种叫法合成一种，别让用户来回换算。

    `offset` 是这一批在整份列表里的起点。翻到第二页时，这批的第 1 条
    在用户屏幕上是第 11 条——**序号必须按屏幕上的数，不是按这批的数**，
    否则「我最推荐第 5 套」指着的是用户眼里的第 15 套。
    """
    order = _order_of(houses, house)
    name = _name(house)
    if not order:
        return name
    order += offset
    return "第 %d 套 %s" % (order, name) if name else "第 %d 套" % order


def lead_line(houses, features=None, offset=0):
    """摆在结果前面的那一句「我最推荐哪一套」。

    分不出高下时**不沉默**，改说「这两套各强在哪」。

    这一点想清楚花了点功夫：一开始是挑不出来就什么都不说，理由是
    「不能硬推」。可用户拿着三套条件相近的房，他要的恰恰就是这句话——
    沉默把问题原样退回给了他。而「A 最便宜、B 面积最大和评价最好，
    看你更在意哪个」既没有编造，也真的帮他往前走了一步。
    **不确定的时候该说的是「凭什么难选」，不是不吭声。**
    """
    scored = rank(houses, features)
    if len(scored) < 2:
        return ""

    top, second = scored[0], scored[1]
    if top[0] - second[0] >= MIN_MARGIN and top[1]:
        return "这几套里我最推荐%s：%s。" % (_where(houses, top[2], offset),
                                    "；".join(top[1][:2]))

    if not top[1] or not second[1]:
        return ""
    return ("%s和%s差不多，前者胜在%s，后者胜在%s，看你更在意哪一头。"
            % (_where(houses, top[2], offset),
               _where(houses, second[2], offset),
               top[1][0], second[1][0]))


# ---------------------------------------------------------------- 推荐词

# 能加进来的设施偏好。**只写数据库真有的标签**——推一句
# 「只看能停车的」，用户点了却搜不出东西，比不推更糟。
_TAG_CHIPS = [
    ("地铁", "只看近地铁的"),
    ("电梯", "只看带电梯的"),
    ("有家具", "只看带家具的"),
    ("可养宠物", "只看能养宠物的"),
    ("近学校", "只看近学校的"),
]


def followups(houses, filters=None, features=None):
    """结果出来之后，主动递几句「接下来可以说什么」。

    **推荐词必须是从这一组结果里长出来的，不能是一张写死的清单。**
    推「只看带电梯的」的前提是这批里真有带电梯的、而且**不是全都有**——
    全都有的话这句话点了等于没点，用户会觉得这个助手在敷衍。

    条数压到 4 条：一屏放得下，也逼着按重要性排。
    """
    houses = [h for h in houses if h]
    if not houses:
        return []

    filters = filters or {}
    have = set(features or [])
    chips = []

    # 一、缩小范围：只推**筛得动**的那些标签
    for tag, phrase in _TAG_CHIPS:
        if tag in have:
            continue
        hit = sum(1 for h in houses if tag in (h.get("tags") or []))
        if 0 < hit < len(houses):
            chips.append(phrase)

    # 二、价格。用户没给上限、而这一组价差明显时才提——
    # 三套都是 2200 元，推「再便宜点的」是无的放矢
    prices = [_num(h.get("rent_price")) for h in houses]
    prices = [p for p in prices if p > 0]
    if (not filters.get("zujia_max") and len(prices) >= 2
            and max(prices) - min(prices) >= 500):
        chips.append("再便宜点的")

    # 三、租赁方式：这一组里不止一种时才值得问
    kinds = {str(h.get("rent_type") or "") for h in houses}
    kinds.discard("")
    if len(kinds) > 1 and "整租" in kinds:
        chips.append("只看整租")

    # 四、**兜底一定要有**：上面三条都不成立时（结果很齐、条件很全），
    # 用户面前不能只剩一个光秃秃的输入框。讲解和对比是现成的能力，
    # 而且正是他看完列表之后最想做的两件事。
    tail = ["介绍下第一套"]
    if len(houses) >= 2:
        tail.append("对比第一套和第二套")
    tail.append("帮我约看房")

    picked = chips[:2] + tail
    # 去重但保持顺序
    return list(dict.fromkeys(picked))[:4]
