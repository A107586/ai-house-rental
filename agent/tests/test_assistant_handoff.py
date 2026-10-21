# -*- coding: utf-8 -*-
"""助手：转人工、检索没把握时的相近条目、房源的「谁带看」。

不依赖网络、数据库与大模型。
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agents.assistant import loop  # noqa: E402
from app.agents.assistant.toolkit import Toolkit, _house_row  # noqa: E402
from app.agents.support import agent as support  # noqa: E402
from tests.test_assistant_agent import ScriptedLLM, deps  # noqa: E402


class TestHandoff(unittest.TestCase):
    """实测用户说「转人工」，助手答「我这边没有转人工的工具」——回复下面却摆着转人工按钮。"""

    def test_handoff_is_in_every_toolset(self):
        for logged_in in (True, False):
            names = {t["name"] for t in Toolkit(deps(logged_in=logged_in)).tools()}
            self.assertIn("handoff", names)

    def test_logged_in_gets_the_button_and_the_turn_stops(self):
        llm = ScriptedLLM({"tool": "handoff", "params": {"question": "这套有没有中介带看"}})
        out = loop.run("转人工", [], llm, Toolkit(deps()))
        self.assertEqual(out["stopped_by"], "handoff")
        self.assertEqual(out["artifacts"]["handoff"]["question"], "这套有没有中介带看")
        self.assertIn("转人工", out["message"])
        self.assertNotIn("没有转人工", out["message"])

    def test_say_explains_before_the_button(self):
        # 是助手查不到才给的按钮，要先说清楚哪件事没查到
        llm = ScriptedLLM({"tool": "handoff", "say": "电梯维保周期平台没有写明。",
                           "params": {"question": "电梯多久维保一次"}})
        out = loop.run("电梯多久维保一次", [], llm, Toolkit(deps()))
        self.assertTrue(out["message"].startswith("电梯维保周期平台没有写明。"))
        self.assertIn("「转人工」", out["message"])

    def test_question_falls_back_to_what_the_user_said(self):
        llm = ScriptedLLM({"tool": "handoff", "params": {}})
        out = loop.run("我要找人工客服", [], llm, Toolkit(deps()))
        self.assertEqual(out["artifacts"]["handoff"]["question"], "我要找人工客服")

    def test_missed_lookup_without_say_still_explains(self):
        refused = {"status": "refused", "message": "没查到", "handoff": {"question": "q", "reason": "r"}}
        llm = ScriptedLLM({"tool": "knowledge", "params": {"question": "电梯多久维保"}},
                          {"tool": "handoff", "params": {"question": "电梯多久维保一次"}})
        out = loop.run("电梯多久维保一次", [], llm, Toolkit(deps(knowledge=lambda q: refused)))
        self.assertTrue(out["message"].startswith("这个问题平台规则里没有查到明确说明"))

    def test_user_asked_directly_gets_no_missed_lead(self):
        llm = ScriptedLLM({"tool": "handoff", "params": {}})
        out = loop.run("转人工", [], llm, Toolkit(deps()))
        self.assertNotIn("没有查到", out["message"])

    def test_anonymous_is_told_to_log_in_without_a_dead_button(self):
        # 工单要回给某个人。按钮摆出来点了也只会报「要先登录」
        llm = ScriptedLLM({"tool": "handoff", "params": {"question": "转人工"}})
        out = loop.run("转人工", [], llm, Toolkit(deps(logged_in=False)))
        self.assertNotIn("handoff", out["artifacts"])
        self.assertIn("登录", out["message"])


class FakeIndex:
    def __init__(self, chunks):
        self.chunks = chunks

    def get(self, cid):
        return self.chunks.get(cid)


class TestRelatedEntries(unittest.TestCase):
    """「这套有中介带看房吗，押金多少」被闸门拒了，「看房是谁带看？」就在相近条目里。"""

    INDEX = FakeIndex({
        "faq-1": {"title": "看房是谁带看？", "body": "房东自己带看；委托了中介的房源由**中介**带看。"},
        "faq-2": {"title": "看房收费吗？", "body": "看房不收费。"},
        "faq-3": {"title": "怎么预约看房？", "body": "详情页点「预约看房」。"},
    })

    def test_related_carries_original_text(self):
        out = support.related(self.INDEX, ["faq-1", "faq-2", "faq-3"])
        self.assertEqual([r["title"] for r in out], ["看房是谁带看？", "看房收费吗？"])
        self.assertEqual(out[0]["body"], "房东自己带看；委托了中介的房源由中介带看。")

    def test_not_filtered_by_coverage(self):
        # 「看房是谁带」分词挑不出话题词，覆盖率记 0，可检索第一名正是那一条。
        # 给模型看的这份不按覆盖率筛，由模型判断对不对得上
        self.assertEqual(len(support.related(self.INDEX, ["faq-1"])), 1)
        self.assertEqual(support.related(self.INDEX, []), [])
        self.assertEqual(support.related(self.INDEX, ["missing"]), [])

    def test_model_sees_the_text_and_is_told_to_judge(self):
        refused = {"status": "refused", "message": "没查到",
                   "alternatives": ["看房是谁带看？"],
                   "related": [{"title": "看房是谁带看？", "body": "委托了中介的房源由中介带看。"}],
                   "handoff": {"question": "中介带看吗", "reason": "cover"}}
        seen = []

        class Peek(ScriptedLLM):
            def chat(self, system, messages, **kw):
                seen.append(messages[-1]["content"])
                return super().chat(system, messages, **kw)

        llm = Peek({"tool": "knowledge", "params": {"question": "有中介带看吗"}},
                   {"answer": "委托了中介的房源由中介带看。"})
        loop.run("有中介带看吗", [], llm, Toolkit(deps(knowledge=lambda q: refused)))
        observation = seen[-1]
        self.assertIn("相近条目原文", observation)
        self.assertIn("委托了中介的房源由中介带看", observation)
        self.assertIn("handoff", observation)


class TestViewingBy(unittest.TestCase):

    def test_house_detail_says_who_shows_the_flat(self):
        row = _house_row({"house_no": "DL0001", "viewing_by": "agent", "viewing_agent": "王*"},
                         full=True)
        self.assertEqual(row["带看"], "委托中介带看（中介 王*）")
        row = _house_row({"house_no": "DL0002", "viewing_by": "landlord"}, full=True)
        self.assertEqual(row["带看"], "房东自己带看")

    def test_list_rows_stay_short(self):
        # 列表里每套都带这一项太占地方，只在看单独一套时给
        row = _house_row({"house_no": "DL0001", "viewing_by": "agent"})
        self.assertNotIn("带看", row)


class TestPrompt(unittest.TestCase):

    def test_prompt_tells_the_model_to_use_handoff(self):
        tools = Toolkit(deps()).tools()
        system = loop._SYSTEM % {"catalog": loop._catalog(tools), "context": ""}
        self.assertIn("handoff", system)
        self.assertIn("不要说没有这个功能", system)
        json.dumps(tools, ensure_ascii=False)                # 工具表能序列化


if __name__ == "__main__":
    unittest.main()
