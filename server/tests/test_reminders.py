# -*- coding: utf-8 -*-
"""定时提醒（app/reminders.py）：快到期、已逾期的账单，快到期、已到期的租约。

重心在两件事上：**同一件事只提醒一次**（跑多少轮都一样），以及
**只提醒还能办的事**（合同结束了的欠款、已经在走退租的租约都不再催）。
时间一律从今天推算，并把 now 直接传给 run()，不依赖跑测试的时刻。
"""

import unittest
from datetime import datetime, timedelta

import app.db as db
from app import reminders
from app.models import Bill, Contract, Message
from tests.base import ApiTestCase, auth_header, end_lease, make_admin


class ReminderCase(ApiTestCase):

    def setUp(self):
        super().setUp()
        self.landlord = auth_header(self.client, "landlord1", "test123")
        self.tenant = auth_header(self.client, "tenant1", "test123")

    def lease(self, months=12):
        start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        no = self.client.post("/api/contracts", headers=self.landlord, json={
            "house_no": "DL0001", "tenant_username": "tenant1",
            "start_date": start.isoformat(), "months": months}).json()["contract"]["contract_no"]
        self.client.post("/api/contracts/%s/activate" % no, headers=self.tenant)
        return no

    def shift(self, no, days):
        """整份合同往前挪 days 天（起止日、账单到期日一起挪）。"""
        session = db.SessionLocal()
        try:
            contract = session.query(Contract).filter_by(contract_no=no).one()
            contract.start_date -= timedelta(days=days)
            contract.end_date -= timedelta(days=days)
            for bill in contract.bills:
                bill.due_date -= timedelta(days=days)
            session.commit()
        finally:
            session.close()

    def sweep(self, now=None):
        session = db.SessionLocal()
        try:
            counts = reminders.run(session, now)
            session.commit()
            return counts
        finally:
            session.close()

    def titles(self, username):
        session = db.SessionLocal()
        try:
            from app.models import User
            uid = session.query(User).filter_by(username=username).one().id
            return [m.title for m in session.query(Message).filter_by(user_id=uid).all()]
        finally:
            session.close()


class TestBills(ReminderCase):

    def test_due_soon_is_sent_once(self):
        self.lease()                      # 押金和第一期房租都在今天到期
        first = self.sweep()
        self.assertGreaterEqual(first["due_soon"], 2)
        again = self.sweep()
        self.assertEqual(again["due_soon"], 0, "同一张账单只提醒一次")
        self.assertEqual(self.titles("tenant1").count(reminders.TITLE_DUE_SOON),
                         first["due_soon"])

    def test_overdue_is_sent_once(self):
        no = self.lease()
        self.shift(no, 10)                # 押金和第一期都过了 10 天
        counts = self.sweep()
        self.assertGreaterEqual(counts["overdue"], 2)
        self.assertEqual(self.sweep()["overdue"], 0)

    def test_paid_bills_are_left_alone(self):
        no = self.lease()
        session = db.SessionLocal()
        try:
            for bill in session.query(Bill).join(Contract).filter(
                    Contract.contract_no == no).all():
                bill.status = "paid"
            session.commit()
        finally:
            session.close()
        counts = self.sweep()
        self.assertEqual((counts["due_soon"], counts["overdue"]), (0, 0))

    def test_arrears_of_an_ended_lease_are_not_chased(self):
        """合同结束后的欠款在线上已经交不了，再发「快到期」「已逾期」只会让人去点一个交不了的按钮。"""
        no = self.lease()
        self.shift(no, 40)
        end_lease(self.client, no, self.landlord, self.tenant)
        counts = self.sweep()
        self.assertEqual((counts["due_soon"], counts["overdue"]), (0, 0))

    def test_far_future_bills_wait(self):
        no = self.lease()
        session = db.SessionLocal()
        try:
            for bill in session.query(Bill).join(Contract).filter(
                    Contract.contract_no == no).all():
                bill.due_date = datetime.now() + timedelta(days=20)
            session.commit()
        finally:
            session.close()
        self.assertEqual(self.sweep()["due_soon"], 0)


