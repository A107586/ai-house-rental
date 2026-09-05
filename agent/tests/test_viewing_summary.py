"""带看纪要整理测试（J-09-c）。

这批用例守两条底线：

1. **标签必须归一化。** 这份输出会汇进房源的「常见异议」聚合。
   大模型交回一个「厨房太小」当新标签，聚合就散了——三个人嫌厨房小
   会显示成三条各出现一次的异议，等于没统计。
2. **到场与否不接受改写。** 它是这份纪要里唯一会影响别人的字段——
   放鸽子率算在租客头上。判错了是在冤枉人。
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agents.viewing import agent as viewing  # noqa: E402
from app.agents.viewing.tools.extract import extract  # noqa: E402
from app.llm.base import LLMProvider, LLMUnavailable  # noqa: E402


class FakeProvider(LLMProvider):

    def __init__(self, payload=None, fail=False):
        self.payload = payload
        self.fail = fail
        self.calls = 0
        self.last_prompt = ""

    @property
    def available(self):
        return True

    def complete(self, system_prompt, user_prompt, max_tokens=None):
        self.calls += 1
        self.last_prompt = user_prompt
        if self.fail:
            raise LLMUnavailable("模拟故障")
        return self.payload

    def complete_json(self, system_prompt, user_prompt, max_tokens=None):
        raw = self.complete(system_prompt, user_prompt, max_tokens)
        return json.loads(raw) if isinstance(raw, str) else raw


DEMO = "小两口，觉得朝向可以，嫌厨房小，问了能不能养猫，说要和家里商量下"


class TestRules(unittest.TestCase):
    """规则层。没有大模型时这就是最终输出（需求 N-07）。"""

    def test_the_example_from_the_design_doc(self):
        r = extract(DEMO)
        self.assertTrue(r["showed_up"])
        self.assertEqual(r["intention"], "medium")
        self.assertEqual(r["concerns"], ["厨房面积"])
        self.assertEqual(r["positives"], ["朝向"])
        self.assertEqual(r["questions"], ["是否可养宠物"])

    def test_labels_are_normalised_not_copied(self):
        """三种说法必须落到同一个标签上，否则聚合永远聚不起来。"""
        for text in ("厨房太小", "厨房窄得转不开身", "灶台小"):
            self.assertEqual(extract(text)["concerns"], ["厨房面积"], text)

    def test_a_specific_phrase_does_not_also_fire_the_generic_label(self):
        """「厨房太小」里含着「太小」。

        不去重的话它会同时命中「厨房面积」和「面积」，
        房源的异议聚合里这一条就被数了两遍。
        """
        self.assertEqual(extract("厨房太小")["concerns"], ["厨房面积"])
        # 反过来，笼统说法该照常命中笼统标签
        self.assertEqual(extract("房间太小")["concerns"], ["面积"])

    def test_two_different_complaints_both_survive(self):
        """去重去的是重叠的字，不是不同的意思。"""
        self.assertEqual(extract("采光差、厨房小")["concerns"],
                         ["厨房面积", "采光"])

    def test_no_show_is_only_taken_from_an_explicit_phrase(self):
        """含糊的一律按到场算——判错了是在冤枉租客。"""
        self.assertFalse(extract("租客临时有事，没来")["showed_up"])
        self.assertTrue(extract("聊得挺好，看了二十分钟")["showed_up"])

    def test_no_show_forces_low_intent(self):
        """人都没来，谈不上意向。"""
        self.assertEqual(extract("放鸽子了")["intention"], "low")

    def test_strong_signal_beats_the_hedge(self):
        """「很满意，回去和家里商量」——两个信号都在，不能只看后半句。"""
        self.assertEqual(
            extract("很满意，说要定，回去和家里商量一下")["intention"], "high")

    def test_summary_reads_like_a_sentence_without_the_llm(self):
        r = extract(DEMO)
        self.assertIn("朝向", r["summary"])
        self.assertIn("厨房面积", r["summary"])
        self.assertTrue(r["summary"].endswith("。"))


class TestFusion(unittest.TestCase):

    def test_llm_cannot_invent_a_label(self):
        """**这条是这批用例的核心。**

        收下「厨房太小」这种自造标签，等于让统计口径由每一次生成决定。
        """
        provider = FakeProvider(json.dumps({
            "showed_up": True, "intention": "medium",
            "concerns": ["厨房太小", "采光"], "positives": [],
            "questions": [], "summary": "顾虑厨房与采光。"}))
        result, source = viewing.analyze(DEMO, provider=provider)
        self.assertEqual(source, "fused")
        # 「采光」在表里，留下；「厨房太小」不在表里，丢掉
        self.assertEqual(result["concerns"], ["采光"])

    def test_llm_cannot_flip_a_no_show_into_attendance(self):
        provider = FakeProvider(json.dumps({
            "showed_up": True, "intention": "high",
            "concerns": [], "positives": [], "questions": [],
            "summary": "客户很满意。"}))
        result, _ = viewing.analyze("租客放鸽子了，没来", provider=provider)
        self.assertFalse(result["showed_up"])
        self.assertEqual(result["intention"], "low")
        self.assertTrue(any("到场" in d for d in result.get("divergence", [])))

    def test_bad_intention_value_falls_back_to_rules(self):
        provider = FakeProvider(json.dumps({
            "showed_up": True, "intention": "非常想要",
            "concerns": [], "positives": [], "questions": [],
            "summary": "x"}))
        result, source = viewing.analyze(DEMO, provider=provider)
        self.assertEqual(source, "rules_fallback")
        self.assertEqual(result["intention"], "medium")

    def test_llm_failure_still_produces_a_record(self):
        """需求 N-07：大模型不可用时照常工作。"""
        result = viewing.run(DEMO, provider=FakeProvider(fail=True))
        self.assertTrue(result["ok"])
        self.assertEqual(result["record"]["concerns"], ["厨房面积"])
        self.assertTrue(result["record"]["feedback"])

    def test_rule_result_is_handed_to_the_model(self):
        provider = FakeProvider(json.dumps({
            "showed_up": True, "intention": "medium", "concerns": [],
            "positives": [], "questions": [], "summary": "x"}))
        viewing.analyze(DEMO, provider=provider)
        self.assertIn("厨房面积", provider.last_prompt)


class TestRun(unittest.TestCase):

    def test_empty_input_asks_for_content(self):
        r = viewing.run("  ")
        self.assertFalse(r["ok"])
        self.assertEqual(r["status"], "need_content")

    def test_record_matches_the_backend_field_names(self):
        """输出要能直接填进 `POST /api/viewings` 的表单，不用再映射一次。"""
        record = viewing.run(DEMO)["record"]
        self.assertEqual(
            set(record),
            {"showed_up", "intention", "concerns", "positives", "questions",
             "feedback"})


if __name__ == "__main__":
    unittest.main()
