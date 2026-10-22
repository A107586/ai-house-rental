"""三处拍板：调低佣金要中介同意、房东确认线下收款、工单状态只往前走（2026-10-23）。

三处原先都是「一方说了算」或「一条死路」：付佣金的房东自己定比例，
合同结束后的欠款线下结清了也记不下来，已完成的工单能被改回待处理。
"""

from datetime import datetime, timedelta

import app.db as db
from app.models import Bill, Contract, Payment
from tests.base import auth_header, end_lease, make_resident
from tests.test_agent_broker import BrokerCase


def _soon(days=3):
    return (datetime.now() + timedelta(days=days)).replace(
        hour=0, minute=0, second=0, microsecond=0).isoformat()


class DecisionCase(BrokerCase):

    def sign(self, house="DL0001", **terms):
        body = {"house_no": house, "tenant_username": "tenant1",
                "start_date": _soon(), "months": 12}
        body.update(terms)
        r = self.client.post("/api/contracts", headers=self.landlord, json=body)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["contract"]

    def titles(self, headers):
        return [m["title"] for m in self.client.get(
            "/api/messages", headers=headers).json()["list"]]


class TestLoweredCommissionNeedsTheAgent(DecisionCase):
    """原先房东签约时把比例改成 0，中介只收到一条「有异议请联系房东」。"""

    def commission_of(self, no):
        rows = self.client.get("/api/agent/commissions", headers=self.agent).json()["list"]
        return next(c for c in rows if c["contract_no"] == no)

    def test_lowered_rate_without_consent_settles_at_the_delegation_rate(self):
        self.delegate("DL0001", rate=50)
        no = self.sign(rent=2000, commission_rate=0)["contract_no"]
        self.client.post("/api/contracts/%s/activate" % no, headers=self.tenant)
        row = self.commission_of(no)
        self.assertEqual((row["rate"], row["amount"]), (50, 1000))

    def test_agent_can_reject(self):
        self.delegate("DL0001", rate=50)
        no = self.sign(rent=2000, commission_rate=10)["contract_no"]
        r = self.client.post("/api/contracts/%s/commission" % no, headers=self.agent,
                             json={"accept": False})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("中介不同意调整佣金比例", self.titles(self.landlord))
        self.client.post("/api/contracts/%s/activate" % no, headers=self.tenant)
        self.assertEqual(self.commission_of(no)["amount"], 1000)

    def test_agent_can_agree(self):
        self.delegate("DL0001", rate=50)
        no = self.sign(rent=2000, commission_rate=30)["contract_no"]
        proposals = self.client.get("/api/agent/dashboard",
                                    headers=self.agent).json()["todo"]["commission_proposals"]
        self.assertEqual([(p["contract_no"], p["proposed_rate"], p["delegation_rate"])
                          for p in proposals], [(no, 30, 50)])
        self.client.post("/api/contracts/%s/commission" % no, headers=self.agent,
                         json={"accept": True})
        self.client.post("/api/contracts/%s/activate" % no, headers=self.tenant)
        self.assertEqual(self.commission_of(no)["amount"], 600)

    def test_raised_rate_needs_no_consent(self):
        self.delegate("DL0001", rate=50)
        got = self.sign(rent=2000, commission_rate=60)
        self.assertEqual(got["commission_state"], "")
        self.client.post("/api/contracts/%s/activate" % got["contract_no"], headers=self.tenant)
        self.assertEqual(self.commission_of(got["contract_no"])["amount"], 1200)

    def test_only_the_delegated_agent_can_respond(self):
        self.delegate("DL0001", rate=50)
        no = self.sign(rent=2000, commission_rate=10)["contract_no"]
        other = self.make_agent("agent2")
        r = self.client.post("/api/contracts/%s/commission" % no, headers=other,
                             json={"accept": True})
        self.assertEqual(r.status_code, 403)
        r = self.client.post("/api/contracts/%s/commission" % no, headers=self.landlord,
                             json={"accept": True})
        self.assertEqual(r.status_code, 403)

    def test_cannot_respond_after_activation(self):
        self.delegate("DL0001", rate=50)
        no = self.sign(rent=2000, commission_rate=10)["contract_no"]
        self.client.post("/api/contracts/%s/activate" % no, headers=self.tenant)
        r = self.client.post("/api/contracts/%s/commission" % no, headers=self.agent,
                             json={"accept": True})
        self.assertEqual(r.status_code, 409)

    def test_landlord_raising_it_back_clears_the_pending_state(self):
        self.delegate("DL0001", rate=50)
        no = self.sign(rent=2000, commission_rate=10)["contract_no"]
        got = self.client.patch("/api/contracts/%s" % no, headers=self.landlord,
                                json={"commission_rate": 50}).json()["contract"]
        self.assertEqual(got["commission_state"], "")


