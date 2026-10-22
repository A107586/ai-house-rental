"""待确认合同的去留：拒签 / 撤回、确认时的条款与起租日、停用房东的房子、凭合同私信的门槛（2026-10-23）。

这几条都是「流程走到一半，另一头的状态没跟上」：
合同从来没有取消这一步，确认时不再看起租日和条款有没有变过，
退租和驳回审核各自漏了停用房东与看房预约；一份待确认合同还能当成「签过约」去私信任何人。
"""

from datetime import datetime, timedelta

import app.db as db
from app.models import Appointment, Contract, House, Message, User
from tests.base import ApiTestCase, auth_header, end_lease, make_admin, settle


def _day(days, hour=0):
    return (datetime.now() + timedelta(days=days)).replace(
        hour=hour, minute=0, second=0, microsecond=0)


class LifecycleCase(ApiTestCase):

    def setUp(self):
        super().setUp()
        self.client.post("/api/auth/register", json={
            "username": "tenant2", "password": "test123", "real_name": "租客乙",
            "phone": "13900001111"})
        self.landlord = auth_header(self.client, "landlord1")
        self.t1 = auth_header(self.client, "tenant1")
        self.t2 = auth_header(self.client, "tenant2")

    def sign(self, tenant="tenant1", house="DL0002", start=None, **terms):
        body = {"house_no": house, "tenant_username": tenant,
                "start_date": (start or _day(3)).isoformat(), "months": 12}
        body.update(terms)
        r = self.client.post("/api/contracts", headers=self.landlord, json=body)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["contract"]

    def contract(self, no, headers=None):
        return self.client.get("/api/contracts/%s" % no,
                               headers=headers or self.landlord).json()

    def inbox(self, username):
        session = db.SessionLocal()
        try:
            uid = session.query(User).filter_by(username=username).one().id
            return [m.title for m in session.query(Message).filter_by(user_id=uid)]
        finally:
            session.close()

    def set_house(self, house_no, status):
        session = db.SessionLocal()
        try:
            session.query(House).filter_by(house_no=house_no).update({"status": status})
            session.commit()
        finally:
            session.close()


class TestCancelPendingContract(LifecycleCase):
    """模型的状态图画着「pending ──任一方取消──► cancelled」，接口却一直没有这一步。"""

    def test_tenant_can_decline(self):
        no = self.sign()["contract_no"]
        r = self.client.post("/api/contracts/%s/cancel" % no, headers=self.t1,
                             json={"reason": "价格再想想"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["contract"]["status"], "cancelled")
        self.assertIn("租客未接受合同", self.inbox("landlord1"))
        timeline = self.client.get("/api/contracts/%s/detail" % no,
                                   headers=self.t1).json()["timeline"]
        self.assertIn("合同已取消", [t["title"] for t in timeline])
        # 拒了就不能再确认
        r = self.client.post("/api/contracts/%s/activate" % no, headers=self.t1)
        self.assertEqual(r.status_code, 409)

    def test_landlord_can_withdraw(self):
        no = self.sign()["contract_no"]
        r = self.client.post("/api/contracts/%s/cancel" % no, headers=self.landlord)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["contract"]["end_reason"], "房东撤回")
        self.assertIn("合同已撤回", self.inbox("tenant1"))

    def test_active_contract_cannot_be_cancelled(self):
        no = self.sign()["contract_no"]
        self.client.post("/api/contracts/%s/activate" % no, headers=self.t1)
        r = self.client.post("/api/contracts/%s/cancel" % no, headers=self.t1)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.contract(no)["status"], "active")

    def test_outsider_cannot_cancel(self):
        no = self.sign()["contract_no"]
        r = self.client.post("/api/contracts/%s/cancel" % no, headers=self.t2)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.contract(no)["status"], "pending")

    def test_cancelled_contract_no_longer_blocks_revert_to_tenant(self):
        no = self.sign()["contract_no"]
        for house_no in ("DL0001", "DL0002", "DL0003"):
            self.set_house(house_no, "offline")
        r = self.client.post("/api/auth/revert-to-tenant", headers=self.landlord)
        self.assertEqual(r.status_code, 400)
        self.assertIn("合同尚未结束", r.json()["detail"])
        self.client.post("/api/contracts/%s/cancel" % no, headers=self.landlord)
        r = self.client.post("/api/auth/revert-to-tenant", headers=self.landlord)
        self.assertEqual(r.status_code, 200, r.text)


