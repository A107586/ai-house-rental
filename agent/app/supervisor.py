"""Supervisor 总调度智能体：判断用户意图，路由到对应子智能体。

采用两级策略：

1. **规则分类**——中文意图词高度可枚举，命中即返回，零延迟零成本；
2. **大模型兜底**——规则置信度低于阈值时才调用，未配置大模型则回退到
   规则的最佳猜测（非功能需求 N-07）。

好处是绝大多数请求根本不需要过大模型，既省 Token 也让每条路由规则
都能脱离大模型单独做单元测试。
"""

from app import fusion
import re

from app.config import settings

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
     ["预约", "约看", "看房", "能去看", "安排看", "改约", "取消预约",
      "什么时候能看", "去看看房子"],
     ["周末", "明天", "后天", "这周", "下周"]),
    ("search",
     ["找房", "租房", "房源", "室一厅", "居室", "整租", "合租", "单间",
      "预算", "多少钱一个月"],
     ["有没有", "推荐", "想要"]),
    ("support",
     ["退租", "水电费", "物业费", "物业", "公告", "平台规则", "怎么收费",
      "缴费方式"],
     ["怎么", "如何", "可以吗", "能不能", "为什么"]),
]

# 报修还认一条**句式**：「某某 + 坏掉的样子」。
#
# 词表永远补不完。实测「暖气不热」「厨房灯不亮」全都落进了「没听懂」——
# 它们既没有「坏」也没有「修」，可**没有人会说「我要报修暖气」**，
# 大家说的就是「暖气不热」。
#
# 用句式而不是继续堆词，是因为这一类说法的结构是固定的：
# 一个东西，加一句它现在不工作 的样子。列出后半截（不热/不亮/打不开…）
# 就能覆盖前半截的任意名词——而名词是列不完的。
#
# 只给 repair 用。别的意图没有这么规整的句式，硬套只会误伤。
# 家里的东西。**有限且稳定**——一套房里会坏的东西就这些。
_FIXTURES = ("暖气", "地暖", "空调", "热水器", "燃气", "煤气", "天然气",
             "灯", "灯泡", "水管", "水龙头", "花洒", "马桶", "下水道",
             "门锁", "门", "窗", "窗户", "冰箱", "洗衣机", "电视",
             "油烟机", "插座", "开关", "电闸", "电路", "网", "宽带",
             "厨房", "卫生间", "浴室", "阳台", "屋里", "家里", "房间")

# 它坏掉的样子。
_FAULTS = ("不热", "不亮", "不出水", "不制热", "不制冷", "不上水", "不下水",
           "不通", "不转", "不动", "打不开", "关不上", "合不上", "锁不上",
           "冲不下去", "没热水", "没水", "没电", "漏气", "不好用", "用不了",
           "坏", "响", "滴水", "渗水")

_FAULT_PATTERN = re.compile(
    "(?:%s)[^，。！？,.!?]{0,4}?(?:%s)"
    % ("|".join(_FIXTURES), "|".join(_FAULTS)))


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

系统的规则引擎已经先判了一遍，结果如下，**供你参考**：

{reference}

规则引擎靠关键词表匹配，说法典型时判得准；含糊或换个说法就会落到
unknown。请在它的基础上给出最终判断。

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
        # 报修还认一条**句式**，不只是词表。见 _FAULT_PATTERN 的说明。
        if intent == "repair":
            fault = _FAULT_PATTERN.search(raw)
            if fault and fault.group(0) not in strong_hits:
                strong_hits = strong_hits + [fault.group(0)]
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
    """完整的意图分类：**规则与大模型同时参与**。

    规则先判一遍，结论作为参考写进提示词，由大模型给出最终意图。
    大模型不可用时直接采用规则结论（需求 N-07）。

    返回 ``{"intent","confidence","source","hits","divergence"}``。
    ``source`` 与 ``divergence`` 一起构成可展示的决策过程——答辩时能说清
    「规则判了什么、大模型改成了什么」，比只给一个结论有说服力。
    """
    intent, confidence, hits = classify_by_rules(text)
    rule_result = {"intent": intent, "confidence": confidence}

    def sanitize(raw):
        got = raw.get("intent")
        # 意图决定请求走哪个子智能体，枚举外的值一律丢弃：
        # 路由错了，后面每一步都是错的
        if got not in INTENT_LABELS:
            return None
        try:
            score = float(raw.get("confidence", 0.0))
        except (TypeError, ValueError):
            score = 0.0
        return {"intent": got,
                "confidence": round(max(0.0, min(score, 1.0)), 2)}

    reference = "- 意图：%s（置信度 %.2f）" % (label(intent), confidence)
    if hits:
        reference += "\n- 命中关键词：" + "、".join(str(h) for h in hits[:5])

    final, source, divergence = fusion.fuse(
        rule_result, provider, _CLASSIFY_SYSTEM,
        _CLASSIFY_PROMPT.format(text=text, reference=reference),
        sanitize, guards=(_keep_strong_rule_intent,),
        enabled=settings.fusion_enabled)

    return {"intent": final["intent"], "confidence": final["confidence"],
            "source": source, "hits": hits, "divergence": divergence}


def _keep_strong_rule_intent(rule_result, fused):
    """护栏：规则以高置信度判定的意图不接受改写。

    强关键词（「报修」「预约看房」这类）命中时，规则的判断几乎不会错，
    而意图判错会把请求整个路由到错误的子智能体——用户说要报修，结果
    走进了找房流程，比答得不好严重得多。

    只拦高置信度的情况：规则本来就不确定时，正该让大模型来定。
    """
    if rule_result.get("confidence", 0) < settings.rule_confidence_threshold:
        return fused, None
    if fused.get("intent") == rule_result.get("intent"):
        return fused, None
    changed = fused.get("intent")
    fused["intent"] = rule_result["intent"]
    fused["confidence"] = rule_result["confidence"]
    return fused, "意图保留规则结果「%s」（大模型判为「%s」）" % (
        label(rule_result["intent"]), label(changed))


def label(intent):
    return INTENT_LABELS.get(intent, intent)
