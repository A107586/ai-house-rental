"""工具：把房源的结构化字段拼成一段可读的描述（AI-08 的核心）。

**所有事实都来自字段，一个字都不许编。** 房源描述是要给租客看的，
写了"步行五分钟到地铁"而实际没有，那是虚假宣传，平台要担责任。
所以这里只做一件事：把已有字段组织成通顺的话。

段落顺序按租客真正关心的先后来排：先说在哪、多大、什么户型，
再说配套，最后说适合谁住。倒过来写的话，最要紧的信息被埋在后面。
"""

# 标签 → 描述短语。写不出短语的标签就直接罗列，不硬凑。
TAG_PHRASES = {
    "电梯": "有电梯上下方便",
    "地铁": "邻近地铁出行便利",
    "有阳台": "带阳台，晾晒和采光都不愁",
    "有家具": "家具齐全，拎包即可入住",
    "可做饭": "厨房可正常开火",
    "可养宠物": "可以养宠物",
    "近学校": "临近学校，适合陪读",
    "独立卫浴": "独立卫浴，不用和人共用",
    "南北通透": "南北通透，通风好",
    "精装修": "精装修，室内状况新",
}

# 面积档位 → 适住人群
AREA_HINTS = (
    (0, 40, "面积紧凑，适合单身或情侣居住"),
    (40, 70, "空间够用，适合两人或小家庭"),
    (70, 110, "空间宽敞，适合三口之家"),
    (110, 10 ** 9, "面积充裕，适合多口之家或合住"),
)

ORIENTATION_PHRASES = {
    "南": "朝南，白天光照充足",
    "东南": "东南向，上午采光好",
    "西南": "西南向，下午光照足",
    "东": "朝东，早晨采光好",
    "北": "朝北，夏天不晒",
}


def _area_hint(area):
    for low, high, text in AREA_HINTS:
        if low <= area < high:
            return text
    return ""


def compose(house):
    """生成描述，返回 ``{"text", "sentences", "used_fields"}``。

    ``used_fields`` 列出这段话用到了哪些字段——房东可以据此核对，
    也让"没有编造"这句话是可验证的，而不是一句承诺。
    """
    house = house or {}
    used = []
    sentences = []

    district = (house.get("district") or "").strip()
    community = (house.get("community") or "").strip()
    layout = (house.get("layout") or "").strip()
    rent_type = (house.get("rent_type") or "").strip()
    floor = (house.get("floor") or "").strip()
    orientation = (house.get("orientation") or "").strip()
    tags = [t for t in (house.get("tags") or []) if t]

    try:
        area = float(house.get("area") or 0)
    except (TypeError, ValueError):
        area = 0
    try:
        price = float(house.get("rent_price") or 0)
    except (TypeError, ValueError):
        price = 0

    # ---------- 第一句：在哪、多大、什么户型 ----------
    where = []
    if district:
        where.append(district)
        used.append("district")
    if community:
        where.append(community)
        used.append("community")

    head = "本房源位于" + "".join(where) if where else "本房源"
    if layout:
        head += "，%s" % layout
        used.append("layout")
    if area > 0:
        head += "，建筑面积约 %.0f 平方米" % area
        used.append("area")
    sentences.append(head + "。")

    # ---------- 第二句：楼层与朝向 ----------
    second = []
    if floor:
        # 楼层字段的常见写法是「11/29层」，前面再加「楼层」二字就成了
        # 「楼层11/29层」，读着别扭。带斜杠的直接用原文。
        second.append(floor if "/" in floor else "楼层%s" % floor)
        used.append("floor")
    if orientation:
        # 前端存的是「朝南」，短语表的键是「南」。不归一化的话会退化成
        # 「朝南向」这种病句。
        key = orientation.lstrip("朝").rstrip("向")
        second.append(ORIENTATION_PHRASES.get(key, "朝%s" % key))
        used.append("orientation")
    if second:
        sentences.append("，".join(second) + "。")

    # ---------- 第三句：配套 ----------
    if tags:
        used.append("tags")
        # 前三个有短语的标签展开成句子，**其余一律罗列，不静默丢弃**——
        # 每个标签都是房东填的卖点，少写一个就是少一分成交机会。
        spelled, leftover = [], []
        for tag in tags:
            phrase = TAG_PHRASES.get(tag)
            if phrase and len(spelled) < 3:
                spelled.append(phrase)
            else:
                leftover.append(tag)
        if spelled:
            sentences.append("、".join(spelled) + "。")
        if leftover:
            # 单独成句而不是接在后面：展开的短语里本身带逗号，
            # 再用顿号接下去会读成一句话里套一句话
            sentences.append("另有" + "、".join(leftover) + "。")

    # ---------- 第四句：租金与出租方式 ----------
    money = []
    if price > 0:
        money.append("月租 %d 元" % round(price))
        used.append("rent_price")
    if rent_type:
        money.append("按%s出租" % rent_type)
        used.append("rent_type")
    if money:
        sentences.append("，".join(money) + "。")

    # ---------- 第五句：适合谁住 ----------
    hint = _area_hint(area) if area > 0 else ""
    if hint:
        sentences.append(hint + "。")

    return {
        "text": "".join(sentences),
        "sentences": sentences,
        "used_fields": used,
    }
