"""工具：自然语言 → 结构化房源筛选条件（AI-01 的第一步）。

设计要点
--------
采用「规则解析优先，低置信度再交给大模型」的两级策略：

* 中文找房表达高度模式化（区域 + 户型 + 价格 + 面积 + 特征），
  规则解析既快又准，且**不消耗 Token、不依赖网络**。
* 只有当规则解析抽不出足够条件时，才调用大模型兜底。

这样即使大模型接口不可用，找房功能仍可降级运行（对应非功能需求 N-07），
同时每一条规则都能脱离大模型单独做单元测试（对应可测试性要求）。

输出字段与 `fangwuxinxi` 表一一对应。
"""

import re

from app.cn_number import CN_NUM_CHARS, parse_amount, _token_to_number

# ---------------------------------------------------------------- 区域

# 大连市行政区划，用于精确匹配
DALIAN_DISTRICTS = [
    "中山区", "西岗区", "沙河口区", "甘井子区", "旅顺口区", "金州区",
    "普兰店区", "瓦房店市", "庄河市", "长海县",
]
# 允许用户省略「区」字
_DISTRICT_SHORT = {d[:-1]: d for d in DALIAN_DISTRICTS if d.endswith("区")}

_GENERIC_AREA = re.compile(r"([一-龥]{2,4}(?:区|县|市))")

# ---------------------------------------------------------------- 户型

_SHI = re.compile(r"([" + CN_NUM_CHARS + r"\d])\s*室")
_TING = re.compile(r"([" + CN_NUM_CHARS + r"\d])\s*厅")
_JU = re.compile(r"([" + CN_NUM_CHARS + r"\d])\s*居(?:室)?")
_SINGLE_ROOM = re.compile(r"单间|开间|一居室|单室")

_CN_DIGIT_OUT = {1: "一", 2: "两", 3: "三", 4: "四", 5: "五", 6: "六"}

# ---------------------------------------------------------------- 房源类型

ROOM_TYPES = {
    "整租": ["整租", "整套", "独立租"],
    "合租": ["合租", "分租", "室友"],
    "公寓": ["公寓", "loft", "LOFT"],
    "住宅": ["住宅", "小区房", "普通住宅"],
    "别墅": ["别墅"],
}

# ---------------------------------------------------------------- 特征词

FEATURES = {
    "地铁": ["地铁", "轻轨", "站近", "近站"],
    "电梯": ["电梯"],
    "近学校": ["学校", "学区", "大学", "校区", "上学"],
    "近医院": ["医院"],
    "有阳台": ["阳台"],
    "可做饭": ["做饭", "厨房", "开火"],
    "有家具": ["家具", "拎包入住", "精装"],
    "可养宠物": ["宠物", "养猫", "养狗"],
    "采光好": ["采光", "朝南", "向阳"],
}

_NUM = r"(?:\d+(?:\.\d+)?|[" + CN_NUM_CHARS + r"]+)"

# 价格：区间优先于单边，避免「2000到3000」被拆成两个上限
_PRICE_RANGE = re.compile(
    r"(" + _NUM + r")\s*(万|千)?\s*(?:元|块|块钱|米)?\s*(?:到|至|-|~|—)\s*"
    r"(" + _NUM + r")\s*(万|千)?\s*(?:元|块|块钱)?"
    # 两道断言缺一不可：
    #   (?!\d)  数字必须整个吃掉。少了它，正则会回溯成「60到9」——
    #           后面跟的是「0」而不是「平」，下面那条前瞻就被绕过去了
    #   后一条  跟着面积单位的不是价格。「面积60到90平」原先被当成
    #           租金 60–90 元，那样的条件一套房也搜不出来
    r"(?!\d)"
    r"(?!\s*(?:平方米|平米|平|㎡))"
)
_PRICE_MAX = re.compile(
    r"(?:预算|租金|房租|价格|价位)?\s*(?:不超过|不高于|最多|最高|低于|少于)?\s*"
    r"(" + _NUM + r")\s*(万|千)?\s*(?:元|块|块钱)?\s*(?:以内|以下|之内|左右以内|封顶)"
)
_PRICE_MAX2 = re.compile(
    r"(?:不超过|不高于|最多|最高|低于|少于|预算)\s*"
    r"(" + _NUM + r")\s*(万|千)?\s*(?:元|块|块钱)?"
)
_PRICE_MIN = re.compile(
    r"(" + _NUM + r")\s*(万|千)?\s*(?:元|块|块钱)?\s*(?:以上|起步|起|往上)"
)

# 面积
_AREA_RANGE = re.compile(
    r"(" + _NUM + r")\s*(?:到|至|-|~|—)\s*(" + _NUM + r")\s*(?:平米|平方米|平|㎡)"
)
_AREA_MIN = re.compile(r"(" + _NUM + r")\s*(?:平米|平方米|平|㎡)\s*(?:以上|往上|起)")
_AREA_MAX = re.compile(r"(" + _NUM + r")\s*(?:平米|平方米|平|㎡)\s*(?:以内|以下|之内)")
_AREA_EQ = re.compile(r"(" + _NUM + r")\s*(?:平米|平方米|平|㎡)")


def _norm_digit(token):
    """户型里的单个数字，转成 1-6 的整数。"""
    value = _token_to_number(token)
    if value is None:
        return None
    value = int(value)
    return value if 1 <= value <= 9 else None


