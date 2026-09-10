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
