"""带看纪要智能体（ViewingAgent）：完成 J-09-c。

中介带完看，口述或简写几句，这里把它整理成结构化纪要。

**这段输出有三个下游**，所以它不是一个「顺手做的小功能」：

1. 房东看得懂的纪要——他把带看委托出去了，这是他唯一能知道
   「今天来的人怎么想」的渠道；
2. 房源的「常见异议」聚合——同一套房被三个人嫌厨房小，
   房东就该降价或在描述里写清楚了；
3. 租客画像，喂给 AI-07 推荐。

正因为有下游，标签必须**归一化**：原样存中介写的词，
三个人嫌厨房小会变成三条各出现一次的异议，等于没统计。
归一化在规则层做（`tools/extract.py`），大模型只能在既定标签里挑。

沿用 `app/fusion.py` 的三条护栏，不新造融合逻辑：
大模型不可用照常工作、硬规则不接受覆盖、输出必须能校验。
"""

from app import fusion
from app.agents.viewing.tools.extract import (CONCERN_KEYWORDS, INTENTIONS,
                                              POSITIVE_KEYWORDS,
                                              QUESTION_KEYWORDS, compose,
                                              extract)
from app.config import settings

_SYSTEM = "你是房屋租赁平台的带看纪要整理员，只输出 JSON，不要任何解释。"

_CONCERN_LABELS = [label for label, _ in CONCERN_KEYWORDS]
_POSITIVE_LABELS = [label for label, _ in POSITIVE_KEYWORDS]
_QUESTION_LABELS = [label for label, _ in QUESTION_KEYWORDS]

_PROMPT = """把下面这段带看口述整理成结构化纪要。

口述原文：「{text}」

**标签只能从下面这三张表里选，不许自己造词。**
造出来的新标签没法和别人的记录聚合到一起，等于白填。

顾虑可选：{concerns}
满意点可选：{positives}
现场问题可选：{questions}

意向只能是：high（当场就想定）/ medium（要回去考虑）/ low（明确不合适）

系统的规则引擎已经先抽了一遍，结果如下，**供你参考**：

{reference}

规则靠关键词表匹配，说法常见时判得准；换个说法就会漏。
请在它的基础上补充与修正。

只输出如下 JSON：
{{"showed_up": true/false, "intention": "<意向>",
  "concerns": ["<标签>"], "positives": ["<标签>"], "questions": ["<标签>"],
  "summary": "<两三句话的纪要，写给房东看>"}}"""

_LABELS = {"showed_up": "是否到场", "intention": "意向",
           "concerns": "顾虑", "positives": "满意点", "questions": "现场问题"}


def _pick(values, allowed):
    """只保留白名单里的标签，去重并保持顺序。

    大模型很爱把「厨房太小」当成一个新标签交回来。丢掉而不是收下——
    收下就等于让统计口径由每一次生成决定。
    """
    out = []
    for item in values or []:
        text = str(item).strip()
        if text in allowed and text not in out:
            out.append(text)
    return out


def analyze(text, provider=None):
    """整理一段口述，返回 ``(result, source)``。"""
    rule_result = extract(text)

    def sanitize(raw):
        intention = str(raw.get("intention") or "").strip()
        if intention not in INTENTIONS:
            return None
        summary = str(raw.get("summary") or "").strip()
        if not summary:
            return None
        return {
            "showed_up": bool(raw.get("showed_up", True)),
            "intention": intention,
            "concerns": _pick(raw.get("concerns"), _CONCERN_LABELS),
            "positives": _pick(raw.get("positives"), _POSITIVE_LABELS),
            "questions": _pick(raw.get("questions"), _QUESTION_LABELS),
            "summary": summary[:300],
            "matched": rule_result["matched"],
        }

    reference = fusion.render_reference(
        {k: rule_result[k] for k in
         ("showed_up", "intention", "concerns", "positives", "questions")},
        _LABELS)

    final, source, divergence = fusion.fuse(
        rule_result, provider, _SYSTEM,
        _PROMPT.format(text=text, reference=reference,
                       concerns="、".join(_CONCERN_LABELS),
                       positives="、".join(_POSITIVE_LABELS),
                       questions="、".join(_QUESTION_LABELS)),
        sanitize, guards=(_never_invent_attendance,),
        enabled=settings.fusion_enabled)

    if divergence:
        final = dict(final, divergence=divergence)
    return final, source


def _never_invent_attendance(rule_result, fused):
    """护栏：原话明说了没来，大模型不得改成到场。

    到场与否是这份纪要里唯一**会影响别人**的字段——放鸽子率算在租客
    头上，不是中介头上。判错了是在冤枉人。

    方向是单向的：规则说「没来」就一定是没来（那是原话里写着的，
    不是推断）；规则说「来了」而大模型判成没来，则允许——
    默认值就是到场，大模型能改成没来说明它读出了规则漏掉的说法。
    """
    if not rule_result.get("showed_up") and fused.get("showed_up"):
        fused["showed_up"] = False
        fused["intention"] = "low"
        return fused, "到场情况保留规则结果（原话里明说了未到场，不接受改写）"
    return fused, None


def run(text, provider=None):
    """带看纪要整理入口。"""
    if not text or not text.strip():
        return {
            "intent": "viewing_summary", "status": "need_content", "ok": False,
            "message": "写几句带看情况吧，比如「嫌厨房小，问了能不能养猫」。",
        }

    result, source = analyze(text, provider=provider)

    # 大模型没参与时，summary 用规则拼的那一句。
    # **不能留空**：留空的话，没有大模型的环境里这个功能就等于不存在，
    # 而降级要求（N-07）说的是「照常工作，只是少一点」
    if not result.get("summary"):
        result["summary"] = compose(
            result.get("showed_up", True), result.get("intention", "medium"),
            result.get("concerns", []), result.get("positives", []),
            result.get("questions", []), text)

    return {
        "intent": "viewing_summary",
        "status": "ok",
        "ok": True,
        "source": source,
        "source_label": fusion.describe_source(source),
        "record": {
            "showed_up": result.get("showed_up", True),
            "intention": result.get("intention", "medium"),
            "concerns": result.get("concerns", []),
            "positives": result.get("positives", []),
            "questions": result.get("questions", []),
            "feedback": result["summary"],
        },
        "divergence": result.get("divergence", []),
        "message": "已整理成纪要，核对无误后提交。",
    }
