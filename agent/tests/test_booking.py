"""AI-06 智能预约看房测试。

所有用例都注入固定的「现在」（2026-09-01 周二 15:00），
否则断言结果会随运行日期漂移。
"""

import os
import sys
import unittest
from datetime import date, datetime, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agents.booking import agent  # noqa: E402
from app.agents.booking.tools.parse_datetime import parse_datetime  # noqa: E402
from app.agents.booking.tools.slots import (  # noqa: E402
    check_slot, find_conflicts, format_slot, slots_in_range, suggest_alternatives)

# 2026-09-01 是星期二
NOW = datetime(2026, 9, 1, 15, 0)


class TestParseWeekday(unittest.TestCase):

    def test_this_week(self):
        self.assertEqual(parse_datetime("这周六", now=NOW)["date"], date(2026, 9, 5))

    def test_next_week_uses_week_boundary(self):
        """回归测试：「下周一」是下周那个周一。

        曾经的实现先取「下一个周一」（今天周二，那已经是 9/7 了）再加七天，
        结果算成 9/14，多跳了整整一周。带修饰词时必须按周边界计算。
        """
        self.assertEqual(parse_datetime("下周一", now=NOW)["date"], date(2026, 9, 7))
        self.assertEqual(parse_datetime("下周六", now=NOW)["date"], date(2026, 9, 12))

    def test_bare_weekday_is_next_occurrence(self):
        self.assertEqual(parse_datetime("周四", now=NOW)["date"], date(2026, 9, 3))

    def test_this_weekday_already_passed_rolls_forward(self):
        """**「这周六」在周日说，指的是下一个周六，不是昨天那个。**

        按 ISO 周算，周日属于「这一周」的最后一天，于是「这周六」算出来是
        昨天——用户拿它去约看房，后端一句「不能预约过去的时间」把他挡回来，
        而他根本不知道自己哪里说错了。

        这个坑只在周日冒出来，平时跑测试撞不到；是某个周日跑验收用例时
        （AI-06 准确率从 100% 掉到 85%）才逮住的。所以这里把「现在」
        钉死在周日，不再靠运气。
        """
        sunday = datetime(2026, 9, 6, 15, 0)      # 周日下午三点
        self.assertEqual(parse_datetime("这周六", now=sunday)["date"],
                         date(2026, 9, 12))
        self.assertEqual(parse_datetime("这周五", now=sunday)["date"],
                         date(2026, 9, 11))

    def test_today_but_the_hour_already_passed_rolls_forward(self):
        """日期没过期、时刻过期了，同样要顺延。

        「这周日上午十点」在周日下午说，日期算出来就是今天——过期的是时刻。
        """
        sunday = datetime(2026, 9, 6, 15, 0)
        parsed = parse_datetime("这周日上午十点", now=sunday)
        self.assertEqual(parsed["date"], date(2026, 9, 13))
        self.assertEqual(parsed["time"], time(10, 0))

    def test_a_still_future_hour_today_is_not_pushed_away(self):
        """今天晚些时候还没到，就不该顺延——那是替用户做主。"""
        sunday = datetime(2026, 9, 6, 9, 0)
        parsed = parse_datetime("这周日下午三点", now=sunday)
        self.assertEqual(parsed["date"], date(2026, 9, 6))

    def test_plain_today_is_never_pushed_to_next_week(self):
        """只有星期表达才顺延。「今天下午三点」说晚了是用户自己说错，
        把它改成下周今天才叫离谱。"""
        late = datetime(2026, 9, 6, 16, 0)
        self.assertEqual(parse_datetime("今天下午三点", now=late)["date"],
                         date(2026, 9, 6))


class TestParseRelativeDay(unittest.TestCase):

    def test_tomorrow(self):
        self.assertEqual(parse_datetime("明天", now=NOW)["date"], date(2026, 9, 2))

    def test_day_after_tomorrow(self):
        self.assertEqual(parse_datetime("后天", now=NOW)["date"], date(2026, 9, 3))

    def test_absolute_date(self):
        self.assertEqual(parse_datetime("9月6号", now=NOW)["date"], date(2026, 9, 6))

    def test_past_date_rolls_to_next_year(self):
        """说「3月5号」时指的是下一个 3 月 5 号，不是已经过去的那个。"""
        self.assertEqual(parse_datetime("3月5号", now=NOW)["date"], date(2027, 3, 5))


class TestParseClock(unittest.TestCase):

    def test_afternoon_converts_to_24h(self):
        self.assertEqual(parse_datetime("下午两点", now=NOW)["time"], time(14, 0))

    def test_half_past(self):
        self.assertEqual(parse_datetime("下午3点半", now=NOW)["time"], time(15, 30))

    def test_morning(self):
        self.assertEqual(parse_datetime("上午十点", now=NOW)["time"], time(10, 0))

    def test_evening(self):
        self.assertEqual(parse_datetime("晚上7点", now=NOW)["time"], time(19, 0))

    def test_period_without_clock(self):
        """只说「下午」，用该时段的默认钟点。"""
        self.assertEqual(parse_datetime("明天下午", now=NOW)["time"], time(14, 0))


