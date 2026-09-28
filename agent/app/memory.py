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

import re

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
    "付款": ("yajin_yue", "fukuan_yue"),
    "押几付几": ("yajin_yue", "fukuan_yue"),
}
# 「取消区域限制」「去掉租金」也是整字段取消。原先只认「不限 / 不管」，
# 这句话什么都没清，后面几轮一直带着中山区，怎么找都是 0 套。
CLEAR_PREFIX = ("不限", "不管", "取消", "去掉", "删掉")
CLEAR_SUFFIX = ("不限", "都行", "无所谓", "随便")

# 「不用两室一厅」这类：否定的是**具体的值**，不是「户型」两个字，
# 所以 CLEAR_TARGETS 那张表一条都命中不了。更糟的是 parse_query 照样
# 从这句里解析出 fangwuhuxing=两室一厅——**用户说不要，系统反而给设上了**。
NEGATE_WORDS = ("不用", "不要", "不想要", "不考虑", "去掉", "取消", "别找")

# 「找其他地区」「换个地区」「别的区域」不是地名，是「把区域这个条件去掉」。
# 而 parse_query 的通用兜底是「两到四个汉字 + 区/县/市」，会把它们整段
# 当成区名吃进去，于是回一句「区域改为 找其他地区。」
_OTHER_AREA = re.compile(
    r"(?:其他|其它|别的|另外|另一个|换个|换一个|换)\s*(?:地区|区域|区|城区)")

# 相对调整的触发词
RELATIVE_RULES = (
    # 「有点贵」不能漏：口语里嫌贵，说「有点贵」的比说「太贵」的更多。
    # 注意它不会误撞 pricier 的「贵点」——那是「贵」在前、「点」在后。
    #
    # 「更X」这一组是长对话测试补的：24 轮那个剧本里用户说
    # 「还有更便宜的吗」，表里一条都没命中——`zujia_max` 纹丝不动，
    # 系统把上一轮那套 2950 元的又端了回来。**「更便宜」不含
    # 「再便宜」也不含「便宜点」**，字面上确实一个都对不上。
    ("cheaper", ("再便宜", "更便宜", "便宜点", "便宜一点", "便宜些",
                 "低一点", "低点", "少一点", "降一点", "预算紧",
                 "太贵", "有点贵", "贵了")),
    ("pricier", ("贵一点", "贵点", "更贵", "高一点", "好一点的",
                 "档次高", "太便宜")),
    ("bigger", ("大一点", "大点", "更大", "大些", "宽敞", "再大", "太小")),
    ("smaller", ("小一点", "小点", "更小", "紧凑", "再小", "太大")),
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
        # 「付款方式不限」「区域条件无所谓」：字段名和「不限」之间常夹一个虚的名词
        after = re.sub(r"^(方式|条件|要求|限制|方面|的)+", "",
                       text[index + len(word):])[:3]
        if any(before.endswith(p) for p in CLEAR_PREFIX) \
                or any(after.startswith(s) for s in CLEAR_SUFFIX):
            hit.update(fields)

    # 「找其他地区」= 把区域条件去掉，不是一个叫「其他地区」的区。
    if _OTHER_AREA.search(text):
        hit.update(CLEAR_TARGETS["区域"])

    # 「不用两室一厅」：否定词后面那截**本身就是一个值**，见 negated_values
    hit.update(negated_values(text).keys())
    return hit


# 否定只管到下一个标点。「不要太贵的，三千以内」里被否定的是「太贵的」，
# 不是「三千以内」——原先一路解析到句尾，把用户刚设的租金上限一起清掉了。
_SEGMENT_END = re.compile(r"[，,。；;！!？?、\s]")


def _negated_spans(text):
    """每个否定词管辖的那一截：(否定词起点, 那截的起点, 终点)。"""
    text = text or ""
    spans = []
    for word in NEGATE_WORDS:
        start = text.find(word)
        while start >= 0:
            begin = start + len(word)
            stop = _SEGMENT_END.search(text, begin)
            end = stop.start() if stop else len(text)
            if end > begin:
                spans.append((start, begin, end))
            start = text.find(word, begin)
    return spans


def negated_values(text):
    """被否定的具体值，``{字段: 值}``。交给 parse_query 去认——
    户型、类型、区域、押几付几将来加了新写法，这里自动跟着走。"""
    out = {}
    for _, begin, end in _negated_spans(text):
        out.update(parse_query(text[begin:end]).get("filters", {}))
    return out


def strip_negated(text):
    """去掉「不要两室一厅」这几截之后的句子，给解析器看的是用户**要**的部分。"""
    text = text or ""
    spans = sorted(_negated_spans(text))
    if not spans:
        return text
    kept, cursor = [], 0
    for start, _, end in spans:
        if start >= cursor:
            kept.append(text[cursor:start])
            cursor = end
    kept.append(text[cursor:])
    return "".join(kept)


# 只是在抱怨、没有「再 / 更 / 点」这种明确要调的说法
_COMPLAINTS = {"cheaper": ("太贵", "有点贵", "贵了"), "pricier": ("太便宜",),
               "bigger": ("太小",), "smaller": ("太大",)}
_OP_FIELDS = {"cheaper": {"zujia_max", "zujia_min"}, "pricier": {"zujia_max", "zujia_min"},
              "bigger": {"mianji_min", "mianji_max"}, "smaller": {"mianji_min", "mianji_max"}}


def _complaint_only(op, text):
    words = dict(RELATIVE_RULES).get(op, ())
    hits = [w for w in words if w in (text or "")]
    return bool(hits) and all(w in _COMPLAINTS.get(op, ()) for w in hits)


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
    "yajin_yue": "押金月数",
    "fukuan_yue": "付款周期",
}


