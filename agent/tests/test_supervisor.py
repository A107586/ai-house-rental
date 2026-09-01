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

    def complete(self, system_prompt, user_prompt, max_tokens=None):
        self.calls += 1
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

    def test_high_confidence_skips_llm(self):
        """规则置信度够高时，绝不能调用大模型——这是省 Token 的关键。"""
        provider = FakeProvider(payload='{"intent":"search","confidence":0.9}')
        result = supervisor.classify("卫生间漏水了，需要报修，马桶也堵了", provider=provider)
        self.assertEqual(result["source"], "rules")
        self.assertEqual(provider.calls, 0)

    def test_low_confidence_calls_llm(self):
        provider = FakeProvider(payload='{"intent":"support","confidence":0.88}')
        result = supervisor.classify("这个平台靠谱吗", provider=provider)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(result["source"], "llm")
        self.assertEqual(result["intent"], "support")

    def test_llm_failure_falls_back_to_rules(self):
        """大模型故障必须降级，不能把异常抛给用户（非功能需求 N-07）。"""
        provider = FakeProvider(fail=True)
        result = supervisor.classify("这个平台靠谱吗", provider=provider)
        self.assertEqual(result["source"], "rules_fallback")

    def test_no_provider_falls_back(self):
        """未配置大模型时服务照常工作。"""
        result = supervisor.classify("这个平台靠谱吗", provider=NullProvider())
        self.assertEqual(result["source"], "rules_fallback")

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
