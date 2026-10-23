"""交易链接口测试：合同、账单、缴费、消息、公告。

业务规则本身在 test_leasing.py 里测过了，这里专测接口层：
**权限边界**（谁能签、谁能确认、谁能缴、谁能扣押金）与状态码。
"""

import unittest
from datetime import datetime, timedelta

from app import leasing
from tests.base import ApiTestCase, auth_header, make_admin, settle, end_lease


def future(days=7):
    return (datetime.now() + timedelta(days=days)).replace(
        hour=0, minute=0, second=0, microsecond=0)


class ContractCase(ApiTestCase):

    def setUp(self):
        super().setUp()
        self.landlord = auth_header(self.client, "landlord1", "test123")
        self.tenant = auth_header(self.client, "tenant1", "test123")

    def sign(self, house_no="DL0001", months=12, pay_months=1):
        return self.client.post("/api/contracts", headers=self.landlord, json={
            "house_no": house_no,
            "tenant_username": "tenant1",
            "start_date": future().isoformat(),
            "months": months,
            "pay_months": pay_months,
        })

    def active_contract(self, **kwargs):
        no = self.sign(**kwargs).json()["contract"]["contract_no"]
        self.client.post("/api/contracts/%s/activate" % no, headers=self.tenant)
        return no

    def pay_deposit(self, contract_no):
        """把这份合同的押金账单缴掉，返回账单号。

        `active_contract` 只是让合同生效，**押金并没有真的收到**。
        涉及退款的用例必须先走这一步——押金没收过却期待退款记录，
        测的就不是真实业务了。
        """
        bills = self.client.get("/api/bills/mine", headers=self.tenant).json()
        deposit = next(b for b in bills["list"]
                       if b["type"] == "deposit" and b["house_no"] == "DL0001"
                       and b["status"] == "unpaid")
        # 三段式改造之后，只调 /pay 账单仍是 unpaid——要缴清得等回调。
        # 这里的用例关心的是「押金已经收到」这个前提，不是支付链路本身
        settle(self.client, deposit["bill_no"], self.tenant)
        return deposit["bill_no"]


class TestCreateContract(ContractCase):

    def test_landlord_can_sign(self):
        response = self.sign()
        self.assertEqual(response.status_code, 200)
        contract = response.json()["contract"]
        self.assertEqual(contract["status"], "pending")
        self.assertEqual(contract["rent"], 2800.0)

    def test_tenant_cannot_sign(self):
        """签约是出租方发起的，租客不能自己给自己签。"""
        response = self.client.post("/api/contracts", headers=self.tenant, json={
            "house_no": "DL0001", "tenant_username": "tenant1",
            "start_date": future().isoformat()})
        self.assertEqual(response.status_code, 403)

    def test_anonymous_rejected(self):
        response = self.client.post("/api/contracts", json={
            "house_no": "DL0001", "tenant_username": "tenant1",
            "start_date": future().isoformat()})
        self.assertEqual(response.status_code, 401)

    def test_rented_house_rejected(self):
        response = self.sign(house_no="DL0003")
        self.assertEqual(response.status_code, 409)

    def test_unknown_house(self):
        response = self.client.post("/api/contracts", headers=self.landlord, json={
            "house_no": "NOPE", "tenant_username": "tenant1",
            "start_date": future().isoformat()})
        self.assertEqual(response.status_code, 404)

    def test_unknown_tenant(self):
        response = self.client.post("/api/contracts", headers=self.landlord, json={
            "house_no": "DL0001", "tenant_username": "nobody",
            "start_date": future().isoformat()})
        self.assertEqual(response.status_code, 404)

    def test_invalid_months(self):
        response = self.client.post("/api/contracts", headers=self.landlord, json={
            "house_no": "DL0001", "tenant_username": "tenant1",
            "start_date": future().isoformat(), "months": 0})
        self.assertEqual(response.status_code, 422)

    def test_tenant_gets_notified(self):
        self.sign()
        data = self.client.get("/api/messages", headers=self.tenant).json()
        self.assertGreater(data["unread"], 0)


class TestActivate(ContractCase):

    def test_tenant_activates(self):
        no = self.sign().json()["contract"]["contract_no"]
        response = self.client.post("/api/contracts/%s/activate" % no,
                                    headers=self.tenant)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["contract"]["status"], "active")
        self.assertEqual(body["bill_count"], 13, "押金 1 条 + 租金 12 条")

    def test_landlord_cannot_activate(self):
        """合同要租客本人确认，房东不能替租客点确认。"""
        no = self.sign().json()["contract"]["contract_no"]
        response = self.client.post("/api/contracts/%s/activate" % no,
                                    headers=self.landlord)
        self.assertEqual(response.status_code, 403)

    def test_house_becomes_rented(self):
        self.active_contract()
        house = self.client.get("/api/houses/DL0001").json()
        self.assertEqual(house["status"], "rented")

    def test_activate_twice(self):
        no = self.active_contract()
        again = self.client.post("/api/contracts/%s/activate" % no,
                                 headers=self.tenant)
        self.assertEqual(again.status_code, 409)


class TestContractVisibility(ContractCase):

    def test_both_sides_can_read(self):
        no = self.sign().json()["contract"]["contract_no"]
        for headers in (self.landlord, self.tenant):
            response = self.client.get("/api/contracts/%s" % no, headers=headers)
            self.assertEqual(response.status_code, 200)

    def test_outsider_cannot_read(self):
        """第三方不能看别人的合同——里面有双方姓名与金额。"""
        no = self.sign().json()["contract"]["contract_no"]
        self.client.post("/api/auth/register", json={
            "username": "outsider", "password": "secret123"})
        outsider = auth_header(self.client, "outsider", "secret123")
        response = self.client.get("/api/contracts/%s" % no, headers=outsider)
        self.assertEqual(response.status_code, 403)

    def test_mine_filters_by_role(self):
        self.sign()
        as_tenant = self.client.get("/api/contracts/mine", headers=self.tenant).json()
        as_landlord = self.client.get("/api/contracts/mine",
                                      headers=self.landlord).json()
        self.assertEqual(as_tenant["total"], 1)
        self.assertEqual(as_landlord["total"], 1)

    def test_unknown_contract(self):
        response = self.client.get("/api/contracts/NOPE", headers=self.tenant)
        self.assertEqual(response.status_code, 404)


