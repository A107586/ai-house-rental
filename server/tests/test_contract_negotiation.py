"""签约时谈定的条款：月租、押金、中介佣金（2026-10-22）。

原先这三样都锁死在房源与委托上：月租取挂牌价，押金按月数乘挂牌价，
佣金按委托时写的比例。议了价只能先去改房源挂牌价再签，
而改挂牌价又会让其他看房的人看到一个专为某人谈的价。
"""

from datetime import datetime, timedelta

from tests.base import ApiTestCase, auth_header
from tests.test_agent_broker import BrokerCase


def _soon(days=7):
    """相对日期：起租日最多往前补 30 天，写死的日子过几周就红"""
    return (datetime.now() + timedelta(days=days)).replace(
        hour=0, minute=0, second=0, microsecond=0).isoformat()


class TermsCase(BrokerCase):

    def sign(self, **terms):
        body = {"house_no": "DL0001", "tenant_username": "tenant1",
                "start_date": _soon(), "months": 12}
        body.update(terms)
        return self.client.post("/api/contracts", headers=self.landlord, json=body)

    def patch(self, contract_no, **terms):
        return self.client.patch("/api/contracts/%s" % contract_no,
                                 headers=self.landlord, json=terms)

    def list_rent(self):
        return self.client.get("/api/houses/DL0001").json()["rent_price"]


class TestNegotiatedRent(TermsCase):

    def test_rent_and_deposit_can_be_agreed_at_signing(self):
        r = self.sign(rent=700, deposit_months=2)
        self.assertEqual(r.status_code, 200, r.text)
        got = r.json()["contract"]
        self.assertEqual(got["rent"], 700)
        # 押二押的是议定后的月租，不是挂牌价
        self.assertEqual(got["deposit"], 1400)
        self.assertEqual(got["list_rent"], self.list_rent())
        self.assertIn("700", got["content"])

    def test_explicit_deposit_amount_wins(self):
        got = self.sign(rent=700, deposit=500, deposit_months=2).json()["contract"]
        self.assertEqual(got["deposit"], 500)

    def test_deposit_is_capped_at_six_months(self):
        r = self.sign(rent=700, deposit=7000)
        self.assertEqual(r.status_code, 409)
        self.assertIn("6 个月", r.json()["detail"])

    def test_omitted_terms_fall_back_to_the_listing(self):
        got = self.sign().json()["contract"]
        self.assertEqual(got["rent"], self.list_rent())
        self.assertIsNone(got["commission_rate"])

    def test_editing_dates_keeps_the_agreed_rent(self):
        """原先改一次日期，月租就被拉回挂牌价，议的价悄悄没了。"""
        no = self.sign(rent=700, deposit_months=1).json()["contract"]["contract_no"]
        got = self.patch(no, months=6).json()["contract"]
        self.assertEqual(got["rent"], 700)
        self.assertEqual(got["deposit"], 700)

    def test_editing_rent_moves_a_whole_month_deposit(self):
        no = self.sign(rent=700, deposit_months=2).json()["contract"]["contract_no"]
        got = self.patch(no, rent=650).json()["contract"]
        self.assertEqual((got["rent"], got["deposit"]), (650, 1300))
        self.assertIn("650", got["content"])

    def test_editing_rent_keeps_an_odd_deposit(self):
        no = self.sign(rent=700, deposit=500).json()["contract"]["contract_no"]
        got = self.patch(no, rent=650).json()["contract"]
        self.assertEqual((got["rent"], got["deposit"]), (650, 500))


class TestCommission(TermsCase):

    def commissions(self):
        return self.client.get("/api/agent/commissions",
                               headers=self.agent).json()["list"]

    def test_no_delegation_no_commission(self):
        r = self.sign(commission_rate=30)
        self.assertEqual(r.status_code, 409)
        self.assertIn("没有委托中介", r.json()["detail"])

    def test_agreed_rate_is_what_gets_settled(self):
        self.delegate("DL0001", rate=50)
        r = self.sign(rent=700, commission_rate=30)
        self.assertEqual(r.status_code, 200, r.text)
        no = r.json()["contract"]["contract_no"]
        self.assertEqual(r.json()["contract"]["commission_rate"], 30)

        # 比例变了要告诉中介：等到「佣金已入账」才发现，合同已经改不回来了
        titles = [m["title"] for m in self.client.get(
            "/api/messages", headers=self.agent).json()["list"]]
        self.assertIn("签约佣金比例有调整", titles)

        self.client.post("/api/contracts/%s/activate" % no, headers=self.tenant)
        row = next(c for c in self.commissions() if c["contract_no"] == no)
        self.assertEqual((row["base_amount"], row["rate"], row["amount"]),
                         (700, 30, 210))

    def test_unchanged_rate_does_not_ping_the_agent(self):
        self.delegate("DL0001", rate=50)
        self.sign(commission_rate=50)
        titles = [m["title"] for m in self.client.get(
            "/api/messages", headers=self.agent).json()["list"]]
        self.assertNotIn("签约佣金比例有调整", titles)

    def test_without_agreed_rate_the_delegation_rate_applies(self):
        self.delegate("DL0001", rate=50)
        no = self.sign(rent=700).json()["contract"]["contract_no"]
        self.client.post("/api/contracts/%s/activate" % no, headers=self.tenant)
        row = next(c for c in self.commissions() if c["contract_no"] == no)
        self.assertEqual(row["amount"], 350)

    def test_rate_can_be_changed_before_activation(self):
        self.delegate("DL0001", rate=50)
        no = self.sign(rent=700).json()["contract"]["contract_no"]
        got = self.patch(no, commission_rate=40).json()["contract"]
        self.assertEqual(got["commission_rate"], 40)
        self.client.post("/api/contracts/%s/activate" % no, headers=self.tenant)
        row = next(c for c in self.commissions() if c["contract_no"] == no)
        self.assertEqual(row["amount"], 280)


class TestTenantConfirmsWhatHeSaw(ApiTestCase):
    """待确认的合同房东能改；租客开着旧页面点确认，确认的却是改过的条款（2026-10-24）。"""

    def setUp(self):
        super().setUp()
        self.landlord = auth_header(self.client, "landlord1", "test123")
        self.tenant = auth_header(self.client, "tenant1", "test123")
        start = (datetime.now() + timedelta(days=5)).replace(hour=0, minute=0, second=0,
                                                            microsecond=0)
        made = self.client.post("/api/contracts", headers=self.landlord, json={
            "house_no": "DL0001", "tenant_username": "tenant1",
            "start_date": start.isoformat(), "months": 12})
        self.contract = made.json()["contract"]

    def test_stale_terms_are_refused(self):
        seen = self.contract["terms_version"]
        self.client.patch("/api/contracts/%s" % self.contract["contract_no"],
                          headers=self.landlord, json={"rent": 3600})
        r = self.client.post("/api/contracts/%s/activate" % self.contract["contract_no"],
                             headers=self.tenant, json={"terms_version": seen})
        self.assertEqual(r.status_code, 409)
        self.assertIn("改过", r.json()["detail"])

    def test_current_terms_go_through(self):
        r = self.client.post("/api/contracts/%s/activate" % self.contract["contract_no"],
                             headers=self.tenant,
                             json={"terms_version": self.contract["terms_version"]})
        self.assertEqual(r.status_code, 200, r.text)

    def test_old_clients_without_a_version_still_work(self):
        r = self.client.post("/api/contracts/%s/activate" % self.contract["contract_no"],
                             headers=self.tenant)
        self.assertEqual(r.status_code, 200, r.text)
