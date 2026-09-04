"""合同条款的两处口径问题：押金（K-02）与甲乙双方姓名（K-03）。

这两条都来自 `docs/贝壳对标待办清单.md`，也都是**组员实际踩到的缺陷**，
不是对着竞品列出来的功能缺口。所以它们各自都要有回归测试兜着——
这类「两处算出两个数」的错，改对一次之后最容易在后续重构里悄悄回退。

单独成文件而不是并进 `test_leasing.py`，是因为那份文件测的是
「排账单、加月份、退租作废哪些账单」这类流程规则；
这里测的是**同一个值在不同地方是否一致**，关注点不一样。
"""

from datetime import datetime

from tests.base import ApiTestCase, auth_header
import app.db as db
from app import leasing
from app.models import House, User


class TermsCase(ApiTestCase):

    def setUp(self):
        super().setUp()
        self.session = db.SessionLocal()
        self.house = self.session.query(House).filter_by(house_no="DL0001").one()
        self.tenant = self.session.query(User).filter_by(username="tenant1").one()

    def tearDown(self):
        self.session.close()


# ------------------------------------------------------------------ K-02 押金

class TestDepositMatchesListing(TermsCase):
    """房东在房源上填的押金，就是合同上要签的那个数。"""

    def test_uses_listing_deposit(self):
        """房源填了押金 800，合同押金必须也是 800。

        改造前这里是 `租金 × 押金月数` = 2800，
        而租客在详情页上看到的是 800——同一套房两个押金。
        """
        self.house.deposit = 800
        self.session.commit()

        contract = leasing.create_contract(
            self.session, self.house, self.tenant, datetime(2026, 10, 1))
        self.assertEqual(float(contract.deposit), 800.0)

    def test_contract_text_quotes_the_same_number(self):
        """正文里的金额和 `contract.deposit` 必须是同一个数。

        AI-02 合同审阅读的就是这段正文；正文和字段对不上，
        审阅结论也就跟着错。
        """
        self.house.deposit = 800
        self.session.commit()

        contract = leasing.create_contract(
            self.session, self.house, self.tenant, datetime(2026, 10, 1))
        self.assertIn("押金人民币 800 元", contract.content)

    def test_deposit_bill_matches_contract(self):
        """押金账单的金额也走同一个口径。"""
        self.house.deposit = 800
        self.session.commit()

        contract = leasing.create_contract(
            self.session, self.house, self.tenant, datetime(2026, 10, 1))
        leasing.activate_contract(self.session, contract)
        self.session.commit()

        deposit_bills = [b for b in contract.bills if b.type == "deposit"]
        self.assertEqual(len(deposit_bills), 1)
        self.assertEqual(float(deposit_bills[0].amount), 800.0)

    def test_explicit_months_wins(self):
        """房东签约时明确选了「押二」，就按押二走，覆盖房源上的默认值。"""
        self.house.deposit = 800
        self.session.commit()

        contract = leasing.create_contract(
            self.session, self.house, self.tenant, datetime(2026, 10, 1),
            deposit_months=2)
        self.assertEqual(float(contract.deposit), 5600.0)

    def test_zero_months_means_no_deposit(self):
        """免押是合法取值。0 不能被当成「没填」而回落到房源押金。"""
        self.house.deposit = 800
        self.session.commit()

        contract = leasing.create_contract(
            self.session, self.house, self.tenant, datetime(2026, 10, 1),
            deposit_months=0)
        self.assertEqual(float(contract.deposit), 0.0)

    def test_falls_back_to_config_when_listing_has_none(self):
        """房源没填押金时才回落到系统配置的押金月数。"""
        self.house.deposit = 0
        self.session.commit()

        contract = leasing.create_contract(
            self.session, self.house, self.tenant, datetime(2026, 10, 1),
            default_deposit_months=2)
        self.assertEqual(float(contract.deposit), 5600.0)