class TestBills(ContractCase):

    def test_tenant_sees_bills(self):
        self.active_contract()
        data = self.client.get("/api/bills/mine", headers=self.tenant).json()
        self.assertEqual(data["total"], 13)
        self.assertEqual(data["unpaid_count"], 13)
        self.assertGreater(data["unpaid_amount"], 0)

    def test_landlord_sees_same_bills(self):
        self.active_contract()
        data = self.client.get("/api/bills/mine", headers=self.landlord).json()
        self.assertEqual(data["total"], 13)

    def test_pay_only_starts_the_payment(self):
        """**发起支付不等于收到钱。**

        改造前这个接口进来就把账单置 paid。那种写法假设
        「发起支付 == 付款成功」，而真实链路里用户可能跳到收银台就走了、
        可能付失败、也可能回调晚到几秒——三种情况旧写法全记成已付款。
        """
        self.active_contract()
        bill_no = self.client.get("/api/bills/mine",
                                  headers=self.tenant).json()["list"][0]["bill_no"]
        response = self.client.post("/api/bills/%s/pay" % bill_no,
                                    headers=self.tenant, json={"method": "wechat"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["payment"]["method"], "wechat")
        # 支付单在途，账单还没付
        self.assertEqual(body["payment"]["status"], "pending")
        self.assertEqual(body["bill"]["status"], "unpaid")

    def test_bill_is_paid_after_the_callback(self):
        """账单是在网关回调那一步才真正变成已付的。"""
        self.active_contract()
        bill_no = self.client.get("/api/bills/mine",
                                  headers=self.tenant).json()["list"][0]["bill_no"]
        response = settle(self.client, bill_no, self.tenant, method="wechat")
        self.assertEqual(response.status_code, 200)

        bills = self.client.get("/api/bills/mine", headers=self.tenant).json()
        paid = next(b for b in bills["list"] if b["bill_no"] == bill_no)
        self.assertEqual(paid["status"], "paid")

    def _expire_payment(self):
        from app import db
        from app.models import Payment
        session = db.SessionLocal()
        try:
            payment = session.query(Payment).one()
            payment.expire_at = datetime.now() - timedelta(minutes=1)
            session.commit()
        finally:
            session.close()

    def _callback(self, trade_no, amount, result="success"):
        from app import gateway
        payload = {"trade_no": trade_no, "amount": float(amount), "result": result}
        payload["sign"] = gateway.sign(payload)
        return self.client.post("/api/payments/notify", json=payload)

    def test_can_pay_again_after_the_checkout_expired(self):
        """打开收银台没付、过了 15 分钟回来，**这张账单必须还能付**。

        原先关单只改状态，旧支付单照样占着 bill_id 的唯一约束，重新发起时
        撞约束，接口回「该账单正在支付或已经支付」——账单从此再也付不了。
        """
        self.active_contract()
        bill = self.client.get("/api/bills/mine", headers=self.tenant).json()["list"][0]
        first = self.client.post("/api/bills/%s/pay" % bill["bill_no"],
                                 headers=self.tenant, json={"method": "alipay"}).json()
        self._expire_payment()

        again = self.client.post("/api/bills/%s/pay" % bill["bill_no"],
                                 headers=self.tenant, json={"method": "wechat"})
        self.assertEqual(again.status_code, 200, again.text)
        payment = again.json()["payment"]
        self.assertEqual(payment["status"], "pending")
        self.assertNotEqual(payment["trade_no"], first["payment"]["trade_no"])

        # 新单能正常入账；过期那张单的回调迟到了，不能把账单记成已付
        stale = self._callback(first["payment"]["trade_no"], bill["amount"])
        self.assertNotEqual(stale.status_code, 200)
        self.assertEqual(self._callback(payment["trade_no"], bill["amount"]).status_code, 200)
        bills = self.client.get("/api/bills/mine", headers=self.tenant).json()["list"]
        self.assertEqual(next(b for b in bills if b["bill_no"] == bill["bill_no"])["status"], "paid")

    def test_can_retry_after_a_failed_payment(self):
        self.active_contract()
        bill = self.client.get("/api/bills/mine", headers=self.tenant).json()["list"][0]
        first = self.client.post("/api/bills/%s/pay" % bill["bill_no"],
                                 headers=self.tenant, json={"method": "alipay"}).json()
        failed = self._callback(first["payment"]["trade_no"], bill["amount"], result="fail")
        self.assertEqual(failed.status_code, 200, failed.text)

        retry = self.client.post("/api/bills/%s/pay" % bill["bill_no"],
                                 headers=self.tenant, json={"method": "alipay"})
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(retry.json()["payment"]["status"], "pending")
        self.assertEqual(retry.json()["payment"]["failure_reason"], "")

    def test_ending_a_lease_voids_the_unpaid_deposit(self):
        """租约结束，没交的押金就不再是一笔该交的钱。

        原先它留着 unpaid：租客的「待缴」里永远挂着一笔付不了的押金，
        页面还让人「与房东线下结清」——而结算里明明写着押金未缴、无需退款。
        """
        no = self.active_contract()
        ended = end_lease(self.client, no, self.landlord, self.tenant)
        self.assertEqual(ended.status_code, 200, ended.text)
        bills = self.client.get("/api/bills/mine", headers=self.tenant).json()
        self.assertEqual(bills["unpaid_count"], 0)
        deposit = next(b for b in bills["list"] if b["type"] == "deposit")
        self.assertEqual(deposit["status"], "cancelled")

    def test_a_paid_deposit_is_still_refunded_not_voided(self):
        no = self.active_contract()
        self.pay_deposit(no)
        ended = end_lease(self.client, no, self.landlord, self.tenant, deduct=300).json()
        self.assertIsNotNone(ended["refund_record"])
        bills = self.client.get("/api/bills/mine", headers=self.tenant).json()["list"]
        self.assertEqual(next(b for b in bills if b["type"] == "deposit")["status"], "paid")

    def test_double_pay_rejected(self):
        """已经缴清的账单不能再付一次。

        重复入账是**对账时最难查的一类错**：钱数对不上，
        但每一笔流水看起来都合法。
        """
        self.active_contract()
        bill_no = self.client.get("/api/bills/mine",
                                  headers=self.tenant).json()["list"][0]["bill_no"]
        settle(self.client, bill_no, self.tenant)
        again = self.client.post("/api/bills/%s/pay" % bill_no,
                                 headers=self.tenant, json={})
        self.assertEqual(again.status_code, 409)

    def test_payment_method_is_validated(self):
        self.active_contract()
        bill_no = self.client.get("/api/bills/mine",
                                  headers=self.tenant).json()["list"][0]["bill_no"]
        response = self.client.post("/api/bills/%s/pay" % bill_no,
                                    headers=self.tenant, json={"method": "abc123"})
        self.assertEqual(response.status_code, 422)

    def test_idempotent_payment_returns_same_trade(self):
        self.active_contract()
        bill_no = self.client.get("/api/bills/mine",
                                  headers=self.tenant).json()["list"][0]["bill_no"]
        payload = {"idempotency_key": "same-click-001"}
        first = self.client.post("/api/bills/%s/pay" % bill_no,
                                 headers=self.tenant, json=payload).json()
        second = self.client.post("/api/bills/%s/pay" % bill_no,
                                  headers=self.tenant, json=payload).json()
        self.assertEqual(first["payment"]["trade_no"],
                         second["payment"]["trade_no"])
        self.assertTrue(second["idempotent"])

    def test_paid_bill_contains_receipt(self):
        self.active_contract()
        bill_no = self.client.get("/api/bills/mine",
                                  headers=self.tenant).json()["list"][0]["bill_no"]
        started = self.client.post("/api/bills/%s/pay" % bill_no,
                                   headers=self.tenant, json={}).json()
        trade_no = started["payment"]["trade_no"]
        receipt = self.client.get("/api/payments/%s" % trade_no,
                                  headers=self.tenant)
        self.assertEqual(receipt.status_code, 200)
        self.assertEqual(receipt.json()["bill"]["bill_no"], bill_no)

    def test_landlord_cannot_pay(self):
        """房东不能替租客缴费。"""
        self.active_contract()
        bill_no = self.client.get("/api/bills/mine",
                                  headers=self.tenant).json()["list"][0]["bill_no"]
        response = self.client.post("/api/bills/%s/pay" % bill_no,
                                    headers=self.landlord, json={})
        self.assertEqual(response.status_code, 403)

    def test_unknown_bill(self):
        response = self.client.post("/api/bills/NOPE/pay",
                                    headers=self.tenant, json={})
        self.assertEqual(response.status_code, 404)


class TestEndAndRenew(ContractCase):

    def test_end_releases_house(self):
        no = self.active_contract()
        response = end_lease(self.client, no, self.landlord, self.tenant)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["contract"]["status"], "ended")
        self.assertEqual(
            self.client.get("/api/houses/DL0001").json()["status"], "available")

    def test_tenant_end_is_a_request_for_the_landlord(self):
        """租客点退租是提申请：合同照常生效，房东收到消息，在房东那边确认时结算押金。"""
        no = self.active_contract()
        response = self.client.post("/api/contracts/%s/end" % no,
                                    headers=self.tenant, json={"reason": "提前退租"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["requested"])
        self.assertEqual(body["contract"]["status"], "active")
        self.assertEqual(body["contract"]["request"]["type"], "end")
        self.assertEqual(body["contract"]["request"]["by"], "tenant")
        self.assertEqual(self.client.get("/api/houses/DL0001").json()["status"], "rented")
        messages = self.client.get("/api/messages", headers=self.landlord).json()["list"]
        self.assertTrue(any(m["title"] == "租客申请退租" for m in messages))

        # 房东确认（带扣款），这一步才真的退
        done = self.client.post("/api/contracts/%s/end" % no, headers=self.landlord,
                                json={"reason": "提前退租", "deduct": 0})
        self.assertEqual(done.json()["contract"]["status"], "ended")
        self.assertIsNone(done.json()["contract"]["request"])

    def _expire(self, contract_no):
        from app import db
        from app.models import Contract
        session = db.SessionLocal()
        try:
            contract = session.query(Contract).filter_by(contract_no=contract_no).one()
            contract.end_date = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            session.commit()
        finally:
            session.close()

    def test_landlord_early_end_needs_the_tenant(self):
        """房东提前退租原先点了就生效、押金扣多少自己定。现在是申请，写明原因与拟扣。"""
        no = self.active_contract()
        response = self.client.post("/api/contracts/%s/end" % no, headers=self.landlord,
                                    json={"reason": "房东要自住", "deduct": 300})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["requested"])
        self.assertEqual(body["contract"]["status"], "active")
        self.assertEqual(body["contract"]["request"]["by"], "landlord")
        self.assertEqual(body["contract"]["request"]["deduct"], 300.0)
        self.assertEqual(self.client.get("/api/houses/DL0001").json()["status"], "rented")
        notes = self.client.get("/api/messages", headers=self.tenant).json()["list"]
        note = next(m for m in notes if m["title"] == "房东申请退租")
        self.assertIn("房东要自住", note["content"])
        self.assertIn("300", note["content"])

    def test_tenant_agrees_and_the_stated_deduction_applies(self):
        no = self.active_contract()
        self.pay_deposit(no)
        self.client.post("/api/contracts/%s/end" % no, headers=self.landlord,
                         json={"reason": "房东要自住", "deduct": 300})
        agreed = self.client.post("/api/contracts/%s/end" % no, headers=self.tenant, json={})
        self.assertEqual(agreed.status_code, 200, agreed.text)
        self.assertEqual(agreed.json()["contract"]["status"], "ended")
        self.assertEqual(agreed.json()["refund"], 2500.0, "租客同意的就是房东写明的扣款")

    def test_tenant_can_decline(self):
        no = self.active_contract()
        self.client.post("/api/contracts/%s/end" % no, headers=self.landlord,
                         json={"reason": "房东要自住"})
        declined = self.client.delete("/api/contracts/%s/request" % no, headers=self.tenant)
        self.assertEqual(declined.status_code, 200)
        self.assertEqual(declined.json()["contract"]["status"], "active")
        self.assertIsNone(declined.json()["contract"]["request"])

    def test_landlord_cannot_propose_more_than_the_deposit(self):
        no = self.active_contract()
        response = self.client.post("/api/contracts/%s/end" % no, headers=self.landlord,
                                    json={"deduct": 99999})
        self.assertEqual(response.status_code, 409)

    def test_expired_lease_is_flagged_and_landlord_ends_it_directly(self):
        """到期退租是合同本来的约定，房东直接办；原先它只显示「剩余 0 天」，谁也不提醒。"""
        no = self.active_contract()
        self._expire(no)
        mine = self.client.get("/api/contracts/mine", headers=self.landlord).json()["list"]
        self.assertTrue(next(c for c in mine if c["contract_no"] == no)["expired"])
        dashboard = self.client.get("/api/landlord/dashboard", headers=self.landlord).json()
        self.assertEqual(dashboard["todo"]["expired_leases"], 1)
        self.assertEqual(dashboard["expired_leases"][0]["contract_no"], no)

        ended = self.client.post("/api/contracts/%s/end" % no, headers=self.landlord,
                                 json={"reason": "租期届满"})
        self.assertEqual(ended.json()["contract"]["status"], "ended")

    def test_admin_ends_directly(self):
        no = self.active_contract()
        admin = make_admin(self.client)
        ended = self.client.post("/api/contracts/%s/end" % no, headers=admin,
                                 json={"reason": "平台介入"})
        self.assertEqual(ended.json()["contract"]["status"], "ended")

    def test_tenant_cannot_deduct_deposit(self):
        """扣押金是出租方的权利，租客不能自己填扣款。"""
        no = self.active_contract()
        response = self.client.post("/api/contracts/%s/end" % no,
                                    headers=self.tenant, json={"deduct": 500})
        self.assertEqual(response.status_code, 403)

    def test_landlord_deducts(self):
        no = self.active_contract()
        self.pay_deposit(no)
        response = end_lease(self.client, no, self.landlord, self.tenant, deduct=500)
        self.assertEqual(response.json()["refund"], 2300.0)
        refunds = self.client.get("/api/refunds/mine",
                                  headers=self.tenant).json()
        self.assertEqual(refunds["total"], 1)
        self.assertEqual(refunds["list"][0]["deduct_amount"], 500.0)

    def test_no_refund_record_when_deposit_never_paid(self):
        """**回归**：押金一分钱没收，不能凭空生成一笔退款记录。

        原先 Refund 是无条件创建的，押金账单没付过时只把 payment_id 置空，
        记录照样生成——账上凭空多出一笔「已退 2800 元」的支出，对账对不平。
        那不是记录不准，是造了一笔没发生过的资金流动。
        """
        no = self.active_contract()
        response = end_lease(self.client, no, self.landlord, self.tenant)
        self.assertEqual(response.status_code, 200)
        # 「应退金额」照常算得出来，那是结算结论
        self.assertEqual(response.json()["refund"], 2800.0)
        # 但「实际退款流水」不该有
        self.assertIsNone(response.json()["refund_record"])
        refunds = self.client.get("/api/refunds/mine",
                                  headers=self.tenant).json()
        self.assertEqual(refunds["total"], 0, "押金没收到就不该有退款记录")

    def test_notice_does_not_claim_a_refund_that_never_happened(self):
        """押金没收过却通知租客「退还押金 2800 元」，会直接引出
        「钱退哪去了」的质问。"""
        no = self.active_contract()
        end_lease(self.client, no, self.landlord, self.tenant)
        messages = self.client.get("/api/messages", headers=self.tenant).json()
        ended = [m for m in messages["list"] if m["title"] == "合同已结束"]
        self.assertTrue(ended)
        self.assertIn("未曾缴纳", ended[0]["content"])
        self.assertNotIn("退还押金", ended[0]["content"])

    def test_deduct_over_deposit_rejected(self):
        no = self.active_contract()
        response = self.client.post("/api/contracts/%s/end" % no,
                                    headers=self.landlord, json={"deduct": 99999})
        self.assertEqual(response.status_code, 409)

    def test_negative_deduct_rejected(self):
        no = self.active_contract()
        response = self.client.post("/api/contracts/%s/end" % no,
                                    headers=self.landlord, json={"deduct": -5})
        self.assertEqual(response.status_code, 422)

    def test_renew_needs_the_other_side(self):
        """租客申请续租，**房东确认**；原先新合同反倒等租客自己确认，房东全程没参与。"""
        no = self.active_contract(months=12)
        before = self.client.get("/api/contracts/mine", headers=self.tenant).json()["list"][0]
        asked = self.client.post("/api/contracts/%s/renew" % no,
                                 headers=self.tenant, json={"months": 6}).json()
        self.assertTrue(asked["requested"])
        self.assertEqual(asked["contract"]["request"],
                         dict(asked["contract"]["request"], type="renew", by="tenant", months=6))
        self.assertEqual(asked["contract"]["end_date"], before["end_date"])     # 还没延长

        # 租客自己再点一次不算确认
        again = self.client.post("/api/contracts/%s/renew" % no,
                                 headers=self.tenant, json={"months": 6}).json()
        self.assertTrue(again["requested"])

        done = self.client.post("/api/contracts/%s/renew" % no,
                                headers=self.landlord, json={}).json()
        self.assertTrue(done["renewed"])
        self.assertEqual(done["contract"]["status"], "active")
        self.assertEqual(done["contract"]["contract_no"], no)
        self.assertGreater(done["contract"]["end_date"], before["end_date"])
        self.assertIsNone(done["contract"]["request"])

    def test_landlord_can_propose_renewal_too(self):
        no = self.active_contract()
        asked = self.client.post("/api/contracts/%s/renew" % no,
                                 headers=self.landlord, json={"months": 12}).json()
        self.assertEqual(asked["contract"]["request"]["by"], "landlord")
        done = self.client.post("/api/contracts/%s/renew" % no,
                                headers=self.tenant, json={}).json()
        self.assertTrue(done["renewed"])

    def test_request_can_be_withdrawn_or_declined(self):
        no = self.active_contract()
        self.client.post("/api/contracts/%s/end" % no, headers=self.tenant, json={})
        declined = self.client.delete("/api/contracts/%s/request" % no, headers=self.landlord)
        self.assertIsNone(declined.json()["contract"]["request"])
        messages = self.client.get("/api/messages", headers=self.tenant).json()["list"]
        self.assertTrue(any(m["title"] == "退租申请未通过" for m in messages))
        self.assertEqual(self.client.delete("/api/contracts/%s/request" % no,
                                            headers=self.tenant).status_code, 409)

    def test_cannot_stack_end_and_renew_requests(self):
        no = self.active_contract()
        self.client.post("/api/contracts/%s/renew" % no, headers=self.tenant, json={})
        clash = self.client.post("/api/contracts/%s/end" % no, headers=self.tenant, json={})
        self.assertEqual(clash.status_code, 409)

    def test_renew_pending_rejected(self):
        no = self.sign().json()["contract"]["contract_no"]
        response = self.client.post("/api/contracts/%s/renew" % no,
                                    headers=self.landlord, json={})
        self.assertEqual(response.status_code, 409)


