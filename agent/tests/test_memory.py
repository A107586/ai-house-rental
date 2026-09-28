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
        """说了 MAX_TURNS 次条件之后，最早那次就不算数了。

        原先这个用例中间垫的是「随便看看」，而它根本不带条件；
        末尾又说了「中山区」，覆盖语义本来就会让断言成立——
        **无论窗口是几轮，这个用例都过**，等于什么也没盯住。
        现在垫的是真的在改条件的轮次。
        """
        texts = ["沙河口区"] + ["两室一厅", "三室一厅"] * memory.MAX_TURNS
        slots, _ = memory.replay(texts)
        self.assertNotIn("suozaidiqu", slots)

    def test_unrelated_turns_do_not_consume_the_window(self):
        """查账单、看评价这些轮次不该把找房条件挤出去。

        实测来源：一段 24 轮的会话，用户第 3 轮说了区域和预算，
        中间问了十几轮合同、账单、评价，到第 22 轮说「还有更便宜的」时
        条件已经全丢，系统答「建议把租价上限从 9 元提高一些」。
        """
        chat = ["我的合同什么时候到期", "这个月房租交了吗", "欠多少",
                "怎么缴费", "评价怎么样", "房东靠谱吗", "谢谢",
                "我约了哪几次看房", "我收藏了几套房", "报修处理了吗",
                "还有别的吗", "嗯"]
        slots, _ = memory.replay(["沙河口区两室一厅", "三千以内"] + chat)
        self.assertEqual(slots["suozaidiqu"], "沙河口区")
        self.assertEqual(slots["zujia_max"], 3000)
        self.assertEqual(slots["fangwuhuxing"], "两室一厅")

    def test_more_X_phrasings_are_recognised(self):
        """「还有更便宜的吗」是最常见的说法之一，原表一条都不命中。

        实测后果：24 轮那个剧本里预算纹丝不动，
        系统把上一轮那套 2950 元的原样又端了回来。
        """
        slots, _ = memory.replay(["沙河口两室一厅三千以内", "还有更便宜的吗"])
        self.assertLess(slots["zujia_max"], 3000)

        slots, _ = memory.replay(["三千以内", "有没有更贵的"])
        self.assertGreater(slots["zujia_max"], 3000)

    def test_more_X_does_not_collide_with_the_opposite_direction(self):
        """「更贵」不能同时撞上 cheaper 里的「太贵」「有点贵」「贵了」。"""
        self.assertEqual(memory.relative_ops("有没有更贵的"), ["pricier"])
        self.assertEqual(memory.relative_ops("还有更便宜的吗"), ["cheaper"])

    def test_reset_still_counts_as_a_turn(self):
        """「重新找」是条件性的轮次，不能因为它没带条件就被滤掉。"""
        slots, _ = memory.replay(["沙河口区两室一厅", "重新找", "中山区"])
        self.assertEqual(slots["suozaidiqu"], "中山区")
        self.assertNotIn("fangwuhuxing", slots)


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

    def test_negated_value_clears_its_own_slot(self):
        """「不用两室一厅」否定的是值，不是「户型」两个字。

        这一条卡过真实会话：CLEAR_TARGETS 里没有「两室一厅」，一条都命中不了，
        而 parse_query 照样从这句里解析出 fangwuhuxing=两室一厅——
        **用户说不要，系统反而给设上了**，上下文里那行「两室一厅」怎么都去不掉。
        """
        self.assertIn("fangwuhuxing", memory.clears("不用两室一厅"))
        slots, _ = memory.replay(["中山区两室一厅", "不用两室一厅"])
        self.assertNotIn("fangwuhuxing", slots)
        self.assertEqual(slots["suozaidiqu"], "中山区")

    def test_other_area_means_drop_the_district(self):
        """「找其他地区」是要换地方，不是一个叫「其他地区」的区。

        parse_query 的通用兜底认「两到四个汉字 + 区」，这句正好是这个形状，
        于是助手回过一句「区域改为 找其他地区。」，此后再也搜不到东西。
        """
        self.assertIn("suozaidiqu", memory.clears("那找其他地区3000以内的"))
        slots, _ = memory.replay(["中山区两室一厅", "那找其他地区3000以内的"])
        self.assertNotIn("suozaidiqu", slots)
        # 同一句里的价格是真条件，不能跟着一起丢
        self.assertEqual(slots["zujia_max"], 3000)
        self.assertEqual(slots["fangwuhuxing"], "两室一厅")

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



class TestUnknownArea(unittest.TestCase):
    """「公司在高新园区」——不是行政区，却被当成区域条件，
    此后每轮从历史重放出来，整段对话都是 0 套。"""

    def test_unknown_area_is_dropped_with_a_note(self):
        slots, _, notes = memory.apply_turn({}, [], "公司在高新园区，预算三千左右")
        self.assertNotIn("suozaidiqu", slots)
        self.assertEqual(slots.get("zujia_max"), 3000)
        self.assertTrue(any("高新园区" in n and "对应不到" in n for n in notes))

    def test_unknown_area_does_not_stick_across_turns(self):
        slots, _ = memory.replay(["公司在高新园区", "预算三千左右", "最好离地铁近点"])
        self.assertNotIn("suozaidiqu", slots)

    def test_negation_stops_at_the_comma(self):
        """否定只管到下一个标点，也只去掉被否定的那个值。"""
        base = {"suozaidiqu": "中山区", "fangwuhuxing": "两室一厅"}
        slots, _, _ = memory.apply_turn(dict(base), [], "不要太贵的，三千以内")
        self.assertEqual(slots.get("zujia_max"), 3000,
                         "「太贵」只是在解释为什么给三千，不该再打折")
        slots, _, _ = memory.apply_turn(dict(base), [], "不用两室一厅，三室的有吗")
        self.assertEqual(slots.get("fangwuhuxing"), "三室")
        slots, _, _ = memory.apply_turn(dict(base, yajin_yue=1, fukuan_yue=1), [],
                                        "取消押一付一的限制，两千以内")
        self.assertNotIn("yajin_yue", slots)
        self.assertEqual(slots.get("zujia_max"), 2000)
        # 明确要再调的照旧叠加
        slots, _, _ = memory.apply_turn(dict(base), [], "三千以内，再便宜点")
        self.assertEqual(slots.get("zujia_max"), 2400)

    def test_payment_terms_become_conditions(self):
        """「押一付一的有吗」原先解析不出任何条件，照旧列出上一批房。"""
        slots, _, _ = memory.apply_turn({"zujia_max": 2000}, [], "押一付一的有吗")
        self.assertEqual(slots, {"zujia_max": 2000, "yajin_yue": 1, "fukuan_yue": 1})
        self.assertIn("押一付一", memory.describe(slots, []))
        # 押零是有效值，不能被当成没说
        slots, _, _ = memory.apply_turn({}, [], "押零付一的")
        self.assertEqual(slots.get("yajin_yue"), 0)
        from app.agents.property_finder.tools.search_properties import build_params
        params = build_params(slots)
        self.assertEqual((params["deposit_months"], params["pay_months"]), (0, 1))
        self.assertEqual(memory.parse_query("押1付12")["filters"]["fukuan_yue"], 12)

    def test_short_district_name_is_normalized(self):
        slots, _, _ = memory.apply_turn({}, [], "x", filters={"suozaidiqu": "沙河口"})
        self.assertEqual(slots["suozaidiqu"], "沙河口区")
