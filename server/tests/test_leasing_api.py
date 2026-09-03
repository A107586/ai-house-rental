"""交易链接口测试：合同、账单、缴费、消息、公告。

业务规则本身在 test_leasing.py 里测过了，这里专测接口层：
**权限边界**（谁能签、谁能确认、谁能缴、谁能扣押金）与状态码。
"""

import unittest
from datetime import datetime, timedelta

from tests.base import ApiTestCase, auth_header


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
        self.client.post("/api/bills/%s/pay" % deposit["bill_no"],
                         headers=self.tenant, json={"method": "alipay"})
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

    def test_pay(self):
        self.active_contract()
        bill_no = self.client.get("/api/bills/mine",
                                  headers=self.tenant).json()["list"][0]["bill_no"]
        response = self.client.post("/api/bills/%s/pay" % bill_no,
                                    headers=self.tenant, json={"method": "wechat"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["bill"]["status"], "paid")
        self.assertEqual(body["payment"]["method"], "wechat")

    def test_double_pay_rejected(self):
        self.active_contract()
        bill_no = self.client.get("/api/bills/mine",
                                  headers=self.tenant).json()["list"][0]["bill_no"]
        self.client.post("/api/bills/%s/pay" % bill_no, headers=self.tenant, json={})
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
        paid = self.client.post("/api/bills/%s/pay" % bill_no,
                                headers=self.tenant, json={}).json()
        trade_no = paid["payment"]["trade_no"]
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
        response = self.client.post("/api/contracts/%s/end" % no,
                                    headers=self.landlord,
                                    json={"reason": "到期"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["contract"]["status"], "ended")
        self.assertEqual(
            self.client.get("/api/houses/DL0001").json()["status"], "available")

    def test_tenant_can_end_without_deduct(self):
        no = self.active_contract()
        response = self.client.post("/api/contracts/%s/end" % no,
                                    headers=self.tenant, json={"reason": "提前退租"})
        self.assertEqual(response.status_code, 200)

    def test_tenant_cannot_deduct_deposit(self):
        """扣押金是出租方的权利，租客不能自己填扣款。"""
        no = self.active_contract()
        response = self.client.post("/api/contracts/%s/end" % no,
                                    headers=self.tenant, json={"deduct": 500})
        self.assertEqual(response.status_code, 403)

    def test_landlord_deducts(self):
        no = self.active_contract()
        self.pay_deposit(no)
        response = self.client.post("/api/contracts/%s/end" % no,
                                    headers=self.landlord, json={"deduct": 500})
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
        response = self.client.post("/api/contracts/%s/end" % no,
                                    headers=self.landlord, json={"deduct": 0})
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
        self.client.post("/api/contracts/%s/end" % no,
                         headers=self.landlord, json={"deduct": 0})
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

    def test_renew(self):
        no = self.active_contract()
        response = self.client.post("/api/contracts/%s/renew" % no,
                                    headers=self.landlord, json={"months": 6})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["contract"]["status"], "pending")

    def test_renew_pending_rejected(self):
        no = self.sign().json()["contract"]["contract_no"]
        response = self.client.post("/api/contracts/%s/renew" % no,
                                    headers=self.landlord, json={})
        self.assertEqual(response.status_code, 409)


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

    def test_income_appears_after_payment(self):
        self.active_contract()
        before = self.client.get("/api/stats").json()
        self.assertEqual(before["income_received"], 0)
        self.assertGreater(before["income_pending"], 0)

        bills = self.client.get("/api/bills/mine", headers=self.tenant).json()["list"]
        rent_bill = [b for b in bills if b["type"] == "rent"][0]
        self.client.post("/api/bills/%s/pay" % rent_bill["bill_no"],
                         headers=self.tenant, json={})

        after = self.client.get("/api/stats").json()
        self.assertEqual(after["income_received"], 2800.0)
        self.assertEqual(len(after["monthly_income"]), 1)

    def test_contract_and_occupancy(self):
        self.active_contract()
        stats = self.client.get("/api/stats").json()
        self.assertEqual(stats["contract_active"], 1)
        self.assertGreater(stats["occupancy_rate"], 0)


if __name__ == "__main__":
    unittest.main()