class TestTenantDetail(ContractCase):
    """房东点开一位租客：只看到他和自己之间的合同、账单、报修。"""

    def tenant_id(self):
        return self.client.get("/api/auth/me", headers=self.tenant).json()["id"]

    def test_landlord_sees_contracts_and_bills(self):
        self.active_contract()
        body = self.client.get("/api/landlord/tenants/%d" % self.tenant_id(),
                               headers=self.landlord).json()
        self.assertEqual(body["tenant"]["username"], "tenant1")
        self.assertNotIn("password_hash", body["tenant"])
        self.assertEqual(len(body["contracts"]), 1)
        self.assertTrue(body["bills"])
        self.assertIn("overdue_count", body["summary"])

    def test_other_landlords_get_nothing(self):
        self.active_contract()
        # 另一位房东：直接以房东身份注册
        self.client.post("/api/auth/register", json={
            "username": "landlord2", "password": "test123", "real_name": "赵房东",
            "phone": "13800000002", "role": "landlord"})
        other = auth_header(self.client, "landlord2", "test123")
        response = self.client.get("/api/landlord/tenants/%d" % self.tenant_id(), headers=other)
        self.assertEqual(response.status_code, 404)

    def test_tenants_cannot_use_it(self):
        response = self.client.get("/api/landlord/tenants/%d" % self.tenant_id(),
                                   headers=self.tenant)
        self.assertEqual(response.status_code, 403)