class TestParseRange(unittest.TestCase):

    def test_recent_days(self):
        result = parse_datetime("最近三天都可以", now=NOW)
        self.assertEqual(result["date_range"], (date(2026, 9, 1), date(2026, 9, 3)))
        self.assertIsNone(result["date"])

    def test_weekend(self):
        result = parse_datetime("这个周末", now=NOW)
        self.assertEqual(result["date_range"], (date(2026, 9, 5), date(2026, 9, 6)))

    def test_anytime(self):
        self.assertIsNotNone(parse_datetime("随时都行", now=NOW)["date_range"])


class TestSlots(unittest.TestCase):

    def test_conflict_within_an_hour(self):
        taken = [datetime(2026, 9, 5, 14, 0)]
        self.assertTrue(find_conflicts(datetime(2026, 9, 5, 14, 30), taken))
        self.assertFalse(find_conflicts(datetime(2026, 9, 5, 15, 0), taken))

    def test_rejects_past(self):
        verdict = check_slot(datetime(2026, 8, 30, 14, 0), [], now=NOW)
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["reason"], "past")

    def test_rejects_outside_business_hours(self):
        verdict = check_slot(datetime(2026, 9, 5, 22, 0), [], now=NOW)
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["reason"], "closed")

    def test_alternatives_skip_taken(self):
        taken = [datetime(2026, 9, 5, 9, 0), datetime(2026, 9, 5, 10, 0)]
        alternatives = suggest_alternatives(date(2026, 9, 5), taken, limit=2)
        self.assertEqual(alternatives[0], datetime(2026, 9, 5, 11, 0))

    def test_slots_in_range(self):
        slots = slots_in_range(date(2026, 9, 2), date(2026, 9, 3), [], limit=4)
        self.assertEqual(len(slots), 4)
        self.assertTrue(all(s.date() in (date(2026, 9, 2), date(2026, 9, 3)) for s in slots))

    def test_format(self):
        self.assertEqual(format_slot(datetime(2026, 9, 5, 14, 0)), "9月5日（周六）14:00")


class FakeBackend(object):

    def __init__(self, taken=None, fail_create=False):
        self.taken = taken or []
        self.fail_create = fail_create
        self.created = None

    def list_viewings(self, house_no):
        return self.taken

    def create_viewing(self, house_no, moment, applicant, token=None):
        if self.fail_create:
            raise RuntimeError("模拟写入失败")
        self.created = {"house_no": house_no, "moment": moment,
                        "applicant": applicant, "token": token}
        return {"ok": True}


class TestBookingAgent(unittest.TestCase):

    def test_creates_when_date_and_time_given(self):
        backend = FakeBackend()
        result = agent.run("这周六下午两点", house_no="F001",
                           applicant={"xingming": "张三"}, now=NOW, backend=backend)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "created")
        self.assertEqual(backend.created["moment"], datetime(2026, 9, 5, 14, 0))

    def test_asks_for_time_when_only_date(self):
        backend = FakeBackend()
        result = agent.run("这周六", house_no="F001", now=NOW, backend=backend)
        self.assertEqual(result["status"], "need_time")
        self.assertTrue(result["alternatives"])
        self.assertIsNone(backend.created)

    def test_offers_choices_for_vague_range(self):
        backend = FakeBackend()
        result = agent.run("最近三天都可以", house_no="F001", now=NOW, backend=backend)
        self.assertEqual(result["status"], "need_choice")
        self.assertTrue(result["alternatives"])

    def test_rejects_taken_slot_and_offers_alternatives(self):
        backend = FakeBackend(taken=[datetime(2026, 9, 5, 14, 0)])
        result = agent.run("这周六下午两点", house_no="F001", now=NOW, backend=backend)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "conflict")
        self.assertIn("已经被预约", result["message"])
        self.assertTrue(result["alternatives"])

    def test_rejects_outside_business_hours(self):
        backend = FakeBackend()
        result = agent.run("这周六晚上十点", house_no="F001", now=NOW, backend=backend)
        self.assertEqual(result["status"], "conflict")
        self.assertIn("09:00", result["message"])

    def test_unparseable_asks_again(self):
        backend = FakeBackend()
        result = agent.run("我想看房", house_no="F001", now=NOW, backend=backend)
        self.assertEqual(result["status"], "need_time")
        self.assertIn("没听清", result["message"])

    def test_backend_down_is_graceful(self):
        class DeadBackend(object):
            def list_viewings(self, house_no):
                raise RuntimeError("连不上")
        result = agent.run("这周六下午两点", house_no="F001", now=NOW,
                           backend=DeadBackend())
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "error")

    def test_create_failure_is_graceful(self):
        backend = FakeBackend(fail_create=True)
        result = agent.run("这周六下午两点", house_no="F001", now=NOW, backend=backend)
        self.assertFalse(result["ok"])
        self.assertIn("预约提交失败", result["message"])


if __name__ == "__main__":
    unittest.main()
