"""管理视图测试：工单归属、租客档案、订单总览、公告上下架。

重点是工单归属——修复前任何房东都能改别人房源工单的状态，
甚至把没修的标成已完成。
"""

import unittest
from datetime import datetime, timedelta

from tests.base import ApiTestCase, auth_header, make_resident, settle


def future(days=7):
    return (datetime.now() + timedelta(days=days)).replace(
        hour=0, minute=0, second=0, microsecond=0)


class ManageCase(ApiTestCase):

    def setUp(self):
        super().setUp()
        self.landlord = auth_header(self.client, "landlord1", "test123")
        self.tenant = auth_header(self.client, "tenant1", "test123")

        # 另一个房东，用来验证跨房东越权
        self.client.post("/api/auth/register", json={
            "username": "landlord2", "password": "test123",
            "real_name": "房东乙", "role": "landlord"})
        self.other = auth_header(self.client, "landlord2", "test123")

        self.client.post("/api/auth/register", json={
            "username": "root", "password": "admin123", "real_name": "管理员"})
        self._promote("root")
        self.admin = auth_header(self.client, "root", "admin123")

    def _promote(self, username):
        import app.db as db
        from app.models import User
        session = db.SessionLocal()
        try:
            user = session.query(User).filter_by(username=username).one()
            user.role = "admin"
            session.commit()
        finally:
            session.close()

    def make_repair(self, house_no="DL0001", content="卫生间漏水"):
        make_resident("tenant1", house_no)   # 报修只收在住租客
        return self.client.post("/api/repairs", headers=self.tenant, json={
            "house_no": house_no, "content": content,
            "category": "水暖", "urgency": "紧急"}).json()["repair"]["id"]

    def make_contract(self, house_no="DL0001"):
        no = self.client.post("/api/contracts", headers=self.landlord, json={
            "house_no": house_no, "tenant_username": "tenant1",
            "start_date": future().isoformat()}).json()["contract"]["contract_no"]
        self.client.post("/api/contracts/%s/activate" % no, headers=self.tenant)
        return no


class TestRepairOwnership(ManageCase):

    def test_owner_can_process(self):
        repair_id = self.make_repair()
        response = self.client.patch("/api/repairs/%d" % repair_id,
                                     headers=self.landlord,
                                     json={"status": "processing"})
        self.assertEqual(response.status_code, 200)

    def test_other_landlord_cannot_process(self):
        """跨房东改工单必须拒绝。修复前这里是 200。"""
        repair_id = self.make_repair()
        response = self.client.patch("/api/repairs/%d" % repair_id,
                                     headers=self.other, json={"status": "done"})
        self.assertEqual(response.status_code, 403)

    def test_admin_can_process_any(self):
        repair_id = self.make_repair()
        response = self.client.patch("/api/repairs/%d" % repair_id,
                                     headers=self.admin, json={"status": "done"})
        self.assertEqual(response.status_code, 200)

    def test_tenant_notified_when_done(self):
        repair_id = self.make_repair()
        self.client.patch("/api/repairs/%d" % repair_id, headers=self.landlord,
                          json={"status": "done"})
        messages = self.client.get("/api/messages", headers=self.tenant).json()
        self.assertIn("报修已处理完成", [m["title"] for m in messages["list"]])


class TestRepairVisibility(ManageCase):

    def test_anonymous_cannot_list_all(self):
        self.make_repair()
        self.assertEqual(self.client.get("/api/repairs").status_code, 401)

    def test_house_scoped_is_public(self):
        """房源详情页要展示该房源维修历史，这个是对租客透明的。"""
        self.make_repair()
        data = self.client.get("/api/repairs",
                               params={"house_no": "DL0001"}).json()
        self.assertEqual(data["total"], 1)

    def test_landlord_sees_only_own(self):
        self.make_repair()
        data = self.client.get("/api/repairs", headers=self.other).json()
        self.assertEqual(data["total"], 0, "别的房东不该看到这条工单")

    def test_tenant_sees_only_own_submissions(self):
        self.make_repair()
        data = self.client.get("/api/repairs", headers=self.tenant).json()
        self.assertEqual(data["total"], 1)

    def test_admin_sees_all(self):
        self.make_repair()
        data = self.client.get("/api/repairs", headers=self.admin).json()
        self.assertEqual(data["total"], 1)


