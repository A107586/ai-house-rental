"""合同详情接口测试（待完善清单 A-3 / A-4）。

「我的租约」原先只有一张摘要卡——起止日期、月租、押金、剩余天数。
缺的是「这份合同上发生过什么」：排了几期账单、交了几期、租期内报过几次修、
状态什么时候变的。这个接口把三块拼在一起返回。

三条边界在这里被钉死：

* **只能看自己的合同**——合同里有双方姓名与全部资金往来
* **报修只取租期之内的**——这套房在别人租的时候报过的修与你无关
* **contract_no 不放宽可见范围**——传别人的合同号只能拿到空列表
"""

from datetime import datetime, timedelta

from tests.base import ApiTestCase, auth_header, make_admin


def future(days=1):
    return (datetime.now() + timedelta(days=days)).replace(
        hour=0, minute=0, second=0, microsecond=0)


class DetailCase(ApiTestCase):

    def setUp(self):
        super().setUp()
        self.admin = make_admin(self.client)
        self.landlord = auth_header(self.client, "landlord1", "test123")
        self.tenant = auth_header(self.client, "tenant1", "test123")

    def sign_and_activate(self, months=12):
        no = self.client.post("/api/contracts", headers=self.landlord, json={
            "house_no": "DL0001", "tenant_username": "tenant1",
            "start_date": future().isoformat(), "months": months,
        }).json()["contract"]["contract_no"]
        self.client.post("/api/contracts/%s/activate" % no, headers=self.tenant)
        return no

    def pay_first_two(self):
        bills = self.client.get("/api/bills/mine", headers=self.tenant).json()
        for bill in bills["list"][:2]:
            self.client.post("/api/bills/%s/pay" % bill["bill_no"],
                             headers=self.tenant, json={"method": "alipay"})

    def detail(self, no, headers=None):
        return self.client.get("/api/contracts/%s/detail" % no,
                               headers=headers or self.tenant)


class TestDetailContent(DetailCase):

    def test_bills_and_summary(self):
        no = self.sign_and_activate(months=12)
        self.pay_first_two()
        body = self.detail(no).json()

        # 1 条押金 + 12 条租金
        self.assertEqual(body["summary"]["total_periods"], 13)
        self.assertEqual(body["summary"]["paid_count"], 2)
        self.assertEqual(body["summary"]["unpaid_count"], 11)
        self.assertGreater(body["summary"]["paid_amount"], 0)
        self.assertEqual(len(body["bills"]), 13)

    def test_bills_sorted_by_due_date(self):
        """账单要按到期日排，用户是按时间理解自己的租约的。"""
        no = self.sign_and_activate()
        dates = [b["due_date"] for b in self.detail(no).json()["bills"]]
        self.assertEqual(dates, sorted(dates))

    def test_timeline_has_creation_and_activation(self):
        no = self.sign_and_activate()
        titles = [t["title"] for t in self.detail(no).json()["timeline"]]
        self.assertIn("合同创建", titles)
        self.assertIn("租客确认，合同生效", titles)

    def test_timeline_records_each_payment(self):
        no = self.sign_and_activate()
        self.pay_first_two()
        timeline = self.detail(no).json()["timeline"]
        paid = [t for t in timeline if t["title"].startswith("缴纳")]
        self.assertEqual(len(paid), 2)
        self.assertIn("元", paid[0]["detail"])

    def test_timeline_is_chronological(self):
        no = self.sign_and_activate()
        self.pay_first_two()
        stamps = [t["at"] for t in self.detail(no).json()["timeline"]]
        self.assertEqual(stamps, sorted(stamps))

    def test_timeline_records_ending(self):
        no = self.sign_and_activate()
        self.client.post("/api/contracts/%s/end" % no, headers=self.landlord,
                         json={"reason": "租期届满"})
        timeline = self.detail(no).json()["timeline"]
        self.assertEqual(timeline[-1]["title"], "合同结束")
        self.assertIn("租期届满", timeline[-1]["detail"])

    def test_repairs_within_lease_are_included(self):
        no = self.sign_and_activate()
        self.client.post("/api/repairs", headers=self.tenant, json={
            "house_no": "DL0001", "content": "水管漏水",
            "contact_name": "租客甲", "contact_phone": "13900005678"})
        body = self.detail(no).json()
        self.assertEqual(len(body["repairs"]), 1)
        self.assertEqual(body["repairs"][0]["content"], "水管漏水")

    def test_repairs_before_lease_are_excluded(self):
        """这套房在别人租的时候报过的修，与这份合同无关——
        摆进来只会让人以为是自己惹的。"""
        self.client.post("/api/repairs", headers=self.tenant, json={
            "house_no": "DL0001", "content": "上一任租客报的修",
            "contact_name": "租客甲", "contact_phone": "13900005678"})
        # 合同起租日在明天，刚才那条报修是现在提交的，不该算进来
        no = self.sign_and_activate()
        self.assertEqual(len(self.detail(no).json()["repairs"]), 0)

    def test_contract_block_is_present(self):
        no = self.sign_and_activate()
        self.assertEqual(self.detail(no).json()["contract"]["contract_no"], no)


