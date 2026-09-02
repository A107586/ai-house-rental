"""工具：从浏览足迹与收藏中提炼用户偏好画像（AI-07 的第一步）。

不用向量化，也不训练模型——数据量小的时候统计规律就够了，
而且每一条推荐理由都能说清楚来源，比黑盒打分更适合答辩演示。

**收藏的权重高于浏览。** 点进去看过只说明「有点兴趣」，
收藏才是明确的正向信号。
"""

from collections import Counter

# 收藏比浏览更能代表真实偏好
FAVORITE_WEIGHT = 3
HISTORY_WEIGHT = 1

# 同一房源反复看也是信号，但增长要收敛，看十次不代表喜欢十倍
MAX_VIEW_BONUS = 2


def _weight(item, source):
    if source == "favorite":
        return FAVORITE_WEIGHT
    views = item.get("my_view_count") or 1
    return HISTORY_WEIGHT + min(views - 1, MAX_VIEW_BONUS)


def build_profile(history=None, favorites=None):
    """提炼偏好画像。

    返回 dict::

        {
          "districts": [("中山区", 5), ...],   # 按权重排序
          "layouts":   [("两室一厅", 4), ...],
          "rent_types":[("整租", 6), ...],
          "tags":      [("电梯", 7), ...],
          "price":     {"min":…, "max":…, "avg":…},
          "area":      {"min":…, "max":…, "avg":…},
          "sample":    12,        # 参与统计的房源数
          "confidence": 0.0-1.0,  # 样本太少时不该硬推
        }
    """
    history = history or []
    favorites = favorites or []

    districts, layouts, rent_types, tags = Counter(), Counter(), Counter(), Counter()
    prices, areas = [], []
    total_weight = 0

    for source, items in (("favorite", favorites), ("history", history)):
        for item in items:
            weight = _weight(item, source)
            total_weight += weight
            if item.get("district"):
                districts[item["district"]] += weight
            if item.get("layout"):
                layouts[item["layout"]] += weight
            if item.get("rent_type"):
                rent_types[item["rent_type"]] += weight
            for tag in item.get("tags") or []:
                tags[tag] += weight
            try:
                prices.append(float(item["rent_price"]))
            except (KeyError, TypeError, ValueError):
                pass
            try:
                areas.append(float(item["area"]))
            except (KeyError, TypeError, ValueError):
                pass

    sample = len(history) + len(favorites)
    return {
        "districts": districts.most_common(3),
        "layouts": layouts.most_common(3),
        "rent_types": rent_types.most_common(2),
        "tags": tags.most_common(5),
        "price": _range(prices),
        "area": _range(areas),
        "sample": sample,
        "confidence": _confidence(sample, len(favorites)),
    }


def _range(values):
    if not values:
        return None
    return {
        "min": round(min(values)),
        "max": round(max(values)),
        "avg": round(sum(values) / len(values)),
    }


def _confidence(sample, favorite_count):
    """样本太少时置信度低，调用方应退回「最新房源」而不是硬推。

    看过三套以上才谈得上偏好；有收藏则显著加分。
    """
    if sample == 0:
        return 0.0
    score = min(sample / 6.0, 0.7) + min(favorite_count * 0.1, 0.3)
    return round(min(score, 1.0), 2)


def describe(profile):
    """把画像说成一句人话，用于「为什么推荐这些」。"""
    if profile["sample"] == 0:
        return "还没有浏览记录，先看看最新房源吧。"

    parts = []
    if profile["districts"]:
        parts.append("常看 " + "、".join(d for d, _ in profile["districts"][:2]))
    if profile["layouts"]:
        parts.append(profile["layouts"][0][0])
    if profile["price"]:
        parts.append("租金多在 %d–%d 元" % (profile["price"]["min"],
                                          profile["price"]["max"]))
    if profile["tags"]:
        parts.append("偏好 " + "、".join(t for t, _ in profile["tags"][:3]))

    return "根据你看过的 %d 套房源：%s。" % (profile["sample"], "，".join(parts))
