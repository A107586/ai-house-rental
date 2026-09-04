"""轨迹级评估与链路追踪测试。

当前智能体工程实践把评估分成三层：

* **端到端** —— 任务成没成（`test_acceptance.py` 的准确率基准）
* **轨迹级** —— 路径是否合理：走了哪几步、有没有多余的工具调用
* **组件级** —— 哪个环节坏了（各模块的单元测试）

我们原先只有第一层和第三层。**缺的是中间这层**——准确率能证明「答对了」，
却证明不了「用合理的路径答对的」。一个每次都把七个工具全调一遍、最后蒙
对答案的智能体，端到端评估看不出任何问题，成本与延迟却是灾难。

这个文件补上第二层。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import fusion, trace as tracing  # noqa: E402
from app.agents.property_finder import agent as finder  # noqa: E402
from app.llm.base import LLMProvider, LLMUnavailable  # noqa: E402


class FakeBackend(object):
    def __init__(self, items=None):
        self.items = items or []
        self.calls = 0

    def list_properties(self, params):
        self.calls += 1
        return {"list": self.items, "total": len(self.items)}


class FakeProvider(LLMProvider):
    available = True

    def __init__(self, payload=None, raises=None):
        self.payload = payload
        self.raises = raises
        self.calls = 0

    def complete_json(self, system, prompt, max_tokens=None):
        self.calls += 1
        if self.raises:
            raise self.raises
        return self.payload

    def complete(self, system, prompt, max_tokens=None):
        return ""


def house(no="DL0001"):
    return {"house_no": no, "title": "测试房", "district": "中山区",
            "rent_price": 2800, "area": 70, "layout": "两室一厅",
            "rent_type": "整租", "tags": []}


class TestTraceBasics(unittest.TestCase):

    def test_records_steps_in_order(self):
        trace = tracing.Trace("demo")
        with trace.step("第一步"):
            pass
        with trace.step("第二步"):
            pass
        names = [s["name"] for s in trace.dump()["steps"]]
        self.assertEqual(names, ["第一步", "第二步"])

    def test_failure_is_recorded_not_swallowed(self):
        """**异常不吞**：追踪的职责是记录，不是改变控制流。
        吞掉异常会让上层以为一切正常，比没有追踪更糟。"""
        trace = tracing.Trace("demo")
        with self.assertRaises(ValueError):
            with trace.step("会炸的一步"):
                raise ValueError("炸了")

        step = trace.dump()["steps"][0]
        self.assertFalse(step["ok"])
        self.assertIn("ValueError", step["detail"])

    def test_step_cap(self):
        """trace 要随响应回传，不能被某个循环撑爆。"""
        trace = tracing.Trace("demo")
        for i in range(tracing.Trace.MAX_STEPS + 20):
            with trace.step("第 %d 步" % i):
                pass
        self.assertEqual(len(trace.dump()["steps"]), tracing.Trace.MAX_STEPS)

    def test_notes(self):
        trace = tracing.Trace("demo").note("置信度", 0.55)
        self.assertEqual(trace.dump()["notes"]["置信度"], 0.55)

    def test_describe_is_readable(self):
        trace = tracing.Trace("search")
        with trace.step("规则解析"):
            pass
        text = tracing.describe(trace.dump())
        self.assertIn("search", text)
        self.assertIn("规则解析", text)


class _FusionAlwaysOn(object):
    """把「规则很确定就跳过大模型」这个开关钉死为关闭（K-10）。

    这一组用例测的是**融合机制本身**：规则结论有没有进提示词、护栏挡不挡得住
    幻觉、大模型输出不可用时退不退得回去。它们必须让大模型真的被调用一次，
    否则测的就是别的东西了。

    开关的默认值是产品决策、会变（现在默认开启）；这些用例不该跟着它飘，
    所以在这里显式钉住，而不是依赖默认值恰好是某个值。
    """

    def setUp(self):
        super().setUp()
        from app.config import settings
        self._skip = settings.fusion_skip_when_confident
        settings.fusion_skip_when_confident = False

    def tearDown(self):
        from app.config import settings
        settings.fusion_skip_when_confident = self._skip
        super().tearDown()


class TestTrajectory(_FusionAlwaysOn, unittest.TestCase):
    """轨迹级：路径是否合理。"""

    def test_rules_only_path_does_not_call_backend_twice(self):
        """一次找房只该查一次房源。多查一次意味着某处白跑了。"""
        backend = FakeBackend([house()])
        finder.run("中山区两室一厅", backend=backend)
        self.assertEqual(backend.calls, 1)

    def test_unparsed_query_skips_the_backend_entirely(self):
        """**没解析出任何条件时不该去查库。**

        「查了再说」看着无害，实际是拿一个空条件做全表扫描，
        既慢又会把「没听懂」变成「给你看看全部房源」。
        """
        backend = FakeBackend([house()])
        result = finder.run("今天天气不错", backend=backend)
        self.assertEqual(backend.calls, 0)
        self.assertEqual(result["status"], "unparsed")

    def test_trace_present_on_success(self):
        result = finder.run("中山区两室一厅", backend=FakeBackend([house()]))
        self.assertIn("trace", result)
        names = [s["name"] for s in result["trace"]["steps"]]
        self.assertIn("规则解析", names)
        self.assertIn("查询房源", names)

    def test_trace_present_on_failure_paths(self):
        """失败路径尤其要有 trace——真正需要排查的就是这几次。"""
        unparsed = finder.run("今天天气不错", backend=FakeBackend())
        self.assertIn("trace", unparsed)

        class Broken:
            def list_properties(self, params):
                raise RuntimeError("后端挂了")

        broken = finder.run("中山区两室一厅", backend=Broken())
        self.assertIn("trace", broken)

    def test_llm_step_appears_only_when_provider_used(self):
        """没配大模型时，轨迹里不该出现大模型这一步——
        否则看 trace 的人会以为调了却没生效。"""
        without = finder.run("中山区两室一厅", backend=FakeBackend([house()]))
        self.assertNotIn("大模型融合",
                         [s["name"] for s in without["trace"]["steps"]])

        provider = FakeProvider({"filters": {"suozaidiqu": "中山区"},
                                 "features": []})
        with_llm = finder.run("中山区两室一厅", provider=provider,
                              backend=FakeBackend([house()]))
        self.assertIn("大模型融合",
                      [s["name"] for s in with_llm["trace"]["steps"]])

    def test_llm_failure_is_visible_in_trace(self):
        """大模型失败时，轨迹要说清楚是**为什么**退回规则的。

        只看到 source=rules_fallback 无从判断是没配 Key、超时，
        还是输出没通过校验——这三种的处理方式完全不同。
        """
        provider = FakeProvider(raises=LLMUnavailable("超时"))
        result = finder.run("中山区两室一厅", provider=provider,
                            backend=FakeBackend([house()]))
        step = next(s for s in result["trace"]["steps"]
                    if s["name"] == "大模型融合")
        self.assertIn("不可用", step["detail"])
        self.assertEqual(result["source"], fusion.SOURCE_FALLBACK)

    def test_llm_rejection_reason_is_visible(self):
        provider = FakeProvider({"filters": {}, "features": []})
        result = finder.run("中山区两室一厅", provider=provider,
                            backend=FakeBackend([house()]))
        step = next(s for s in result["trace"]["steps"]
                    if s["name"] == "大模型融合")
        self.assertIn("校验", step["detail"])

    def test_no_redundant_llm_calls(self):
        """一次请求只问一次大模型。问两次意味着提示词被重复构造，
        成本直接翻倍。"""
        provider = FakeProvider({"filters": {"suozaidiqu": "中山区"},
                                 "features": []})
        finder.run("中山区两室一厅", provider=provider,
                   backend=FakeBackend([house()]))
        self.assertEqual(provider.calls, 1)

    def test_total_time_is_reported(self):
        result = finder.run("中山区两室一厅", backend=FakeBackend([house()]))
        self.assertIn("total_ms", result["trace"])
        self.assertGreaterEqual(result["trace"]["total_ms"], 0)

    def test_rule_confidence_is_noted(self):
        """规则的置信度要留在轨迹里——它是「为什么走了这条路」的依据。"""
        result = finder.run("中山区两室一厅", backend=FakeBackend([house()]))
        step = next(s for s in result["trace"]["steps"]
                    if s["name"] == "规则解析")
        self.assertIn("置信度", step["detail"])
