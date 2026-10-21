"""户主档案测试（F-02）。

「用户管理」看的是账号本身，户主档案要回答的是另一类问题：
这个房东手上有多少房、租出去几套、这个月收了多少、有没有积压的工单。
"""

from datetime import datetime, timedelta

from tests.base import ApiTestCase, auth_header, make_admin, make_resident


def future(days=1):
    return (datetime.now() + timedelta(days=days)).replace(
        hour=0, minute=0, second=0, microsecond=0)


class LandlordRosterCase(ApiTestCase):

    def setUp(self):
        super().setUp()
        self.admin = make_admin(self.client)
        self.landlord = auth_header(self.client, "landlord1", "test123")
        self.tenant = auth_header(self.client, "tenant1", "test123")

    def roster(self, **params):
        return self.client.get("/api/admin/landlords", headers=self.admin,
                               params=params).json()


class TestRoster(LandlordRosterCase):

    def test_lists_only_landlords(self):
        body = self.roster()
        self.assertGreaterEqual(body["total"], 1)
        self.assertTrue(all(r["role"] == "landlord" for r in body["list"]))

    def test_counts_houses(self):
        row = next(r for r in self.roster()["list"]
                   if r["username"] == "landlord1")
        self.assertEqual(row["house_total"], 3)
        self.assertEqual(row["house_rented"], 1)

    def test_phone_is_masked(self):
        """管理员联系房东走站内消息，不需要看到完整号码。"""
        row = next(r for r in self.roster()["list"]
                   if r["username"] == "landlord1")
        self.assertEqual(row["phone"], "138****1234")

    def test_counts_open_repairs(self):
        make_resident("tenant1", "DL0001")   # 报修只收在住租客
        self.client.post("/api/repairs", headers=self.tenant, json={
            "house_no": "DL0001", "content": "水管漏水",
            "contact_name": "租客甲", "contact_phone": "13900005678"})
        row = next(r for r in self.roster()["list"]
                   if r["username"] == "landlord1")
        self.assertEqual(row["repair_open"], 1)

    def test_income_and_unpaid(self):
        no = self.client.post("/api/contracts", headers=self.landlord, json={
            "house_no": "DL0001", "tenant_username": "tenant1",
            "start_date": future().isoformat(), "months": 12,
        }).json()["contract"]["contract_no"]
        self.client.post("/api/contracts/%s/activate" % no, headers=self.tenant)

        row = next(r for r in self.roster()["list"]
                   if r["username"] == "landlord1")
        self.assertEqual(row["contract_active"], 1)
        self.assertGreater(row["unpaid"], 0, "刚生成的账单应计入待收")
        self.assertEqual(row["month_income"], 0, "还没缴费，本月收入应为 0")

    def test_search_by_name(self):
        self.assertEqual(self.roster(keyword="landlord1")["total"], 1)
        self.assertEqual(self.roster(keyword="不存在的房东")["total"], 0)

    def test_sorted_by_house_count(self):
        self.client.post("/api/auth/register", json={
            "username": "landlord9", "password": "test123", "role": "landlord"})
        rows = self.roster()["list"]
        counts = [r["house_total"] for r in rows]
        self.assertEqual(counts, sorted(counts, reverse=True))

    def test_landlord_with_no_house(self):
        """新注册的房东一套房都没有，不能因此报错或被漏掉。"""
        self.client.post("/api/auth/register", json={
            "username": "landlord9", "password": "test123", "role": "landlord"})
        row = next(r for r in self.roster()["list"]
                   if r["username"] == "landlord9")
        self.assertEqual(row["house_total"], 0)
        self.assertEqual(row["month_income"], 0)


class TestRosterAccess(LandlordRosterCase):

    def test_landlord_denied(self):
        """房东能翻这张表就等于能看别的房东的经营数据。"""
        self.assertEqual(
            self.client.get("/api/admin/landlords",
                            headers=self.landlord).status_code, 403)

    def test_tenant_denied(self):
        self.assertEqual(
            self.client.get("/api/admin/landlords",
                            headers=self.tenant).status_code, 403)

    def test_anonymous_denied(self):
        self.assertEqual(
            self.client.get("/api/admin/landlords").status_code, 401)