class TestLandlordTenants(ManageCase):

    def test_lists_signed_tenants(self):
        self.make_contract()
        data = self.client.get("/api/landlord/tenants",
                               headers=self.landlord).json()
        self.assertEqual(data["total"], 1)
        row = data["list"][0]
        self.assertEqual(row["username"], "tenant1")
        self.assertEqual(row["active_count"], 1)
        self.assertIn("DL0001", row["houses"])
        self.assertGreater(row["unpaid_total"], 0)

    def test_overdue_total_counts_only_overdue_bills(self):
        """unpaid_total 含还没到期的账单；拖欠了多少要看 overdue_total。

        只给前者时，助手对房东说「林小雨一共欠 39650 元」，实际逾期只有 2 笔。
        """
        self.make_contract()
        import app.db as db
        from app.models import Bill
        session = db.SessionLocal()
        try:
            first = session.query(Bill).order_by(Bill.due_date).first()
            first.due_date = datetime.now() - timedelta(days=3)
            amount = float(first.amount)
            session.commit()
        finally:
            session.close()
        row = self.client.get("/api/landlord/tenants",
                              headers=self.landlord).json()["list"][0]
        self.assertEqual(row["overdue_count"], 1)
        self.assertEqual(row["overdue_total"], amount)
        self.assertGreater(row["unpaid_total"], row["overdue_total"])

    def test_phone_masked(self):
        self.make_contract()
        response = self.client.get("/api/landlord/tenants", headers=self.landlord)
        self.assertNotIn("13900005678", response.text)

    def test_paid_total_grows(self):
        self.make_contract()
        bills = self.client.get("/api/bills/mine", headers=self.tenant).json()["list"]
        # 已收金额只认确认到账的钱，所以要走完回调那一段
        settle(self.client, bills[0]["bill_no"], self.tenant)
        data = self.client.get("/api/landlord/tenants",
                               headers=self.landlord).json()
        self.assertGreater(data["list"][0]["paid_total"], 0)

    def test_other_landlord_sees_nothing(self):
        self.make_contract()
        data = self.client.get("/api/landlord/tenants", headers=self.other).json()
        self.assertEqual(data["total"], 0)

    def test_tenant_cannot_access(self):
        response = self.client.get("/api/landlord/tenants", headers=self.tenant)
        self.assertEqual(response.status_code, 403)


class TestRemindTenant(ManageCase):
    """催缴提醒：只催名下真正逾期的，一天一次。"""

    def overdue_contract(self):
        self.make_contract()
        import app.db as db
        from app.models import Bill
        session = db.SessionLocal()
        try:
            bill = session.query(Bill).order_by(Bill.due_date).first()
            bill.due_date = (datetime.now() - timedelta(days=5)).date()
            session.commit()
        finally:
            session.close()

    def tenant_id(self):
        rows = self.client.get("/api/landlord/tenants",
                               headers=self.landlord).json()["list"]
        return rows[0]["id"]

    def remind(self, headers, tenant_id=None):
        return self.client.post("/api/landlord/tenants/%s/remind"
                                % (tenant_id or self.tenant_id()), headers=headers)

    def test_tenant_receives_the_reminder_once_a_day(self):
        self.overdue_contract()
        first = self.remind(self.landlord)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["count"], 1)
        titles = [m["title"] for m in self.client.get(
            "/api/messages", headers=self.tenant).json()["list"]]
        self.assertIn("房东提醒你缴纳逾期账单", titles)
        self.assertEqual(self.remind(self.landlord).status_code, 429,
                         "同一天再催一次不该再发")

    def test_nothing_overdue_is_not_sent(self):
        self.make_contract()
        self.assertEqual(self.remind(self.landlord).status_code, 400)

    def test_other_landlord_cannot_remind(self):
        self.overdue_contract()
        tenant_id = self.tenant_id()
        self.assertEqual(self.remind(self.other, tenant_id).status_code, 400,
                         "别人合同上的账单不能拿来催")

    def test_tenant_cannot_call_it(self):
        self.overdue_contract()
        tenant_id = self.tenant_id()
        self.assertEqual(self.remind(self.tenant, tenant_id).status_code, 403)


class TestAdminOverview(ManageCase):

    def test_contracts(self):
        self.make_contract()
        data = self.client.get("/api/admin/contracts", headers=self.admin).json()
        self.assertEqual(data["total"], 1)

    def test_contracts_filter_by_status(self):
        self.make_contract()
        data = self.client.get("/api/admin/contracts", params={"status": "active"},
                               headers=self.admin).json()
        self.assertEqual(data["total"], 1)
        empty = self.client.get("/api/admin/contracts", params={"status": "ended"},
                                headers=self.admin).json()
        self.assertEqual(empty["total"], 0)

    def test_appointments(self):
        self.client.post("/api/appointments", headers=self.tenant, json={
            "house_no": "DL0001", "viewing_time": future().isoformat()})
        data = self.client.get("/api/admin/appointments", headers=self.admin).json()
        self.assertEqual(data["total"], 1)

    def test_landlord_cannot_access(self):
        for path in ("/api/admin/contracts", "/api/admin/appointments"):
            response = self.client.get(path, headers=self.landlord)
            self.assertEqual(response.status_code, 403, path)


class TestNoticeManage(ManageCase):

    def make_notice(self):
        return self.client.post("/api/notices", headers=self.admin, json={
            "title": "测试公告", "content": "公告正文"}).json()["notice"]["id"]

    def test_publish_and_unpublish(self):
        notice_id = self.make_notice()
        self.assertEqual(self.client.get("/api/notices").json()["total"], 1)

        self.client.patch("/api/notices/%d" % notice_id,
                          params={"published": False}, headers=self.admin)
        self.assertEqual(self.client.get("/api/notices").json()["total"], 0,
                         "下架后前台不该再展示")

        admin_view = self.client.get("/api/admin/notices", headers=self.admin).json()
        self.assertEqual(admin_view["total"], 1, "管理员仍能看到已下架的")
        self.assertFalse(admin_view["list"][0]["is_published"])

    def test_landlord_cannot_toggle(self):
        notice_id = self.make_notice()
        response = self.client.patch("/api/notices/%d" % notice_id,
                                     params={"published": False},
                                     headers=self.landlord)
        self.assertEqual(response.status_code, 403)

    def test_unknown_notice(self):
        response = self.client.patch("/api/notices/9999",
                                     params={"published": False},
                                     headers=self.admin)
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
