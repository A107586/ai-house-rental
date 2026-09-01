"""Supervisor 总调度智能体：判断用户意图，路由到对应子智能体。

采用两级策略：

1. **规则分类**——中文意图词高度可枚举，命中即返回，零延迟零成本；
2. **大模型兜底**——规则置信度低于阈值时才调用，未配置大模型则回退到
   规则的最佳猜测（非功能需求 N-07）。

好处是绝大多数请求根本不需要过大模型，既省 Token 也让每条路由规则
都能脱离大模型单独做单元测试。
"""

from app.config import settings
from app.llm.base import LLMUnavailable

# 意图 -> (强特征词, 弱特征词)。顺序即优先级：越靠前的意图越具体。
#
# 强弱之分很关键：「怎么」「如何」这类泛问句词几乎出现在任何提问里，
# 若与「退租」「水电费」同等看待，一句「今天股市怎么样」就会被误判成客服问答，
# 而且置信度足够高、根本不会转交大模型复核。
# 因此规定：**只有命中强特征词的意图才成为候选**，弱特征词仅用于加权区分。
INTENT_KEYWORDS = [
    ("repair",
     ["报修", "漏水", "堵了", "不制冷", "维修", "跳闸", "停电", "没热水",
      "马桶", "水管", "漏电"],
     ["坏了", "修一下", "不好使"]),
    ("contract",
     ["合同", "条款", "违约", "押金退", "退押金", "签约", "租约"],
     ["审阅", "看一下"]),
    ("booking",
     ["预约", "约看", "看房时间", "能去看", "安排看", "改约", "取消预约",
      "什么时候能看"],
     ["周末", "明天", "后天"]),
    ("search",
     ["找房", "租房", "房源", "室一厅", "居室", "整租", "合租", "单间",
      "预算", "多少钱一个月"],
     ["有没有", "推荐", "想要"]),
    ("support",
     ["退租", "水电费", "物业费", "物业", "公告", "平台规则", "怎么收费",
      "缴费方式"],
     ["怎么", "如何", "可以吗", "能不能", "为什么"]),
]

# 意图 -> 面向用户的中文名，用于日志与前端展示
INTENT_LABELS = {
    "search": "找房",
    "booking": "预约看房",
    "contract": "合同审阅",
    "repair": "报修",
    "support": "客服问答",
    "unknown": "无法识别",
}

_CLASSIFY_SYSTEM = "你是房屋租赁平台的意图分类器，只输出 JSON，不要任何解释。"

_CLASSIFY_PROMPT = """把用户这句话归入下列意图之一：

- search：找房、查询房源
- booking：预约看房、修改或取消预约
- contract：租赁合同相关，条款、押金、违约
- repair：房屋报修、设施损坏
- support：平台规则咨询、常见问题
- unknown：与租房无关

用户输入：「{text}」

只输出如下 JSON：
{{"intent": "<上述之一>", "confidence": <0 到 1 的小数>}}"""


_WEAK_WEIGHT = 0.3


def classify_by_rules(text):
    """纯规则意图分类，返回 ``(intent, confidence, hits)``。

    只有命中强特征词的意图才会成为候选；若一句话里只有泛问句词，
    返回 ``unknown`` 且置信度为 0，交由大模型判断。
    """
    if not text or not text.strip():
        return "unknown", 0.0, []

    raw = text.strip()
    scores = {}
    hits = {}
    order = {}
    for index, (intent, strong, weak) in enumerate(INTENT_KEYWORDS):
        strong_hits = [kw for kw in strong if kw in raw]
        if not strong_hits:
            continue
        weak_hits = [kw for kw in weak if kw in raw]
        scores[intent] = len(strong_hits) + len(weak_hits) * _WEAK_WEIGHT
        hits[intent] = strong_hits + weak_hits
        order[intent] = index

    if not scores:
        return "unknown", 0.0, []

    # 得分相同时，按 INTENT_KEYWORDS 中的顺序取更具体的意图
    best = min(scores, key=lambda i: (-scores[i], order[i]))

    score = scores[best]
    if score >= 2:
        confidence = 0.9
    elif score >= 1.3:
        confidence = 0.8
    else:
        confidence = 0.65
    return best, confidence, hits[best]


def classify(text, provider=None):
    """完整的意图分类：规则优先，低置信度时交给大模型。

    返回 ``{"intent","confidence","source","hits"}``，
    其中 ``source`` 标明结论来自 ``rules`` 还是 ``llm``，便于答辩时展示决策过程。
    """
    intent, confidence, hits = classify_by_rules(text)
    if confidence >= settings.rule_confidence_threshold:
        return {"intent": intent, "confidence": confidence,
                "source": "rules", "hits": hits}

    if provider is None or not provider.available:
        return {"intent": intent, "confidence": confidence,
                "source": "rules_fallback", "hits": hits}

    try:
        data = provider.complete_json(
            _CLASSIFY_SYSTEM, _CLASSIFY_PROMPT.format(text=text))
    except LLMUnavailable:
        return {"intent": intent, "confidence": confidence,
                "source": "rules_fallback", "hits": hits}

    if not isinstance(data, dict) or data.get("intent") not in INTENT_LABELS:
        return {"intent": intent, "confidence": confidence,
                "source": "rules_fallback", "hits": hits}

    try:
        llm_confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        llm_confidence = 0.0

    return {
        "intent": data["intent"],
        "confidence": round(max(0.0, min(llm_confidence, 1.0)), 2),
        "source": "llm",
        "hits": hits,
    }


def label(intent):
    return INTENT_LABELS.get(intent, intent)
