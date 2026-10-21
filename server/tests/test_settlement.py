# -*- coding: utf-8 -*-
"""退租结算与资金记录：已付未住的租金退回、支付尝试全留痕、收支流水。

## 为什么值得单独一个文件

**提前退租时，已经交了的、还没住的那几个月房租，原先不退。**
退租只作废「没交的、还没到期的」账单；押一付三的租客月初交了三个月、
月中搬走，后两个半月的钱就留在房东那边——而系统自己生成的合同里写着
「甲方应退还已付但尚未居住月份的租金」（2026-10-22 起按天退回，每笔一条退款流水）。

同一次还补了三件「钱的记录」上的事：

- 一张账单只许一条支付记录：收银台过期或失败后重新发起会撞唯一约束。
  现在每次尝试一条，成功、失败、超时关单都留着；
- 退款有接口没页面，退了多少、哪天退的谁也看不到。现在有一张合成的收支流水；
- 房东的「本月收租」按账单原额算，退回去的钱还算在收入里。
"""

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta

from sqlalchemy import create_engine

from app import db, migrations
from app.models import Bill, Contract, Payment
from tests.base import ApiTestCase, auth_header, end_lease, settle


class SettlementCase(ApiTestCase):

    def setUp(self):
        super().setUp()
        self.landlord = auth_header(self.client, "landlord1", "test123")
        self.tenant = auth_header(self.client, "tenant1", "test123")

    def lease(self, start, months=3, pay_months=1):
        no = self.client.post("/api/contracts", headers=self.landlord, json={
            "house_no": "DL0001", "tenant_username": "tenant1",
            "start_date": start.date().isoformat(), "months": months,
            "pay_months": pay_months}).json()["contract"]["contract_no"]
        self.client.post("/api/contracts/%s/activate" % no, headers=self.tenant)
        return no

    def shift_start(self, contract_no, days_ago):
        """把整份合同往前挪：起租日变成 days_ago 天前，账单到期日一起挪。"""
        session = db.SessionLocal()
        try:
            contract = session.query(Contract).filter_by(contract_no=contract_no).one()
            target = datetime.now().date() - timedelta(days=days_ago)
            delta = (contract.start_date.date() - target).days
            contract.start_date -= timedelta(days=delta)
            contract.end_date -= timedelta(days=delta)
            for bill in contract.bills:
                bill.due_date -= timedelta(days=delta)
            session.commit()
        finally:
            session.close()

    def bills(self, contract_no):
        rows = self.client.get("/api/bills/mine", headers=self.tenant,
                               params={"contract_no": contract_no}).json()["list"]
        return sorted(rows, key=lambda b: (b["due_date"], b["type"] != "deposit"))

    def rent_bills(self, contract_no):
        return [b for b in self.bills(contract_no) if b["type"] == "rent"]


