"""会话记忆（AI-09 多轮对话记忆）。

设计上有一条刻意的选择：**记忆是推导出来的，不是存下来的。**

数据库里只存原始对话文本，槽位（区域、户型、租金区间……）每次请求都按
同一套规则把历史轮次重放一遍算出来。这样做的好处是解析规则升级之后，
历史会话的理解会跟着一起升级，不会出现「存量槽位与新规则打架」的问题；
代价只是每次多跑几次纯字符串解析，对十轮以内的会话可以忽略。

支持三类多轮语义：

* **累积**——「中山区两室一厅」→「三千以内的」应当合并成三个条件，
  而不是把前面的忘掉重新搜。
* **覆盖与清除**——「换成沙河口」替换区域，「不限租金」清掉价格区间。
* **相对调整**——「再便宜点」「面积大一点」脱离上文没有意义，
  只有在已经有对应槽位时才成立。这是多轮记忆最能体现价值的一类。
"""

from app.agents.property_finder.tools.parse_query import parse_query

# 明确要求从头开始。命中后此前所有槽位作废。
RESET_WORDS = ("重新找", "重新搜", "重新开始", "重来", "换个需求",
               "清空", "重新说", "不找这个了")

# 「不限X」「X不限」「不管X」——把对应槽位清掉
CLEAR_TARGETS = {
    "租金": ("zujia_min", "zujia_max"),
    "价格": ("zujia_min", "zujia_max"),
    "预算": ("zujia_min", "zujia_max"),
    "面积": ("mianji_min", "mianji_max"),
    "户型": ("fangwuhuxing",),
    "区域": ("suozaidiqu",),
    "地区": ("suozaidiqu",),
    "类型": ("fangyuanleixing",),
}
CLEAR_PREFIX = ("不限", "不管")
CLEAR_SUFFIX = ("不限", "都行", "无所谓", "随便")

# 相对调整的触发词
RELATIVE_RULES = (
    # 「有点贵」不能漏：口语里嫌贵，说「有点贵」的比说「太贵」的更多。
    # 注意它不会误撞 pricier 的「贵点」——那是「贵」在前、「点」在后。
    ("cheaper", ("再便宜", "便宜点", "便宜一点", "便宜些", "低一点", "低点",
                 "少一点", "降一点", "预算紧", "太贵", "有点贵", "贵了")),
    ("pricier", ("贵一点", "贵点", "高一点", "好一点的", "档次高", "太便宜")),
    ("bigger", ("大一点", "大点", "大些", "宽敞", "再大", "太小")),
    ("smaller", ("小一点", "小点", "紧凑", "再小", "太大")),
)

# 每次相对调整的幅度。等比而非固定值：预算 2000 和 20000 的用户，
# 心里的「便宜一点」显然不是同一个数。
PRICE_STEP_DOWN = 0.8
PRICE_STEP_UP = 1.25
AREA_STEP_DOWN = 0.8
AREA_STEP_UP = 1.25

# 会话最多回溯的轮数。再往前的内容与当前需求基本无关，
# 还会让重放的开销和误合并的风险一起变大。
MAX_TURNS = 10


def is_reset(text):
    return any(word in (text or "") for word in RESET_WORDS)


def clears(text):
    """识别要清除哪些槽位，返回字段名集合。

    只认「不限X」「X不限」这类明确的取消说法。若只要出现「租金」两字
    就清空价格区间，那「租金三千以内」这种正常表述会被误伤。
    """
    text = text or ""
    hit = set()
    for word, fields in CLEAR_TARGETS.items():
        if word not in text:
            continue
        index = text.index(word)
        before = text[max(0, index - 2):index]
        after = text[index + len(word):index + len(word) + 3]
        if any(before.endswith(p) for p in CLEAR_PREFIX) \
                or any(after.startswith(s) for s in CLEAR_SUFFIX):
            hit.update(fields)
    return hit


def relative_ops(text):
    """识别相对调整指令，返回操作名列表。"""
    text = text or ""
    return [name for name, words in RELATIVE_RULES
            if any(word in text for word in words)]


def _round_money(value):
    """租金取整到百元。算出 2399.9 这种数字既不像人话也不好看。"""
    return max(int(round(value / 100.0)) * 100, 100)


def _round_area(value):
    return max(int(round(value / 5.0)) * 5, 10)


def apply_relative(slots, op):
    """按相对指令调整槽位，返回 ``(是否成功, 说明)``。

    对应槽位不存在时**不猜**：「再便宜点」在没说过预算时无从谈起，
    硬编一个区间只会让用户莫名其妙。
    """
    if op == "cheaper":
        if slots.get("zujia_max"):
            slots["zujia_max"] = _round_money(slots["zujia_max"] * PRICE_STEP_DOWN)
            # 下限会挡住更便宜的房源，与「再便宜点」的诉求直接冲突
            slots.pop("zujia_min", None)
            return True, "预算上限下调到 %d 元" % slots["zujia_max"]
        return False, "还没说过预算，直接说个数吧，比如「三千以内」。"

    if op == "pricier":
        if slots.get("zujia_max"):
            slots["zujia_max"] = _round_money(slots["zujia_max"] * PRICE_STEP_UP)
            return True, "预算上限放宽到 %d 元" % slots["zujia_max"]
        return False, "还没说过预算，直接说个数吧。"

    if op == "bigger":
        if slots.get("mianji_min"):
            slots["mianji_min"] = _round_area(slots["mianji_min"] * AREA_STEP_UP)
        elif slots.get("mianji_max"):
            slots["mianji_min"] = _round_area(slots["mianji_max"])
            slots.pop("mianji_max", None)
        else:
            return False, "还没说过面积要求，可以说「80 平以上」。"
        return True, "面积下限提到 %d 平" % slots["mianji_min"]

    if op == "smaller":
        if slots.get("mianji_max"):
            slots["mianji_max"] = _round_area(slots["mianji_max"] * AREA_STEP_DOWN)
        elif slots.get("mianji_min"):
            slots["mianji_max"] = _round_area(slots["mianji_min"])
            slots.pop("mianji_min", None)
        else:
            return False, "还没说过面积要求，可以说「60 平以内」。"
        return True, "面积上限降到 %d 平" % slots["mianji_max"]

    return False, ""


