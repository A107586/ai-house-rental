# -*- coding: utf-8 -*-
"""房源讲解与对比。

这两件事是**替用户拿主意**，所以最大的风险不是算错，是**说得比事实多**：
一句「性价比超高」用户信了、跑去看了半天房、发现是骗人的——从那一刻起
这个助手说的每一句话他都不会再信，包括那些是真的。

所以用例大都压在「不许编」和「短板不许藏」上。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agents.support.tools import pitch  # noqa: E402


class FakeBackend(object):
    """只回同区同户型的挂牌，用来算价位。"""

    def __init__(self, rows=None):
        self.rows = rows or []

    def get(self, path, params=None, token=None):
        return {"list": self.rows}


GOOD = {
    "house_no": "DL0007", "title": "中山区青泥洼桥 两室一厅 78平",
    "district": "中山区", "community": "青泥洼桥", "layout": "两室一厅",
    "area": 78.6, "rent_price": 4150, "deposit": 4150, "floor": "8/30层",
    "orientation": "朝南", "elevator": True, "rating": 5.0, "review_count": 3,
    "view_count": 366, "service_fee": 0, "verify_status": "verified",
    "tags": ["可做饭", "有阳台", "采光好", "地铁", "有家具"],
}

PEERS = [
    {"area": 78.6, "rent_price": 4150},
    {"area": 72.6, "rent_price": 3200},
    {"area": 62.2, "rent_price": 2650},
]


class TestHighlights(unittest.TestCase):

    def test_every_highlight_points_at_a_field(self):
        """**亮点必须落在字段上。**

        标签里没写的东西一个字都不许出现——这是这段话可信的全部依据。
        """
        got = pitch.highlights(GOOD)
        self.assertTrue(got)
        # 房子没有的标签，不许在话里出现
        self.assertNotIn("宠物", " ".join(got))
        self.assertNotIn("医院", " ".join(got))

    def test_no_highlights_invented_for_a_bare_listing(self):
        bare = {"house_no": "DL9", "layout": "一室", "rent_price": 1000,
                "tags": [], "rating": 0, "review_count": 0, "view_count": 3,
                "service_fee": 100}
        self.assertEqual(pitch.highlights(bare), [])

    def test_rating_needs_someone_to_have_rated(self):
        """没人评过就别说「住过的租客给了 0 分」。"""
        quiet = dict(GOOD, rating=0, review_count=0)
        self.assertNotIn("分", " ".join(pitch.highlights(quiet)))


class TestCautions(unittest.TestCase):
    """**短板不许藏。** 租客当天就能看见房子。"""

    def test_high_floor_without_a_lift_is_said_out_loud(self):
        walkup = dict(GOOD, floor="6/6层", elevator=False)
        self.assertIn("电梯", " ".join(pitch.cautions(walkup)))

    def test_unverified_listing_is_disclosed(self):
        raw = dict(GOOD, verify_status="unverified")
        self.assertIn("核验", " ".join(pitch.cautions(raw)))

    def test_being_expensive_is_disclosed_not_spun(self):
        """比同类贵就说贵，还要告诉他这条可以拿去谈。

        推销的边界在这里：**可以帮他争取，不可以帮着掩盖。**
        """
        price = pitch.price_context(FakeBackend(PEERS), GOOD)
        self.assertGreater(price["diff"], 0)
        first = pitch.cautions(GOOD, price)[0]
        self.assertIn("高", first)
        self.assertIn("谈", first)

    def test_price_caution_comes_first_because_it_is_the_actionable_one(self):
        """说话时只说得下两条，第一条得是最有用的那条。"""
        price = pitch.price_context(FakeBackend(PEERS), GOOD)
        got = pitch.cautions(dict(GOOD, floor="1/30层"), price)
        self.assertIn("中位价", got[0])


class TestPriceContext(unittest.TestCase):

    def test_compares_unit_price_not_total_rent(self):
        """**比的是每平米。**

        同样是两室一厅，78 平和 62 平的总价本来就该差一截，
        拿总价说「便宜 1500」是在误导。
        """
        price = pitch.price_context(FakeBackend(PEERS), GOOD)
        self.assertAlmostEqual(price["unit"], 52.8, places=1)
        self.assertEqual(price["scope"], "中山区两室一厅")

    def test_a_single_listing_is_not_a_market(self):
        """就一套同类房源时不给结论——「比中位价低 0%」是句废话。"""
        self.assertEqual(pitch.price_context(FakeBackend(PEERS[:1]), GOOD), {})

    def test_backend_failure_does_not_break_the_pitch(self):
        class Broken(object):
            def get(self, path, params=None, token=None):
                raise RuntimeError("挂了")

        self.assertEqual(pitch.price_context(Broken(), GOOD), {})
        message, data = pitch.describe(Broken(), GOOD)
        self.assertTrue(message)


class TestDescribe(unittest.TestCase):

    def test_does_not_repeat_what_the_title_already_says(self):
        """标题里写着「中山区青泥洼桥 两室一厅 78平」，再复述一遍就成了
        「…78平，中山区青泥洼桥，79 平米」——**而且 78.6 进位成 79，
        和标题自相矛盾**。"""
        message, _ = pitch.describe(FakeBackend(PEERS), GOOD)
        self.assertEqual(message.count("青泥洼桥"), 1)
        self.assertNotIn("79", message)

    def test_ends_with_who_it_suits(self):
        message, data = pitch.describe(FakeBackend(PEERS), GOOD)
        self.assertTrue(data["suits"])
        self.assertTrue(message.strip().endswith(data["suits"]))


class TestCompare(unittest.TestCase):

    A = dict(GOOD)
    B = {"house_no": "DL0009", "title": "中山区青泥洼桥 两室一厅 72平",
         "district": "中山区", "community": "青泥洼桥", "layout": "两室一厅",
         "area": 72.6, "rent_price": 3200, "deposit": 3200, "floor": "1/29层",
         "orientation": "朝南", "rating": 0, "review_count": 0,
         "tags": ["有家具"], "verify_status": "unverified"}

    def test_needs_two_to_compare(self):
        message, data = pitch.compare(FakeBackend(), [self.A])
        self.assertEqual(message, "")
        self.assertEqual(data, {})

    def test_marks_the_better_side_only_where_there_is_one(self):
        """户型、朝向、位置没有高下之分，不该有任何一边被标成更优。"""
        _, data = pitch.compare(FakeBackend(), [self.A, self.B])
        by_label = {row["label"]: row for row in data["rows"]}
        self.assertEqual(by_label["月租"]["best"], 1)     # 便宜的那套
        self.assertEqual(by_label["面积"]["best"], 0)     # 大的那套
        self.assertIsNone(by_label["户型"]["best"])
        self.assertIsNone(by_label["朝向"]["best"])

    def test_no_reviews_is_not_a_zero_score(self):
        """**没人评过不等于评了 0 分。**

        照原样填会写出「5 分 / 0 分」，还把另一套标成更优——
        这是拿「没有数据」当成了「数据很差」。
        """
        _, data = pitch.compare(FakeBackend(), [self.A, self.B])
        row = [r for r in data["rows"] if r["label"] == "评分"][0]
        self.assertEqual(row["values"][1], "—")
        self.assertIsNone(row["best"])

    def test_does_not_declare_an_overall_winner(self):
        """**一套便宜一套大，谁更好取决于这个人缺钱还是缺地方。**

        替他下这个结论是越界。能做的是把差在哪里摆清楚。
        """
        message, _ = pitch.compare(FakeBackend(), [self.A, self.B])
        self.assertNotIn("推荐你选", message)
        self.assertNotIn("更值得", message)
        self.assertIn("预算紧", message)

    def test_identical_values_are_not_both_marked_best(self):
        same = dict(self.B, rent_price=self.A["rent_price"])
        _, data = pitch.compare(FakeBackend(), [self.A, same])
        row = [r for r in data["rows"] if r["label"] == "月租"][0]
        self.assertIsNone(row["best"])


BUDGET = {"house_no": "DL0008", "title": "中山区桂林街 两室一厅 62平",
          "district": "中山区", "layout": "两室一厅", "area": 62.2,
          "rent_price": 2650, "rating": 0, "review_count": 0,
          "tags": ["地铁", "可做饭", "电梯", "有阳台", "可养宠物"],
          "rent_type": "整租", "verify_status": "unverified"}

MID = {"house_no": "DL0009", "title": "中山区青泥洼桥 两室一厅 72平",
       "district": "中山区", "layout": "两室一厅", "area": 72.6,
       "rent_price": 3200, "rating": 0, "review_count": 0,
       "tags": ["有家具", "可做饭"], "rent_type": "公寓",
       "verify_status": "unverified"}

# 一套「乏善可陈的贵房子」：没人评过、没核验、配套也少。
# 拿它和 BUDGET 比，胜负是清楚的；GOOD 那种（已核验、三条好评、
# 面积最大）和 BUDGET 比才是真的难分——两种情形各测一遍。
PLAIN = {"house_no": "DL0010", "title": "中山区人民路 两室一厅 70平",
         "district": "中山区", "layout": "两室一厅", "area": 70.0,
         "rent_price": 4200, "rating": 0, "review_count": 0,
         "tags": ["可做饭"], "rent_type": "整租",
         "verify_status": "unverified"}


class TestBestOf(unittest.TestCase):
    """从结果里挑一套先看。**挑错了比不挑更糟**——用户会照着去看房。"""

    def test_picks_the_one_that_wins_on_price(self):
        house, reason = pitch.best_of([PLAIN, MID, BUDGET])
        self.assertEqual(house["house_no"], "DL0008")
        self.assertIn("最便宜", reason)

    def test_the_reason_names_real_fields(self):
        """理由里的每个数字都要能在房源字段里找到。"""
        _, reason = pitch.best_of([PLAIN, MID, BUDGET])
        self.assertIn("42.6", reason)          # 2650 / 62.2

    def test_a_single_five_star_review_does_not_beat_a_much_lower_price(self):
        """**一条五星评价不是口碑，是一个人的看法。**

        不设门槛的话，刚上架、被评过一次五分的房子就能压过每月便宜
        一千五的那套——而用户真正在意的是后者。
        """
        thin = dict(PLAIN, rating=5.0, review_count=1)
        house, _ = pitch.best_of([thin, BUDGET])
        self.assertEqual(house["house_no"], "DL0008")

    def test_one_house_is_nothing_to_choose_from(self):
        self.assertEqual(pitch.best_of([GOOD])[0], None)

    def test_a_near_tie_refuses_to_pick(self):
        """**「差不多」的时候硬挑一套，是这类功能最容易失信的地方。**

        用户点进去发现和第二套没区别，下一次就不会再看这行字了。
        """
        twin = dict(BUDGET, house_no="DL0088")
        self.assertEqual(pitch.best_of([BUDGET, twin])[0], None)

    def test_a_stated_preference_counts(self):
        """他明确说过要「有家具」，那就不该只按价格排。"""
        scored = pitch.rank([BUDGET, MID], features=["有家具"])
        by_no = {h["house_no"]: s for s, _, h in scored}
        self.assertGreater(by_no["DL0009"], 0.8)


class TestLeadLine(unittest.TestCase):

    def test_says_which_one_and_why(self):
        line = pitch.lead_line([PLAIN, MID, BUDGET])
        self.assertIn("最推荐", line)
        self.assertIn("DL0008", line)

    def test_a_tie_explains_the_trade_off_instead_of_going_quiet(self):
        """**不确定的时候该说的是「凭什么难选」，不是不吭声。**

        沉默把问题原样退回给了用户，而他正是因为选不出来才问的。

        GOOD 已核验、三条好评、面积最大，BUDGET 单价最低——
        这两套**本来就难分**，说「各强在哪」才是实话。
        """
        line = pitch.lead_line([GOOD, BUDGET])
        self.assertTrue(line)
        self.assertIn("看你更在意", line)

    def test_nothing_to_say_about_a_lone_result(self):
        self.assertEqual(pitch.lead_line([GOOD]), "")


class TestFollowups(unittest.TestCase):
    """推荐词。**必须从这一组结果里长出来。**"""

    def test_only_suggests_a_tag_that_actually_narrows(self):
        """全都带电梯时推「只看带电梯的」，点了等于没点。"""
        both = [dict(BUDGET), dict(MID, tags=["电梯", "有家具"])]
        chips = pitch.followups(both, {}, [])
        self.assertNotIn("只看带电梯的", chips)

    def test_suggests_a_tag_that_some_have_and_some_do_not(self):
        """一半有、一半没有的标签才筛得动，才值得推。

        用两套只在「有家具」上有差别的房源，免得这条断言被
        排在更前面的地铁、电梯挤掉——推荐词一共只放得下几条。
        """
        bare = dict(MID, house_no="DL0011", tags=["可做饭"])
        chips = pitch.followups([MID, bare], {}, [])
        self.assertIn("只看带家具的", chips)

    def test_does_not_repeat_what_the_user_already_asked_for(self):
        chips = pitch.followups([MID, BUDGET], {}, ["有家具"])
        self.assertNotIn("只看带家具的", chips)

    def test_no_cheaper_suggestion_when_the_prices_are_all_alike(self):
        """三套都是 2650 元，推「再便宜点的」是无的放矢。"""
        same = [dict(BUDGET), dict(BUDGET, house_no="DL0088")]
        self.assertNotIn("再便宜点的", pitch.followups(same, {}, []))

    def test_no_cheaper_suggestion_when_he_already_set_a_ceiling(self):
        chips = pitch.followups([BUDGET, MID], {"zujia_max": 3000}, [])
        self.assertNotIn("再便宜点的", chips)

    def test_always_leaves_a_way_forward(self):
        """条件很全、结果很齐时，用户面前不能只剩一个光秃秃的输入框。"""
        chips = pitch.followups([BUDGET, MID], {"zujia_max": 3000}, [])
        self.assertTrue(chips)
        self.assertIn("介绍下第一套", chips)

    def test_capped_so_they_fit_on_one_line(self):
        chips = pitch.followups([BUDGET, MID, PLAIN], {}, [])
        self.assertLessEqual(len(chips), 4)

    def test_no_results_means_no_suggestions(self):
        self.assertEqual(pitch.followups([], {}, []), [])


if __name__ == "__main__":
    unittest.main()
