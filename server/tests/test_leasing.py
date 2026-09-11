"""交易链业务规则测试：签约、排账单、缴费、退租、续租。

直接测 `app.leasing` 里的纯业务函数，不经过 HTTP——
这层的规则最容易出错（日期加月、末期不足整周期、退租作废哪些账单），
单独测能把问题定位得很准。
"""

import unittest
from datetime import datetime, timedelta

from tests.base import ApiTestCase, engine
import app.db as db
from app import leasing
from app.models import Bill, Contract, House, Message, User


class TestAddMonths(unittest.TestCase):
    """按月加日期。月末溢出是这里最容易写错的地方。"""

    def test_plain(self):
        self.assertEqual(leasing.add_months(datetime(2026, 1, 15), 1),
                         datetime(2026, 2, 15))

    def test_cross_year(self):
        self.assertEqual(leasing.add_months(datetime(2026, 11, 10), 3),
                         datetime(2027, 2, 10))

    def test_month_end_overflow(self):
        """1 月 31 日加一个月应落到 2 月 28 日，不能抛异常。"""
        self.assertEqual(leasing.add_months(datetime(2026, 1, 31), 1),
                         datetime(2026, 2, 28))

    def test_leap_year(self):
        self.assertEqual(leasing.add_months(datetime(2028, 1, 31), 1),
                         datetime(2028, 2, 29))

    def test_zero(self):
        self.assertEqual(leasing.add_months(datetime(2026, 5, 5), 0),
                         datetime(2026, 5, 5))


class TestMonthsBetween(unittest.TestCase):

    def test_exact_year(self):
        self.assertEqual(leasing.months_between(datetime(2026, 1, 1),
                                                datetime(2027, 1, 1)), 12)

    def test_half_month_counts_as_one(self):
        """不足一月按一月算，不能少收租。"""
        self.assertEqual(leasing.months_between(datetime(2026, 1, 1),
                                                datetime(2026, 1, 20)), 1)

    def test_partial_month_rounds_up(self):
        self.assertEqual(leasing.months_between(datetime(2026, 1, 10),
                                                datetime(2026, 3, 20)), 3)


class LeasingCase(ApiTestCase):
    """带数据库的业务流程测试。"""

    def setUp(self):
        super().setUp()
        self.session = db.SessionLocal()
        self.house = self.session.query(House).filter_by(house_no="DL0001").one()
        self.tenant = self.session.query(User).filter_by(username="tenant1").one()

    def tearDown(self):
        self.session.close()

    def sign(self, months=12, pay_months=1):
        contract = leasing.create_contract(
            self.session, self.house, self.tenant,
            start_date=datetime(2026, 10, 1), months=months, pay_months=pay_months)
        self.session.commit()
        return contract


class TestCreateContract(LeasingCase):

    def test_fields(self):
        contract = self.sign()
        self.assertEqual(contract.status, "pending")
        self.assertEqual(float(contract.rent), 2800.0)
        self.assertEqual(float(contract.deposit), 2800.0)
        self.assertEqual(contract.end_date, datetime(2027, 10, 1))
        self.assertTrue(contract.contract_no.startswith("HT"))

    def test_content_contains_key_clauses(self):
        """合同正文要写全条款——AI-02 合同审阅要读的就是这段。"""
        text = self.sign().content
        for clause in ("租赁期限", "租金", "押金", "维修责任", "违约责任"):
            self.assertIn(clause, text)

    def test_pending_does_not_occupy_house(self):
        """待确认阶段不能占用房源，否则一挂着就没人能签了。"""
        self.sign()
        self.assertEqual(self.house.status, "available")

    def test_rejects_unavailable_house(self):
        rented = self.session.query(House).filter_by(house_no="DL0003").one()
        with self.assertRaises(ValueError):
            leasing.create_contract(self.session, rented, self.tenant,
                                    datetime(2026, 10, 1))

    def test_rejects_zero_months(self):
        with self.assertRaises(ValueError):
            leasing.create_contract(self.session, self.house, self.tenant,
                                    datetime(2026, 10, 1), months=0)


class TestActivate(LeasingCase):

    def test_activate_occupies_house(self):
        contract = self.sign()
        leasing.activate_contract(self.session, contract)
        self.session.commit()
        self.assertEqual(contract.status, "active")
        self.assertEqual(self.house.status, "rented")
        self.assertIsNotNone(contract.signed_at)

    def test_activate_twice_rejected(self):
        contract = self.sign()
        leasing.activate_contract(self.session, contract)
        with self.assertRaises(ValueError):
            leasing.activate_contract(self.session, contract)

    def test_notifies_both_sides(self):
        contract = self.sign()
        leasing.activate_contract(self.session, contract)
        self.session.commit()
        for uid in (contract.tenant_id, contract.landlord_id):
            count = self.session.query(Message).filter_by(user_id=uid).count()
            self.assertGreater(count, 0)


