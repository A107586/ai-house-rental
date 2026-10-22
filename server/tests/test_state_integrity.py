"""状态与日期的一致性：已出租只由合同推进、退租关掉在途支付、续租日期跟起租日、带看记录的关联（2026-10-23）。

这几条都是两份记录各说各话：房源说「在架」而合同还生效着，
支付单说「等待付款」而合同已经结束，续租账单排在 28 日而合同写的是 31 日，
带看纪要挂着别的房子的预约。
"""

import calendar
from datetime import datetime, timedelta

import app.db as db
from app import leasing
from app.main import REMIND_TITLE
from app.models import Contract, House, Message, User
from tests.base import ApiTestCase, auth_header, end_lease, make_admin
from tests.test_agent_broker import BrokerCase


def _day(days, hour=0):
    return (datetime.now() + timedelta(days=days)).replace(
        hour=hour, minute=0, second=0, microsecond=0)


class IntegrityCase(ApiTestCase):

    def setUp(self):
        super().setUp()
        self.client.post("/api/auth/register", json={
            "username": "tenant2", "password": "test123", "real_name": "租客乙",
            "phone": "13900001111"})
        self.landlord = auth_header(self.client, "landlord1")
        self.t1 = auth_header(self.client, "tenant1")
        self.t2 = auth_header(self.client, "tenant2")

    def lease(self, tenant="tenant1", headers=None, house="DL0002", start=None):
        r = self.client.post("/api/contracts", headers=self.landlord, json={
            "house_no": house, "tenant_username": tenant,
            "start_date": (start or _day(2)).isoformat(), "months": 12})
        self.assertEqual(r.status_code, 200, r.text)
        no = r.json()["contract"]["contract_no"]
        r = self.client.post("/api/contracts/%s/activate" % no, headers=headers or self.t1)
        self.assertEqual(r.status_code, 200, r.text)
        return no


class TestRentedFollowsContracts(IntegrityCase):

    def test_admin_cannot_relist_a_house_with_an_active_lease(self):
        """原先管理员一改「在架」，另一位租客就能签约生效：同一套房两份生效中的合同。"""
        self.lease()
        admin = make_admin(self.client)
        r = self.client.patch("/api/houses/DL0002/status?status=available", headers=admin)
        self.assertEqual(r.status_code, 409)
        self.assertIn("生效中的租约", r.json()["detail"])
        session = db.SessionLocal()
        try:
            self.assertEqual(session.query(House).filter_by(house_no="DL0002").one().status,
                             "rented")
        finally:
            session.close()

    def test_nobody_marks_rented_by_hand(self):
        admin = make_admin(self.client)
        r = self.client.patch("/api/houses/DL0001/status?status=rented", headers=admin)
        self.assertEqual(r.status_code, 409)

    def test_admin_can_still_fix_a_rented_house_without_a_lease(self):
        """种子里的 DL0003 是「已出租」却没有合同——这种对不上的数据管理员得修得动。"""
        admin = make_admin(self.client)
        r = self.client.patch("/api/houses/DL0003/status?status=offline", headers=admin)
        self.assertEqual(r.status_code, 200, r.text)


class TestLeaseEndClosesPayments(IntegrityCase):

    def test_checkout_left_open_across_lease_end(self):
        """退租前开着收银台、退租后付款：原先支付单永远「等待付款」，查状态每次都抛异常。"""
        no = self.lease(start=_day(-10))
        bills = self.client.get("/api/bills/mine?contract_no=%s" % no,
                                headers=self.t1).json()["list"]
        rent = next(b for b in bills if b["type"] == "rent")
        trade = self.client.post("/api/bills/%s/pay" % rent["bill_no"], headers=self.t1,
                                 json={"method": "alipay"}).json()["payment"]["trade_no"]
        end_lease(self.client, no, self.landlord, self.t1)

        r = self.client.get("/api/payments/%s/status" % trade, headers=self.t1)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["payment"]["status"], "closed")
        r = self.client.post("/api/payments/%s/mock-pay" % trade, headers=self.t1)
        self.assertEqual(r.status_code, 409)