class TestOfflineReceipt(DecisionCase):
    """合同结束后的欠款线上付不了（_guard_payable），原先线下结清了也没地方记。"""

    def _ended_with_arrears(self):
        """一份已结束、留着一张「住过没交」租金的合同。"""
        no = self.sign(house="DL0002")["contract_no"]
        self.client.post("/api/contracts/%s/activate" % no, headers=self.tenant)
        # 把起租日挪到两个月前：第一期整段住完，退租后它就是欠款
        session = db.SessionLocal()
        try:
            c = session.query(Contract).filter_by(contract_no=no).one()
            shift = timedelta(days=65)
            c.start_date -= shift
            c.end_date -= shift
            for bill in c.bills:
                bill.due_date -= shift
            session.commit()
        finally:
            session.close()
        end_lease(self.client, no, self.landlord, self.tenant)
        bills = self.client.get("/api/bills/mine?contract_no=%s" % no,
                                headers=self.tenant).json()["list"]
        arrears = [b for b in bills if b["type"] == "rent" and b["status"] == "unpaid"]
        self.assertTrue(arrears)
        return no, arrears[0]

    def test_tenant_cannot_pay_arrears_online(self):
        _, bill = self._ended_with_arrears()
        r = self.client.post("/api/bills/%s/pay" % bill["bill_no"], headers=self.tenant,
                             json={"method": "alipay"})
        self.assertEqual(r.status_code, 409)

    def test_landlord_records_offline_receipt(self):
        _, bill = self._ended_with_arrears()
        r = self.client.post("/api/bills/%s/offline-receipt" % bill["bill_no"],
                             headers=self.landlord)
        self.assertEqual(r.status_code, 200, r.text)
        got = r.json()["bill"]
        self.assertEqual(got["status"], "paid")
        self.assertEqual(got["payment"]["method"], "offline")
        self.assertIn("账单已确认收款", self.titles(self.tenant))
        # 收支流水里有这一笔
        records = self.client.get("/api/money/records?side=landlord",
                                  headers=self.landlord).json()
        self.assertIn(bill["bill_no"], str(records))
        # 再点一次不会收两遍
        r = self.client.post("/api/bills/%s/offline-receipt" % bill["bill_no"],
                             headers=self.landlord)
        self.assertEqual(r.status_code, 409)

    def test_only_the_payee_can_record(self):
        _, bill = self._ended_with_arrears()
        r = self.client.post("/api/bills/%s/offline-receipt" % bill["bill_no"],
                             headers=self.tenant)
        self.assertEqual(r.status_code, 403)
        r = self.client.post("/api/bills/%s/offline-receipt" % bill["bill_no"],
                             headers=self.admin)
        self.assertEqual(r.status_code, 200, r.text)

    def test_refused_while_tenant_is_paying_online(self):
        no = self.sign(house="DL0002")["contract_no"]
        self.client.post("/api/contracts/%s/activate" % no, headers=self.tenant)
        bill = next(b for b in self.client.get("/api/bills/mine?contract_no=%s" % no,
                                               headers=self.tenant).json()["list"]
                    if b["type"] == "rent")
        started = self.client.post("/api/bills/%s/pay" % bill["bill_no"], headers=self.tenant,
                                   json={"method": "alipay"})
        self.assertEqual(started.json()["payment"]["status"], "pending")
        r = self.client.post("/api/bills/%s/offline-receipt" % bill["bill_no"],
                             headers=self.landlord)
        self.assertEqual(r.status_code, 409)
        self.assertIn("正在线上支付", r.json()["detail"])

    def test_cancelled_bill_cannot_be_received(self):
        no = self.sign(house="DL0002")["contract_no"]
        self.client.post("/api/contracts/%s/activate" % no, headers=self.tenant)
        end_lease(self.client, no, self.landlord, self.tenant)
        session = db.SessionLocal()
        try:
            cancelled = session.query(Bill).join(Contract).filter(
                Contract.contract_no == no, Bill.status == "cancelled").first()
            bill_no = cancelled.bill_no
            paid_before = session.query(Payment).count()
        finally:
            session.close()
        r = self.client.post("/api/bills/%s/offline-receipt" % bill_no, headers=self.landlord)
        self.assertEqual(r.status_code, 409)
        session = db.SessionLocal()
        try:
            self.assertEqual(session.query(Payment).count(), paid_before)
        finally:
            session.close()


class TestRepairOnlyMovesForward(DecisionCase):

    def repair(self):
        make_resident()
        r = self.client.post("/api/repairs", headers=self.tenant,
                             json={"house_no": "DL0001", "content": "厨房水龙头漏水"})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["repair"]["id"]

    def set(self, rid, status):
        return self.client.patch("/api/repairs/%d" % rid, headers=self.landlord,
                                 json={"status": status})

    def test_done_cannot_go_back(self):
        rid = self.repair()
        self.assertEqual(self.set(rid, "processing").status_code, 200)
        self.assertEqual(self.set(rid, "done").status_code, 200)
        r = self.set(rid, "pending")
        self.assertEqual(r.status_code, 409)
        self.assertIn("重新报修", r.json()["detail"])
        self.assertEqual(self.set(rid, "processing").status_code, 409)

    def test_processing_cannot_go_back_to_pending(self):
        rid = self.repair()
        self.set(rid, "processing")
        self.assertEqual(self.set(rid, "pending").status_code, 409)

    def test_repeating_done_does_not_notify_twice(self):
        rid = self.repair()
        self.set(rid, "done")
        self.set(rid, "done")
        self.assertEqual(self.titles(self.tenant).count("报修已处理完成"), 1)
