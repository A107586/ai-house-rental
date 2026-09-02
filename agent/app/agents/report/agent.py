"""经营分析智能体（ReportAgent）：完成 AI-05。

从 `/api/stats` 取数生成经营分析报告。

需求 AI-05 的验收标准写明：**报告中所有数字必须可追溯到数据库记录，
不允许出现模型编造的数据。** 因此这里的分工是：

* 数字与结论一律由规则计算，大模型碰不到；
* 大模型只在配置了 Key 时被允许把结论**改写得更顺**，
  且改写后要校验关键数字仍在文本里，否则丢弃改写、用原文。

这条校验是硬约束——大模型很容易在润色时把「出租率 12%」写成
「出租率约一成半」，那就违反可追溯性了。
"""

import re

from app.agents.report.tools.analyze import analyze, to_text
from app.llm.base import LLMUnavailable

_SYSTEM = "你是房屋租赁平台的经营分析助手。"

_PROMPT = """把下面这份经营分析改写得更连贯易读。

**硬性要求：所有数字必须原样保留，一个都不能改、不能省略、不能换算。**
不要添加原文没有的结论或数据。只调整措辞与段落衔接。

原文：
{text}

直接输出改写后的正文，不要加标题或解释。"""


def _numbers(text):
    """抽出文本里的所有数字，用于校验改写是否篡改了数据。"""
    return set(re.findall(r"\d+(?:\.\d+)?", text or ""))


def run(stats, provider=None, polish=True):
    """生成经营分析报告。

    :param polish: 是否允许大模型润色。关掉可以确保完全由规则生成。
    """
    if not stats:
        return {
            "intent": "report", "status": "no_data", "ok": False,
            "message": "没有拿到统计数据，无法生成报告。",
        }

    result = analyze(stats)
    text = to_text(result)
    source = "rules"

    if polish and provider is not None and provider.available:
        try:
            polished = provider.complete(_SYSTEM, _PROMPT.format(text=text))
        except LLMUnavailable:
            polished = None

        if polished and polished.strip():
            # 关键数字必须一个不少地留在改写后的文本里，
            # 否则说明大模型做了换算或省略，直接丢弃改写
            if _numbers(text) <= _numbers(polished):
                text = polished.strip()
                source = "llm_polished"

    return {
        "intent": "report",
        "status": "ok",
        "ok": True,
        "source": source,
        "sections": result["sections"],
        "highlights": result["highlights"],
        "alerts": result["alerts"],
        "text": text,
        "message": "已生成经营分析报告，共 %d 个部分。" % len(result["sections"]),
    }
