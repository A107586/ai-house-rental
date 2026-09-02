"""Supervisor 意图路由测试。

用假 provider 验证「规则优先、低置信度才走大模型」的分层逻辑，
全程不发起任何网络请求。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import supervisor  # noqa: E402
from app.llm.base import LLMProvider, LLMUnavailable, NullProvider, extract_json  # noqa: E402


class FakeProvider(LLMProvider):
    """可控的假大模型：记录被调用次数，返回预设结果。"""

    name = "fake"

    def __init__(self, payload=None, fail=False):
        self.payload = payload
        self.fail = fail
        self.calls = 0
        self.last_prompt = ""

    def complete(self, system_prompt, user_prompt, max_tokens=None):
        self.calls += 1
        self.last_prompt = user_prompt
        if self.fail:
            raise LLMUnavailable("模拟大模型故障")
        return self.payload


class TestRuleClassification(unittest.TestCase):

    def test_repair(self):
        intent, confidence, _ = supervisor.classify_by_rules("卫生间漏水了，能报修吗")
        self.assertEqual(intent, "repair")
        self.assertGreaterEqual(confidence, 0.6)

    def test_search(self):
        intent, _, _ = supervisor.classify_by_rules("有没有中山区两室一厅的房源")
        self.assertEqual(intent, "search")

    def test_booking(self):
        intent, _, _ = supervisor.classify_by_rules("我想预约这周六看房")
        self.assertEqual(intent, "booking")

    def test_contract(self):
        intent, _, _ = supervisor.classify_by_rules("帮我看看这份合同的违约条款")
        self.assertEqual(intent, "contract")

    def test_unknown(self):
        intent, confidence, _ = supervisor.classify_by_rules("今天股市怎么样")
        self.assertEqual(intent, "unknown")
        self.assertEqual(confidence, 0.0)

    def test_specific_intent_wins(self):
        """既像报修又像客服问答时，应取更具体的报修。"""
        intent, _, _ = supervisor.classify_by_rules("空调不制冷，怎么报修")
        self.assertEqual(intent, "repair")

    def test_weak_keyword_alone_is_not_an_intent(self):
        """回归测试：泛问句词单独出现不足以定意图。

        「怎么」「如何」几乎出现在任何提问里。若把它们当成和「退租」
        同等强度的信号，「今天股市怎么样」会被判成客服问答并给出
        足够高的置信度，从而**永远不会转交大模型复核**。
        """
        for text in ("今天股市怎么样", "这个怎么用", "为什么呢"):
            intent, confidence, _ = supervisor.classify_by_rules(text)
            self.assertEqual(intent, "unknown", text)
            self.assertEqual(confidence, 0.0, text)

    def test_weak_keyword_boosts_strong_match(self):
        """弱特征词本身不定意图，但可以给已命中强特征的意图加权。"""
        _, low, _ = supervisor.classify_by_rules("退租")
        _, high, _ = supervisor.classify_by_rules("退租怎么办理")
        self.assertGreater(high, low)


class TestRouting(unittest.TestCase):

    def test_llm_is_always_consulted(self):
        """规则与大模型**同时参与**，规则再确定也要过一遍大模型。

        旧架构是「置信度够就跳过」，好处是省 Token，代价是规则覆盖不到
        的说法永远得不到修正。现在改为每次都问，规则的结论作为参考
        一并交过去；成本由 FUSION_ENABLED 开关兜底（见下一个用例）。
        """
        provider = FakeProvider(payload='{"intent":"repair","confidence":0.9}')
        result = supervisor.classify("卫生间漏水了，需要报修", provider=provider)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(result["source"], "fused")

    def test_can_be_switched_off_for_cost(self):
        """需求 N-08 要求 Token 消耗受控，所以留了关掉融合的开关。

        关掉之后功能仍然完整，只是少了大模型的修正。
        """
        from app.config import settings
        provider = FakeProvider(payload='{"intent":"search","confidence":0.9}')
        original = settings.fusion_enabled
        settings.fusion_enabled = False
        try:
            result = supervisor.classify("卫生间漏水了，需要报修",
                                         provider=provider)
            self.assertEqual(provider.calls, 0)
            self.assertEqual(result["source"], "rules")
        finally:
            settings.fusion_enabled = original

    def test_rule_conclusion_is_in_the_prompt(self):
        """规则的结论必须真的进到提示词里，否则「融合」就是空话。"""
        provider = FakeProvider(payload='{"intent":"repair","confidence":0.9}')
        supervisor.classify("卫生间漏水了，需要报修", provider=provider)
        self.assertIn("报修", provider.last_prompt)

    def test_llm_decides_when_rules_are_unsure(self):
        provider = FakeProvider(payload='{"intent":"support","confidence":0.88}')
        result = supervisor.classify("这个平台靠谱吗", provider=provider)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(result["source"], "fused")
        self.assertEqual(result["intent"], "support")

    def test_strong_rule_intent_is_not_overridden(self):
        """**护栏**：规则高置信度判定的意图不接受改写。

        意图判错会把请求整个路由到错误的子智能体——用户说要报修，
        结果走进找房流程，比答得不好严重得多。
        """
        provider = FakeProvider(payload='{"intent":"search","confidence":0.99}')
        result = supervisor.classify("卫生间漏水了，需要报修", provider=provider)
        self.assertEqual(result["intent"], "repair")
        self.assertTrue(any("保留规则结果" in d
                            for d in result.get("divergence", [])))

    def test_llm_failure_falls_back_to_rules(self):
        """大模型故障必须降级，不能把异常抛给用户（非功能需求 N-07）。"""
        provider = FakeProvider(fail=True)
        result = supervisor.classify("这个平台靠谱吗", provider=provider)
        self.assertEqual(result["source"], "rules_fallback")

    def test_no_provider_falls_back(self):
        """未配置大模型时服务照常工作（需求 N-07）。

        source 是 rules 而不是 rules_fallback：什么都没失败，
        只是没配大模型。fallback 专指「调用了但输出不可用」。
        """
        result = supervisor.classify("这个平台靠谱吗", provider=NullProvider())
        self.assertEqual(result["source"], "rules")

    def test_llm_garbage_output_falls_back(self):
        """大模型返回了不存在的意图，必须丢弃而不是照单全收。"""
        provider = FakeProvider(payload='{"intent":"下单买房","confidence":0.99}')
        result = supervisor.classify("这个平台靠谱吗", provider=provider)
        self.assertEqual(result["source"], "rules_fallback")


class TestJsonExtraction(unittest.TestCase):
    """大模型很少老老实实只输出 JSON，容错必须到位。"""

    def test_plain(self):
        self.assertEqual(extract_json('{"a":1}'), {"a": 1})

    def test_code_fence(self):
        self.assertEqual(extract_json('```json\n{"a":1}\n```'), {"a": 1})

    def test_surrounding_text(self):
        self.assertEqual(extract_json('好的，结果是：{"a":1} 希望有帮助'), {"a": 1})

    def test_invalid(self):
        self.assertIsNone(extract_json("完全不是 JSON"))
        self.assertIsNone(extract_json(""))


if __name__ == "__main__":
    unittest.main()
