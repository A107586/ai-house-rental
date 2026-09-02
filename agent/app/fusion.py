"""规则与大模型的融合层。

**架构：规则与大模型同时参与，规则的结论作为输入交给大模型，
由大模型给出最终结果。**

这与「规则优先、大模型兜底」的二选一不同。二选一的问题是：规则置信度
够高时大模型完全不参与，可规则再全也覆盖不了所有说法；置信度不够时又
完全丢掉规则的结论，大模型得从零开始猜。融合的做法是两者都跑，把规则
算出来的结构化结果写进提示词当作**参考答案**，大模型在此基础上补充与
修正——它既拿到了确定的部分，又能处理规则没想到的表达。

三条护栏，缺一不可：

**一、大模型不可用时必须照常工作。** 这是需求 N-07 的原文要求，不是
可选项。没有 Key、网络不通、返回超时——任何一种情况都直接采用规则结论，
整条链路不受影响。项目的全部自动化测试就是在这个模式下跑的。

**二、硬性规则不接受覆盖。** 需求 AI-04 明写「漏水、断电、煤气等关键词
必须判为紧急」。大模型若判成「一般」，这里会改回「紧急」并记一条分歧。
这类规则是需求条款，不是偏好——漏判一次可能出事，不能拿概率去赌。

**三、输出必须能校验。** 大模型返回的字段名会臆造、类型会飘、数值会
超界。全部经过白名单与类型检查，检查不过就整体丢弃、退回规则结论。

融合的结果里始终带着 `rule_result` 与 `divergence`，答辩时能当场展示
「规则说什么、大模型改了什么、为什么」——这比只给一个最终答案有说服力。
"""

import json

from app.llm.base import LLMUnavailable

# source 的三种取值
SOURCE_RULES = "rules"              # 大模型没参与（不可用或被关闭）
SOURCE_FUSED = "fused"              # 两者都参与，采用了融合结果
SOURCE_FALLBACK = "rules_fallback"  # 大模型参与了但输出不可用，退回规则


def describe_source(source):
    return {
        SOURCE_RULES: "规则解析，未调用大模型",
        SOURCE_FUSED: "规则 + 大模型融合",
        SOURCE_FALLBACK: "大模型输出不可用，已退回规则结果",
    }.get(source, source)


def _diff(rule_result, final):
    """列出大模型改动了哪些字段，供界面展示与答辩说明。"""
    changes = []
    for key in sorted(set(rule_result) | set(final)):
        before, after = rule_result.get(key), final.get(key)
        if before == after:
            continue
        if before is None:
            changes.append("补充 %s=%s" % (key, after))
        elif after is None:
            changes.append("移除 %s（原 %s）" % (key, before))
        else:
            changes.append("%s 由 %s 改为 %s" % (key, before, after))
    return changes


def fuse(rule_result, provider, system, prompt, sanitize,
         guards=(), enabled=True):
    """执行一次融合。

    :param rule_result: 规则算出的结构化结果（dict）
    :param provider:    大模型提供方，None 或不可用时直接退回规则结果
    :param system:      系统提示词
    :param prompt:      用户提示词。**调用方需自行把 rule_result 写进去**
                        ——不同任务的呈现方式差别很大，统一格式反而别扭
    :param sanitize:    ``fn(raw_dict) -> dict|None``，白名单与类型校验，
                        返回 None 表示输出不可用
    :param guards:      ``[fn(rule_result, fused) -> (fused, note|None)]``，
                        硬性规则的兜底，在采用融合结果之前逐个执行
    :param enabled:     置 False 可强制走纯规则，便于对照测试

    :returns: ``(final, source, divergence)``
    """
    if not enabled or provider is None or not getattr(provider, "available", False):
        return dict(rule_result), SOURCE_RULES, []

    try:
        raw = provider.complete_json(system, prompt)
    except LLMUnavailable:
        return dict(rule_result), SOURCE_FALLBACK, []
    except Exception:
        # 提供方实现里的任何意外都不该把整条请求带崩
        return dict(rule_result), SOURCE_FALLBACK, []

    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return dict(rule_result), SOURCE_FALLBACK, []

    if not isinstance(raw, dict):
        return dict(rule_result), SOURCE_FALLBACK, []

    cleaned = sanitize(raw)
    if not cleaned:
        return dict(rule_result), SOURCE_FALLBACK, []

    divergence = _diff(rule_result, cleaned)

    # 硬性规则最后过一遍。放在这里而不是提示词里：写进提示词只是「请你
    # 注意」，大模型可以不照做；写在这里是无论如何都改回来。
    for guard in guards:
        cleaned, note = guard(rule_result, cleaned)
        if note:
            divergence.append(note)

    return cleaned, SOURCE_FUSED, divergence


def render_reference(rule_result, labels=None):
    """把规则结论渲染成提示词里的「参考答案」段落。

    空结果也要如实写出来——告诉大模型「规则什么都没解析出来」，
    比不提这一段更有用：它由此知道这是个规则覆盖不到的表达，
    该自己从头判断。
    """
    if not rule_result:
        return "（规则未能解析出任何字段）"
    labels = labels or {}
    lines = []
    for key, value in rule_result.items():
        if value in (None, "", [], {}):
            continue
        lines.append("- %s：%s" % (labels.get(key, key), value))
    return "\n".join(lines) if lines else "（规则未能解析出任何字段）"