class TestMessages(ContractCase):

    def test_list_and_read(self):
        self.sign()
        data = self.client.get("/api/messages", headers=self.tenant).json()
        self.assertGreater(data["unread"], 0)

        message_id = data["list"][0]["id"]
        self.client.patch("/api/messages/%d/read" % message_id, headers=self.tenant)
        after = self.client.get("/api/messages", headers=self.tenant).json()
        self.assertEqual(after["unread"], data["unread"] - 1)

    def test_read_all(self):
        self.active_contract()
        self.client.post("/api/messages/read-all", headers=self.tenant)
        data = self.client.get("/api/messages", headers=self.tenant).json()
        self.assertEqual(data["unread"], 0)

    def test_cannot_read_others_message(self):
        self.sign()
        message_id = self.client.get(
            "/api/messages", headers=self.tenant).json()["list"][0]["id"]
        response = self.client.patch("/api/messages/%d/read" % message_id,
                                     headers=self.landlord)
        self.assertEqual(response.status_code, 404)

    def test_requires_login(self):
        self.assertEqual(self.client.get("/api/messages").status_code, 401)


class TestNotices(ContractCase):

    def test_landlord_cannot_publish(self):
        response = self.client.post("/api/notices", headers=self.landlord, json={
            "title": "测试公告", "content": "内容"})
        self.assertEqual(response.status_code, 403)

    def test_public_list(self):
        response = self.client.get("/api/notices")
        self.assertEqual(response.status_code, 200)
        self.assertIn("list", response.json())

    def test_empty_title_rejected(self):
        response = self.client.post("/api/notices", headers=self.landlord, json={
            "title": "", "content": "内容"})
        self.assertEqual(response.status_code, 422)