LABELS = {
    "suozaidiqu": "区域",
    "fangwuhuxing": "户型",
    "fangyuanleixing": "房源类型",
    "zujia_min": "租金下限",
    "zujia_max": "租金上限",
    "mianji_min": "面积下限",
    "mianji_max": "面积上限",
}


def _label(field):
    return LABELS.get(field, field)


def apply_turn(slots, features, text, filters=None, feats=None):
    """把一轮对话作用到当前槽位上，返回 ``(slots, features, notes)``。

    :param filters: 本轮已解析好的条件。传入可复用上层（可能走了大模型）
                    的解析结果；不传则用规则解析。
    """
    slots = dict(slots or {})
    features = list(features or [])
    notes = []

    if is_reset(text):
        return {}, [], ["已清空之前的条件，重新开始。"]

    for field in clears(text):
        if slots.pop(field, None) is not None:
            notes.append("已取消对%s的限制。" % _label(field))

    if filters is None:
        parsed = parse_query(text)
        filters, feats = parsed["filters"], parsed["features"]

    # 同一字段后说的覆盖先说的：「换成沙河口」要替换掉中山区
    for key, value in (filters or {}).items():
        if key in slots and slots[key] != value:
            notes.append("%s改为 %s。" % (_label(key), value))
        slots[key] = value

    for feature in (feats or []):
        if feature not in features:
            features.append(feature)

    # 相对调整放在最后：「三千以内，再便宜点」应当基于本轮说的三千来算
    for op in relative_ops(text):
        _, note = apply_relative(slots, op)
        if note:
            notes.append(note)

    return slots, features, notes


def continues_search(text):
    """本轮是不是在承接上文的找房需求。

    多轮对话里**意图本身也需要上下文**：「三千以内的」「再便宜点」
    单独看都不像找房，可一旦上一轮在找房，它们就只可能是在调条件。
    没有这一步，记忆写得再好也没机会生效。

    判据要收紧到「确实说出了可执行的条件变化」，否则「今天天气不错」
    也会被当成在改需求。
    """
    if not text:
        return False
    if relative_ops(text) or clears(text):
        return True
    parsed = parse_query(text)
    return bool(parsed["filters"] or parsed["features"])


def replay(texts):
    """重放历史轮次，得到进入本轮之前的槽位。

    只取最近 MAX_TURNS 轮，且一律用规则解析——历史轮次已经答复过了，
    没必要为了重建上下文再去调一遍大模型。
    """
    slots, features = {}, []
    for text in (texts or [])[-MAX_TURNS:]:
        slots, features, _ = apply_turn(slots, features, text)
    return slots, features


def describe(slots, features):
    """把当前槽位说成一句人话，用于告诉用户「我现在记住了什么」。"""
    if not slots and not features:
        return ""
    parts = []
    if slots.get("suozaidiqu"):
        parts.append(slots["suozaidiqu"])
    if slots.get("fangwuhuxing"):
        parts.append(slots["fangwuhuxing"])
    if slots.get("fangyuanleixing"):
        parts.append(slots["fangyuanleixing"])

    low, high = slots.get("zujia_min"), slots.get("zujia_max")
    if low and high:
        parts.append("租金 %d–%d 元" % (low, high))
    elif high:
        parts.append("租金 %d 元以内" % high)
    elif low:
        parts.append("租金 %d 元以上" % low)

    amin, amax = slots.get("mianji_min"), slots.get("mianji_max")
    if amin and amax:
        parts.append("面积 %d–%d 平" % (amin, amax))
    elif amin:
        parts.append("面积 %d 平以上" % amin)
    elif amax:
        parts.append("面积 %d 平以内" % amax)

    if features:
        parts.append("偏好" + "、".join(features[:3]))
    return "、".join(parts)


# ------------------------------------------------------------------
# 指代消解
# ------------------------------------------------------------------

ORDINALS = (("第一", 0), ("第二", 1), ("第三", 2), ("第四", 3), ("第五", 4),
            ("第1", 0), ("第2", 1), ("第3", 2), ("第4", 3), ("第5", 4),
            ("头一", 0), ("最后", -1))

# 「那套」「这个」指代上一轮结果里的第一套
DEICTIC = ("那套", "这套", "那个", "这个", "刚才那", "刚说的")


def resolve_house(text, last_house_nos):
    """把「第二套」「那套」解析成房源编号。

    上一轮的结果由前端回传——前端本来就持有展示状态，
    智能体不必为了指代消解再存一份可能过期的列表。
    """
    if not last_house_nos:
        return None
    text = text or ""
    for word, index in ORDINALS:
        if word in text:
            try:
                return last_house_nos[index]
            except IndexError:
                return None
    if any(word in text for word in DEICTIC):
        return last_house_nos[0]
    return None