def _label(field):
    return LABELS.get(field, field)


UNKNOWN_AREA_NOTE = ("「%s」对应不到平台上的某个区，先不按区域筛。"
                     "房源是按行政区找的（中山区、西岗区、沙河口区、甘井子区、"
                     "旅顺口区、金州区等），告诉我想住哪个区会更准。")

def speaks_up(note):
    """这条说明要不要写进回答正文。

    多数说明（「租金改为 3000」）只是回显，放在卡片旁边就够了；
    这几条说的是**用户提的条件没被执行**，不说在正文里，
    他看到的这批房就是一个解释不了的结果。
    """
    return "对应不到" in note


# 「在高新园区」「去沙河口」——规则解析器的通用兜底会把前面的介词一起吞进来
_AREA_LEADING = ("我公司在", "公司在", "单位在", "上班在", "住在", "我在", "在",
                 "去", "到", "离", "靠近", "近")


def _strip_area(value):
    raw = str(value or "").strip()
    for lead in _AREA_LEADING:
        if raw.startswith(lead) and len(raw) > len(lead):
            return raw[len(lead):]
    return raw


def known_district(value):
    """把一个区名规范成平台认识的写法；认不出返回空串。

    交给规则解析器去认，而不是在这里再维护一张别名表——
    「甘井子」「沙河口」这类省略写法，它已经会了。
    """
    from app.agents.property_finder.tools.parse_query import DALIAN_DISTRICTS
    raw = _strip_area(value)
    if raw in DALIAN_DISTRICTS:
        return raw
    guessed = parse_query(raw)["filters"].get("suozaidiqu")
    return guessed if guessed in DALIAN_DISTRICTS else ""


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

    cleared = clears(text)
    for field in cleared:
        if slots.pop(field, None) is not None:
            notes.append("已取消对%s的限制。" % _label(field))

    if filters is None:
        parsed = parse_query(strip_negated(text))
        filters, feats = parsed["filters"], parsed["features"]

    # **被否定的那个值，不许本轮的解析结果再把它写回来。**
    # 「不用两室一厅」里那个「两室一厅」是被否定的对象，不是新条件；
    # 上游（模型、或没去掉否定的解析）可能照样给出 fangwuhuxing=两室一厅。
    # 只拦**那个值**，不拦整个字段：「不用两室一厅，三室的有吗」的三室要留下。
    # 「不限租金」这类整字段取消的，本轮也不该再设这个字段。
    negated = negated_values(text)
    whole = cleared - set(negated)
    filters = {k: v for k, v in (filters or {}).items()
               if k not in whole and not (k in negated and negated[k] == v)}

    # **不认识的区名不能当筛选条件。**
    #
    # 「公司在高新园区」——模型填 district=高新园区，规则更糟，读成「在高新园区」。
    # 平台的房源按行政区划分，这两个值一套房都筛不出来；更麻烦的是每轮都会
    # 从历史重放条件，这个区名于是**永远卡在上下文里**，之后怎么问都是 0 套，
    # 用户看到的是一连串「没有找到符合条件的房源」，不知道卡在哪。
    # 能规范成已知区名的（「甘井子」→甘井子区）就规范，规范不了的丢掉并说一声。
    area = filters.get("suozaidiqu")
    if area:
        known = known_district(area)
        if known:
            filters["suozaidiqu"] = known
        else:
            filters.pop("suozaidiqu")
            notes.append(UNKNOWN_AREA_NOTE % _strip_area(area))

    # 同一字段后说的覆盖先说的：「换成沙河口」要替换掉中山区
    for key, value in (filters or {}).items():
        if key in slots and slots[key] != value:
            notes.append("%s改为 %s。" % (_label(key), value))
        slots[key] = value

    for feature in (feats or []):
        if feature not in features:
            features.append(feature)

    # 相对调整放在最后：「三千以内，再便宜点」应当基于本轮说的三千来算。
    # 但**抱怨式的说法不叠加在本轮刚说的数上**：「不要太贵的，三千以内」
    # 里「太贵」只是在解释为什么给三千，原先照样再打八折成了 2400。
    said = set(filters or {})
    for op in relative_ops(text):
        if _complaint_only(op, text) and said & _OP_FIELDS.get(op, set()):
            continue
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

    一律用规则解析——历史轮次已经答复过了，没必要为了重建上下文
    再去调一遍大模型。

    **窗口只数「说了条件」的那些轮次。**

    原先是无差别取最近 MAX_TURNS 句，在 24 轮的长会话测试里出了事：
    用户第 3 轮说了「沙河口 / 三千以内 / 两室一厅」，中间问了十几轮
    合同、账单、评价，等到第 22 轮说「还有更便宜的吗」，那三个条件
    早被无关的闲聊挤出了窗口——重放出来的槽位是空的，
    系统只能当他从没说过预算，于是给了一句莫名其妙的
    「建议把租价上限从 9 元提高一些」。

    窗口本来要限的是「一个条件说出来之后还算数多久」。这个「多久」
    该按**他又提了几次条件**来数，而不是按他一共说了几句话——
    问账单、看评价这些轮次既不改需求，也就不该消耗需求的寿命。

    被滤掉的轮次本来就是空操作：没有重置、没有清除、解析不出条件，
    `apply_turn` 对它们什么也不做。所以这里只改变了「谁能留在窗口里」，
    没有改变任何一轮自身的语义，重放依然是纯函数、依然可重现。
    """
    turns = [t for t in (texts or []) if is_reset(t) or continues_search(t)]
    slots, features = {}, []
    for text in turns[-MAX_TURNS:]:
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

    deposit, pay = slots.get("yajin_yue"), slots.get("fukuan_yue")
    if deposit is not None or pay is not None:
        digits = "零一二三四五六七八九"
        say = lambda n: digits[n] if 0 <= n < 10 else str(n)   # noqa: E731
        parts.append("%s%s" % ("押%s" % say(deposit) if deposit is not None else "",
                               "付%s" % say(pay) if pay is not None else ""))

    if features:
        parts.append("偏好" + "、".join(features[:3]))
    return "、".join(parts)


# ------------------------------------------------------------------
# 指代消解
# ------------------------------------------------------------------

# 序数词 → 下标。
#
# **原来是一张写死的表，只到「第五」。** 而搜索一次返回 10 套、
# 界面也列 10 套，助手自己还会说「我最推荐第 6 套」——用户照着这句话
# 回一句「看看第六套」，表里查不到，被当成一次新的搜索，
# 回一句「没听懂你的找房需求」。
#
# 往表里继续加「第六」「第七」不解决问题，只是把墙往后挪：翻页之后
# 列表会到第 20 套、第 30 套。所以改成**解析数字本身**——
# 中文数字和阿拉伯数字都认，多长都认。
_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9}

_ORDINAL_RE = re.compile(r"第\s*([0-9]+|[零一二两三四五六七八九十]+)\s*(?:套|个|条|间|号)?")


def _cn_number(raw):
    """把「十二」「二十」「三」这类写法读成整数。读不出来返回 None。

    只处理 100 以内：**序号超过两位数就不是人在数东西了**，
    多半是把电话号码或价格里的数字误认成了序号。
    """
    raw = (raw or "").strip()
    if raw.isdigit():
        value = int(raw)
        return value if 0 < value <= 99 else None
    if "十" not in raw:
        total = 0
        for ch in raw:
            if ch not in _CN_DIGITS:
                return None
            total = total * 10 + _CN_DIGITS[ch]
        return total or None
    head, _, tail = raw.partition("十")
    tens = _CN_DIGITS.get(head, 1) if head else 1
    ones = 0
    if tail:
        if tail not in _CN_DIGITS:
            return None
        ones = _CN_DIGITS[tail]
    value = tens * 10 + ones
    return value if 0 < value <= 99 else None


def ordinal_indexes(text):
    """一句话里出现的全部序号，按出现顺序返回下标（从 0 起）。

    「最后」「头一」这两个不是数字，单独认。
    """
    raw = text or ""
    found = []
    for match in _ORDINAL_RE.finditer(raw):
        value = _cn_number(match.group(1))
        if value:
            found.append((match.start(), value - 1))
    for word, index in (("头一", 0), ("最后", -1)):
        if word in raw:
            found.append((raw.index(word), index))
    found.sort(key=lambda pair: pair[0])
    return [index for _, index in found]


# 序号越界的哨兵。用一个独有的对象而不是字符串或 None——
# 它要能和「真的没解析出来」区分开，也不可能和任何房源编号相等。
OUT_OF_RANGE = object()

# 「那套」「这个」：有当前房源就指它，否则指上一轮结果里的第一套
DEICTIC = ("那套", "这套", "那个", "这个", "刚才那", "刚说的")


# 房源编号：DL 加四位数字。**用户会直接把编号打出来**——
# 助手自己在每句话里都带着「（DL0041）」，他照抄一个再问下去是最自然的
# 反应。原来只认「第几套」和「这套」，于是「DL0041 评价怎么样」
# 解析不出任何房源，被当成一次新的找房请求。
_HOUSE_NO_RE = re.compile(r"(?<![A-Za-z0-9])(DL\s*[0-9]{3,6})(?![0-9])", re.IGNORECASE)


def house_no_in(text):
    """从一句话里认出房源编号。认不出返回空串。

    大小写和中间的空格都放过：用户打的是「dl 0041」也该认。

    边界不能用 `\b`：中文在 Unicode 下也算词字符，「0041评价」的
    数字和汉字之间**没有词边界**，一加 `\b` 就整句匹配不上——
    而「dl0041评价怎么样」恰恰是最常见的问法。
    """
    match = _HOUSE_NO_RE.search(text or "")
    if not match:
        return ""
    return match.group(1).replace(" ", "").upper()


def resolve_house(text, last_house_nos, current=None):
    """把「第二套」「那套」解析成房源编号。

    上一轮的结果由前端回传——前端本来就持有展示状态，
    智能体不必为了指代消解再存一份可能过期的列表。

    `current` 是用户此刻打开着的那套房。**「这套」优先指它**：
    从房源详情页唤起助手时，屏幕上占满整页的就是这一套，
    这时候把「这套」解释成列表里的第一条，是在跟用户看到的东西对着干。

    序号越界返回 `OUT_OF_RANGE` 而不是 None：两者都是「解析不出来」，
    但**该说的话完全不同**——「第二十套」要回「只有 10 套」，
    没说序号才该退回去问条件。混成一个返回值，上层就分不清了。
    """
    text = text or ""
    # 直接打出来的编号最明确，优先于序号与指代
    typed = house_no_in(text)
    if typed:
        return typed
    indexes = ordinal_indexes(text)
    if indexes:
        if not last_house_nos:
            return None
        try:
            return last_house_nos[indexes[0]]
        except IndexError:
            return OUT_OF_RANGE
    if any(word in text for word in DEICTIC):
        return current or (last_house_nos[0] if last_house_nos else None)
    return None


def is_weak_reference(text, last_house_nos, current=None):
    """这次「这套」是不是**猜**出来的。

    `resolve_house` 有四条路径，只有最后一条是猜：

        打出编号    DL0007        —— 明确
        说了序号    第二套         —— 明确
        有打开的房   current       —— 屏幕上就是它
        **都没有**  列表第一条      —— 猜

    最后这条的问题不是猜得对不对，是**猜完不说**。用户刚看完一段
    讲解，接着问「周边有什么」，拿到的是另一套房的周边——他没有
    任何线索能发现指代跑偏了，只会觉得这个助手前言不搭后语。
    实测里连续三轮「这套」指向了三套不同的房子。

    返回 True 时，调用方该在回答前面点一句「（你说的是 DL00xx）」。
    **猜可以，不告诉不行。**
    """
    if not last_house_nos:
        return False
    if house_no_in(text or "") or ordinal_indexes(text or ""):
        return False
    if current:
        return False
    return any(word in (text or "") for word in DEICTIC)


def resolve_houses(text, last_house_nos):
    """一句话里提到的**所有**序号，按出现顺序返回房源编号。

    对比要两套。`resolve_house` 只返回第一个命中的序号，
    「第一套和第二套哪个好」用它拿到的永远是第一套，
    对比就成了「拿第一套和第一套比」。

    去重是必要的：「第二套比第2套」这种说法在中英混合的序号写法下
    会命中同一位，重复一次会让对比表凭空多出一列。
    """
    if not last_house_nos:
        return []
    seen = []
    for index in ordinal_indexes(text):
        try:
            house_no = last_house_nos[index]
        except IndexError:
            continue
        if house_no and house_no not in seen:
            seen.append(house_no)
    return seen
