# -*- coding: utf-8 -*-
"""开场白与办事轨迹的测试。

这两件事都属于「主动服务」：不等用户问，先把他手头没办完的事摆出来，
以及把一件事走到哪一步画成一条轨迹。它们的共同风险不是算错，
而是**说得比事实多**——多报一笔账单、把没到的节点标成已完成，
用户会照着它做决定。所以用例大都压在「不能多说」上。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agents.support.tools import greeting, progress  # noqa: E402


class FakeBackend(object):
    """按路径喂数据的假后端。没准备的路径一律返回空，模拟接口不存在。"""

    def __init__(self, payloads=None, boom=()):
        self.payloads = payloads or {}
        self.boom = set(boom)
        self.calls = []

    def get(self, path, params=None, token=None):
        self.calls.append(path)
        if path in self.boom:
            raise RuntimeError("后端炸了")
        return self.payloads.get(path, {})


BUSY = {
    "/api/bills/mine": {"list": [
        {"bill_no": "ZD1", "amount": 2200, "status": "unpaid",
         "due_date": "2026-09-01", "overdue": True},
        {"bill_no": "ZD2", "amount": 1800, "status": "unpaid",
         "due_date": "2026-10-01"},
        {"bill_no": "ZD3", "amount": 2200, "status": "paid"},
    ]},
    "/api/appointments/mine": {"list": [
        {"id": 1, "status": "pending", "viewing_time": "2026-09-06 10:00:00"},
        {"id": 2, "status": "cancelled", "viewing_time": "2026-09-01 10:00:00"},
    ]},
    "/api/repairs": {"list": [
        {"id": 5, "status": "processing", "content": "厨房水管漏水"},
        {"id": 6, "status": "done", "content": "空调不制冷"},
    ]},
    "/api/messages": {"list": [{"id": 1, "title": "合同已结束"}], "unread": 2},
    "/api/reviews/pending": {"list": [{"contract_no": "HT9"}]},
}


class TestGreeting(unittest.TestCase):
    """开场白：先看这个人有什么没办完，再决定说什么。"""

    def test_guest_gets_the_generic_opening(self):
        got = greeting.build(FakeBackend(), "")
        self.assertFalse(got["personalised"])
        self.assertEqual(got["chips"], greeting.GUEST_CHIPS)

    def test_no_backend_call_without_a_token(self):
        """未登录时一次后端都不该调——没有身份，查谁的？"""
        backend = FakeBackend()
        greeting.build(backend, "")
        self.assertEqual(backend.calls, [])

    def test_busy_user_gets_their_own_todo_list(self):
        got = greeting.build(FakeBackend(BUSY), "tok")
        self.assertTrue(got["personalised"])
        self.assertIn("2 笔待缴账单", got["message"])
        self.assertIn("4000", got["message"])          # 2200 + 1800
        self.assertIn("已逾期", got["message"])
        self.assertIn("1 条看房预约", got["message"])

    def test_only_counts_what_is_actually_pending(self):
        """已缴的账单、已取消的预约、已完成的工单都不算数。

        多报一件事的代价是实的：用户会以为自己还欠着钱、还有约要赴。
        """
        got = greeting.build(FakeBackend(BUSY), "tok")
        self.assertNotIn("3 笔", got["message"])       # 第三笔已缴
        self.assertNotIn("2 条看房预约", got["message"])  # 第二条已取消
        self.assertIn("1 条报修", got["message"])      # 第二条已完成

    def test_money_first_then_waiting_then_optional(self):
        """排序就是重要性：要花钱的最前，等别人的居中，有空再弄的最后。"""
        items = greeting.collect(FakeBackend(BUSY), "tok")
        order = [item["chip"] for item in items]
        self.assertEqual(order[0], "交最近一笔房租")
        self.assertLess(order.index("查我的预约进度"), order.index("帮我评价"))

    def test_chips_are_capped_and_keep_a_general_exit(self):
        """一屏放不下五条。但要留一个通用出口，
        免得用户想问别的、面前却只有自己的账单。"""
        got = greeting.build(FakeBackend(BUSY), "tok")
        self.assertLessEqual(len(got["chips"]), 4)
        self.assertIn("押金怎么退", got["chips"])

    def test_idle_user_falls_back_to_the_generic_opening(self):
        got = greeting.build(FakeBackend({}), "tok")
        self.assertFalse(got["personalised"])

    def test_a_broken_backend_does_not_break_the_opening(self):
        """某个接口炸了，开场白照常出——它不值得让面板打不开。"""
        backend = FakeBackend(BUSY, boom=["/api/repairs", "/api/messages"])
        got = greeting.build(backend, "tok")
        self.assertTrue(got["personalised"])
        self.assertIn("待缴账单", got["message"])
        self.assertNotIn("报修", got["message"])

    def test_house_context_leads_with_that_house(self):
        """从房源详情页进来，第一句就该是关于这套房的。"""
        got = greeting.build(FakeBackend(BUSY), "tok", house={
            "house_no": "DL0007", "title": "中山区青泥洼桥 两室一厅 78平",
            "rent_price": 4150})
        self.assertIn("DL0007", got["message"])
        self.assertIn("4150", got["message"])
        self.assertIn("约看房", got["chips"])

    def test_house_context_wins_over_the_todo_list(self):
        """他正在看某套房，此刻关心的是这套房，不是上个月的账单。"""
        got = greeting.build(FakeBackend(BUSY), "tok",
                             house={"house_no": "DL0007"})
        self.assertNotIn("待缴账单", got["message"])


class TestProgress(unittest.TestCase):
    """办事轨迹：走到哪一步了。**说错一步比不给轨迹更糟。**"""

    def test_new_repair_waits_for_dispatch(self):
        steps = progress.repair_steps({"status": "pending",
                                       "created_at": "2026-09-03 10:00:00"})
        state = {s["label"]: s["state"] for s in steps}
        self.assertEqual(state["已提交"], progress.DONE)
        self.assertEqual(state["已派单"], progress.CURRENT)
        self.assertEqual(state["已完成"], progress.FUTURE)

    def test_assigned_repair_moves_on(self):
        steps = progress.repair_steps({"status": "pending",
                                       "assignee": "王师傅"})
        state = {s["label"]: s["state"] for s in steps}
        self.assertEqual(state["已派单"], progress.DONE)
        self.assertEqual(state["维修中"], progress.CURRENT)
        self.assertIn("王师傅", [s["note"] for s in steps][1])

    def test_finished_repair_is_all_done(self):
        steps = progress.repair_steps({"status": "done", "assignee": "王师傅"})
        self.assertTrue(all(s["state"] == progress.DONE for s in steps))

    def test_pending_booking_waits_for_the_landlord(self):
        steps = progress.booking_steps({"status": "pending",
                                        "viewing_time": "2099-01-01 10:00:00"})
        state = {s["label"]: s["state"] for s in steps}
        self.assertEqual(state["等房东确认"], progress.CURRENT)
        self.assertEqual(state["按时看房"], progress.FUTURE)

    def test_confirmed_but_not_yet_viewed(self):
        """**房东确认了不等于已经看过房。**

        标成已完成，用户会以为这件事结束了，今天就不出门了。
        """
        steps = progress.booking_steps({"status": "confirmed",
                                        "viewing_time": "2099-01-01 10:00:00"})
        state = {s["label"]: s["state"] for s in steps}
        self.assertEqual(state["等房东确认"], progress.DONE)
        self.assertEqual(state["按时看房"], progress.CURRENT)

    def test_a_past_viewing_is_done(self):
        steps = progress.booking_steps({"status": "confirmed",
                                        "viewing_time": "2020-01-01 10:00:00"})
        self.assertEqual(steps[-1]["state"], progress.DONE)

    def test_cancelled_booking_stops_early(self):
        steps = progress.booking_steps({"status": "cancelled"})
        self.assertEqual(len(steps), 2)
        self.assertIn("取消", steps[-1]["label"])

    def test_timeline_follows_the_row_the_sentence_is_about(self):
        """**轨迹要跟着 focus 走。**

        取数函数说的是「等房东确认的那一条」，而列表里第一条可能是
        已取消的——文字和轨迹各说各的，摆在同一张卡片上互相打脸。
        """
        data = {"list": [{"id": 2, "status": "cancelled"},
                         {"id": 1, "status": "pending"}],
                "focus": {"id": 1, "status": "pending"}}
        progress.attach("booking", data)
        self.assertEqual(data["timeline"]["of"]["id"], 1)

    def test_topics_without_a_timeline_get_none(self):
        """不是每类数据都该有时间轴。硬给账单套一条只会变成装饰。"""
        data = {"list": [{"bill_no": "ZD1"}]}
        progress.attach("bill", data)
        self.assertNotIn("timeline", data)


if __name__ == "__main__":
    unittest.main()