class TestCompetingContracts(LifecycleCase):

    def test_activation_cancels_other_pending_contracts_on_the_house(self):
        """原先 B 永远「待确认」，确认又 409「已被占用」，房东永远退不回租客。"""
        a = self.sign("tenant1")["contract_no"]
        b = self.sign("tenant2")["contract_no"]
        r = self.client.post("/api/contracts/%s/activate" % a, headers=self.t1)
        self.assertEqual(r.status_code, 200, r.text)
        got = self.contract(b, self.t2)
        self.assertEqual(got["status"], "cancelled")
        self.assertIn("已与其他租客签约", got["end_reason"])
        self.assertIn("合同已取消", self.inbox("tenant2"))

    def test_same_tenant_same_house_only_one_pending(self):
        first = self.sign()["contract_no"]
        r = self.client.post("/api/contracts", headers=self.landlord, json={
            "house_no": "DL0002", "tenant_username": "tenant1",
            "start_date": _day(3).isoformat(), "months": 6})
        self.assertEqual(r.status_code, 409)
        self.assertIn(first, r.json()["detail"])
        # 撤回之后可以重开
        self.client.post("/api/contracts/%s/cancel" % first, headers=self.landlord)
        self.sign(months=6)


class TestActivationChecks(LifecycleCase):

    def _age_start(self, no, days_ago):
        """模拟合同放了很久才确认：把起租日挪到 days_ago 天前。"""
        session = db.SessionLocal()
        try:
            c = session.query(Contract).filter_by(contract_no=no).one()
            c.start_date = _day(-days_ago)
            c.end_date = _day(-days_ago + 365)
            session.commit()
        finally:
            session.close()

    def test_stale_start_date_is_rejected_at_activation(self):
        """起租日 90 天前的合同原先一确认就是 4 期逾期账单。"""
        no = self.sign(start=_day(-29))["contract_no"]
        self._age_start(no, 90)
        r = self.client.post("/api/contracts/%s/activate" % no, headers=self.t1)
        self.assertEqual(r.status_code, 409)
        self.assertIn("起租日", r.json()["detail"])
        self.assertEqual(self.contract(no)["status"], "pending")

    def test_start_date_within_window_still_activates(self):
        no = self.sign(start=_day(-20))["contract_no"]
        r = self.client.post("/api/contracts/%s/activate" % no, headers=self.t1)
        self.assertEqual(r.status_code, 200, r.text)

    def test_terms_changed_after_tenant_looked_is_rejected(self):
        seen = self.sign()
        self.client.patch("/api/contracts/%s" % seen["contract_no"],
                          headers=self.landlord, json={"rent": 5000})
        r = self.client.post("/api/contracts/%s/activate" % seen["contract_no"],
                             headers=self.t1, json={"terms_digest": seen["terms_digest"]})
        self.assertEqual(r.status_code, 409)
        self.assertIn("修改", r.json()["detail"])
        # 重新看过（拿到新指纹）再确认就行
        fresh = self.contract(seen["contract_no"], self.t1)
        self.assertEqual(fresh["rent"], 5000)
        r = self.client.post("/api/contracts/%s/activate" % seen["contract_no"],
                             headers=self.t1, json={"terms_digest": fresh["terms_digest"]})
        self.assertEqual(r.status_code, 200, r.text)

    def test_digest_is_stable_across_reads(self):
        created = self.sign(rent=3333)
        again = self.contract(created["contract_no"], self.t1)
        self.assertEqual(created["terms_digest"], again["terms_digest"])


