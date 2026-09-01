"""工具：报修描述的故障分类与紧急程度判定（AI-04 的核心）。

与找房、预约一样走「规则优先、大模型兜底」的两级策略。
报修描述里的关键词高度集中（漏水、跳闸、不制冷……），
规则分类既准又快，还能保证在没有大模型时系统照常派单。

**紧急程度的判定优先于分类**：一条描述可能同时提到「水管漏水」和
「顺便看看灯」，分类可以取主要项，但只要涉及漏水、燃气、触电这类
安全隐患，就必须判为紧急，不能被次要内容稀释。
"""

# 故障分类 -> 关键词
CATEGORY_KEYWORDS = [
    ("水暖", ["漏水", "水管", "马桶", "下水", "堵了", "堵塞", "水龙头", "花洒",
             "地漏", "暖气", "热水器", "没热水", "水表", "返水", "渗水"]),
    ("电路", ["跳闸", "停电", "漏电", "电闸", "插座", "开关", "电线", "短路",
             "灯不亮", "灯泡", "电表", "没电"]),
    ("家电", ["空调", "冰箱", "洗衣机", "油烟机", "微波炉", "电视", "热水壶",
             "不制冷", "不制热", "不通电", "烘干机"]),
    ("门窗", ["门锁", "锁坏", "关不上", "窗户", "玻璃", "纱窗", "门把手",
             "推拉门", "防盗门", "钥匙"]),
    ("家具", ["桌子", "椅子", "床", "衣柜", "沙发", "柜门", "抽屉", "床板"]),
    ("燃气", ["燃气", "煤气", "天然气", "灶台", "打不着火", "气味"]),
]

# 紧急程度关键词。安全隐患一律最高优先级。
URGENT_KEYWORDS = ["漏水", "漏电", "燃气", "煤气", "天然气", "触电", "冒烟",
                   "起火", "着火", "停电", "没电", "返水", "淹了", "短路",
                   "关不上", "锁坏", "打不着火"]

LOW_KEYWORDS = ["有点", "偶尔", "不急", "有空", "方便的时候", "轻微", "小问题",
                "松了", "有异响"]

# 故障分类 -> 建议维修工种
ASSIGNEE_BY_CATEGORY = {
    "水暖": "水暖工",
    "电路": "电工",
    "家电": "家电维修师傅",
    "门窗": "门窗维修师傅",
    "家具": "木工",
    "燃气": "燃气专员",
    "其他": "综合维修工",
}

URGENCY_LEVELS = ("紧急", "一般", "可延后")


def classify_repair(text):
    """把一段报修描述分析成结构化工单信息。

    返回 dict::

        {
          "category": "水暖",
          "urgency": "紧急",
          "assignee": "水暖工",
          "confidence": 0.0-1.0,
          "matched": {"category": [...], "urgency": [...]},
        }
    """
    empty = {"category": "", "urgency": "", "assignee": "",
             "confidence": 0.0, "matched": {"category": [], "urgency": []}}
    if not text or not text.strip():
        return empty

    raw = text.strip()

    # ---------- 分类 ----------
    scores = {}
    hits = {}
    for category, keywords in CATEGORY_KEYWORDS:
        matched = [kw for kw in keywords if kw in raw]
        if matched:
            scores[category] = len(matched)
            hits[category] = matched

    if scores:
        order = {c: i for i, (c, _) in enumerate(CATEGORY_KEYWORDS)}
        category = min(scores, key=lambda c: (-scores[c], order[c]))
        category_hits = hits[category]
    else:
        category = "其他"
        category_hits = []

    # ---------- 紧急程度 ----------
    urgent_hits = [kw for kw in URGENT_KEYWORDS if kw in raw]
    low_hits = [kw for kw in LOW_KEYWORDS if kw in raw]

    if urgent_hits:
        # 安全隐患不因为用户说「不急」而降级
        urgency = "紧急"
    elif low_hits:
        urgency = "可延后"
    else:
        urgency = "一般"

    # ---------- 置信度 ----------
    confidence = 0.0
    if category_hits:
        confidence += 0.5 + min(len(category_hits) - 1, 2) * 0.1
    if urgent_hits or low_hits:
        confidence += 0.3
    elif category_hits:
        # 分类明确但紧急程度靠默认值，略微降低置信度
        confidence += 0.1

    return {
        "category": category,
        "urgency": urgency,
        "assignee": ASSIGNEE_BY_CATEGORY.get(category, "综合维修工"),
        "confidence": round(min(confidence, 1.0), 2),
        "matched": {"category": category_hits, "urgency": urgent_hits + low_hits},
    }


def describe(result):
    """生成一句面向管理员的分析说明。"""
    if not result["category"]:
        return "无法识别故障类型，请人工分派。"

    parts = ["故障类型判定为「%s」" % result["category"]]
    if result["matched"]["category"]:
        parts.append("依据关键词：%s" % "、".join(result["matched"]["category"]))
    parts.append("紧急程度「%s」" % result["urgency"])
    if result["urgency"] == "紧急" and result["matched"]["urgency"]:
        parts.append("涉及 %s 等安全隐患，建议优先处理"
                     % "、".join(result["matched"]["urgency"][:3]))
    parts.append("建议派单给%s" % result["assignee"])
    return "；".join(parts) + "。"
