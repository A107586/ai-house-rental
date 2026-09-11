"""规则置信度短路（K-10 提速开关）。

融合模式下**每一次请求都要等一趟大模型往返**，哪怕规则已经百分之百确定。
「中山区两室一厅三千以内」这类说法规则全都认得，为它多付一趟往返，
用户感受到的就是「智能体很慢」——而慢在助手类产品里不是体验问题，
是功能问题：等三秒的助手，用户第二次就自己去点筛选栏了。

**这个开关默认关闭**，因为它推翻的是 `app/fusion.py` 开头那段刻意的
设计决定（规则与大模型同时参与）。所以这份测试同时守两件事：

1. 开关打开时，高置信度的解析确实不调大模型；
2. **开关关闭时行为和以前一模一样**——默认路径没有被这次改动动过。
"""

import unittest

from app import fusion
from app.llm.base import LLMProvider


class CountingProvider(LLMProvider):
    """记账用的假提供方：只关心「被调了几次」。"""

    available = True

    def __init__(self, payload=None):
        self.calls = 0
        self.payload = payload if payload is not None else {"filters": {}}

    def complete(self, system_prompt, user_prompt, max_tokens=None):
        import json
        self.calls += 1
        return json.dumps(self.payload, ensure_ascii=False)


RULE = {"filters": {"suozaidiqu": "中山区"}, "features": []}


def sanitize(raw):
    return raw if isinstance(raw, dict) and raw.get("filters") else None


class TestShortCircuit(unittest.TestCase):

    def fuse(self, provider, **kw):
        return fusion.fuse(RULE, provider, "system", "prompt", sanitize, **kw)

    def test_high_confidence_skips_the_model(self):
        provider = CountingProvider()
        final, source, divergence = self.fuse(
            provider, confidence=0.9, confidence_threshold=0.5)

        self.assertEqual(provider.calls, 0)
        self.assertEqual(source, fusion.SOURCE_RULES)
        self.assertEqual(final, RULE)
        self.assertEqual(divergence, [])

    def test_low_confidence_still_calls_the_model(self):
        """方向不能搞反。

        置信度**低**的时候恰恰最需要大模型补救——短路只针对
        「规则已经很确定」的那一类，不是「能省则省」。
        """
        provider = CountingProvider({"filters": {"suozaidiqu": "沙河口区"}})
        final, source, _ = self.fuse(
            provider, confidence=0.2, confidence_threshold=0.5)

        self.assertEqual(provider.calls, 1)
        self.assertEqual(source, fusion.SOURCE_FUSED)
        self.assertEqual(final["filters"]["suozaidiqu"], "沙河口区")

    def test_exactly_at_threshold_skips(self):
        """阈值取「大于等于」。边界行为要写死在测试里，
        免得后来的人凭直觉改成严格大于又说不清为什么。"""
        provider = CountingProvider()
        self.fuse(provider, confidence=0.5, confidence_threshold=0.5)
        self.assertEqual(provider.calls, 0)

    def test_default_path_is_untouched(self):
        """**不传置信度就是老行为。**

        这一条守的是「默认关闭」这个承诺：调用方没开开关时，
        `fuse` 必须照旧每次都问大模型。
        """
        provider = CountingProvider({"filters": {"suozaidiqu": "沙河口区"}})
        _, source, _ = self.fuse(provider)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(source, fusion.SOURCE_FUSED)

    def test_threshold_without_confidence_does_not_skip(self):
        """只给阈值、不给置信度，不能当成「置信度无穷大」。"""
        provider = CountingProvider({"filters": {"suozaidiqu": "沙河口区"}})
        self.fuse(provider, confidence_threshold=0.5)
        self.assertEqual(provider.calls, 1)

    def test_short_circuit_is_recorded_in_trace(self):
        """跳过也要在链路里留一条。

        答辩要展示决策过程，而「这次为什么没调大模型」正是要解释的；
        什么都不记的话，链路图上会凭空少一步。
        """
        from app import trace as tracing

        provider = CountingProvider()
        trace = tracing.Trace("search")
        self.fuse(provider, confidence=0.9, confidence_threshold=0.5,
                  trace=trace)
        steps = " ".join(str(s) for s in trace.dump().get("steps", []))
        self.assertIn("跳过大模型", steps)


class TestFinderRespectsTheSwitch(unittest.TestCase):
    """找房链路上的开关接线。"""

    def test_switch_on_by_default(self):
        """**默认开启**（团队定的）。

        这条用例守的是一个产品决策，不是一段逻辑：默认值改动时它会红，
        逼着改的人重新读一遍 config.py 里那段取舍说明——
        置信度高只说明规则认得出，不等于它认得对。
        """
        from app.config import settings
        self.assertTrue(settings.fusion_skip_when_confident)

    def test_switch_on_skips_for_a_confident_query(self):
        from app.agents.property_finder import agent
        from app.config import settings

        provider = CountingProvider({"filters": {"suozaidiqu": "沙河口区"}})
        original = settings.fusion_skip_when_confident
        settings.fusion_skip_when_confident = True
        try:
            filters, _, source, _ = agent.understand(
                "中山区两室一厅，租金三千以内", provider=provider)
        finally:
            settings.fusion_skip_when_confident = original

        self.assertEqual(provider.calls, 0)
        self.assertEqual(source, fusion.SOURCE_RULES)
        # 规则自己的结论要完好地留下来
        self.assertEqual(filters["suozaidiqu"], "中山区")


class TestTimeout(unittest.TestCase):

    def test_default_timeout_is_seven_seconds(self):
        """30 → 8 → 7 秒。

        降级路径是完整可用的，不是残次品——没有理由为了
        「说不定第 29 秒能回来」让用户干等着。

        后来又从 8 收到 7：实测中转站成功调用的耗时中位 3.4 秒、
        p95 6.6 秒，超过 7 秒的基本都是挂死，等下去也等不到。
        """
        from app.config import Settings
        self.assertEqual(Settings().llm_timeout, 7.0)

    def test_turn_budget_default(self):
        """一轮对话在大模型上花的总墙钟时间，默认 12 秒。

        从 20 收到 12：超时不再重试之后，单次最坏是 7 秒而不是 16 秒，
        一轮里两三次调用也就十来秒。留着 20 秒只会让最坏情况更坏，
        而最坏情况正是用户抱怨「太慢」的那几次。
        """
        from app.config import Settings
        self.assertEqual(Settings().llm_turn_budget, 12.0)
