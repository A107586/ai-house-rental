# -*- coding: utf-8 -*-
"""「为你推荐」的推荐理由：大模型写一句话，但话里的数字必须出自数据。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agents.recommend import agent  # noqa: E402

RESULT = {
    "ok": True,
    "reason": "根据你看过的 12 套房源：常看 中山区，两室一厅，租金多在 850–3950 元。",
    "items": [{"title": "中山区青泥洼桥 两室一厅 70平", "district": "中山区", "rent_price": 3050,
               "match_reasons": ["在你常看的 中山区", "户型 两室一厅"]}],
}


class Provider:
    available = True

    def __init__(self, text):
        self.text = text

    def complete_json(self, system, prompt, max_tokens=None):
        return {"text": self.text}


class TestExplain(unittest.TestCase):

    def test_grounded_sentence_is_used(self):
        text = "你常看中山区的两室，青泥洼桥这套 3050 元，离你看过的几套都不远。"
        self.assertEqual(agent.explain(RESULT, Provider(text)), text)

    def test_made_up_number_throws_the_sentence_away(self):
        self.assertEqual(agent.explain(RESULT, Provider("这套步行 5 分钟到地铁。")), "")

    def test_too_long_is_dropped(self):
        self.assertEqual(agent.explain(RESULT, Provider("好" * 120)), "")

    def test_no_model_or_no_result(self):
        self.assertEqual(agent.explain(RESULT, None), "")
        self.assertEqual(agent.explain({"ok": False}, Provider("x")), "")


if __name__ == "__main__":
    unittest.main()
