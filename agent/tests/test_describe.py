"""房源描述生成测试（AI-08）。

这批用例守的是一条底线：**一个字都不许编**。

房源描述是给租客看的，也是签约前的要约内容。写了「步行五分钟到地铁」
而实际没有，那是虚假宣传，平台要担责任。而大模型最擅长的恰恰是补上
这类听着自然、却没有依据的细节——所以润色必须被卡死。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agents.describe import agent as describe  # noqa: E402
from app.agents.describe.tools.compose import compose  # noqa: E402
from app.llm.base import LLMProvider, LLMUnavailable  # noqa: E402

FULL = {
    "district": "中山区", "community": "青泥洼桥", "layout": "两室一厅",
    "area": 78, "rent_price": 4150, "rent_type": "整租",
    "floor": "11/29层", "orientation": "南",
    "tags": ["电梯", "地铁", "有阳台", "可养宠物"],
}


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


class TestCompose(unittest.TestCase):

    def test_uses_every_given_field(self):
        r = compose(FULL)
        for field in ("district", "community", "layout", "area",
                      "rent_price", "rent_type", "floor", "orientation",
                      "tags"):
            self.assertIn(field, r["used_fields"], field)

    def test_numbers_match_the_input(self):
        r = compose(FULL)
        self.assertIn("78", r["text"])
        self.assertIn("4150", r["text"])

    def test_no_tag_is_silently_dropped(self):
        """每个标签都是房东填的卖点，少写一个就是少一分成交机会。"""
        r = compose(FULL)
        self.assertIn("可养宠物", r["text"])

    def test_unknown_tags_are_listed_plainly(self):
        """写不出短语的标签就直接罗列，不硬凑。"""
        r = compose(dict(FULL, tags=["电梯", "独立门禁", "带地下车位"]))
        self.assertIn("独立门禁", r["text"])
        self.assertIn("带地下车位", r["text"])

    def test_floor_is_not_doubled(self):
        """楼层写作「11/29层」，前面再加「楼层」就成了「楼层11/29层」。"""
        r = compose(FULL)
        self.assertNotIn("楼层11/29层", r["text"])
        self.assertIn("11/29层", r["text"])

    def test_missing_fields_do_not_break(self):
        r = compose({"district": "甘井子区"})
        self.assertIn("甘井子区", r["text"])
        self.assertTrue(r["text"].endswith("。"))

    def test_empty_house(self):
        self.assertEqual(compose({})["text"], "本房源。")

    def test_bad_numbers_are_ignored(self):
        r = compose(dict(FULL, area="不是数字", rent_price=None))
        self.assertNotIn("area", r["used_fields"])
        self.assertNotIn("rent_price", r["used_fields"])

    def test_area_hint_matches_size(self):
        small = compose(dict(FULL, area=30))["text"]
        large = compose(dict(FULL, area=130))["text"]
        self.assertIn("单身", small)
        self.assertIn("多口之家", large)


class TestDescribeAgent(unittest.TestCase):

    def test_rules_only_without_provider(self):
        r = describe.run(FULL)
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["source"], "rules")

    def test_no_data(self):
        self.assertFalse(describe.run(None)["ok"])

    def test_too_little_info(self):
        r = describe.run({})
        self.assertEqual(r["status"], "no_data")
        self.assertIn("先把", r["message"])

    def test_polish_accepted_when_faithful(self):
        rules = describe.run(FULL)["text"]
        provider = FakeProvider(payload=rules.replace("。", "，").rstrip("，") + "。")
        r = describe.run(FULL, provider=provider)
        self.assertEqual(r["source"], "llm_polished")

    def test_polish_rejected_when_numbers_change(self):
        """改了面积或租金就是篡改事实，直接丢弃改写。"""
        provider = FakeProvider(
            payload="本房源位于中山区青泥洼桥，两室一厅，约 80 平方米，"
                    "月租 4000 元。")
        r = describe.run(FULL, provider=provider)
        self.assertEqual(r["source"], "rules")

    def test_polish_rejected_when_facts_are_added(self):
        """大模型最爱补的「交通便利」「周边配套齐全」都不带数字，
        只能靠篇幅兜住。"""
        rules = describe.run(FULL)["text"]
        provider = FakeProvider(
            payload=rules + "步行五分钟即到地铁站，周边商圈成熟，"
                            "菜市场、超市、三甲医院一应俱全，"
                            "小区绿化率高达百分之四十，物业管理到位，"
                            "是难得一见的优质房源，看房请从速。")
        r = describe.run(FULL, provider=provider)
        self.assertEqual(r["source"], "rules", "凭空添加的内容必须被丢弃")

    def test_llm_failure_falls_back(self):
        r = describe.run(FULL, provider=FakeProvider(fail=True))
        self.assertEqual(r["source"], "rules")
        self.assertTrue(r["ok"])

    def test_polish_can_be_disabled(self):
        provider = FakeProvider(payload="随便改的内容")
        describe.run(FULL, provider=provider, polish=False)
        self.assertEqual(provider.calls, 0)

    def test_empty_polish_is_ignored(self):
        r = describe.run(FULL, provider=FakeProvider(payload="   "))
        self.assertEqual(r["source"], "rules")