class TestLeases(ReminderCase):

    def test_lease_ending_within_a_month_tells_both_sides(self):
        no = self.lease(months=1)
        self.shift(no, 5)                 # 一个月可能是 31 天，往前挪几天才稳稳落在 30 天内
        counts = self.sweep()
        self.assertEqual(counts["lease_soon"], 2)
        self.assertIn(reminders.TITLE_LEASE_SOON, self.titles("tenant1"))
        self.assertIn(reminders.TITLE_LEASE_SOON, self.titles("landlord1"))
        self.assertEqual(self.sweep()["lease_soon"], 0)

    def test_lease_with_a_request_in_progress_is_not_nagged(self):
        no = self.lease(months=1)
        self.shift(no, 5)
        self.client.post("/api/contracts/%s/renew" % no, headers=self.tenant, json={"months": 12})
        self.assertEqual(self.sweep()["lease_soon"], 0)

    def test_expired_lease_reminds_the_landlord(self):
        no = self.lease(months=1)
        self.shift(no, 40)                # 已过期 10 天，还是生效中
        counts = self.sweep()
        self.assertEqual(counts["lease_over"], 1)
        self.assertIn(reminders.TITLE_LEASE_OVER, self.titles("landlord1"))
        self.assertNotIn(reminders.TITLE_LEASE_OVER, self.titles("tenant1"))


class TestAdminTrigger(ReminderCase):

    def test_admin_can_run_a_round_now(self):
        self.lease()
        self.assertEqual(self.client.post("/api/admin/reminders/run",
                                          headers=self.landlord).status_code, 403)
        admin = make_admin(self.client)
        r = self.client.post("/api/admin/reminders/run", headers=admin)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertGreater(r.json()["total"], 0)
        again = self.client.post("/api/admin/reminders/run", headers=admin)
        self.assertEqual(again.json()["total"], 0)


class TestPendingContracts(ReminderCase):
    """待确认的合同：等久了提醒租客一次；起租日过去太久、已经确认不了的，自动作废（2026-10-24）。"""

    def draft(self, start_in_days=5):
        start = (datetime.now() + timedelta(days=start_in_days)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        return self.client.post("/api/contracts", headers=self.landlord, json={
            "house_no": "DL0001", "tenant_username": "tenant1",
            "start_date": start.isoformat(), "months": 12}).json()["contract"]["contract_no"]

    def age(self, no, created_days_ago=0, start_days_ago=None):
        session = db.SessionLocal()
        try:
            contract = session.query(Contract).filter_by(contract_no=no).one()
            contract.created_at = datetime.now() - timedelta(days=created_days_ago)
            if start_days_ago is not None:
                contract.start_date = datetime.now() - timedelta(days=start_days_ago)
            session.commit()
        finally:
            session.close()

    def test_waiting_contract_nudges_the_tenant_once(self):
        no = self.draft()
        self.assertEqual(self.sweep()["contract_waiting"], 0, "刚发出的不催")
        self.age(no, created_days_ago=3)
        self.assertEqual(self.sweep()["contract_waiting"], 1)
        self.assertEqual(self.sweep()["contract_waiting"], 0)
        self.assertIn(reminders.TITLE_CONTRACT_WAITING, self.titles("tenant1"))

    def test_stale_contract_is_called_off(self):
        no = self.draft()
        self.age(no, created_days_ago=60, start_days_ago=45)
        self.assertEqual(self.sweep()["contract_stale"], 1)
        session = db.SessionLocal()
        try:
            self.assertEqual(session.query(Contract).filter_by(contract_no=no).one().status,
                             "cancelled")
        finally:
            session.close()
        self.assertIn("待确认的合同已失效", self.titles("landlord1"))
        self.assertIn("合同已失效", self.titles("tenant1"))


if __name__ == "__main__":
    unittest.main()
