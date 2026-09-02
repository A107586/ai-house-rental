"""定价建议（L-10）。

房东发布房源时最难填的一栏就是租金——填高了没人问，填低了自己吃亏。
这里按同区域、同户型的在租房源给出一个参考区间。

三条设计取舍：

**一、用中位数而不是平均值。** 一套定价离谱的房源就能把平均值拉歪，
中位数不会。租金分布本来也是右偏的，均值天然偏高。

**二、按面积折算再比。** 同样是"中山区两室一厅"，60 平和 110 平不是
一回事。所以先算每平米单价，再乘以本房源的面积。

**三、样本不足就明说，不硬给。** 少于三套同类房源时，给出的区间毫无
统计意义。这时候退回更宽的口径（只按区域），并且如实告诉房东参考性有限——
一个看起来精确、实则瞎编的数字比不给更糟。
"""

# 低于这个样本量就不按细口径给建议
MIN_SAMPLE = 3

# 建议区间取中位单价的上下浮动比例
BAND = 0.12


def _median(values):
    ordered = sorted(values)
    n = len(ordered)
    if not n:
        return 0.0
    mid = n // 2
    if n % 2:
        return float(ordered[mid])
    return (float(ordered[mid - 1]) + float(ordered[mid])) / 2


def suggest(houses, district, layout, area):
    """给出定价建议。

    :param houses: 参与比较的在租房源，需含 district / layout /
                   rent_price / area 四个字段
    :returns: ``{"ok", "low", "high", "median", "unit_price",
                 "sample", "basis", "message"}``
    """
    area = float(area or 0)
    if area <= 0:
        return {"ok": False, "sample": 0, "basis": "",
                "message": "先填面积，才能给出定价建议。"}

    def unit_prices(rows):
        out = []
        for h in rows:
            try:
                price = float(h.get("rent_price") or 0)
                size = float(h.get("area") or 0)
            except (TypeError, ValueError):
                continue
            if price > 0 and size > 0:
                out.append(price / size)
        return out

    same = [h for h in houses
            if h.get("district") == district and h.get("layout") == layout]
    # 不写成「中山区的两室一厅」——后面还要接「的 N 套」，
    # 连起来就是「中山区的两室一厅的 3 套」，多一个「的」
    basis = "%s%s" % (district, layout)
    prices = unit_prices(same)

    if len(prices) < MIN_SAMPLE:
        # 退回更宽的口径：只按区域比
        same = [h for h in houses if h.get("district") == district]
        basis = district
        prices = unit_prices(same)

    if len(prices) < MIN_SAMPLE:
        return {
            "ok": False, "sample": len(prices), "basis": basis,
            "message": "%s的在租房源太少（%d 套），给不出有意义的参考区间。"
                       % (basis, len(prices)),
        }

    unit = _median(prices)
    median = unit * area
    low = int(round(median * (1 - BAND) / 50)) * 50
    high = int(round(median * (1 + BAND) / 50)) * 50

    return {
        "ok": True,
        "low": low,
        "high": high,
        "median": int(round(median / 50)) * 50,
        "unit_price": round(unit, 1),
        "sample": len(prices),
        "basis": basis,
        "message": "参考%s的 %d 套在租房源，中位单价 %.1f 元/㎡，"
                   "按 %.0f㎡ 折算，建议定价 %d–%d 元。"
                   % (basis, len(prices), unit, area, low, high),
    }
