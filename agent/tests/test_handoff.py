"""需求摘要交接测试（J-09-a）。

守两条：

1. **硬条件那一行不许被大模型碰。** 中介照着它挑房源，数字必须原样搬运。
2. **不许编。** 摘要里每一条都要能指回原话里的某个词。编出一条不存在的
   需求，中介会照着它推错房源，租客一头雾水。
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agents.handoff import agent as handoff  # noqa: E402
from app.agents.handoff.tools.soft import extract, render_hard  # noqa: E402
from app.llm.base import LLMProvider, LLMUnavailable  # noqa: E402

DEMO = "中山区两室一厅，租金三千以内，最好带电梯，主要是上班近，周末想看房"


class FakeProvider(LLMProvider):

    def __init__(self, payload=None, fail=False):
        self.payload = payload
        self.fail = fail
        self.last_prompt = ""

    @property
    def available(self):
        return True

    def complete(self, system_prompt, user_prompt, max_tokens=None):
        self.last_prompt = user_prompt
        if self.fail:
            raise LLMUnavailable("模拟故障")
        return self.payload

    def complete_json(self, system_prompt, user_prompt, max_tokens=None):
        raw = self.complete(system_prompt, user_prompt, max_tokens)
        return json.loads(raw) if isinstance(raw, str) else raw


class TestSoftRules(unittest.TestCase):

    def test_the_example_from_the_design_doc(self):
        """设计文档里画的那个例子，规则层就该抽得出来。"""
        r = extract(DEMO)
        self.assertIn("通勤优先", r["soft"])
        self.assertIn("看房时段", r["soft"])

    def test_every_label_can_point_back_at_a_word(self):
        """`matched` 是「这条是从哪个词判出来的」，答辩时要能当场说清。"""
        for label, word in extract(DEMO)["matched"]:
            self.assertIn(word, DEMO)

    def test_it_would_rather_miss_than_guess(self):
        """宁可漏，不可错——编一条需求比少一条糟得多。"""
        self.assertEqual(extract("随便看看")["soft"], [])


class TestHardLine(unittest.TestCase):

    def test_numbers_are_carried_verbatim(self):
        line = render_hard({"suozaidiqu": "中山区", "zujia_max": 3000},
                           ["电梯"])
        self.assertIn("3000", line)
        self.assertIn("中山区", line)
        self.assertIn("电梯", line)

    def test_bounds_keep_their_label_but_values_do_not(self):
        """「≤3000元」不写「租金」就看不出是钱；「中山区」不必写「区域」。"""
        line = render_hard({"suozaidiqu": "中山区", "zujia_max": 3000}, [])
        self.assertIn("租金上限≤3000元", line)
        self.assertNotIn("区域中山区", line)


class TestRun(unittest.TestCase):

    def test_hard_line_survives_a_lying_model(self):
        """**核心用例。** 大模型改不了硬条件——它根本碰不到那一段。"""
        provider = FakeProvider(json.dumps(
            {"soft": ["学区"], "note": "这位租客要找沙河口区五千以上的三室。"}))
        r = handoff.run(DEMO, provider=provider)
        self.assertIn("中山区", r["summary"]["hard"])
        self.assertIn("3000", r["summary"]["hard"])
        self.assertNotIn("沙河口", r["summary"]["hard"])

    def test_model_cannot_invent_a_soft_label(self):
        provider = FakeProvider(json.dumps(
            {"soft": ["养鱼", "通勤优先"], "note": "通勤优先。"}))
        r = handoff.run(DEMO, provider=provider)
        self.assertEqual(r["summary"]["soft"], ["通勤优先"])

    def test_works_without_the_model(self):
        """需求 N-07。"""
        r = handoff.run(DEMO, provider=FakeProvider(fail=True))
        self.assertTrue(r["ok"])
        self.assertIn("中山区", r["summary"]["hard"])
        self.assertTrue(r["summary"]["note"])

    def test_empty_input(self):
        r = handoff.run("   ")
        self.assertFalse(r["ok"])
        self.assertEqual(r["status"], "need_content")

    def test_browsing_history_is_optional_and_capped(self):
        viewed = [{"house_no": "DL%04d" % i} for i in range(10)]
        r = handoff.run(DEMO, viewed=viewed)
        self.assertEqual(len(r["summary"]["viewed"]), 5)

    def test_rendered_text_is_readable(self):
        r = handoff.run(DEMO, viewed=[{"house_no": "DL0032"}])
        text = handoff.render_text(r["summary"])
        self.assertIn("需求摘要", text)
        self.assertIn("中山区", text)
        self.assertIn("DL0032", text)

    def test_note_does_not_repeat_the_hard_line(self):
        """摘要里硬条件已经单独一行，概述再抄一遍是同一句话说两遍。"""
        r = handoff.run(DEMO)
        self.assertNotIn("3000", r["summary"]["note"])


if __name__ == "__main__":
    unittest.main()
