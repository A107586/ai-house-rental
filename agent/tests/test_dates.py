# -*- coding: utf-8 -*-
"""到期天数。

来源是一次长对话测试的实测失败：用户问「我的合同**还有几天**到期」，
系统答「租到 2027-09-01」——内容正确，但问的是天数、答的是日期。

这里盯住三件事：

1. **换算对**，且以「今天」为基准可注入，测试不会随日期漂移。
2. **边界上说人话**——今天到期、明天到期、已经过期，各有各的说法，
   「还有 0 天」和「还有 -5 天」都是病句。
3. **认不出的日期原样退回**，绝不猜。后端有一天换了日期格式，
   用户该看到原始值，而不是一个算错的天数。
"""

import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agents.support.tools.dates import days_left, until_text  # noqa: E402

TODAY = datetime.date(2026, 9, 22)


class TestDaysLeft(unittest.TestCase):

    def test_future_date(self):
        self.assertEqual(days_left("2027-09-01", TODAY), 344)

    def test_today_is_zero(self):
        self.assertEqual(days_left("2026-09-22", TODAY), 0)

    def test_past_date_is_negative(self):
        self.assertEqual(days_left("2026-09-10", TODAY), -12)

    def test_unparsable_returns_none(self):
        for bad in ("", None, "abc", "2026/09/22", 20260922):
            self.assertIsNone(days_left(bad, TODAY), bad)

    def test_iso_datetime_is_accepted(self):
        """后端有的接口回的是带时间的 ISO 串，截前 10 位照样能算。"""
        self.assertEqual(days_left("2027-09-01T00:00:00", TODAY), 344)


class TestUntilText(unittest.TestCase):

    def test_says_both_date_and_days(self):
        text = until_text("2027-09-01", TODAY)
        self.assertIn("2027-09-01", text)
        self.assertIn("344 天", text)

    def test_boundaries_read_like_a_person(self):
        self.assertIn("就是今天", until_text("2026-09-22", TODAY))
        self.assertIn("明天", until_text("2026-09-23", TODAY))
        self.assertIn("过期 12 天", until_text("2026-09-10", TODAY))

    def test_no_zero_or_negative_day_counts(self):
        """「还有 0 天」「还有 -5 天」是病句，任何输入都不该出现。"""
        for value in ("2026-09-22", "2026-09-10", "2026-01-01"):
            text = until_text(value, TODAY)
            self.assertNotIn("还有 0 天", text)
            self.assertNotIn("还有 -", text)

    def test_unparsable_falls_back_to_raw(self):
        self.assertEqual(until_text("2026/09/22", TODAY), "2026/09/22")
        self.assertEqual(until_text(None, TODAY), "")


if __name__ == "__main__":
    unittest.main()
