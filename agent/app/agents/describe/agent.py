"""房源描述生成智能体（DescribeAgent）：完成 AI-08。

与 AI-05 经营分析同一套分工：**事实由规则拼，大模型只允许润色措辞**，
且润色后要校验没有引入新的事实。

为什么卡得这么死：房源描述是给租客看的，也是签约前的要约内容。写了
「步行五分钟到地铁」而实际没有，那是虚假宣传，平台要担责任。大模型
最擅长的恰恰是补上这类听着自然、但没有依据的细节。

校验的办法是**检查数字**：原文里的面积、租金、楼层若在改写后变了或丢了，
就说明模型动了事实，直接丢弃改写。这条和 AI-05 用的是同一个判据。
"""

import re

from app.agents.describe.tools.compose import compose
from app.llm.base import LLMUnavailable

_SYSTEM = "你是房屋租赁平台的文案助手。"

_PROMPT = """把下面这段房源描述改写得更通顺自然。

**硬性要求：**
1. 所有数字必须原样保留，一个都不能改、不能省略、不能换算。
2. **不得添加原文没有的信息**。没写的配套、交通、周边一律不许补。
3. 只调整措辞与语序，字数控制在原文的 1.5 倍以内。

原文：
{text}

直接输出改写后的正文，不要加标题或解释。"""

# 改写后不得超过原文的这个倍数。超了基本可以确定是加内容了。
MAX_EXPANSION = 1.6


def _numbers(text):
    return set(re.findall(r"\d+(?:\.\d+)?", text or ""))


def run(house, provider=None, polish=True):
    """生成房源描述。

    :param polish: 是否允许大模型润色。关掉可确保完全由规则生成。
    """
    if house is None:
        return {"intent": "describe", "status": "no_data", "ok": False,
                "message": "没有房源信息，无法生成描述。"}

    result = compose(house)
    text = result["text"]
    # 判据是「用到了几个字段」而不是「文本非空」：字段全空时
    # compose 仍会拼出「本房源。」——那是一句话，但不是一段描述。
    if not result["used_fields"]:
        return {"intent": "describe", "status": "no_data", "ok": False,
                "message": "房源信息太少，先把区域、户型、面积填上。"}

    source = "rules"
    if polish and provider is not None and provider.available:
        try:
            polished = provider.complete(_SYSTEM, _PROMPT.format(text=text))
        except LLMUnavailable:
            polished = None

        if polished and polished.strip():
            candidate = polished.strip()
            # 两道校验：数字不能变，篇幅不能明显变长。
            # 前者防篡改事实，后者防凭空添内容——大模型最爱补的
            # 「交通便利」「周边配套齐全」都不带数字，只能靠长度兜住。
            if (_numbers(text) == _numbers(candidate)
                    and len(candidate) <= len(text) * MAX_EXPANSION):
                text = candidate
                source = "llm_polished"

    return {
        "intent": "describe",
        "status": "ok",
        "ok": True,
        "source": source,
        "text": text,
        "sentences": result["sentences"],
        "used_fields": result["used_fields"],
        "message": "已根据房源信息生成描述，共 %d 句。"
                   % len(result["sentences"]),
    }
