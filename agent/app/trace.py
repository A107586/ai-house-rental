"""智能体调用链路追踪。

当前智能体工程实践里反复强调的一件事是 **trace-level observability**：
每一步的工具调用、观测结果、延迟都要可追溯。评估要分三层——端到端
（任务成没成）、轨迹级（路径是否合理）、组件级（哪个环节坏了）。

我们原先只有第一层和第三层：准确率基准回答「答对了没有」，单元测试回答
「哪个函数坏了」。**缺的是中间那层**——一次请求内部经过了哪几步、各花
多久、大模型改了什么、护栏有没有触发。答辩被问「找房请求内部是怎么走的」
答不上来，正是缺这个。

设计上有两条：

**一、trace 是数据，不是日志。** 随响应一起返回给前端，而不是只打到
控制台。前端已有「决策过程」面板，直接展示；答辩时能当场翻开看。

**二、失败也要留痕。** 只记成功路径的追踪没有用——真正要查的恰恰是
出问题的那次。每一步都记 ok 与耗时，异常也记下来。
"""

import time


class Trace(object):
    """一次请求的调用链路。

    用法::

        trace = Trace("search")
        with trace.step("规则解析"):
            ...
        trace.note("命中区域", "中山区")
        result["trace"] = trace.dump()
    """

    # 单次请求最多记多少步。正常路径远用不到，设上限是防止某个循环
    # 把 trace 撑爆——它是要随响应回传的
    MAX_STEPS = 40

    def __init__(self, intent=""):
        self.intent = intent
        self.started = time.time()
        self.steps = []
        self.notes = {}

    def step(self, name):
        return _Step(self, name)

    def note(self, key, value):
        """记一条键值观测，如「规则置信度 0.55」。"""
        self.notes[key] = value
        return self

    def _record(self, name, elapsed_ms, ok, detail):
        if len(self.steps) >= self.MAX_STEPS:
            return None
        entry = {
            "name": name,
            "ms": elapsed_ms,
            "ok": ok,
            "detail": detail,
        }
        self.steps.append(entry)
        return entry

    def dump(self):
        """导出给前端。总耗时单独给一项，省得前端自己加。"""
        return {
            "intent": self.intent,
            "total_ms": int((time.time() - self.started) * 1000),
            "steps": self.steps,
            "notes": self.notes,
        }


class _Step(object):
    """上下文管理器，负责计时与异常记录。

    异常**不吞**：追踪的职责是记录发生了什么，不是改变控制流。
    吞掉异常会让上层以为一切正常，那比没有追踪更糟。
    """

    def __init__(self, trace, name):
        self.trace = trace
        self.name = name
        self.detail = ""
        self.started = None
        # 出块之后 says() 要能改到已记录的那一条
        self.entry = None

    def __enter__(self):
        self.started = time.time()
        return self

    def says(self, detail):
        """补一句这一步做了什么，如「大模型补充了租金上限」。

        **出了 with 块之后调用同样有效。** 很多判断（输出校验、护栏）
        发生在 with 块之外，若那时 says 变成空操作，轨迹里就只剩一个
        「大模型融合 30ms」，说不出它到底做了什么、为什么退回规则。
        """
        self.detail = str(detail)[:120]
        if self.entry is not None:
            self.entry["detail"] = self.detail
        return self

    def __exit__(self, exc_type, exc, tb):
        elapsed = int((time.time() - self.started) * 1000)
        if exc_type is None:
            self.entry = self.trace._record(self.name, elapsed, True,
                                            self.detail)
        else:
            self.entry = self.trace._record(
                self.name, elapsed, False,
                "%s: %s" % (exc_type.__name__, str(exc)[:80]))
        return False


def describe(trace_dump):
    """把 trace 渲染成一行人话，用于日志与答辩讲解。"""
    if not trace_dump:
        return ""
    parts = ["%s（%dms）" % (trace_dump.get("intent", "?"),
                            trace_dump.get("total_ms", 0))]
    for item in trace_dump.get("steps", []):
        mark = "" if item["ok"] else "✗"
        parts.append("%s%s %dms" % (mark, item["name"], item["ms"]))
    return " → ".join(parts)
