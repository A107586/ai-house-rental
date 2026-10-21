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
        # 记下提示词，用于验证规则的结论确实被带了进去
        self.last_prompt = ""

    def complete(self, system_prompt, user_prompt, max_tokens=None):
        self.calls += 1
        self.last_prompt = user_prompt
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

    def test_llm_is_always_consulted(self):
        """规则与大模型**同时参与**，规则再确定也要过一遍大模型。

        旧架构是「置信度够就跳过大模型」，那样规则覆盖不到的说法永远
        得不到修正；现在规则的结论作为参考交给大模型，由它给最终结果。
        """
        provider = FakeProvider(payload='{"category":"水暖","urgency":"紧急"}')
        _, source = agent.analyze("卫生间水管漏水了", provider=provider)
        self.assertEqual(provider.calls, 1, "规则确定时也要调用大模型")
        self.assertEqual(source, "fused")

    def test_rule_conclusion_is_in_the_prompt(self):
        """规则的结论必须真的进到提示词里，否则「融合」就是空话。"""
        provider = FakeProvider(payload='{"category":"水暖","urgency":"紧急"}')
        agent.analyze("卫生间水管漏水了", provider=provider)
        self.assertIn("水暖", provider.last_prompt)
        self.assertIn("紧急", provider.last_prompt)

    def test_llm_fixes_what_rules_missed(self):
        """规则认不出的说法由大模型补上——这是融合的主要收益。

        这句话没有任何可命中的关键词，规则只能落到「其他」。
        """
        provider = FakeProvider(
            payload='{"category":"家具","urgency":"一般","reason":"床板松动"}')
        result, source = agent.analyze("说不上来哪里不对劲", provider=provider)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(source, "fused")
        self.assertEqual(result["category"], "家具")

    def test_safety_urgency_is_never_downgraded(self):
        """**护栏**：规则判为紧急的，大模型不得降级。

        需求 AI-04 原文写明涉及漏水、漏电、燃气泄漏必须判紧急。
        写在提示词里只是「请你注意」，大模型可以不照做；写在护栏里
        是无论它怎么答都改回来——漏判一次可能真出事。
        """
        provider = FakeProvider(payload='{"category":"水暖","urgency":"可延后"}')
        result, _ = agent.analyze("卫生间水管漏水了", provider=provider)
        self.assertEqual(result["urgency"], "紧急")
        self.assertTrue(any("不降级" in d for d in result.get("divergence", [])))

    def test_upgrading_urgency_is_allowed(self):
        """反过来允许升级：多派一次工只是多跑一趟，
        与漏判的代价完全不对等。"""
        provider = FakeProvider(payload='{"category":"家具","urgency":"紧急"}')
        result, _ = agent.analyze("衣柜门有点松", provider=provider)
        self.assertEqual(result["urgency"], "紧急")

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
                           provider=NullProvider(), backend=backend, token="tok")
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
                           provider=NullProvider(), backend=FakeBackend(fail=True),
                           token="tok")
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "error")

    def test_submitting_needs_a_login(self):
        """不带身份就不落库——原先匿名就能往任何房源的公开维修历史里写一条。
        分析结果照给，用户能看到「这是水暖、紧急」，只是提交要先登录。"""
        backend = FakeBackend()
        result = agent.run("卫生间水管漏水了", house_no="DL0001",
                           provider=NullProvider(), backend=backend)
        self.assertEqual(result["status"], "need_login")
        self.assertFalse(result["ok"])
        self.assertEqual(result["category"], "水暖")
        self.assertIsNone(backend.created)

    def test_token_is_passed_to_the_backend(self):
        backend = FakeBackend()
        agent.run("水管漏水", house_no="DL0001", provider=NullProvider(),
                  backend=backend, token="tok")
        self.assertEqual(backend.created["token"], "tok")

    def test_backend_refusal_reason_reaches_the_user(self):
        from app.backend import BackendError

        class Refuses(FakeBackend):
            def create_repair(self, **kwargs):
                raise BackendError("只有正住在这套房的租客、房东或受托中介可以报修")

        result = agent.run("水管漏水", house_no="DL0002", provider=NullProvider(),
                           backend=Refuses(), token="tok")
        self.assertFalse(result["ok"])
        self.assertIn("正住在这套房", result["message"])

    def test_response_shape(self):
        result = agent.run("水管漏水", house_no="DL0001",
                           provider=NullProvider(), backend=FakeBackend(), token="tok")
        for key in ("intent", "status", "ok", "message"):
            self.assertIn(key, result)


if __name__ == "__main__":
    unittest.main()