class TestStatsIncome(ContractCase):
    """需求 F-07 收租统计：/api/stats 要能反映真实租金收入。"""

    def setUp(self):
        super().setUp()
        # `/api/stats` 分两档：钱那一档只给管理员。
        # 这几条测的正是钱，所以要以管理员身份取
        self.admin = make_admin(self.client)

    def test_income_appears_after_payment(self):
        self.active_contract()
        before = self.client.get("/api/stats", headers=self.admin).json()
        self.assertEqual(before["income_received"], 0)
        self.assertGreater(before["income_pending"], 0)

        bills = self.client.get("/api/bills/mine", headers=self.tenant).json()["list"]
        rent_bill = [b for b in bills if b["type"] == "rent"][0]
        # 收入只认**确认到账**的钱，所以必须走完回调那一段。
        # 只发起支付就计入收入，等于把在途的钱当成已收
        settle(self.client, rent_bill["bill_no"], self.tenant)

        after = self.client.get("/api/stats", headers=self.admin).json()
        self.assertEqual(after["income_received"], 2800.0)
        self.assertEqual(len(after["monthly_income"]), 1)

    def test_contract_and_occupancy(self):
        self.active_contract()
        stats = self.client.get("/api/stats", headers=self.admin).json()
        self.assertEqual(stats["contract_active"], 1)
        self.assertGreater(stats["occupancy_rate"], 0)