def parse_query(text):
    """把一句自然语言解析成结构化筛选条件。

    返回 dict::

        {
          "filters": {...},        # 可直接喂给房源查询接口
          "features": [...],       # 无法用数据库字段表达的偏好，用于结果排序
          "confidence": 0.0-1.0,   # 低于阈值时应交由大模型重新解析
          "matched": [...],        # 命中的规则，便于调试与答辩演示
        }
    """
    if not text or not text.strip():
        return {"filters": {}, "features": [], "confidence": 0.0, "matched": []}

    raw = text.strip()
    filters = {}
    matched = []

    # ---------- 区域 ----------
    area = None
    for district in DALIAN_DISTRICTS:
        if district in raw:
            area = district
            break
    if area is None:
        for short, full in _DISTRICT_SHORT.items():
            # 避免「金州」误配到「金州区」以外的词，要求短名独立出现
            if short in raw:
                area = full
                break
    if area is None:
        generic = _GENERIC_AREA.search(raw)
        if generic:
            area = generic.group(1)
    if area:
        filters["suozaidiqu"] = area
        matched.append("区域")

    # ---------- 户型 ----------
    shi = ting = None
    m = _SHI.search(raw)
    if m:
        shi = _norm_digit(m.group(1))
    m = _TING.search(raw)
    if m:
        ting = _norm_digit(m.group(1))
    if shi is None:
        m = _JU.search(raw)
        if m:
            shi = _norm_digit(m.group(1))
    if shi is None and _SINGLE_ROOM.search(raw):
        shi, ting = 1, 0

    if shi:
        if ting:
            filters["fangwuhuxing"] = "%s室%s厅" % (
                _CN_DIGIT_OUT.get(shi, shi), _CN_DIGIT_OUT.get(ting, ting))
        else:
            filters["fangwuhuxing"] = "%s室" % _CN_DIGIT_OUT.get(shi, shi)
        matched.append("户型")

    # ---------- 租价 ----------
    price_done = False
    m = _PRICE_RANGE.search(raw)
    if m:
        low = parse_amount(m.group(1), m.group(2))
        high = parse_amount(m.group(3), m.group(4))
        if low and high:
            filters["zujia_min"], filters["zujia_max"] = int(min(low, high)), int(max(low, high))
            matched.append("租价区间")
            price_done = True
    if not price_done:
        # 上限与下限**各自独立判断**。原先这两段用 `if not price_done`
        # 串在一起，匹配到上限就再也不看下限了——于是「两千以上三千以下」
        # 只能解析出上限，下限被整段跳过。一句话里同时给出两者很常见。
        for pattern in (_PRICE_MAX, _PRICE_MAX2):
            m = pattern.search(raw)
            if m:
                value = parse_amount(m.group(1), m.group(2))
                if value:
                    filters["zujia_max"] = int(value)
                    matched.append("租价上限")
                    break

        m = _PRICE_MIN.search(raw)
        if m:
            value = parse_amount(m.group(1), m.group(2))
            if value:
                filters["zujia_min"] = int(value)
                matched.append("租价下限")

        # 上下限都拿到时校正一次顺序：「三千以下两千以上」这种倒装
        # 说法也要能用，不能因为写反了就给出一个空区间
        low, high = filters.get("zujia_min"), filters.get("zujia_max")
        if low and high and low > high:
            filters["zujia_min"], filters["zujia_max"] = high, low

    # ---------- 面积 ----------
    m = _AREA_RANGE.search(raw)
    if m:
        low = _token_to_number(m.group(1))
        high = _token_to_number(m.group(2))
        if low and high:
            filters["mianji_min"], filters["mianji_max"] = int(min(low, high)), int(max(low, high))
            matched.append("面积区间")
    else:
        m = _AREA_MIN.search(raw)
        if m:
            value = _token_to_number(m.group(1))
            if value:
                filters["mianji_min"] = int(value)
                matched.append("面积下限")
        else:
            m = _AREA_MAX.search(raw)
            if m:
                value = _token_to_number(m.group(1))
                if value:
                    filters["mianji_max"] = int(value)
                    matched.append("面积上限")
            else:
                # 光说「80平」没说以上还是以下。_AREA_EQ 这条正则原先
                # 定义了却从没被用过，于是这类说法什么也解析不出来。
                #
                # 按「大约 80 平」理解，取上下各 15% 的区间：说 80 平的人
                # 不会因为一套 78 平就不看了，但按下限 80 处理会把它排除。
                m = _AREA_EQ.search(raw)
                if m:
                    value = _token_to_number(m.group(1))
                    if value:
                        filters["mianji_min"] = int(value * 0.85)
                        filters["mianji_max"] = int(value * 1.15)
                        matched.append("面积约值")

    # ---------- 房源类型 ----------
    for name, words in ROOM_TYPES.items():
        if any(w in raw for w in words):
            filters["fangyuanleixing"] = name
            matched.append("房源类型")
            break

    # ---------- 特征偏好 ----------
    features = [name for name, words in FEATURES.items() if any(w in raw for w in words)]

    return {
        "filters": filters,
        "features": features,
        "confidence": _confidence(filters, features, raw),
        "matched": matched,
    }


def _confidence(filters, features, raw):
    """按抽取到的条件数量估算置信度。

    低于 0.5 说明这句话可能根本不是找房需求，或表达超出了规则覆盖范围，
    调用方应转交大模型重新解析。
    """
    score = 0.0
    # 结构化条件权重最高
    if "suozaidiqu" in filters:
        score += 0.3
    if "fangwuhuxing" in filters:
        score += 0.25
    if "zujia_max" in filters or "zujia_min" in filters:
        score += 0.25
    if "mianji_max" in filters or "mianji_min" in filters:
        score += 0.1
    if "fangyuanleixing" in filters:
        score += 0.1
    score += min(len(features), 3) * 0.05

    # 出现明确的找房动词，说明意图清晰
    if re.search(r"找|租|想要|求|看看|有没有|推荐", raw):
        score += 0.1
    return round(min(score, 1.0), 2)