class TestDepositThroughApi(ApiTestCase):
    """接口层：房东不传 deposit_months 时，押金取房源上填的值。"""

    def setUp(self):
        super().setUp()
        self.landlord = auth_header(self.client, "landlord1", "test123")
        self.tenant = auth_header(self.client, "tenant1", "test123")
        session = db.SessionLocal()
        try:
            house = session.query(House).filter_by(house_no="DL0001").one()
            house.deposit = 800
            session.commit()
        finally:
            session.close()

    def sign(self, **extra):
        payload = {
            "house_no": "DL0001",
            "tenant_username": "tenant1",
            "start_date": datetime(2026, 10, 1).isoformat(),
            "months": 12,
        }
        payload.update(extra)
        return self.client.post("/api/contracts", headers=self.landlord,
                                json=payload)

    def test_listing_deposit_is_used(self):
        response = self.sign()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(float(response.json()["contract"]["deposit"]), 800.0)

    def test_detail_and_contract_agree(self):
        """详情页展示的押金，和签出来的合同押金，必须是同一个数。

        这正是缺陷的原始现象：两个页面对同一套房给出两个押金。
        """
        listed = self.client.get("/api/houses/DL0001").json()["deposit"]
        signed = self.sign().json()["contract"]["deposit"]
        self.assertEqual(float(listed), float(signed))

    def test_explicit_deposit_months_still_honoured(self):
        response = self.sign(deposit_months=2)
        self.assertEqual(float(response.json()["contract"]["deposit"]), 5600.0)


# ------------------------------------------------------------------ K-03 姓名

class TestPartiesMustBeNamed(TermsCase):
    """合同是法律文本，甲乙双方必须是真实姓名，不能是登录名。"""

    def test_rejects_tenant_without_real_name(self):
        self.tenant.real_name = ""
        self.session.commit()

        with self.assertRaises(ValueError) as caught:
            leasing.create_contract(self.session, self.house, self.tenant,
                                    datetime(2026, 10, 1))
        self.assertIn("租客尚未实名", str(caught.exception))

    def test_rejects_landlord_without_real_name(self):
        landlord = self.session.get(User, self.house.landlord_id)
        landlord.real_name = ""
        self.session.commit()

        with self.assertRaises(ValueError) as caught:
            leasing.create_contract(self.session, self.house, self.tenant,
                                    datetime(2026, 10, 1))
        self.assertIn("出租方尚未实名", str(caught.exception))

    def test_whitespace_only_name_is_not_a_name(self):
        """只填了空格不算实名——否则绕过校验只要敲一下空格键。"""
        self.tenant.real_name = "   "
        self.session.commit()

        with self.assertRaises(ValueError):
            leasing.create_contract(self.session, self.house, self.tenant,
                                    datetime(2026, 10, 1))

    def test_no_contract_is_left_behind_when_rejected(self):
        """拒绝签约时不能留下半份合同。"""
        from app.models import Contract

        self.tenant.real_name = ""
        self.session.commit()
        before = self.session.query(Contract).count()

        with self.assertRaises(ValueError):
            leasing.create_contract(self.session, self.house, self.tenant,
                                    datetime(2026, 10, 1))
        self.session.rollback()
        self.assertEqual(self.session.query(Contract).count(), before)

    def test_text_carries_both_real_names(self):
        contract = leasing.create_contract(
            self.session, self.house, self.tenant, datetime(2026, 10, 1))
        self.assertIn("出租方（甲方）：房东甲", contract.content)
        self.assertIn("承租方（乙方）：租客甲", contract.content)
        self.assertNotIn("tenant1", contract.content)


class TestUnnamedTenantRejectedByApi(ApiTestCase):

    def setUp(self):
        super().setUp()
        self.landlord = auth_header(self.client, "landlord1", "test123")
        session = db.SessionLocal()
        try:
            tenant = session.query(User).filter_by(username="tenant1").one()
            tenant.real_name = ""
            session.commit()
        finally:
            session.close()

    def test_returns_409_with_actionable_message(self):
        response = self.client.post("/api/contracts", headers=self.landlord, json={
            "house_no": "DL0001",
            "tenant_username": "tenant1",
            "start_date": datetime(2026, 10, 1).isoformat(),
            "months": 12,
        })
        self.assertEqual(response.status_code, 409)
        # 提示要能告诉房东「去哪儿改」，只说「未实名」是没法照着做的
        self.assertIn("编辑资料", response.json()["detail"])
