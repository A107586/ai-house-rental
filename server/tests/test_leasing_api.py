"""交易链接口测试：合同、账单、缴费、消息、公告。

业务规则本身在 test_leasing.py 里测过了，这里专测接口层：
**权限边界**（谁能签、谁能确认、谁能缴、谁能扣押金）与状态码。
"""

import unittest
from datetime import datetime, timedelta

from tests.base import ApiTestCase, auth_header, make_admin, settle


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
        response = self.client.post("/api/contracts/%s/end" % no,
                                    headers=self.landlord,
                                    json={"reason": "到期"})
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

    退租只会作废「未到期的租金」账单，押金与已到期未缴的租金仍是 unpaid。
    原先 pay_bill 只看账单状态、不看合同状态，于是**合同都结束了还能继续
    缴费**——收进来的钱既没有对应租约，也不会再产生任何权利义务，
    退租结算早就做完了，这笔钱只能靠人工退回去。

    这个洞是做合同档案时发现的：时间线上「缴纳押金」排在「合同结束」
    之后，一眼就看出顺序不对。
    """

    def test_cannot_pay_after_contract_ended(self):
        no = self.active_contract()
        bills = self.client.get("/api/bills/mine", headers=self.tenant).json()
        deposit = next(b for b in bills["list"]
                       if b["type"] == "deposit" and b["status"] == "unpaid")

        self.client.post("/api/contracts/%s/end" % no, headers=self.landlord,
                         json={"reason": "提前退租"})

        r = self.client.post("/api/bills/%s/pay" % deposit["bill_no"],
                             headers=self.tenant, json={"method": "alipay"})
        self.assertEqual(r.status_code, 409)
        self.assertIn("已结束", r.json()["detail"])

    def test_bill_stays_unpaid_after_rejection(self):
        """被拒之后账单状态不能被改动，更不能产生流水。"""
        no = self.active_contract()
        bills = self.client.get("/api/bills/mine", headers=self.tenant).json()
        deposit = next(b for b in bills["list"]
                       if b["type"] == "deposit" and b["status"] == "unpaid")
        self.client.post("/api/contracts/%s/end" % no, headers=self.landlord,
                         json={"reason": "提前退租"})
        self.client.post("/api/bills/%s/pay" % deposit["bill_no"],
                         headers=self.tenant, json={"method": "alipay"})

        after = self.client.get("/api/bills/mine", headers=self.tenant).json()
        same = next(b for b in after["list"]
                    if b["bill_no"] == deposit["bill_no"])
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

    def _make(self):
        houses = self.client.get("/api/landlord/houses",
                                 headers=self.landlord).json()["list"]
        house = next(h for h in houses if h["status"] == "available")
        r = self.client.post("/api/contracts", headers=self.landlord, json={
            "house_no": house["house_no"], "tenant_username": "tenant1",
            "start_date": "2026-10-01T00:00:00", "months": 12,
            "pay_months": 1})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["contract"]

    def test_landlord_can_fix_the_dates(self):
        contract = self._make()
        r = self.client.patch("/api/contracts/%s" % contract["contract_no"],
                              headers=self.landlord,
                              json={"start_date": "2026-11-15T00:00:00",
                                    "months": 6})
        self.assertEqual(r.status_code, 200, r.text)
        got = r.json()["contract"]
        self.assertTrue(got["start_date"].startswith("2026-11-15"))
        self.assertTrue(got["end_date"].startswith("2027-05-15"))

    def test_body_text_is_re_rendered(self):
        """**只改字段不重渲染正文，页面上显示的还是旧条款。**

        用户看到日期变了、正文没变，会不知道该信哪个——
        而合同正文正是双方最后要认的那份东西。
        """
        contract = self._make()
        r = self.client.patch("/api/contracts/%s" % contract["contract_no"],
                              headers=self.landlord,
                              json={"start_date": "2026-11-15T00:00:00",
                                    "months": 6, "pay_months": 3})
        body = r.json()["contract"]["content"]
        self.assertIn("2026 年 11 月 15 日", body)
        self.assertIn("每 3 个月支付一次", body)
        self.assertNotIn("2026 年 10 月 01 日", body)

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


if __name__ == "__main__":
    unittest.main()
