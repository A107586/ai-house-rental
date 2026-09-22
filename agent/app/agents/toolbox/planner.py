# -*- coding: utf-8 -*-
"""工具选择器（G1）。

一次大模型调用，回答一个问题：**这句话该调哪个工具、参数是什么。**

这里有三条自我约束，都是从前面那些 bug 里长出来的：

1. **拿不准就返回 none。** 提示词里明写了这一条，`none` 也是一个正经
   选项而不是失败态。模型硬挑一个的代价，正是「看看第六套」变成一次
   新的找房请求那种——**答非所问比说「没听懂」更糟**，
   因为用户看不出它误解了什么。

2. **不让模型自己编房源编号。** 用户说「第三套」时，模型该填的是
   `ordinal: 3`，编号由代码从上一轮列表里查。让它猜 DL 开头的编号，
   它会猜出一个存在但不相干的房子——而那看起来完全正常。

3. **超时就走规则。** `llm_timeout` 默认 8 秒，超时抛
   `LLMUnavailable`，调用方退回原来的关键词链路。规则链路是完整可用的，
   不是残次品。
"""

import json
import os

from app.agents.toolbox import catalog
from app.llm.base import LLMUnavailable


def _max_tokens():
    """选择器这一次调用的输出额度。

    **它是总额度，思考也算在里面。** 150 是给不思考的模型定的：
    输出只有一小段 JSON，够用且省时（实测 400 压到 60，同一句话
    从 4.6 秒降到 3.7 秒，答案一字没变）。

    但接**会思考的模型**时 150 是致命的：思考先把额度吃光，
    `content` 返回空串，`extract_json` 解析失败，于是每一次都走
    `tool="none"` 退回规则——而 `/health` 仍然显示 healthy=true、
    failures=0，从监控上完全看不出大模型没在工作。

    所以做成可配：不思考的模型保持 150，开思考时要给到
    「思考预算 + JSON」以上，否则等于没接。
    """
    try:
        return max(32, int(os.environ.get("LLM_PLANNER_MAX_TOKENS", "150")))
    except (TypeError, ValueError):
        return 150

_SYSTEM = "你是租房平台助手的工具选择器。只输出一个 JSON 对象，不要任何解释。"

_PROMPT = """用户对助手说了一句话。从下面的工具里挑**一个**最合适的，
并填好参数。

可用工具：
{catalog}

{context}

用户这句话：{text}

输出格式（严格 JSON，不要代码块）：
{{"tool": "工具名", "params": {{...}}, "confidence": 0.0到1.0, "why": "一句话理由"}}

规则：
1. 拿不准就输出 tool 为 "none"，confidence 给 0。**宁可退回去，
   也不要挑一个看起来差不多的**——挑错的话用户会得到一个和他的问题
   毫无关系的回答，而他看不出哪里出了错。
2. 用户说「第三套」「第 6 套」「最后那套」时，填 ordinal（数字，从 1 开始），
   **不要自己编 house_no**。只有用户原话里出现了 DL 开头的编号才填 house_no。
3. params 里只放你有把握的字段，其余的**不要出现**，也不要填 null 或空串。
4. 一句话里同时有条件和动作时（「帮我约第二套看房」），选动作那个工具。
5. 「我约的看房」是 my_appointments，「谁约了我的房」是 landlord_appointments，
   两者不要混。
"""


def _context_block(house_no, last_house_nos, context_text):
    """告诉模型「现在屏幕上有什么」。

    **只给编号和条数，不给整份房源明细。** 给明细的话模型会忍不住
    直接从里面编答案，而房源字段必须由代码从库里取——
    金额、评分错一个，整段话就从「懂行」变成「不可信」。
    """
    lines = []
    if context_text:
        lines.append("已记住的检索条件：%s" % context_text)
    if last_house_nos:
        lines.append("上一轮列出了 %d 套房，序号 1 到 %d（编号依次为 %s）"
                     % (len(last_house_nos), len(last_house_nos),
                        "、".join(last_house_nos[:10])))
    if house_no:
        lines.append("用户此刻正打开着房源 %s，他说「这套」就是指它" % house_no)
    if not lines:
        return "当前没有上下文：用户刚开始说话。"
    return "当前上下文：\n" + "\n".join("- " + x for x in lines)


