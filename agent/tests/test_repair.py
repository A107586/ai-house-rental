"""AI-04 报修工单智能分派测试。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agents.repair import agent  # noqa: E402
from app.agents.repair.tools.classify import classify_repair, describe  # noqa: E402
from app.llm.base import LLMProvider, LLMUnavailable, NullProvider  # noqa: E402


class FakeProvider(LLMProvider):

    def __init__(self, payload=None, fail=False):
        self.payload = payload
        self.fail = fail
        self.calls = 0

    def complete(self, system_prompt, user_prompt, max_tokens=None):
        self.calls += 1
        if self.fail:
            raise LLMUnavailable("模拟故障")
        return self.payload


class FakeBackend(object):

    def __init__(self, fail=False):
        self.fail = fail
        self.created = None

    def create_repair(self, **kwargs):
        if self.fail:
            raise RuntimeError("模拟写入失败")
        self.created = kwargs
        return {"ok": True}


class TestCategory(unittest.TestCase):

    def test_plumbing(self):
        self.assertEqual(classify_repair("卫生间水管漏水了")["category"], "水暖")

    def test_electric(self):
        self.assertEqual(classify_repair("厨房跳闸，全屋没电")["category"], "电路")

    def test_appliance(self):
        self.assertEqual(classify_repair("空调不制冷")["category"], "家电")

    def test_door_window(self):
        self.assertEqual(classify_repair("门锁坏了打不开")["category"], "门窗")

    def test_furniture(self):
        self.assertEqual(classify_repair("衣柜门掉了")["category"], "家具")

    def test_gas(self):
        self.assertEqual(classify_repair("闻到煤气味了")["category"], "燃气")

    def test_unknown_falls_back_to_other(self):
        result = classify_repair("有点问题说不上来")
        self.assertEqual(result["category"], "其他")
        self.assertLess(result["confidence"], 0.5)

    def test_empty(self):
        self.assertEqual(classify_repair("")["category"], "")
        self.assertEqual(classify_repair(None)["confidence"], 0.0)


class TestUrgency(unittest.TestCase):

    def test_safety_issue_is_urgent(self):
        for text in ("卫生间漏水", "插座漏电", "闻到燃气味", "全屋停电"):
            self.assertEqual(classify_repair(text)["urgency"], "紧急", text)

    def test_polite_wording_is_low(self):
        self.assertEqual(classify_repair("灯泡坏了，不急，有空再说")["urgency"], "可延后")

    def test_default_is_normal(self):
        self.assertEqual(classify_repair("洗衣机有点吵")["urgency"], "可延后")
        self.assertEqual(classify_repair("空调不制冷")["urgency"], "一般")

    def test_safety_beats_user_saying_not_urgent(self):
        """安全隐患不能因为租客说「不急」就降级——这是硬规则。"""
        result = classify_repair("厨房水管漏水，不急，有空来看看就行")
        self.assertEqual(result["urgency"], "紧急")


class TestAssignee(unittest.TestCase):

    def test_mapping(self):
        self.assertEqual(classify_repair("水管漏水")["assignee"], "水暖工")
        self.assertEqual(classify_repair("跳闸了")["assignee"], "电工")
        self.assertEqual(classify_repair("衣柜门掉了")["assignee"], "木工")

    def test_describe_mentions_keywords(self):
        text = describe(classify_repair("卫生间水管漏水"))
        self.assertIn("水暖", text)
        self.assertIn("紧急", text)


class TestAnalyze(unittest.TestCase):

    def test_high_confidence_skips_llm(self):
        provider = FakeProvider(payload='{"category":"家电","urgency":"一般"}')
        _, source = agent.analyze("卫生间水管漏水了", provider=provider)
        self.assertEqual(source, "rules")
        self.assertEqual(provider.calls, 0)

    def test_low_confidence_calls_llm(self):
        provider = FakeProvider(
            payload='{"category":"家具","urgency":"一般","reason":"床板松动"}')
        # 这句话没有任何可命中的关键词，规则置信度为 0，才会转交大模型
        result, source = agent.analyze("说不上来哪里不对劲", provider=provider)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(source, "llm")
        self.assertEqual(result["category"], "家具")

    def test_llm_invalid_category_is_rejected(self):
        """大模型返回枚举外的值必须丢弃，不能写进数据库。"""
        provider = FakeProvider(payload='{"category":"外星科技","urgency":"一般"}')
        _, source = agent.analyze("说不上来哪里怪", provider=provider)
        self.assertEqual(source, "rules_fallback")

    def test_llm_failure_falls_back(self):
        provider = FakeProvider(fail=True)
        _, source = agent.analyze("说不上来哪里怪", provider=provider)
        self.assertEqual(source, "rules_fallback")


class TestRun(unittest.TestCase):

    def test_creates_ticket(self):
        backend = FakeBackend()
        result = agent.run("卫生间水管漏水了", house_no="DL0001",
                           provider=NullProvider(), backend=backend)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "created")
        self.assertEqual(backend.created["category"], "水暖")
        self.assertEqual(backend.created["urgency"], "紧急")

    def test_preview_does_not_submit(self):
        backend = FakeBackend()
        result = agent.run("空调不制冷", house_no="DL0001", backend=backend,
                           provider=NullProvider(), submit=False)
        self.assertEqual(result["status"], "analyzed")
        self.assertIsNone(backend.created)

    def test_missing_house_no(self):
        result = agent.run("空调不制冷", provider=NullProvider(),
                           backend=FakeBackend())
        self.assertEqual(result["status"], "need_house")
        self.assertFalse(result["ok"])

    def test_empty_text(self):
        result = agent.run("", house_no="DL0001", provider=NullProvider())
        self.assertEqual(result["status"], "need_content")

    def test_backend_failure_is_graceful(self):
        result = agent.run("水管漏水", house_no="DL0001",
                           provider=NullProvider(), backend=FakeBackend(fail=True))
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "error")

    def test_response_shape(self):
        result = agent.run("水管漏水", house_no="DL0001",
                           provider=NullProvider(), backend=FakeBackend())
        for key in ("intent", "status", "ok", "message"):
            self.assertIn(key, result)


if __name__ == "__main__":
    unittest.main()