class TestDetailAccess(DetailCase):
    """合同里有双方姓名与全部资金往来，不能被第三方看到。"""

    def test_tenant_can_read(self):
        no = self.sign_and_activate()
        self.assertEqual(self.detail(no).status_code, 200)

    def test_landlord_can_read(self):
        no = self.sign_and_activate()
        self.assertEqual(self.detail(no, self.landlord).status_code, 200)

    def test_admin_can_read(self):
        no = self.sign_and_activate()
        self.assertEqual(self.detail(no, self.admin).status_code, 200)

    def test_outsider_denied(self):
        no = self.sign_and_activate()
        self.client.post("/api/auth/register", json={
            "username": "stranger", "password": "test123", "role": "tenant"})
        other = auth_header(self.client, "stranger", "test123")
        self.assertEqual(self.detail(no, other).status_code, 403)

    def test_anonymous_denied(self):
        no = self.sign_and_activate()
        self.assertEqual(
            self.client.get("/api/contracts/%s/detail" % no).status_code, 401)

    def test_unknown_contract_404(self):
        self.assertEqual(self.detail("HT-NOT-EXIST").status_code, 404)


class TestBillContractFilter(DetailCase):
    """A-3：账单要能按合同筛选，且不能借此看到别人的账单。"""

    def test_filter_narrows_to_one_contract(self):
        first = self.sign_and_activate(months=12)
        all_bills = self.client.get("/api/bills/mine",
                                    headers=self.tenant).json()
        filtered = self.client.get("/api/bills/mine?contract_no=%s" % first,
                                   headers=self.tenant).json()
        self.assertEqual(filtered["total"], 13)
        self.assertLessEqual(filtered["total"], all_bills["total"])
        self.assertTrue(all(b["contract_no"] == first
                            for b in filtered["list"]))

    def test_filter_does_not_widen_visibility(self):
        """传别人的合同号只能拿到空列表，不能借这个参数越权。"""
        no = self.sign_and_activate()
        self.client.post("/api/auth/register", json={
            "username": "stranger2", "password": "test123", "role": "tenant"})
        other = auth_header(self.client, "stranger2", "test123")
        body = self.client.get("/api/bills/mine?contract_no=%s" % no,
                               headers=other).json()
        self.assertEqual(body["total"], 0)

    def test_unknown_contract_returns_empty_not_everything(self):
        """合同号不存在时必须返回空，而不是忽略参数把全部账单吐出来——
        忽略是最坏的一种失败。"""
        self.sign_and_activate()
        body = self.client.get("/api/bills/mine?contract_no=HT-NOPE",
                               headers=self.tenant).json()
        self.assertEqual(body["total"], 0)

    def test_bill_carries_contract_no(self):
        no = self.sign_and_activate()
        bills = self.client.get("/api/bills/mine", headers=self.tenant).json()
        self.assertTrue(all(b["contract_no"] for b in bills["list"]))
        self.assertIn(no, {b["contract_no"] for b in bills["list"]})
