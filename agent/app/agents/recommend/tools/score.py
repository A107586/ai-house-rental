"""工具：按偏好画像给候选房源打分（AI-07 的第二步）。

打分是加权求和，每一项都记下命中原因——**推荐必须能解释**。
「因为算法觉得你喜欢」在答辩时说不过去，也没法让用户信服。

权重分配的依据：区域与租金是租房决策里最硬的两个约束，
户型次之，标签属于加分项。
"""

WEIGHTS = {
    "district": 30,
    "price": 25,
    "layout": 20,
    "tags": 15,      # 五个标签均分
    "rent_type": 10,
}

# 已看过的房源要压分，不然推荐位全被看过的占满
SEEN_PENALTY = 0.35


def score_house(house, profile, seen=None):
    """给一套房源打分。

    返回 ``(score, reasons)``，score 为 0–100，reasons 是命中的理由列表。
    """
    seen = seen or set()
    score = 0.0
    reasons = []

    # ---------- 区域 ----------
    districts = dict(profile.get("districts") or [])
    if districts and house.get("district") in districts:
        top = max(districts.values())
        ratio = districts[house["district"]] / top
        score += WEIGHTS["district"] * ratio
        reasons.append("在你常看的 %s" % house["district"])

    # ---------- 租金 ----------
    price_range = profile.get("price")
    price = _num(house.get("rent_price"))
    if price_range and price is not None:
        low, high = price_range["min"], price_range["max"]
        # 容差不能只看区间宽度：用户若只看过同价位的几套，区间会退化成一个点，
        # 那样任何价差都会让分数直接归零。按均价的四分之一兜底。
        tolerance = max(high - low, (price_range["avg"] or 0) * 0.25, 1)
        if low <= price <= high:
            score += WEIGHTS["price"]
            reasons.append("租金 %d 元在你关注的区间内" % price)
        else:
            distance = (low - price) if price < low else (price - high)
            decay = max(0.0, 1 - distance / tolerance)
            score += WEIGHTS["price"] * decay
            if decay > 0.3 and price < low:
                reasons.append("租金 %d 元比你常看的更便宜" % price)

    # ---------- 户型 ----------
    layouts = dict(profile.get("layouts") or [])
    if layouts and house.get("layout") in layouts:
        top = max(layouts.values())
        score += WEIGHTS["layout"] * (layouts[house["layout"]] / top)
        reasons.append("户型 %s 与你的偏好一致" % house["layout"])

    # ---------- 标签 ----------
    liked = dict(profile.get("tags") or [])
    hits = [t for t in (house.get("tags") or []) if t in liked]
    if liked and hits:
        score += WEIGHTS["tags"] * min(len(hits) / 3.0, 1.0)
        reasons.append("有你在意的 " + "、".join(hits[:3]))

    # ---------- 出租方式 ----------
    rent_types = dict(profile.get("rent_types") or [])
    if rent_types and house.get("rent_type") in rent_types:
        score += WEIGHTS["rent_type"]
        reasons.append(house["rent_type"])

    # ---------- 看过的压分 ----------
    if house.get("house_no") in seen:
        score *= SEEN_PENALTY
        reasons.append("你看过这套")

    # 评分高的略微加权，但不能盖过匹配度
    rating = _num(house.get("rating")) or 0
    if rating >= 4:
        score += 3
        reasons.append("租客评分 %.1f" % rating)

    return round(min(score, 100), 1), reasons


def recommend(candidates, profile, seen=None, limit=6, min_score=15):
    """挑出最匹配的若干套。

    分数过低的宁可不推——推一堆不相关的房源比不推更伤信任。
    """
    seen = seen or set()
    scored = []
    for house in candidates or []:
        score, reasons = score_house(house, profile, seen)
        if score >= min_score:
            scored.append({**house, "match_score": score, "match_reasons": reasons})

    scored.sort(key=lambda h: -h["match_score"])
    return scored[:limit]


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
