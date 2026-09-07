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
"""

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

    lines = []
    cheap = min(houses, key=lambda h: _num(h.get("rent_price"), 1e9))
    big = max(houses, key=lambda h: _num(h.get("area")))
    if cheap is big:
        lines.append("%s 又便宜又大，这一组里它占优。"
                     % (cheap.get("house_no") or ""))
    else:
        gap = _num(big.get("rent_price")) - _num(cheap.get("rent_price"))
        area_gap = _num(big.get("area")) - _num(cheap.get("area"))
        lines.append(
            "%s 每月便宜 %s 元，%s 多出 %s 平米——多花的钱买的就是这点空间。"
            % (cheap.get("house_no") or "", _money(gap),
               big.get("house_no") or "", _money(area_gap)))
        lines.append("预算紧就看 %s，两个人住、东西多就看 %s。"
                     % (cheap.get("house_no") or "", big.get("house_no") or ""))

    # 各自最拿得出手的一条
    for house in houses:
        good = highlights(house)
        if good:
            lines.append("%s 的长处：%s。" % (house.get("house_no") or "",
                                          good[0]))

    return " ".join(lines), {"rows": rows, "houses": houses}
