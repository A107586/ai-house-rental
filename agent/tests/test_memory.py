"""AI-09 多轮对话记忆测试。

重点验证三件事：

1. **累积不丢**——后一轮的补充条件要叠加到前一轮上，不是推倒重来。
2. **相对指令只在有上文时成立**——「再便宜点」没说过预算时不能瞎猜。
3. **记忆是推导出来的**——同一串对话重放两次，结果必须完全一致。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import memory  # noqa: E402


class TestAccumulate(unittest.TestCase):
    """累积语义。"""

    def test_second_turn_adds_to_first(self):
        slots, _ = memory.replay(["中山区两室一厅", "三千以内的"])
        self.assertEqual(slots["suozaidiqu"], "中山区")
        self.assertEqual(slots["fangwuhuxing"], "两室一厅")
        self.assertEqual(slots["zujia_max"], 3000)

    def test_features_accumulate_without_duplicates(self):
        _, features = memory.replay(["要带电梯的", "最好有地铁", "还是要电梯"])
        self.assertIn("电梯", features)
        self.assertEqual(features.count("电梯"), 1)

    def test_turn_with_nothing_parseable_keeps_slots(self):
        """一句没有条件的话不能把已经说好的需求清空。"""
        slots, _ = memory.replay(["中山区两室一厅", "嗯", "那怎么样"])
        self.assertEqual(slots["suozaidiqu"], "中山区")

    def test_only_recent_turns_are_replayed(self):
        texts = ["沙河口区"] + ["随便看看"] * memory.MAX_TURNS + ["中山区"]
        slots, _ = memory.replay(texts)
        # 沙河口区已经被挤出窗口
        self.assertEqual(slots["suozaidiqu"], "中山区")


class TestOverrideAndClear(unittest.TestCase):
    """覆盖与清除语义。"""

    def test_later_value_replaces_earlier(self):
        slots, _ = memory.replay(["中山区两室一厅", "换成沙河口"])
        self.assertEqual(slots["suozaidiqu"], "沙河口区")

    def test_override_is_announced(self):
        slots, feats = memory.replay(["中山区"])
        _, _, notes = memory.apply_turn(slots, feats, "换成沙河口")
        self.assertTrue(any("改为" in n for n in notes))

    def test_clear_price(self):
        slots, _ = memory.replay(["中山区三千以内", "不限租金"])
        self.assertNotIn("zujia_max", slots)
        self.assertEqual(slots["suozaidiqu"], "中山区")

    def test_normal_price_phrase_is_not_a_clear(self):
        """「租金三千以内」里也有「租金」两字，不能误判成取消限制。"""
        self.assertEqual(memory.clears("租金三千以内"), set())
        slots, _ = memory.replay(["租金三千以内"])
        self.assertEqual(slots["zujia_max"], 3000)

    def test_suffix_form_of_clear(self):
        self.assertIn("suozaidiqu", memory.clears("区域都行"))

    def test_reset_wipes_everything(self):
        slots, features = memory.replay(["中山区两室一厅带电梯", "重新找"])
        self.assertEqual(slots, {})
        self.assertEqual(features, [])

    def test_reset_then_new_condition(self):
        slots, _ = memory.replay(["中山区两室一厅", "重新找", "金州区一室一厅"])
        self.assertEqual(slots["suozaidiqu"], "金州区")
        self.assertEqual(slots["fangwuhuxing"], "一室一厅")


class TestRelative(unittest.TestCase):
    """相对调整——多轮记忆最能体现价值的一类指令。"""

    def test_cheaper_needs_context(self):
        """没说过预算时不猜。硬编一个区间只会让用户莫名其妙。"""
        slots, _, notes = memory.apply_turn({}, [], "再便宜点")
        self.assertEqual(slots, {})
        self.assertTrue(any("还没说过预算" in n for n in notes))

    def test_cheaper_lowers_ceiling(self):
        slots, _ = memory.replay(["中山区三千以内", "再便宜点"])
        self.assertEqual(slots["zujia_max"], 2400)
        self.assertEqual(slots["suozaidiqu"], "中山区", "调价不该影响其他条件")

    def test_cheaper_drops_the_floor(self):
        """留着下限会把更便宜的房源挡在外面，与诉求直接冲突。"""
        slots, _ = memory.replay(["两千到三千", "再便宜点"])
        self.assertNotIn("zujia_min", slots)

    def test_cheaper_twice_compounds(self):
        slots, _ = memory.replay(["三千以内", "再便宜点", "还是有点贵"])
        self.assertEqual(slots["zujia_max"], 1900)

    def test_pricier_raises_ceiling(self):
        slots, _ = memory.replay(["三千以内", "贵一点也行"])
        self.assertEqual(slots["zujia_max"], 3800)

    def test_adjustment_is_proportional(self):
        """等比而非固定值：预算 2000 和 20000 的用户，
        心里的「便宜一点」显然不是同一个数。"""
        low, _ = memory.replay(["2000以内", "再便宜点"])
        high, _ = memory.replay(["20000以内", "再便宜点"])
        self.assertEqual(low["zujia_max"], 1600)
        self.assertEqual(high["zujia_max"], 16000)

    def test_bigger_raises_area_floor(self):
        slots, _ = memory.replay(["80平以上", "面积大一点"])
        self.assertEqual(slots["mianji_min"], 100)

    def test_smaller_needs_context(self):
        slots, _, notes = memory.apply_turn({}, [], "小一点的")
        self.assertEqual(slots, {})
        self.assertTrue(any("面积" in n for n in notes))

    def test_price_is_rounded_to_hundreds(self):
        """2399.9 这种数字既不像人话也不好看。"""
        slots, _ = memory.replay(["2999以内", "再便宜点"])
        self.assertEqual(slots["zujia_max"] % 100, 0)

    def test_same_turn_value_then_adjustment(self):
        """「三千以内，再便宜点」应当基于本轮说的三千来算。"""
        slots, _, _ = memory.apply_turn({}, [], "三千以内，再便宜点")
        self.assertEqual(slots["zujia_max"], 2400)


class TestContinuesSearch(unittest.TestCase):
    """多轮对话里意图本身也需要上下文——这是记忆能否生效的前提。

    「三千以内的」单独分类必然是 unknown，一旦上一轮在找房，
    它就只可能是在调条件。没有这一步，记忆写得再好也没机会生效。
    """

    def test_bare_price_continues(self):
        self.assertTrue(memory.continues_search("三千以内的"))

    def test_relative_continues(self):
        self.assertTrue(memory.continues_search("再便宜点"))

    def test_district_switch_continues(self):
        self.assertTrue(memory.continues_search("换成沙河口"))

    def test_clear_continues(self):
        self.assertTrue(memory.continues_search("不限租金"))

    def test_feature_continues(self):
        self.assertTrue(memory.continues_search("要带电梯的"))

    def test_small_talk_does_not_continue(self):
        """判据要收紧，否则「今天天气不错」也会被当成在改需求。"""
        for text in ["今天天气不错", "谢谢", "嗯嗯", ""]:
            self.assertFalse(memory.continues_search(text), text)


class TestDeterminism(unittest.TestCase):
    """记忆是推导出来的，不是存下来的——同一串对话必须重放出同一结果。"""

    def test_replay_is_stable(self):
        texts = ["中山区两室一厅", "三千以内", "再便宜点", "要带电梯"]
        first = memory.replay(texts)
        second = memory.replay(texts)
        self.assertEqual(first, second)

    def test_replay_equals_step_by_step(self):
        texts = ["中山区", "两室一厅", "三千以内"]
        slots, features = {}, []
        for text in texts:
            slots, features, _ = memory.apply_turn(slots, features, text)
        self.assertEqual((slots, features), memory.replay(texts))


class TestDescribe(unittest.TestCase):

    def test_empty(self):
        self.assertEqual(memory.describe({}, []), "")

    def test_full(self):
        slots, features = memory.replay(["中山区两室一厅，两千到三千，带电梯"])
        text = memory.describe(slots, features)
        self.assertIn("中山区", text)
        self.assertIn("两室一厅", text)
        self.assertIn("2000", text)

    def test_ceiling_only(self):
        self.assertIn("以内", memory.describe({"zujia_max": 3000}, []))

    def test_floor_only(self):
        self.assertIn("以上", memory.describe({"zujia_min": 2000}, []))


class TestResolveHouse(unittest.TestCase):
    """指代消解。"""

    def test_ordinal(self):
        self.assertEqual(
            memory.resolve_house("那第二套呢", ["A", "B", "C"]), "B")

    def test_digit_form(self):
        self.assertEqual(memory.resolve_house("第3套", ["A", "B", "C"]), "C")

    def test_deictic_points_at_first(self):
        self.assertEqual(memory.resolve_house("那套怎么样", ["A", "B"]), "A")

    def test_last(self):
        self.assertEqual(memory.resolve_house("最后一套", ["A", "B"]), "B")

    def test_out_of_range_is_distinguishable(self):
        """说了第五套但只有两套，宁可不认，也不能随手给一套。

        **返回值从 None 改成了 OUT_OF_RANGE**（G1）。原来越界和
        「压根没说序号」都返回 None，上层分不开这两种情况，于是
        「第二十套怎么样」和「今天天气不错」得到同一句回话——
        而前者该回「这一组只有 10 套」，后者才该退回去问条件。
        """
        self.assertIs(memory.resolve_house("第五套", ["A", "B"]),
                      memory.OUT_OF_RANGE)

    def test_out_of_range_needs_a_list_first(self):
        """还没列过任何房源时说「第五套」，是「没有上下文」不是「越界」。"""
        self.assertIsNone(memory.resolve_house("第五套", []))

    def test_no_reference(self):
        self.assertIsNone(memory.resolve_house("中山区两室一厅", ["A"]))

    def test_no_previous_result(self):
        self.assertIsNone(memory.resolve_house("那第二套", []))