class TestNoPaymentAfterEnd(ContractCase):
    """合同结束之后不能再缴费。

    退租会作废未到期的租金和没交的押金，**已到期未缴的租金仍是 unpaid**——
    那是住过的月份，是真欠款。原先 pay_bill 只看账单状态、不看合同状态，
    于是**合同都结束了还能继续缴费**——收进来的钱既没有对应租约，也不会再
    产生任何权利义务，退租结算早就做完了，这笔钱只能靠人工退回去。

    （这两条原先拿没交的押金当例子；10-22 起押金在退租时作废，
    改用一张已到期的租金账单，测的还是「欠款在、但不能再走线上缴费」。）

    这个洞是做合同档案时发现的：时间线上「缴纳押金」排在「合同结束」
    之后，一眼就看出顺序不对。
    """

    def _overdue_rent(self, contract_no):
        """把第一期租金挪到昨天到期：住过的月份，退租时不作废，是真欠款。"""
        from app import db
        from app.models import Bill, Contract
        session = db.SessionLocal()
        try:
            contract = session.query(Contract).filter_by(contract_no=contract_no).one()
            bill = (session.query(Bill).filter_by(contract_id=contract.id, type="rent")
                    .order_by(Bill.due_date).first())
            bill.due_date = datetime.now() - timedelta(days=1)
            session.commit()
            return {"bill_no": bill.bill_no}
        finally:
            session.close()

    def test_cannot_pay_after_contract_ended(self):
        no = self.active_contract()
        owed = self._overdue_rent(no)

        end_lease(self.client, no, self.landlord, self.tenant, reason="提前退租")

        r = self.client.post("/api/bills/%s/pay" % owed["bill_no"],
                             headers=self.tenant, json={"method": "alipay"})
        self.assertEqual(r.status_code, 409)
        self.assertIn("已结束", r.json()["detail"])

    def test_bill_stays_unpaid_after_rejection(self):
        """被拒之后账单状态不能被改动，更不能产生流水。"""
        no = self.active_contract()
        owed = self._overdue_rent(no)
        end_lease(self.client, no, self.landlord, self.tenant, reason="提前退租")
        self.client.post("/api/bills/%s/pay" % owed["bill_no"],
                         headers=self.tenant, json={"method": "alipay"})

        after = self.client.get("/api/bills/mine", headers=self.tenant).json()
        same = next(b for b in after["list"]
                    if b["bill_no"] == owed["bill_no"])
        self.assertEqual(same["status"], "unpaid")
        self.assertIsNone(same.get("payment"))

    def test_paying_before_ending_still_works(self):
        """正常路径不能被误伤。"""
        no = self.active_contract()
        self.pay_deposit(no)
        bills = self.client.get("/api/bills/mine", headers=self.tenant).json()
        deposit = next(b for b in bills["list"] if b["type"] == "deposit")
        self.assertEqual(deposit["status"], "paid")

class TestStatsAggregationParity(ContractCase):
    """/api/stats 改成 SQL 聚合之后，数字必须与逐行统计**完全一致**。

    原先这个接口把五张表全量拉进内存再用 Python 循环数数，
    100 并发时 p50 到了 1114ms（需求 N-01 要求 1 秒以内）。
    改成 COUNT / SUM / GROUP BY 之后快了，但**快而算错是更坏的结果**，
    所以这里拿同一份数据两种算法对一遍。

    尤其是逾期：Bill.overdue 是 Python 侧的属性，SQL 里得重写一遍判定，
    两处只要改一处就会悄悄对不上。
    """

    def setUp(self):
        super().setUp()
        # `/api/stats` 分两档：钱那一档只给管理员。
        # 这几条测的正是钱，所以要以管理员身份取
        self.admin = make_admin(self.client)

    def test_counts_match_row_by_row(self):
        no = self.active_contract()
        self.pay_deposit(no)

        stats = self.client.get("/api/stats", headers=self.admin).json()

        import app.db as db
        from app.models import Bill, Contract, House
        session = db.SessionLocal()
        try:
            houses = session.query(House).all()
            bills = session.query(Bill).all()
            contracts = session.query(Contract).all()

            self.assertEqual(stats["house_total"], len(houses))
            self.assertEqual(stats["house_available"],
                             sum(1 for h in houses if h.status == "available"))
            self.assertEqual(stats["house_rented"],
                             sum(1 for h in houses if h.status == "rented"))
            self.assertEqual(stats["contract_active"],
                             sum(1 for c in contracts if c.status == "active"))

            paid = [b for b in bills if b.status == "paid"]
            unpaid = [b for b in bills if b.status == "unpaid"]
            self.assertAlmostEqual(
                stats["income_received"],
                round(sum(float(b.amount) for b in paid), 2), places=2)
            self.assertAlmostEqual(
                stats["income_pending"],
                round(sum(float(b.amount) for b in unpaid), 2), places=2)

            # 关键：SQL 的逾期判定要与 Bill.overdue 属性一致
            self.assertEqual(stats["overdue_count"],
                             sum(1 for b in unpaid if b.overdue))
        finally:
            session.close()

    def test_district_grouping_matches(self):
        stats = self.client.get("/api/stats", headers=self.admin).json()
        import app.db as db
        from app.models import House
        session = db.SessionLocal()
        try:
            available = [h for h in session.query(House).all()
                         if h.status == "available"]
            expected = {}
            for house in available:
                row = expected.setdefault(house.district, [])
                row.append(float(house.rent_price))

            got = {row["district"]: row for row in stats["by_district"]}
            self.assertEqual(set(got), set(expected))
            for district, prices in expected.items():
                self.assertEqual(got[district]["count"], len(prices))
                self.assertAlmostEqual(got[district]["avg_rent"],
                                       round(sum(prices) / len(prices), 2),
                                       places=2)
        finally:
            session.close()

    def test_monthly_income_only_counts_paid_rent(self):
        """按月收租只统计**已缴的租金**，押金与未缴的不能混进去。"""
        no = self.active_contract()
        self.pay_deposit(no)
        stats = self.client.get("/api/stats", headers=self.admin).json()
        total = sum(m["amount"] for m in stats["monthly_income"])
        # 刚才只缴了押金，租金一期没缴，月度收租应当为 0
        self.assertEqual(total, 0)