class TestBills(LeasingCase):

    def bills_of(self, contract):
        return (self.session.query(Bill)
                .filter_by(contract_id=contract.id)
                .order_by(Bill.due_date, Bill.id).all())

    def test_monthly_bills(self):
        """月付 12 个月：1 条押金 + 12 条租金。"""
        contract = self.sign(months=12, pay_months=1)
        leasing.activate_contract(self.session, contract)
        self.session.commit()

        bills = self.bills_of(contract)
        self.assertEqual(len(bills), 13)
        self.assertEqual(sum(1 for b in bills if b.type == "deposit"), 1)
        self.assertEqual(sum(1 for b in bills if b.type == "rent"), 12)

    def test_quarterly_bills(self):
        """押一付三：12 个月排 4 期，每期 3 个月租金。"""
        contract = self.sign(months=12, pay_months=3)
        leasing.activate_contract(self.session, contract)
        self.session.commit()

        rents = [b for b in self.bills_of(contract) if b.type == "rent"]
        self.assertEqual(len(rents), 4)
        self.assertEqual(float(rents[0].amount), 2800.0 * 3)

    def test_last_period_may_be_short(self):
        """租期 10 个月按季付：最后一期只有 1 个月，不能多收。"""
        contract = self.sign(months=10, pay_months=3)
        leasing.activate_contract(self.session, contract)
        self.session.commit()

        rents = [b for b in self.bills_of(contract) if b.type == "rent"]
        self.assertEqual(len(rents), 4)
        self.assertEqual(float(rents[-1].amount), 2800.0)

    def test_total_equals_rent_times_months(self):
        """所有租金账单加起来必须正好等于月租 × 租期，不能多也不能少。"""
        contract = self.sign(months=7, pay_months=2)
        leasing.activate_contract(self.session, contract)
        self.session.commit()

        total = sum(float(b.amount) for b in self.bills_of(contract)
                    if b.type == "rent")
        self.assertEqual(total, 2800.0 * 7)

    def test_deposit_due_on_start(self):
        contract = self.sign()
        leasing.activate_contract(self.session, contract)
        self.session.commit()
        deposit = [b for b in self.bills_of(contract) if b.type == "deposit"][0]
        self.assertEqual(deposit.due_date, contract.start_date)


class TestPayment(LeasingCase):

    def first_bill(self, contract):
        return (self.session.query(Bill)
                .filter_by(contract_id=contract.id).order_by(Bill.id).first())

    def test_pay(self):
        contract = self.sign()
        leasing.activate_contract(self.session, contract)
        self.session.commit()

        bill = self.first_bill(contract)
        payment = leasing.pay_bill(self.session, bill, self.tenant)
        self.session.commit()

        self.assertEqual(bill.status, "paid")
        self.assertIsNotNone(bill.paid_at)
        self.assertEqual(float(payment.amount), float(bill.amount))
        self.assertTrue(payment.trade_no.startswith("LS"))

    def test_double_pay_rejected(self):
        """重复缴费必须拒绝，否则会产生两条流水。"""
        contract = self.sign()
        leasing.activate_contract(self.session, contract)
        self.session.commit()

        bill = self.first_bill(contract)
        leasing.pay_bill(self.session, bill, self.tenant)
        self.session.commit()
        with self.assertRaises(ValueError):
            leasing.pay_bill(self.session, bill, self.tenant)

    def test_overdue_flag(self):
        contract = self.sign()
        leasing.activate_contract(self.session, contract)
        bill = self.first_bill(contract)
        bill.due_date = datetime.now() - timedelta(days=3)
        self.session.commit()
        self.assertTrue(bill.overdue)

        leasing.pay_bill(self.session, bill, self.tenant)
        self.session.commit()
        self.assertFalse(bill.overdue, "已缴的账单不该再算逾期")