class TestPrepaidRentIsRefunded(SettlementCase):

    def test_a_period_not_yet_started_is_refunded_in_full(self):
        no = self.lease(datetime.now() + timedelta(days=5), months=3)
        first = self.rent_bills(no)[0]
        settle(self.client, first["bill_no"], self.tenant)

        ended = end_lease(self.client, no, self.landlord, self.tenant, reason="不租了").json()
        self.assertEqual(ended["settlement"]["rent_refund"], first["amount"])
        records = ended["rent_refund_records"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["type"], "rent")
        self.assertEqual(records[0]["bill_no"], first["bill_no"])

        refunds = self.client.get("/api/refunds/mine", headers=self.tenant).json()["list"]
        self.assertIn("rent", [r["type"] for r in refunds])

    def test_the_current_period_is_refunded_by_days(self):
        """押一付三：起租 10 天后退租，这一期按天退回没住的部分。"""
        no = self.lease(datetime.now() + timedelta(days=1), months=3, pay_months=3)
        self.shift_start(no, days_ago=10)
        bill = self.rent_bills(no)[0]
        settle(self.client, bill["bill_no"], self.tenant)

        ended = end_lease(self.client, no, self.landlord, self.tenant).json()
        refund = ended["settlement"]["rent_refunds"][0]
        self.assertEqual(refund["lived_days"], 10)
        total = refund["total_days"]
        expected = round(bill["amount"] - round(bill["amount"] * 10 / total, 2), 2)
        self.assertAlmostEqual(refund["refund"], expected, places=2)
        self.assertGreater(refund["refund"], bill["amount"] * 0.8)

    def test_unpaid_current_period_is_cut_to_the_days_lived(self):
        """没交的这一期，退租后只欠住过的那几天，不是整期。"""
        no = self.lease(datetime.now() + timedelta(days=1), months=3)
        self.shift_start(no, days_ago=10)
        bill = self.rent_bills(no)[0]
        ended = end_lease(self.client, no, self.landlord, self.tenant).json()
        prorated = ended["settlement"]["prorated"][0]
        self.assertEqual(prorated["bill_no"], bill["bill_no"])
        self.assertLess(prorated["amount"], bill["amount"])
        after = next(b for b in self.rent_bills(no) if b["bill_no"] == bill["bill_no"])
        self.assertEqual(after["status"], "unpaid")
        self.assertAlmostEqual(after["amount"], prorated["amount"], places=2)
        self.assertIn("按实际居住", after["remark"])

    def test_preview_matches_what_actually_happens(self):
        no = self.lease(datetime.now() + timedelta(days=5), months=3)
        for bill in self.rent_bills(no)[:2]:
            settle(self.client, bill["bill_no"], self.tenant)
        preview = self.client.get("/api/contracts/%s/settlement" % no,
                                  headers=self.landlord, params={"deduct": 0}).json()
        ended = end_lease(self.client, no, self.landlord, self.tenant).json()
        self.assertEqual(preview["settlement"], ended["settlement"])

    def test_tenant_preview_uses_the_landlords_stated_deduction(self):
        no = self.lease(datetime.now() + timedelta(days=5), months=3)
        self.client.post("/api/contracts/%s/end" % no, headers=self.landlord,
                         json={"reason": "自住", "deduct": 200})
        preview = self.client.get("/api/contracts/%s/settlement" % no,
                                  headers=self.tenant, params={"deduct": 0}).json()
        self.assertEqual(preview["settlement"]["deduct"], 200.0)


class TestPaymentAttemptsAreKept(SettlementCase):

    def test_expired_and_failed_attempts_stay_on_the_bill(self):
        from app import gateway
        no = self.lease(datetime.now() + timedelta(days=5))
        bill = self.rent_bills(no)[0]
        first = self.client.post("/api/bills/%s/pay" % bill["bill_no"], headers=self.tenant,
                                 json={"method": "alipay"}).json()["payment"]
        session = db.SessionLocal()
        try:
            session.query(Payment).filter_by(trade_no=first["trade_no"]).one().expire_at = \
                datetime.now() - timedelta(minutes=1)
            session.commit()
        finally:
            session.close()
        second = self.client.post("/api/bills/%s/pay" % bill["bill_no"], headers=self.tenant,
                                  json={"method": "wechat"}).json()["payment"]
        payload = {"trade_no": second["trade_no"], "amount": float(second["amount"]),
                   "result": "fail", "reason": "余额不足"}
        payload["sign"] = gateway.sign(payload)
        self.client.post("/api/payments/notify", json=payload)
        settle(self.client, bill["bill_no"], self.tenant)

        after = next(b for b in self.rent_bills(no) if b["bill_no"] == bill["bill_no"])
        self.assertEqual(after["status"], "paid")
        self.assertEqual([a["status"] for a in after["attempts"]],
                         ["closed", "failed", "succeeded"])
        self.assertEqual(after["attempts"][1]["failure_reason"], "余额不足")
        self.assertEqual(after["payment"]["status"], "succeeded",
                         "bill.payment 是付成的那一笔，不是最近一次尝试")


