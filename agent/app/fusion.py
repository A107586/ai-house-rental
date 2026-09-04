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
SOURCE_RULES = "rules"              # 大模型没参与（不可用、被关闭，或规则已经足够确定）
SOURCE_FUSED = "fused"              # 两者都参与，采用了融合结果
SOURCE_FALLBACK = "rules_fallback"  # 大模型参与了但输出不可用，退回规则


def describe_source(source):
    return {
        SOURCE_RULES: "规则解析，未调用大模型",
        SOURCE_FUSED: "规则 + 大模型融合",
        SOURCE_FALLBACK: "大模型输出不可用，已退回规则结果",
    }.get(source, source)


class _NullStep(object):
    """不传 trace 时的空实现。

    这样上面的代码只写一份，不用到处 `if trace:`——散落的判空是
    最容易漏掉一处的写法。
    """

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def says(self, detail):
        return self


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
         guards=(), enabled=True, trace=None, confidence=None,
         confidence_threshold=None):
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
    :param trace:       可选的 `app.trace.Trace`，记下大模型这一步的耗时
                        与结果。**失败也记**——真正要查的恰恰是出问题那次
    :param confidence:  规则解析的置信度。够高就直接短路，不调大模型（K-10）
    :param confidence_threshold: 短路阈值，通常取自 `settings`

    :returns: ``(final, source, divergence)``
    """
    if not enabled or provider is None or not getattr(provider, "available", False):
        return dict(rule_result), SOURCE_RULES, []

    # ---- K-10 规则足够确定时直接短路，不等大模型 ----
    #
    # 融合模式原本对每一次请求都调一次大模型，**哪怕规则已经百分之百
    # 确定**。用户问一句「有没有两室的」，规则 5 毫秒就能答，
    # 却要为此等一趟网络往返；赶上大模型慢或超时，就是几十秒。
    #
    # 「慢」在助手类产品里不是体验问题，是**功能问题**：等三秒的助手，
    # 用户第二次就自己去点筛选栏了。
    #
    # 短路的判据用的是已有的 `rule_confidence_threshold` 配置——这个阈值
    # 一直存在，只是融合模式下从来没被用来做这个决定。
    # 注意方向：置信度**高**才跳过大模型；低的时候恰恰最需要它补救。
    if (confidence is not None and confidence_threshold is not None
            and confidence >= confidence_threshold):
        if trace:
            with trace.step("规则置信度足够，跳过大模型") as step:
                step.says("置信度 %.2f ≥ 阈值 %.2f"
                          % (confidence, confidence_threshold))
        return dict(rule_result), SOURCE_RULES, []

    step = trace.step("大模型融合") if trace else _NullStep()
    with step:
        try:
            raw = provider.complete_json(system, prompt)
        except LLMUnavailable as exc:
            step.says("不可用：%s" % exc)
            return dict(rule_result), SOURCE_FALLBACK, []
        except Exception as exc:
            # 提供方实现里的任何意外都不该把整条请求带崩
            step.says("异常：%s" % type(exc).__name__)
            return dict(rule_result), SOURCE_FALLBACK, []

    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            step.says("输出不是合法 JSON，已退回规则")
            return dict(rule_result), SOURCE_FALLBACK, []

    if not isinstance(raw, dict):
        step.says("输出不是对象，已退回规则")
        return dict(rule_result), SOURCE_FALLBACK, []

    cleaned = sanitize(raw)
    if not cleaned:
        step.says("输出未通过校验，已退回规则")
        return dict(rule_result), SOURCE_FALLBACK, []

    divergence = _diff(rule_result, cleaned)

    # 硬性规则最后过一遍。放在这里而不是提示词里：写进提示词只是「请你
    # 注意」，大模型可以不照做；写在这里是无论如何都改回来。
    for guard in guards:
        cleaned, note = guard(rule_result, cleaned)
        if note:
            divergence.append(note)

    step.says("改动 %d 处" % len(divergence) if divergence else "与规则一致")
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
