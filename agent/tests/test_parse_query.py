"""parse_query 与中文数字解析的单元测试。

刻意使用标准库 unittest，不依赖 pytest 或任何第三方包——
组员拉下代码后无需装环境即可运行：

    python -m unittest discover -s tests -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.cn_number import cn_to_int  # noqa: E402
from app.agents.property_finder import agent  # noqa: E402
from app.agents.property_finder.tools.parse_query import parse_query  # noqa: E402


class TestChineseNumber(unittest.TestCase):

    def test_simple(self):
        self.assertEqual(cn_to_int("三千"), 3000)
        self.assertEqual(cn_to_int("八千"), 8000)
        self.assertEqual(cn_to_int("一万"), 10000)

    def test_spoken_ellipsis(self):
        """「两千五」在口语里是 2500，不是 2005。"""
        self.assertEqual(cn_to_int("两千五"), 2500)
        self.assertEqual(cn_to_int("一万二"), 12000)
        self.assertEqual(cn_to_int("三千二"), 3200)

    def test_ten_prefix(self):
        """「十五」省略了前面的一。"""
        self.assertEqual(cn_to_int("十五"), 15)
        self.assertEqual(cn_to_int("二十"), 20)

    def test_arabic_passthrough(self):
        self.assertEqual(cn_to_int("1500"), 1500)

    def test_invalid(self):
        self.assertIsNone(cn_to_int("你好"))
        self.assertIsNone(cn_to_int(""))
        self.assertIsNone(cn_to_int(None))


class TestDistrict(unittest.TestCase):

    def test_full_name(self):
        self.assertEqual(parse_query("中山区的房子")["filters"]["suozaidiqu"], "中山区")

    def test_short_name(self):
        """用户常省略「区」字。"""
        self.assertEqual(parse_query("沙河口 单间")["filters"]["suozaidiqu"], "沙河口区")

    def test_generic_area(self):
        """非大连的区县也要能识别。"""
        self.assertEqual(parse_query("朝阳区两室一厅")["filters"]["suozaidiqu"], "朝阳区")


class TestLayout(unittest.TestCase):

    def test_shi_ting(self):
        self.assertEqual(parse_query("两室一厅")["filters"]["fangwuhuxing"], "两室一厅")
        self.assertEqual(parse_query("3室2厅")["filters"]["fangwuhuxing"], "三室两厅")

    def test_ju(self):
        self.assertEqual(parse_query("一居室")["filters"]["fangwuhuxing"], "一室")
        self.assertEqual(parse_query("两居")["filters"]["fangwuhuxing"], "两室")

    def test_single_room(self):
        self.assertEqual(parse_query("想租个单间")["filters"]["fangwuhuxing"], "一室")


class TestPrice(unittest.TestCase):

    def test_max_with_suffix(self):
        self.assertEqual(parse_query("租金三千以内")["filters"]["zujia_max"], 3000)
        self.assertEqual(parse_query("3000以下")["filters"]["zujia_max"], 3000)

    def test_max_with_prefix(self):
        self.assertEqual(parse_query("预算不超过五千")["filters"]["zujia_max"], 5000)

    def test_min(self):
        self.assertEqual(parse_query("2000以上的房子")["filters"]["zujia_min"], 2000)

    def test_range(self):
        """区间必须整体识别，不能被拆成两个单边条件。"""
        filters = parse_query("2000到3000的房子")["filters"]
        self.assertEqual(filters["zujia_min"], 2000)
        self.assertEqual(filters["zujia_max"], 3000)
        filters = parse_query("1500-2500")["filters"]
        self.assertEqual(filters["zujia_min"], 1500)
        self.assertEqual(filters["zujia_max"], 2500)

    def test_wan_unit(self):
        self.assertEqual(parse_query("不超过1.5万")["filters"]["zujia_max"], 15000)


class TestArea(unittest.TestCase):

    def test_min(self):
        self.assertEqual(parse_query("80平以上")["filters"]["mianji_min"], 80)

    def test_max(self):
        self.assertEqual(parse_query("100平米以内")["filters"]["mianji_max"], 100)

    def test_range(self):
        filters = parse_query("60到90平米")["filters"]
        self.assertEqual(filters["mianji_min"], 60)
        self.assertEqual(filters["mianji_max"], 90)


class TestRoomTypeAndFeatures(unittest.TestCase):

    def test_room_type(self):
        self.assertEqual(parse_query("合租的房子")["filters"]["fangyuanleixing"], "合租")
        self.assertEqual(parse_query("想整租")["filters"]["fangyuanleixing"], "整租")

    def test_features(self):
        features = parse_query("最好有电梯，离地铁近")["features"]
        self.assertIn("电梯", features)
        self.assertIn("地铁", features)


class TestConfidence(unittest.TestCase):

    def test_rich_query_high_confidence(self):
        result = parse_query("中山区两室一厅，租金三千以内，最好带电梯")
        self.assertGreaterEqual(result["confidence"], 0.8)

    def test_irrelevant_query_zero_confidence(self):
        """与租房无关的输入必须给低置信度，以便转交大模型判断。"""
        self.assertEqual(parse_query("今天天气怎么样")["confidence"], 0.0)

    def test_empty(self):
        result = parse_query("")
        self.assertEqual(result["confidence"], 0.0)
        self.assertEqual(result["filters"], {})


class TestRealWorldQueries(unittest.TestCase):
    """需求文档 AI-01 验收标准中的样例语句。"""

    def test_full_sentence(self):
        result = parse_query(
            "帮我找一个月租3000以内，两室一厅，距离学校近一点，最好有地铁的房子")
        self.assertEqual(result["filters"]["zujia_max"], 3000)
        self.assertEqual(result["filters"]["fangwuhuxing"], "两室一厅")
        self.assertIn("地铁", result["features"])
        self.assertIn("近学校", result["features"])

    def test_terse_query(self):
        result = parse_query("甘井子区 80平以上的三室 预算不超过五千")
        filters = result["filters"]
        self.assertEqual(filters["suozaidiqu"], "甘井子区")
        self.assertEqual(filters["fangwuhuxing"], "三室")
        self.assertEqual(filters["mianji_min"], 80)
        self.assertEqual(filters["zujia_max"], 5000)


if __name__ == "__main__":
    unittest.main()


class TestDistrictMustBeARealDistrict(unittest.TestCase):
    """**区域只可能是那几个。**

    大连的行政区是一张穷举表。规则解析器只认表里的，可大模型不受这个
    约束——实测它把「桂林街」填进了 `suozaidiqu`。后端按等值匹配一查
    就是 0 套，用户得到「没有找到符合条件的房源」，**而桂林街明明有
    三套房**。首页把两个搜索框合成一个之后，这条路上的每一次搜索都
    要经过这一层，错一次就是一次空结果。

    认不出来的不丢掉，改当关键词：后端的 keyword 会去比对小区、地址、
    商圈、标题，那正是街道名该去的地方。
    """

    def test_a_street_name_becomes_a_keyword_not_a_district(self):
        got = agent._sanitize({"suozaidiqu": "桂林街"})
        self.assertNotIn("suozaidiqu", got)
        self.assertEqual(got["guanjianci"], "桂林街")

    def test_a_real_district_is_kept(self):
        self.assertEqual(agent._sanitize({"suozaidiqu": "中山区"}),
                         {"suozaidiqu": "中山区"})

    def test_a_short_district_name_is_completed(self):
        """「沙河口」是简称，不是错的——补成全称，别丢进关键词。"""
        self.assertEqual(agent._sanitize({"suozaidiqu": "沙河口"}),
                         {"suozaidiqu": "沙河口区"})

    def test_other_conditions_survive_alongside_the_keyword(self):
        """「青泥洼桥两室一厅」：地名进关键词，户型照样留着。

        两个框合并之前这句话是**没人管得了**的——自然语言框会丢掉
        青泥洼桥，关键词框又不认户型。
        """
        got = agent._sanitize({"suozaidiqu": "青泥洼桥",
                               "fangwuhuxing": "两室一厅"})
        self.assertEqual(got["fangwuhuxing"], "两室一厅")
        self.assertEqual(got["guanjianci"], "青泥洼桥")

    def test_a_city_outside_dalian_is_not_treated_as_a_district(self):
        got = agent._sanitize({"suozaidiqu": "北京市"})
        self.assertNotIn("suozaidiqu", got)