class TestHouseAfterLease(LifecycleCase):

    def test_disabled_landlords_house_stays_offline_after_lease_ends(self):
        """停用时只下架在架的房；出租中的那套原先退租后照样回到在架、照样能约看。"""
        no = self.sign()["contract_no"]
        self.client.post("/api/contracts/%s/activate" % no, headers=self.t1)
        admin = make_admin(self.client)
        session = db.SessionLocal()
        landlord_id = session.query(User).filter_by(username="landlord1").one().id
        session.close()
        self.client.patch("/api/admin/users/%d" % landlord_id, headers=admin,
                          json={"status": "disabled"})

        self.client.post("/api/contracts/%s/end" % no, headers=self.t1, json={"reason": "搬走"})
        r = self.client.post("/api/contracts/%s/end" % no, headers=admin, json={"reason": "搬走"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.client.get("/api/houses/DL0002", headers=admin).json()["status"],
                         "offline")
        r = self.client.post("/api/appointments", headers=self.t2, json={
            "house_no": "DL0002", "viewing_time": _day(2, 10).isoformat(),
            "contact_phone": "13900001111"})
        self.assertEqual(r.status_code, 409)

    def test_active_landlords_house_returns_to_market(self):
        no = self.sign()["contract_no"]
        self.client.post("/api/contracts/%s/activate" % no, headers=self.t1)
        end_lease(self.client, no, self.landlord, self.t1)
        self.assertEqual(self.client.get("/api/houses/DL0002").json()["status"], "available")

    def test_rejected_review_cancels_open_viewings(self):
        """驳回即下架，和房东自己下架一样收掉还没到时间的看房。"""
        r = self.client.post("/api/appointments", headers=self.t1, json={
            "house_no": "DL0002", "viewing_time": _day(2, 10).isoformat()})
        self.assertEqual(r.status_code, 200, r.text)
        appointment_id = r.json()["appointment"]["id"]
        self.client.patch("/api/appointments/%d" % appointment_id, headers=self.landlord,
                          json={"status": "confirmed"})
        r = self.client.put("/api/houses/DL0002", headers=self.landlord,
                            json={"title": "甘井子三室精装修"})
        self.assertEqual(r.json()["house"]["status"], "pending")

        admin = make_admin(self.client)
        r = self.client.patch("/api/houses/DL0002/review", headers=admin,
                              json={"approved": False, "reason": "图文不符"})
        self.assertEqual(r.status_code, 200, r.text)
        session = db.SessionLocal()
        try:
            self.assertEqual(session.get(Appointment, appointment_id).status, "cancelled")
        finally:
            session.close()
        self.assertIn("看房预约已取消", self.inbox("tenant1"))


class TestDepositRefundShown(LifecycleCase):

    def test_paid_deposit_shows_actual_refund(self):
        no = self.sign()["contract_no"]
        self.client.post("/api/contracts/%s/activate" % no, headers=self.t1)
        bills = self.client.get("/api/bills/mine?contract_no=%s" % no, headers=self.t1).json()
        deposit = next(b for b in bills["list"] if b["type"] == "deposit")
        settle(self.client, deposit["bill_no"], self.t1)
        end_lease(self.client, no, self.landlord, self.t1, deduct=200)
        got = self.contract(no, self.t1)
        self.assertEqual(got["deposit_refund"], got["deposit"] - 200)

    def test_unpaid_deposit_is_not_shown_as_refunded(self):
        """原先卡片上是「已退还押金 4200 元」，通知却说「押金未曾缴纳，无需退款」。"""
        no = self.sign()["contract_no"]
        self.client.post("/api/contracts/%s/activate" % no, headers=self.t1)
        end_lease(self.client, no, self.landlord, self.t1)
        got = self.contract(no, self.t1)
        self.assertEqual(got["status"], "ended")
        self.assertIsNone(got["deposit_refund"])


class TestContactNeedsARealLease(LifecycleCase):
    """「签过约」原先认任何一份合同：房东给任意用户名发一份待确认合同，就能私信他，免打扰也拦不住。"""

    def setUp(self):
        super().setUp()
        session = db.SessionLocal()
        try:
            session.query(User).filter_by(username="tenant2").one().accept_contact = False
            session.commit()
        finally:
            session.close()

    def contact(self):
        return self.client.post("/api/conversations", headers=self.landlord,
                                json={"house_no": "DL0002", "tenant_username": "tenant2"})

    def test_pending_contract_does_not_unlock_contact(self):
        self.assertEqual(self.contact().status_code, 403)
        self.sign("tenant2")
        self.assertEqual(self.contact().status_code, 403)

    def test_declined_contract_does_not_unlock_contact(self):
        no = self.sign("tenant2")["contract_no"]
        self.client.post("/api/contracts/%s/cancel" % no, headers=self.t2)
        self.assertEqual(self.contact().status_code, 403)

    def test_current_tenant_can_be_contacted_despite_do_not_disturb(self):
        no = self.sign("tenant2")["contract_no"]
        self.client.post("/api/contracts/%s/activate" % no, headers=self.t2)
        r = self.contact()
        self.assertEqual(r.status_code, 200, r.text)

    def test_former_tenant_with_do_not_disturb_is_left_alone(self):
        no = self.sign("tenant2")["contract_no"]
        self.client.post("/api/contracts/%s/activate" % no, headers=self.t2)
        end_lease(self.client, no, self.landlord, self.t2)
        r = self.contact()
        self.assertEqual(r.status_code, 403)
        self.assertIn("不接受主动联系", r.json()["detail"])