class TestEndContract(LeasingCase):

    def test_releases_house(self):
        contract = self.sign()
        leasing.activate_contract(self.session, contract)
        self.session.commit()

        leasing.end_contract(self.session, contract, reason="到期")
        self.session.commit()

        self.assertEqual(contract.status, "ended")
        self.assertEqual(self.house.status, "available")

    def test_cancels_future_rent_bills(self):
        """人搬走了还挂着未来的待缴账单是不对的。"""
        contract = self.sign(months=12)
        leasing.activate_contract(self.session, contract)
        self.session.commit()

        result = leasing.end_contract(self.session, contract, reason="提前退租")
        self.session.commit()

        self.assertGreater(result["cancelled_bills"], 0)
        future_unpaid = [b for b in contract.bills
                         if b.status == "unpaid" and b.type == "rent"
                         and b.due_date > datetime.now()]
        self.assertEqual(future_unpaid, [])

    def test_paid_bills_are_untouched(self):
        """已缴的账单不能被退租作废掉。"""
        contract = self.sign()
        leasing.activate_contract(self.session, contract)
        self.session.commit()

        bill = sorted(contract.bills, key=lambda b: b.id)[0]
        leasing.pay_bill(self.session, bill, self.tenant)
        self.session.commit()

        leasing.end_contract(self.session, contract)
        self.session.commit()
        self.assertEqual(bill.status, "paid")

    def test_deposit_refund(self):
        contract = self.sign()
        leasing.activate_contract(self.session, contract)
        self.session.commit()

        result = leasing.end_contract(self.session, contract, deduct=500)
        self.session.commit()
        self.assertEqual(result["refund"], 2300.0)
        self.assertEqual(float(contract.deposit_refund), 2300.0)

    def test_deduct_cannot_exceed_deposit(self):
        contract = self.sign()
        leasing.activate_contract(self.session, contract)
        self.session.commit()
        with self.assertRaises(ValueError):
            leasing.end_contract(self.session, contract, deduct=99999)

    def test_negative_deduct_rejected(self):
        contract = self.sign()
        leasing.activate_contract(self.session, contract)
        self.session.commit()
        with self.assertRaises(ValueError):
            leasing.end_contract(self.session, contract, deduct=-1)

    def test_end_pending_rejected(self):
        contract = self.sign()
        with self.assertRaises(ValueError):
            leasing.end_contract(self.session, contract)


class TestRenew(LeasingCase):

    def test_renew_creates_new_pending_contract(self):
        contract = self.sign(months=12)
        leasing.activate_contract(self.session, contract)
        self.session.commit()

        new_contract = leasing.renew_contract(self.session, contract, months=6)
        self.session.commit()

        self.assertEqual(contract.status, "ended")
        self.assertEqual(contract.end_reason, "续租")
        self.assertEqual(new_contract.status, "pending")
        self.assertEqual(new_contract.start_date, contract.end_date)
        self.assertNotEqual(new_contract.contract_no, contract.contract_no)

    def test_renew_requires_active(self):
        contract = self.sign()
        with self.assertRaises(ValueError):
            leasing.renew_contract(self.session, contract)


class TestDaysLeft(LeasingCase):

    def test_active_contract(self):
        contract = self.sign()
        leasing.activate_contract(self.session, contract)
        contract.end_date = datetime.now() + timedelta(days=30)
        self.session.commit()
        self.assertIn(contract.days_left, (29, 30))

    def test_ended_contract_is_zero(self):
        contract = self.sign()
        leasing.activate_contract(self.session, contract)
        leasing.end_contract(self.session, contract)
        self.session.commit()
        self.assertEqual(contract.days_left, 0)


if __name__ == "__main__":
    unittest.main()


class TestZeroDepositMakesNoBill(unittest.TestCase):
    """免押就不排押金账单。

    原先无条件排一条。合同选了「押零付一」时这条账单的金额是 0——
    它在库里是一条正常记录，可在用户眼里是一张「押金 0 元，待缴」的单子：
    个人中心给它配了「立即缴费」按钮，收银台写着「确认支付 ¥0.00」；
    智能助手说「帮我交房租」时它还因为到期日最早被排在第一位，
    助手于是问「要我帮你缴纳押金 0 元吗」。

    **一笔不用付的钱不该长成一张待缴账单。**
    """

    def test_no_deposit_bill_when_deposit_is_zero(self):
        from decimal import Decimal

        class _Fake(object):
            id = 1
            deposit = Decimal("0")
            rent = Decimal("2000")
            pay_months = 1
            start_date = datetime(2026, 10, 1)
            end_date = datetime(2027, 10, 1)

        made = []

        class _Session(object):
            def add_all(self, rows):
                made.extend(rows)

            def add(self, row):
                made.append(row)

            def flush(self):
                pass

        bills = leasing.generate_bills(_Session(), _Fake())
        kinds = [b.type for b in bills]
        self.assertNotIn("deposit", kinds)
        self.assertTrue(all(float(b.amount) > 0 for b in bills))

    def test_deposit_bill_still_made_when_there_is_one(self):
        from decimal import Decimal

        class _Fake(object):
            id = 1
            deposit = Decimal("2000")
            rent = Decimal("2000")
            pay_months = 1
            start_date = datetime(2026, 10, 1)
            end_date = datetime(2027, 10, 1)

        class _Session(object):
            def add_all(self, rows):
                pass

            def add(self, row):
                pass

            def flush(self):
                pass

        bills = leasing.generate_bills(_Session(), _Fake())
        self.assertIn("deposit", [b.type for b in bills])
