"""融合层测试（规则 + 大模型同时参与）。

架构是：规则先解析一遍，结论作为参考答案写进提示词，由大模型给出最终
结果。这批用例守的是三条护栏——少任何一条，这个架构都不能上线：

1. **大模型不可用时必须照常工作**（需求 N-07）
2. **硬性规则不接受覆盖**（如需求 AI-04 点名的紧急判定）
3. **输出必须能校验**，校验不过就整体退回规则结论
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import fusion  # noqa: E402
from app.agents.property_finder import agent as finder  # noqa: E402
from app.llm.base import LLMProvider, LLMUnavailable, NullProvider  # noqa: E402


class FakeProvider(LLMProvider):
    """可控的假大模型。payload 直接作为 complete_json 的返回值。"""

    available = True

    def __init__(self, payload=None, raises=None):
        self.payload = payload
        self.raises = raises
        self.calls = 0
        self.last_prompt = ""

    def complete_json(self, system, prompt, max_tokens=None):
        self.calls += 1
        self.last_prompt = prompt
        if self.raises:
            raise self.raises
        return self.payload

    def complete(self, system, prompt, max_tokens=None):
        return ""


def keep_ok(raw):
    """只放行白名单字段，值必须是正整数。"""
    if not isinstance(raw, dict):
        return None
    clean = {}
    for key in ("a", "b"):
        value = raw.get(key)
        if isinstance(value, int) and value > 0:
            clean[key] = value
    return clean or None


RULES = {"a": 1}


class TestDegradation(unittest.TestCase):
    """护栏一：大模型不可用时必须照常工作（N-07）。"""

    def test_no_provider(self):
        final, source, _ = fusion.fuse(RULES, None, "s", "p", keep_ok)
        self.assertEqual(final, RULES)
        self.assertEqual(source, fusion.SOURCE_RULES)

    def test_provider_unavailable(self):
        final, source, _ = fusion.fuse(RULES, NullProvider(), "s", "p", keep_ok)
        self.assertEqual(final, RULES)
        self.assertEqual(source, fusion.SOURCE_RULES)

    def test_provider_raises_unavailable(self):
        provider = FakeProvider(raises=LLMUnavailable("网络不通"))
        final, source, _ = fusion.fuse(RULES, provider, "s", "p", keep_ok)
        self.assertEqual(final, RULES)
        self.assertEqual(source, fusion.SOURCE_FALLBACK)

    def test_provider_raises_anything_else(self):
        """提供方实现里的意外不该把整条请求带崩。"""
        provider = FakeProvider(raises=RuntimeError("驱动写崩了"))
        final, source, _ = fusion.fuse(RULES, provider, "s", "p", keep_ok)
        self.assertEqual(final, RULES)
        self.assertEqual(source, fusion.SOURCE_FALLBACK)

    def test_disabled_switch(self):
        provider = FakeProvider({"a": 9})
        final, source, _ = fusion.fuse(RULES, provider, "s", "p", keep_ok,
                                       enabled=False)
        self.assertEqual(final, RULES)
        self.assertEqual(provider.calls, 0, "关掉时不该发起调用")

    def test_returned_result_is_a_copy(self):
        """退回规则结论时给的是副本，调用方改它不能污染原始规则结果。"""
        rules = {"a": 1}
        final, _, _ = fusion.fuse(rules, None, "s", "p", keep_ok)
        final["a"] = 999
        self.assertEqual(rules["a"], 1)


class TestValidation(unittest.TestCase):
    """护栏三：输出必须能校验，校验不过就整体退回规则结论。"""

    def test_accepts_clean_output(self):
        provider = FakeProvider({"a": 7, "b": 8})
        final, source, _ = fusion.fuse(RULES, provider, "s", "p", keep_ok)
        self.assertEqual(final, {"a": 7, "b": 8})
        self.assertEqual(source, fusion.SOURCE_FUSED)

    def test_rejects_non_dict(self):
        for payload in ([1, 2], 42, None):
            final, source, _ = fusion.fuse(
                RULES, FakeProvider(payload), "s", "p", keep_ok)
            self.assertEqual(final, RULES, payload)
            self.assertEqual(source, fusion.SOURCE_FALLBACK)

    def test_parses_json_string(self):
        """有的提供方把 JSON 当字符串返回，也要认。"""
        final, source, _ = fusion.fuse(
            RULES, FakeProvider('{"a": 5}'), "s", "p", keep_ok)
        self.assertEqual(final, {"a": 5})
        self.assertEqual(source, fusion.SOURCE_FUSED)

    def test_rejects_broken_json_string(self):
        final, source, _ = fusion.fuse(
            RULES, FakeProvider("{这不是 JSON"), "s", "p", keep_ok)
        self.assertEqual(final, RULES)
        self.assertEqual(source, fusion.SOURCE_FALLBACK)

    def test_drops_invented_fields(self):
        """字段名会臆造，白名单之外的一律丢掉。"""
        provider = FakeProvider({"a": 3, "drop_table": "users"})
        final, _, _ = fusion.fuse(RULES, provider, "s", "p", keep_ok)
        self.assertEqual(final, {"a": 3})

    def test_rejects_wrong_types(self):
        provider = FakeProvider({"a": "三千", "b": -5})
        final, source, _ = fusion.fuse(RULES, provider, "s", "p", keep_ok)
        self.assertEqual(final, RULES)
        self.assertEqual(source, fusion.SOURCE_FALLBACK)


class TestGuards(unittest.TestCase):
    """护栏二：硬性规则不接受覆盖。"""

    def test_guard_can_override(self):
        def force_a(rule_result, fused):
            if fused.get("a") != rule_result.get("a"):
                fused["a"] = rule_result["a"]
                return fused, "a 保留规则结果"
            return fused, None

        provider = FakeProvider({"a": 99, "b": 2})
        final, source, divergence = fusion.fuse(
            RULES, provider, "s", "p", keep_ok, guards=(force_a,))
        self.assertEqual(final["a"], 1, "护栏必须改回规则的结论")
        self.assertEqual(final["b"], 2, "护栏之外的字段仍采用大模型的")
        self.assertIn("a 保留规则结果", divergence)
        self.assertEqual(source, fusion.SOURCE_FUSED)

    def test_guard_silent_when_agreed(self):
        def force_a(rule_result, fused):
            return fused, None

        provider = FakeProvider({"a": 1})
        _, _, divergence = fusion.fuse(RULES, provider, "s", "p", keep_ok,
                                       guards=(force_a,))
        self.assertEqual(divergence, [])


class TestDivergence(unittest.TestCase):
    """分歧要说得出来——答辩时能当场展示「规则说什么、大模型改了什么」。"""

    def test_reports_addition(self):
        provider = FakeProvider({"a": 1, "b": 2})
        _, _, divergence = fusion.fuse(RULES, provider, "s", "p", keep_ok)
        self.assertTrue(any("补充" in d for d in divergence))

    def test_reports_change(self):
        provider = FakeProvider({"a": 5})
        _, _, divergence = fusion.fuse(RULES, provider, "s", "p", keep_ok)
        self.assertTrue(any("改为" in d for d in divergence))

    def test_reports_removal(self):
        provider = FakeProvider({"b": 2})
        _, _, divergence = fusion.fuse(RULES, provider, "s", "p", keep_ok)
        self.assertTrue(any("移除" in d for d in divergence))

    def test_no_divergence_when_identical(self):
        provider = FakeProvider({"a": 1})
        _, _, divergence = fusion.fuse(RULES, provider, "s", "p", keep_ok)
        self.assertEqual(divergence, [])


class TestReference(unittest.TestCase):
    """规则结论要如实写进提示词。"""

    def test_renders_labels(self):
        text = fusion.render_reference({"suozaidiqu": "中山区"},
                                       {"suozaidiqu": "区域"})
        self.assertIn("区域", text)
        self.assertIn("中山区", text)

    def test_empty_is_stated_explicitly(self):
        """空结果也要写出来。告诉大模型「规则什么都没解析出来」比不提更有用
        ——它由此知道这是规则覆盖不到的表达，该自己从头判断。"""
        self.assertIn("未能解析", fusion.render_reference({}))

    def test_skips_empty_values(self):
        text = fusion.render_reference({"a": "", "b": None, "c": "有值"})
        self.assertIn("有值", text)
        self.assertNotIn("a：", text)


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


class TestFinderFusion(_FusionAlwaysOn, unittest.TestCase):
    """AI-01 接入融合后的端到端行为。"""

    def test_rule_result_is_in_the_prompt(self):
        """规则的结论必须真的出现在提示词里，否则融合就是空话。"""
        provider = FakeProvider({"filters": {"suozaidiqu": "中山区"},
                                 "features": []})
        finder.understand("中山区两室一厅", provider=provider)
        self.assertIn("中山区", provider.last_prompt)
        self.assertIn("两室一厅", provider.last_prompt)

    def test_rules_used_when_no_provider(self):
        filters, _, source, _ = finder.understand("中山区两室一厅")
        self.assertEqual(filters["suozaidiqu"], "中山区")
        self.assertEqual(source, fusion.SOURCE_RULES)

    def test_llm_can_add_what_rules_missed(self):
        """规则认不出的说法由大模型补上——这是融合的主要收益。"""
        provider = FakeProvider({
            "filters": {"suozaidiqu": "中山区", "zujia_max": 3000},
            "features": []})
        filters, _, source, divergence = finder.understand(
            "中山区的房子，价钱别太离谱", provider=provider)
        self.assertEqual(filters["zujia_max"], 3000)
        self.assertEqual(source, fusion.SOURCE_FUSED)
        self.assertTrue(divergence)

    def test_district_guard_blocks_hallucination(self):
        """大模型把区域换掉不是纠正，是幻觉——区域写错整页结果全错。"""
        provider = FakeProvider({"filters": {"suozaidiqu": "西岗区"},
                                 "features": []})
        filters, _, _, divergence = finder.understand(
            "中山区两室一厅", provider=provider)
        self.assertEqual(filters["suozaidiqu"], "中山区")
        self.assertTrue(any("保留规则结果" in d for d in divergence))

    def test_falls_back_when_llm_returns_nothing_useful(self):
        provider = FakeProvider({"filters": {}, "features": []})
        filters, _, source, _ = finder.understand(
            "中山区两室一厅", provider=provider)
        self.assertEqual(filters["suozaidiqu"], "中山区")
        self.assertEqual(source, fusion.SOURCE_FALLBACK)