def _coerce(value, kind):
    """把模型给的值收成 schema 说的类型。收不了返回 None。

    模型经常把数字写成字符串（"3000"）、把单元素写成裸值而不是数组。
    这些都是**格式问题不是意图问题**，在这里悄悄修掉比退回重来划算；
    但类型对不上又修不了的（把区名填进 rent_max），一律丢弃——
    带着一个坏参数去查库，得到的空结果会被当成「没有符合的房源」。
    """
    if value is None or value == "":
        return None
    if kind == "string":
        return str(value).strip() or None
    if kind == "number":
        try:
            num = float(value)
        except (TypeError, ValueError):
            return None
        return int(num) if num == int(num) else num
    if kind.endswith("[]"):
        inner = kind[:-2]
        items = value if isinstance(value, list) else [value]
        out = [_coerce(v, inner) for v in items]
        out = [v for v in out if v is not None]
        return out or None
    return None


def _clean(tool, params):
    """按 schema 过一遍参数：认识的留下、不认识的丢掉。

    **不认识就丢，不是报错。** 模型多给一个字段是常事，为此整轮失败
    不值得；而多出来的字段如果原样传给后端，就成了一个谁也没审过的
    输入通道。
    """
    spec = tool.get("params") or {}
    out = {}
    for key, raw in (params or {}).items():
        if key not in spec:
            continue
        value = _coerce(raw, spec[key][0])
        if value is not None:
            out[key] = value
    return out


def plan(text, provider, logged_in=False, role="", house_no="",
         last_house_nos=None, context_text=""):
    """挑一个工具。返回 ``{"tool", "params", "confidence", "why"}``。

    模型不可用、超时、输出不是 JSON、挑了一个不存在的工具——
    这四种情况**一律返回 tool="none"**，由调用方退回规则链路。
    对上层来说它们没有区别，都是「这一层没接住」。
    """
    empty = {"tool": "none", "params": {}, "confidence": 0.0, "why": ""}
    if provider is None or not getattr(provider, "available", False):
        return dict(empty, why="未配置大模型")

    tools = catalog.visible_tools(logged_in, role)
    prompt = _PROMPT.format(
        catalog=catalog.render_catalog(tools),
        context=_context_block(house_no, last_house_nos or [], context_text),
        text=(text or "").strip()[:300])

    try:
        # 输出只有一小段 JSON，150 足够。给 400 的话，**推理型模型会
        # 拿这个额度去想**——实测 max_tokens 从 400 压到 60，
        # 同一句话从 4.6 秒降到 3.7 秒，答案一字没变
        data = provider.complete_json(_SYSTEM, prompt,
                                      max_tokens=_max_tokens())
    except LLMUnavailable as exc:
        return dict(empty, why="大模型不可用：%s" % exc)
    except Exception as exc:                                 # noqa: BLE001
        # 兜住一切。**这一层挂掉不该让整个对话挂掉**——
        # 它后面还有一条完整的规则链路
        return dict(empty, why="选择器异常：%s" % type(exc).__name__)

    if not isinstance(data, dict):
        return dict(empty, why="输出不是 JSON 对象")

    name = str(data.get("tool") or "").strip()
    tool = catalog.BY_NAME.get(name)
    if tool is None or name == "none":
        return dict(empty, why=str(data.get("why") or "")[:120] or "未选中工具")

    # 选了一个当前身份用不上的工具（未登录却选了 pay_bill）。
    # 这不是模型的错，是它没有身份信息——但结果一样不能用
    if tool not in tools:
        return dict(empty, why="所选工具在当前身份下不可用")

    try:
        confidence = float(data.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0.0

    return {
        "tool": name,
        "params": _clean(tool, data.get("params")),
        "confidence": max(0.0, min(1.0, confidence)),
        "why": str(data.get("why") or "")[:120],
    }


def describe(decision):
    """给 trace 用的一行说明。答辩时要能当场展示「它为什么调了这个」。"""
    return "工具选择：%s（把握 %.0f%%）%s" % (
        decision.get("tool") or "none",
        (decision.get("confidence") or 0) * 100,
        ("——" + decision["why"]) if decision.get("why") else "")


def dump(decision):
    return json.dumps(decision, ensure_ascii=False)