class TestMoneyRecords(SettlementCase):

    def test_both_sides_see_the_same_money_from_their_own_side(self):
        no = self.lease(datetime.now() + timedelta(days=5), months=2)
        deposit = next(b for b in self.bills(no) if b["type"] == "deposit")
        rent = self.rent_bills(no)[0]
        settle(self.client, deposit["bill_no"], self.tenant)
        settle(self.client, rent["bill_no"], self.tenant)
        end_lease(self.client, no, self.landlord, self.tenant, deduct=100)

        mine = self.client.get("/api/money/records", headers=self.tenant,
                               params={"side": "tenant"}).json()
        theirs = self.client.get("/api/money/records", headers=self.landlord,
                                 params={"side": "landlord"}).json()
        kinds = sorted((r["kind"], r["title"]) for r in mine["list"])
        self.assertEqual(kinds, [("payment", "缴纳押金"), ("payment", "缴纳租金"),
                                 ("refund", "押金退款"), ("refund", "退回预付租金")])
        self.assertEqual(mine["total_out"], theirs["total_in"])
        self.assertEqual(mine["total_in"], theirs["total_out"])
        self.assertEqual(mine["total_in"], deposit["amount"] - 100 + rent["amount"])

    def test_landlord_income_is_net_of_refunds(self):
        no = self.lease(datetime.now() + timedelta(days=5), months=2)
        rent = self.rent_bills(no)[0]
        settle(self.client, rent["bill_no"], self.tenant)
        before = self.client.get("/api/landlord/dashboard", headers=self.landlord).json()
        end_lease(self.client, no, self.landlord, self.tenant)
        after = self.client.get("/api/landlord/dashboard", headers=self.landlord).json()
        self.assertEqual(before["month_income"] - after["month_income"], rent["amount"],
                         "退回去的预付租金不能还算在本月收租里")

    def test_strangers_see_nothing(self):
        no = self.lease(datetime.now() + timedelta(days=5))
        settle(self.client, self.rent_bills(no)[0]["bill_no"], self.tenant)
        self.client.post("/api/auth/register", json={
            "username": "outsider", "password": "secret123", "real_name": "路人"})
        outsider = auth_header(self.client, "outsider", "secret123")
        self.assertEqual(self.client.get("/api/money/records", headers=outsider).json()["total"], 0)


class TestMigration0021(unittest.TestCase):
    """旧库的 payments.bill_id 上有两条唯一索引，迁移后同一张账单能有多条支付记录。"""

    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.engine = create_engine("sqlite:///" + self.path.replace("\\", "/"), future=True)

    def tearDown(self):
        self.engine.dispose()
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def test_unique_indexes_are_dropped_and_refunds_get_columns(self):
        conn = sqlite3.connect(self.path)
        conn.execute("CREATE TABLE bills (id INTEGER PRIMARY KEY, contract_id INTEGER, type VARCHAR(20))")
        conn.execute("CREATE TABLE payments (id INTEGER PRIMARY KEY, bill_id INTEGER, trade_no VARCHAR(40))")
        conn.execute("CREATE UNIQUE INDEX ix_payments_bill_id ON payments (bill_id)")
        conn.execute("CREATE UNIQUE INDEX ux_payments_bill_id ON payments (bill_id)")
        conn.execute("CREATE TABLE refunds (id INTEGER PRIMARY KEY, contract_id INTEGER)")
        conn.execute("INSERT INTO bills (id, contract_id, type) VALUES (7, 3, 'deposit')")
        conn.execute("INSERT INTO refunds (contract_id) VALUES (3)")
        conn.commit()
        conn.close()

        step = next(f for v, _, f in migrations._STEPS if v == "0021")
        with self.engine.begin() as connection:
            step(connection, self.engine)

        conn = sqlite3.connect(self.path)
        try:
            conn.execute("INSERT INTO payments (bill_id, trade_no) VALUES (1, 'a')")
            conn.execute("INSERT INTO payments (bill_id, trade_no) VALUES (1, 'b')")
            columns = [row[1] for row in conn.execute("PRAGMA table_info(refunds)")]
            backfilled = conn.execute("SELECT type, bill_id FROM refunds").fetchone()
        finally:
            conn.close()
        self.assertIn("type", columns)
        self.assertIn("bill_id", columns)
        self.assertEqual(backfilled, ("deposit", 7), "已有的押金退款补上对应的押金账单")


if __name__ == "__main__":
    unittest.main()