class TestContractUpdate(ApiTestCase):
    """改一份还没生效的合同（B4）。

    合同原先建出来就定死了：日期填错一天、押付方式选错一档，唯一的办法是
    让租客先拒签、房东再重开一份——而重开的那份是新编号，
    之前发出去的链接全部作废。**一个填错日期的代价不该是这个。**
    """

    def setUp(self):
        super().setUp()
        self.landlord = auth_header(self.client, "landlord1", "test123")
        self.tenant = auth_header(self.client, "tenant1", "test123")
        # 相对日期：起租日最多往前补 30 天，写死 2026-10-01 的话过几周就红
        self.start = (datetime.now() + timedelta(days=10)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        self.moved = self.start + timedelta(days=45)

    def _make(self):
        houses = self.client.get("/api/landlord/houses",
                                 headers=self.landlord).json()["list"]
        house = next(h for h in houses if h["status"] == "available")
        r = self.client.post("/api/contracts", headers=self.landlord, json={
            "house_no": house["house_no"], "tenant_username": "tenant1",
            "start_date": self.start.isoformat(), "months": 12,
            "pay_months": 1})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["contract"]

    def _sign(self, **overrides):
        body = {"house_no": "DL0001", "tenant_username": "tenant1",
                "start_date": self.start.isoformat(), "months": 12}
        body.update(overrides)
        return self.client.post("/api/contracts", headers=self.landlord, json=body)

    def test_landlord_cannot_sign_with_himself(self):
        """和自己签约：房源显示已出租，退租后他还成了能给自己房子写评价的「租客」。"""
        r = self._sign(tenant_username="landlord1")
        self.assertEqual(r.status_code, 409)
        self.assertIn("自己", r.json()["detail"])

    def test_start_date_has_a_sane_range(self):
        """往前补几个月：确认那一刻起前面每一期都逾期；往后一年多也不是真合同。"""
        long_ago = (datetime.now() - timedelta(days=60)).isoformat()
        far_away = (datetime.now() + timedelta(days=400)).isoformat()
        self.assertEqual(self._sign(start_date=long_ago).status_code, 409)
        self.assertEqual(self._sign(start_date=far_away).status_code, 409)
        a_week_ago = (datetime.now() - timedelta(days=7)).isoformat()
        self.assertEqual(self._sign(start_date=a_week_ago).status_code, 200,
                         "先搬进去后补签很常见，往前补一周要允许")

    def test_editing_cannot_backdate_either(self):
        contract = self._make()
        r = self.client.patch("/api/contracts/%s" % contract["contract_no"],
                              headers=self.landlord,
                              json={"start_date": (datetime.now() - timedelta(days=90)).isoformat()})
        self.assertEqual(r.status_code, 409)

    def test_disabled_tenant_cannot_be_signed(self):
        from app import db
        from app.models import User
        session = db.SessionLocal()
        try:
            session.query(User).filter_by(username="tenant1").one().status = "disabled"
            session.commit()
        finally:
            session.close()
        self.assertEqual(self._sign().status_code, 409)

    def test_landlord_can_fix_the_dates(self):
        contract = self._make()
        r = self.client.patch("/api/contracts/%s" % contract["contract_no"],
                              headers=self.landlord,
                              json={"start_date": self.moved.isoformat(),
                                    "months": 6})
        self.assertEqual(r.status_code, 200, r.text)
        got = r.json()["contract"]
        self.assertTrue(got["start_date"].startswith(self.moved.strftime("%Y-%m-%d")))
        self.assertTrue(got["end_date"].startswith(
            leasing.add_months(self.moved, 6).strftime("%Y-%m-%d")))

    def test_body_text_is_re_rendered(self):
        """**只改字段不重渲染正文，页面上显示的还是旧条款。**

        用户看到日期变了、正文没变，会不知道该信哪个——
        而合同正文正是双方最后要认的那份东西。
        """
        contract = self._make()
        r = self.client.patch("/api/contracts/%s" % contract["contract_no"],
                              headers=self.landlord,
                              json={"start_date": self.moved.isoformat(),
                                    "months": 6, "pay_months": 3})
        body = r.json()["contract"]["content"]
        self.assertIn(self.moved.strftime("%Y 年 %m 月 %d 日"), body)
        self.assertIn("每 3 个月支付一次", body)
        self.assertNotIn(self.start.strftime("%Y 年 %m 月 %d 日"), body)

    def test_deposit_months_recomputes_the_amount(self):
        contract = self._make()
        rent = contract["rent"]
        r = self.client.patch("/api/contracts/%s" % contract["contract_no"],
                              headers=self.landlord,
                              json={"deposit_months": 2})
        self.assertAlmostEqual(r.json()["contract"]["deposit"], rent * 2, 2)

    def test_omitted_fields_stay_put(self):
        """每一项都可以不填，不填就是不动。"""
        contract = self._make()
        r = self.client.patch("/api/contracts/%s" % contract["contract_no"],
                              headers=self.landlord, json={"pay_months": 3})
        got = r.json()["contract"]
        self.assertEqual(got["start_date"][:10], contract["start_date"][:10])
        self.assertEqual(got["end_date"][:10], contract["end_date"][:10])
        self.assertEqual(got["pay_months"], 3)

    def test_tenant_cannot_edit(self):
        contract = self._make()
        r = self.client.patch("/api/contracts/%s" % contract["contract_no"],
                              headers=self.tenant, json={"months": 3})
        self.assertEqual(r.status_code, 403)

    def test_active_contract_is_frozen(self):
        """**这条边界不是保守，是账单。**

        整期账单在生效那一刻一次排出。改一份已生效合同的租期或付款周期，
        意味着已排出的账单要重排——而其中可能已经有付过款的。
        那是一整套对账逻辑，不是一个 PATCH 能承担的。
        """
        contract = self._make()
        self.client.post("/api/contracts/%s/activate" % contract["contract_no"],
                         headers=self.tenant)
        r = self.client.patch("/api/contracts/%s" % contract["contract_no"],
                              headers=self.landlord, json={"months": 3})
        self.assertEqual(r.status_code, 409)
        self.assertIn("待确认", r.json()["detail"])

    def test_tenant_is_notified(self):
        """改完要通知租客。**他可能已经看过一遍准备点确认了**，
        条款变了却没人告诉他，等于让他确认了一份没看过的合同。"""
        contract = self._make()
        self.client.patch("/api/contracts/%s" % contract["contract_no"],
                          headers=self.landlord, json={"months": 6})
        msgs = self.client.get("/api/messages", headers=self.tenant).json()
        titles = [m["title"] for m in msgs.get("list", [])]
        self.assertIn("合同条款已修改", titles)



class TestPendingContractCanBeCalledOff(ApiTestCase):
    """待确认的合同原先没有任何一条路能结束：发错了人、租客不想签，它就一直挂着（2026-10-23）。"""

    def setUp(self):
        super().setUp()
        self.landlord = auth_header(self.client, "landlord1", "test123")
        self.tenant = auth_header(self.client, "tenant1", "test123")

    def _draft(self, tenant="tenant1", start=None):
        start = start or (datetime.now() + timedelta(days=5)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        made = self.client.post("/api/contracts", headers=self.landlord, json={
            "house_no": "DL0001", "tenant_username": tenant,
            "start_date": start.isoformat(), "months": 12})
        self.assertEqual(made.status_code, 200, made.text)
        return made.json()["contract"]["contract_no"]

    def _messages(self, headers):
        return [m["title"] for m in self.client.get("/api/messages", headers=headers).json()["list"]]

    def test_tenant_declines(self):
        no = self._draft()
        r = self.client.post("/api/contracts/%s/cancel" % no, headers=self.tenant,
                             json={"reason": "租金谈不拢"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["contract"]["status"], "cancelled")
        self.assertIn("租客没有签这份合同", self._messages(self.landlord))
        # 拒签了就不能再确认
        again = self.client.post("/api/contracts/%s/activate" % no, headers=self.tenant)
        self.assertEqual(again.status_code, 409)

    def test_landlord_withdraws(self):
        no = self._draft()
        r = self.client.post("/api/contracts/%s/cancel" % no, headers=self.landlord, json={})
        self.assertEqual(r.json()["contract"]["status"], "cancelled")
        self.assertIn("房东撤回了合同", self._messages(self.tenant))

    def test_active_contract_cannot_be_called_off(self):
        no = self._draft()
        self.client.post("/api/contracts/%s/activate" % no, headers=self.tenant)
        r = self.client.post("/api/contracts/%s/cancel" % no, headers=self.landlord, json={})
        self.assertEqual(r.status_code, 409)

    def test_signing_one_cancels_the_others_on_that_house(self):
        self.client.post("/api/auth/register", json={
            "username": "tenant2", "password": "test123", "real_name": "租客乙",
            "phone": "13900002222"})
        other = self._draft("tenant2")
        mine = self._draft()
        self.client.post("/api/contracts/%s/activate" % mine, headers=self.tenant)
        second = auth_header(self.client, "tenant2", "test123")
        detail = self.client.get("/api/contracts/%s" % other, headers=second).json()
        self.assertEqual(detail["status"], "cancelled")
        self.assertIn("合同已失效", self._messages(second))

    def test_stale_start_date_is_rechecked_on_activation(self):
        """签约时起租日合规，租客隔太久才确认，起租日就成了很久以前——一生效就是几期逾期账单。"""
        import app.db as db
        from app.models import Contract
        no = self._draft()
        session = db.SessionLocal()
        try:
            row = session.query(Contract).filter_by(contract_no=no).one()
            row.start_date = datetime.now() - timedelta(days=90)
            session.commit()
        finally:
            session.close()
        r = self.client.post("/api/contracts/%s/activate" % no, headers=self.tenant)
        self.assertEqual(r.status_code, 409)
        self.assertIn("起租日", r.json()["detail"])


class TestRenewWithNewRent(ApiTestCase):
    """续租时可以重新谈月租。原先只能按原租金续，想涨想降都得退租重签（2026-10-24）。"""

    def setUp(self):
        super().setUp()
        self.landlord = auth_header(self.client, "landlord1", "test123")
        self.tenant = auth_header(self.client, "tenant1", "test123")
        start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        no = self.client.post("/api/contracts", headers=self.landlord, json={
            "house_no": "DL0001", "tenant_username": "tenant1",
            "start_date": start.isoformat(), "months": 3}).json()["contract"]["contract_no"]
        self.client.post("/api/contracts/%s/activate" % no, headers=self.tenant)
        self.no = no

    def renew(self, headers, **body):
        return self.client.post("/api/contracts/%s/renew" % self.no, headers=headers, json=body)

    def rent_bills(self):
        rows = self.client.get("/api/bills/mine", headers=self.tenant,
                               params={"side": "tenant"}).json()["list"]
        return sorted((b for b in rows if b["type"] == "rent"), key=lambda b: b["due_date"])

    def test_landlord_proposes_tenant_accepts(self):
        asked = self.renew(self.landlord, months=6, rent=3000)
        self.assertEqual(asked.json()["contract"]["request"]["rent"], 3000)
        done = self.renew(self.tenant)              # 不带月租 = 同意
        self.assertTrue(done.json().get("renewed"), done.text)
        self.assertEqual(done.json()["contract"]["rent"], 3000)
        amounts = [b["amount"] for b in self.rent_bills()]
        # 原来 3 期按 2800，续的 6 期按 3000；已经排出的不动
        self.assertEqual(amounts[:3], [2800, 2800, 2800])
        self.assertEqual(amounts[3:], [3000] * 6)

    def test_counter_offer_becomes_a_new_request(self):
        self.renew(self.landlord, months=12, rent=3200)
        counter = self.renew(self.tenant, rent=3000)
        self.assertFalse(counter.json().get("renewed"))
        request = counter.json()["contract"]["request"]
        self.assertEqual((request["by"], request["rent"]), ("tenant", 3000))
        done = self.renew(self.landlord)
        self.assertEqual(done.json()["contract"]["rent"], 3000)

    def test_plain_renewal_keeps_the_rent(self):
        self.renew(self.tenant, months=6)
        done = self.renew(self.landlord)
        self.assertEqual(done.json()["contract"]["rent"], 2800)
        self.assertIsNone(done.json()["contract"]["request"])


if __name__ == "__main__":
    unittest.main()
