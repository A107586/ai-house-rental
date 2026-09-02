"""AI-07 房源推荐与 AI-05 经营分析测试。

推荐的重点是**可解释**与**不硬推**：每条要说得出理由，
样本不足时宁可退回最新房源。

报告的重点是**数字可追溯**：需求 AI-05 写明不允许出现模型编造的数据，
所以有专门用例验证大模型篡改数字时改写会被丢弃。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agents.recommend import agent as recommend_agent  # noqa: E402
from app.agents.recommend.tools.profile import build_profile, describe  # noqa: E402
from app.agents.recommend.tools.score import recommend, score_house  # noqa: E402
from app.agents.report import agent as report_agent  # noqa: E402
from app.agents.report.tools.analyze import analyze, to_text  # noqa: E402
from app.llm.base import LLMProvider, LLMUnavailable, NullProvider  # noqa: E402


def house(no, district="中山区", layout="两室一厅", price=2800, area=70,
          rent_type="整租", tags=None, rating=0):
    return {
        "house_no": no, "title": "%s%s" % (district, layout),
        "district": district, "layout": layout, "rent_price": price,
        "area": area, "rent_type": rent_type, "tags": tags or [],
        "rating": rating,
    }


class FakeBackend(object):

    def __init__(self, history=None, favorites=None, candidates=None,
                 stats=None, fail=False):
        self._history = history or []
        self._favorites = favorites or []
        self._candidates = candidates or []
        self._stats = stats or {}
        self.fail = fail
        self.token_seen = None

    def list_history(self, token):
        self.token_seen = token
        return self._history

    def list_favorites(self, token):
        return self._favorites

    def list_candidates(self, params=None, limit=100):
        if self.fail:
            raise RuntimeError("模拟后端不可用")
        # 模拟后端的参数校验：/api/houses 的 limit 上限就是 100
        if limit > 100:
            raise RuntimeError("limit 超过后端上限 100")
        self.limit_seen = limit
        return self._candidates

    def stats(self):
        return self._stats


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


# ==================================================== 偏好画像

class TestProfile(unittest.TestCase):

    def test_empty(self):
        profile = build_profile()
        self.assertEqual(profile["sample"], 0)
        self.assertEqual(profile["confidence"], 0.0)

    def test_district_and_layout(self):
        profile = build_profile(history=[
            house("A"), house("B"), house("C", district="西岗区")])
        self.assertEqual(profile["districts"][0][0], "中山区")
        self.assertEqual(profile["layouts"][0][0], "两室一厅")

    def test_favorite_outweighs_history(self):
        """收藏是明确的正向信号，权重必须高于随手点开。"""
        profile = build_profile(
            history=[house("A", district="西岗区"),
                     house("B", district="西岗区")],
            favorites=[house("C", district="中山区")])
        self.assertEqual(profile["districts"][0][0], "中山区")

    def test_repeat_view_adds_weight_but_converges(self):
        """看十次不代表喜欢十倍，加成要收敛。"""
        once = build_profile(history=[dict(house("A"), my_view_count=1)])
        many = build_profile(history=[dict(house("A"), my_view_count=10)])
        self.assertGreater(many["districts"][0][1], once["districts"][0][1])
        self.assertLessEqual(many["districts"][0][1], 4)

    def test_price_range(self):
        profile = build_profile(history=[
            house("A", price=2000), house("B", price=3000)])
        self.assertEqual(profile["price"]["min"], 2000)
        self.assertEqual(profile["price"]["max"], 3000)
        self.assertEqual(profile["price"]["avg"], 2500)

    def test_confidence_grows_with_sample(self):
        few = build_profile(history=[house("A")])
        many = build_profile(history=[house(str(i)) for i in range(8)])
        self.assertLess(few["confidence"], many["confidence"])

    def test_describe_mentions_district_and_price(self):
        profile = build_profile(history=[house("A"), house("B")])
        text = describe(profile)
        self.assertIn("中山区", text)
        self.assertIn("2800", text)


# ==================================================== 打分

class TestScore(unittest.TestCase):

    def setUp(self):
        self.profile = build_profile(
            history=[house("A", tags=["电梯", "地铁"]),
                     house("B", tags=["电梯"])],
            favorites=[house("C", tags=["电梯", "地铁"])])

    def test_perfect_match_scores_high(self):
        score, reasons = score_house(
            house("X", tags=["电梯", "地铁"]), self.profile)
        self.assertGreater(score, 80)
        self.assertTrue(reasons)

    def test_mismatch_scores_low(self):
        score, _ = score_house(
            house("X", district="旅顺口区", layout="三室两厅",
                  price=9000, rent_type="合租"), self.profile)
        self.assertLess(score, 20)

    def test_every_hit_has_a_reason(self):
        """推荐必须能解释，「算法觉得你喜欢」说不过去。"""
        _, reasons = score_house(house("X", tags=["电梯"]), self.profile)
        for reason in reasons:
            self.assertTrue(reason.strip())

    def test_seen_house_is_penalized(self):
        fresh, _ = score_house(house("X"), self.profile)
        seen, reasons = score_house(house("X"), self.profile, seen={"X"})
        self.assertLess(seen, fresh)
        self.assertIn("你看过这套", reasons)

    def test_cheaper_than_usual_is_a_plus(self):
        _, reasons = score_house(house("X", price=2400), self.profile)
        self.assertTrue(any("便宜" in r or "区间内" in r for r in reasons))

    def test_recommend_sorts_by_score(self):
        items = recommend([
            house("far", district="旅顺口区", layout="三室两厅", price=9000),
            house("near", tags=["电梯", "地铁"]),
        ], self.profile)
        self.assertEqual(items[0]["house_no"], "near")

    def test_recommend_drops_low_scores(self):
        """推一堆不相关的比不推更伤信任。"""
        items = recommend([house("far", district="旅顺口区", layout="三室两厅",
                                 price=99999, rent_type="合租")], self.profile)
        self.assertEqual(items, [])

    def test_limit(self):
        pool = [house("H%d" % i, tags=["电梯"]) for i in range(20)]
        self.assertEqual(len(recommend(pool, self.profile, limit=4)), 4)


# ==================================================== 推荐智能体

class TestRecommendAgent(unittest.TestCase):

    def test_no_history_falls_back(self):
        backend = FakeBackend()
        result = recommend_agent.run(token="t", backend=backend)
        self.assertEqual(result["status"], "fallback")
        self.assertFalse(result["ok"])

    def test_too_few_samples_falls_back(self):
        """样本太少时硬推不如不推。"""
        backend = FakeBackend(history=[house("A")])
        result = recommend_agent.run(token="t", backend=backend)
        self.assertEqual(result["status"], "fallback")

    def test_recommends_with_enough_history(self):
        backend = FakeBackend(
            history=[house(str(i), tags=["电梯"]) for i in range(5)],
            favorites=[house("F", tags=["电梯", "地铁"])],
            candidates=[house("NEW", tags=["电梯", "地铁"]),
                        house("BAD", district="旅顺口区", layout="三室两厅",
                              price=9000)])
        result = recommend_agent.run(token="t", backend=backend)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["items"][0]["house_no"], "NEW")
        self.assertTrue(result["items"][0]["match_reasons"])

    def test_token_is_passed_through(self):
        """智能体不持有身份，令牌必须原样转发给业务后端。"""
        backend = FakeBackend(history=[house(str(i)) for i in range(5)],
                              candidates=[house("NEW")])
        recommend_agent.run(token="abc123", backend=backend)
        self.assertEqual(backend.token_seen, "abc123")

    def test_candidate_pool_within_backend_limit(self):
        """回归：曾经请求 limit=120，被后端参数校验拒绝，推荐直接报错。

        假后端不校验参数，这个问题只有真跑起来才暴露，所以在假后端里
        也加上同样的上限校验。
        """
        backend = FakeBackend(history=[house(str(i)) for i in range(6)],
                              candidates=[house("NEW")])
        result = recommend_agent.run(token="t", backend=backend)
        self.assertEqual(result["status"], "ok")
        self.assertLessEqual(backend.limit_seen, 100)

    def test_backend_failure_is_graceful(self):
        backend = FakeBackend(history=[house(str(i)) for i in range(6)],
                              fail=True)
        result = recommend_agent.run(token="t", backend=backend)
        self.assertEqual(result["status"], "error")
        self.assertFalse(result["ok"])

    def test_no_match_returns_empty_status(self):
        backend = FakeBackend(
            history=[house(str(i)) for i in range(6)],
            candidates=[house("BAD", district="旅顺口区", layout="三室两厅",
                              price=99999, rent_type="合租")])
        result = recommend_agent.run(token="t", backend=backend)
        self.assertEqual(result["status"], "empty")


# ==================================================== 经营分析

STATS = {
    "house_total": 108, "house_rented": 13, "house_available": 95,
    "occupancy_rate": 12.0, "avg_rent": 2821,
    "income_received": 42350, "income_pending": 103250, "overdue_count": 2,
    "monthly_income": [{"month": "2026-08", "amount": 20000},
                       {"month": "2026-09", "amount": 31150}],
    "appointment_pending": 3, "repair_pending": 4, "repair_urgent": 2,
    "contract_active": 6,
    "by_district": [
        {"district": "沙河口区", "count": 18, "avg_rent": 3381},
        {"district": "旅顺口区", "count": 18, "avg_rent": 1956},
    ],
}


class TestAnalyze(unittest.TestCase):

    def test_four_sections(self):
        result = analyze(STATS)
        titles = [s["title"] for s in result["sections"]]
        self.assertEqual(titles, ["整体概况", "收租情况", "区域分布", "运营待办"])

    def test_low_occupancy_advice(self):
        text = to_text(analyze(STATS))
        self.assertIn("出租率偏低", text)

    def test_high_occupancy_advice(self):
        text = to_text(analyze(dict(STATS, occupancy_rate=88.0)))
        self.assertIn("高位", text)

    def test_overdue_becomes_alert(self):
        result = analyze(STATS)
        self.assertTrue(any("逾期" in a for a in result["alerts"]))

    def test_urgent_repair_becomes_alert(self):
        result = analyze(STATS)
        self.assertTrue(any("紧急报修" in a for a in result["alerts"]))

    def test_no_overdue_says_so(self):
        text = to_text(analyze(dict(STATS, overdue_count=0)))
        self.assertIn("没有逾期", text)

    def test_month_over_month_trend(self):
        text = to_text(analyze(STATS))
        self.assertIn("增加", text)

    def test_district_gap(self):
        text = to_text(analyze(STATS))
        self.assertIn("沙河口区", text)
        self.assertIn("旅顺口区", text)

    def test_empty(self):
        self.assertEqual(analyze({})["sections"], [])


class TestReportAgent(unittest.TestCase):

    def test_rules_only_without_llm(self):
        result = report_agent.run(STATS, provider=NullProvider())
        self.assertEqual(result["source"], "rules")
        self.assertTrue(result["ok"])
        self.assertIn("108", result["text"])

    def test_llm_polish_accepted_when_numbers_intact(self):
        original = to_text(analyze(STATS))
        provider = FakeProvider(payload=original + " 补充一句衔接。")
        result = report_agent.run(STATS, provider=provider)
        self.assertEqual(result["source"], "llm_polished")

    def test_llm_polish_rejected_when_numbers_changed(self):
        """需求 AI-05 硬性要求：数字必须可追溯，改写不得篡改数据。"""
        provider = FakeProvider(payload="出租率约一成，收租情况良好。")
        result = report_agent.run(STATS, provider=provider)
        self.assertEqual(result["source"], "rules", "篡改数字的改写必须丢弃")
        self.assertIn("108", result["text"])

    def test_llm_failure_falls_back(self):
        provider = FakeProvider(fail=True)
        result = report_agent.run(STATS, provider=provider)
        self.assertEqual(result["source"], "rules")

    def test_polish_can_be_disabled(self):
        original = to_text(analyze(STATS))
        provider = FakeProvider(payload=original + " 额外内容。")
        result = report_agent.run(STATS, provider=provider, polish=False)
        self.assertEqual(result["source"], "rules")
        self.assertEqual(provider.calls, 0)

    def test_no_data(self):
        result = report_agent.run({}, provider=NullProvider())
        self.assertEqual(result["status"], "no_data")
        self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main()