class TestRenewalDates(ApiTestCase):
    """起租日按今天推算：确认签约时会查「最多往前补 30 天、往后一年」，写死的日子过几周就红。"""

    def _sign_and_activate(self, start, months):
        session = db.SessionLocal()
        house = session.query(House).filter_by(house_no="DL0002").one()
        tenant = session.query(User).filter_by(username="tenant1").one()
        contract = leasing.create_contract(session, house, tenant, start, months=months)
        leasing.activate_contract(session, contract)
        return session, contract

    def test_month_end_lease_renews_on_the_same_day(self):
        """8 月 31 日起租 6 个月，到期日是 2 月 28（29）日；续租原先从这天往后推，每月都成了 28 日。"""
        today = datetime.now()
        year = today.year if (today.month, today.day) <= (8, 31) else today.year + 1
        session, contract = self._sign_and_activate(datetime(year, 8, 31), 6)
        try:
            leasing.renew_contract(session, contract, months=3)
            feb = calendar.monthrange(year + 1, 2)[1]
            dues = sorted(b.due_date.date().isoformat()
                          for b in contract.bills if b.type == "rent")
            # 续租第一期从旧到期日（2 月最后一天）起，之后回到每月最后一天
            self.assertEqual(dues[-3:], ["%d-02-%02d" % (year + 1, feb),
                                         "%d-03-31" % (year + 1), "%d-04-30" % (year + 1)])
            self.assertEqual(contract.end_date.date().isoformat(), "%d-05-31" % (year + 1))
            self.assertEqual(len(dues), len(set(dues)))
        finally:
            session.close()

    def test_ordinary_renewal_unchanged(self):
        start = _day(3)
        session, contract = self._sign_and_activate(start, 12)
        try:
            leasing.renew_contract(session, contract, months=12)
            self.assertEqual(contract.end_date, leasing.add_months(start, 24))
            rents = [b for b in contract.bills if b.type == "rent"]
            self.assertEqual(len(rents), 24)
        finally:
            session.close()


class TestViewingRecordLinks(BrokerCase):

    def setUp(self):
        super().setUp()
        self.delegate("DL0001", rate=50)

    def record(self, **extra):
        body = {"house_no": "DL0001", "tenant_username": "tenant1", "showed_up": True,
                "intention": "high", "feedback": "采光满意"}
        body.update(extra)
        return self.client.post("/api/viewings", headers=self.agent, json=body)

    def test_plain_walk_in_record_still_works(self):
        r = self.record()
        self.assertEqual(r.status_code, 200, r.text)

    def test_appointment_must_belong_to_this_house_and_tenant(self):
        r = self.client.post("/api/appointments", headers=self.tenant, json={
            "house_no": "DL0002", "viewing_time": _day(2, 10).isoformat()})
        self.assertEqual(r.status_code, 200, r.text)
        other_house = r.json()["appointment"]["id"]
        r = self.record(appointment_id=other_house)
        self.assertEqual(r.status_code, 400)

    def test_viewer_must_be_able_to_act_as_tenant(self):
        r = self.record(tenant_username="landlord1")
        self.assertEqual(r.status_code, 400)


class TestRemindWording(IntegrityCase):

    def test_ended_lease_arrears_are_not_sent_to_the_online_cashier(self):
        """已结束合同的欠款线上付不了，提醒原先照样叫人「在账单里缴纳」。"""
        no = self.lease(start=_day(-29))
        session = db.SessionLocal()
        try:
            # 让第一期整段住完再退租：它就成了合同结束后的欠款
            c = session.query(Contract).filter_by(contract_no=no).one()
            for bill in c.bills:
                bill.due_date -= timedelta(days=40)
            c.start_date -= timedelta(days=40)
            c.end_date -= timedelta(days=40)
            session.commit()
            tenant_id = c.tenant_id
        finally:
            session.close()
        end_lease(self.client, no, self.landlord, self.t1)
        r = self.client.post("/api/landlord/tenants/%d/remind" % tenant_id,
                             headers=self.landlord)
        self.assertEqual(r.status_code, 200, r.text)
        session = db.SessionLocal()
        try:
            text = session.query(Message).filter_by(
                user_id=tenant_id, title=REMIND_TITLE).order_by(Message.id.desc()).first()
            self.assertIn("线下结清", text.content)
            self.assertNotIn("里缴纳", text.content)
        finally:
            session.close()
