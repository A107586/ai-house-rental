"""房东端与管理员端接口测试：工作台、房源编辑、审核、用户管理。

重点在两处容易出事的地方：
1. 房源状态的可见性——待审与已下架不能被匿名用户查到
2. 管理员的自我保护——不能把自己禁用或降级，否则后台直接锁死
"""

import unittest
from datetime import datetime, timedelta

from tests.base import ApiTestCase, auth_header, settle


def future(days=7):
    return (datetime.now() + timedelta(days=days)).replace(
        hour=0, minute=0, second=0, microsecond=0)


class AdminCase(ApiTestCase):

    def setUp(self):
        super().setUp()
        self.landlord = auth_header(self.client, "landlord1", "test123")
        self.tenant = auth_header(self.client, "tenant1", "test123")
        # base 里没有管理员，这里补一个并提权
        self.client.post("/api/auth/register", json={
            "username": "root", "password": "admin123", "real_name": "管理员"})
        self._promote("root")
        self.admin = auth_header(self.client, "root", "admin123")

    def _promote(self, username):
        """直接改库提权——注册接口不允许自封管理员，这是刻意的。"""
        import app.db as db
        from app.models import User
        session = db.SessionLocal()
        try:
            user = session.query(User).filter_by(username=username).one()
            user.role = "admin"
            session.commit()
        finally:
            session.close()

    def publish(self):
        return self.client.post("/api/houses", headers=self.landlord, json={
            "title": "新发布房源", "district": "中山区", "rent_price": 3000,
            "area": 80, "layout": "两室一厅"}).json()["house"]["house_no"]


class TestHouseVisibility(AdminCase):
    """待审核与已下架的房源不能对外可见。"""

    def test_anonymous_cannot_query_pending(self):
        """房东一发布就能被匿名用户查到的话，审核机制形同虚设。"""
        self.publish()
        data = self.client.get("/api/houses", params={"status": "pending"}).json()
        self.assertEqual(data["total"], 2, "应回落到 available，而不是列出待审房源")
        self.assertTrue(all(h["status"] == "available" for h in data["list"]))

    def test_tenant_cannot_query_offline(self):
        data = self.client.get("/api/houses", params={"status": "offline"},
                               headers=self.tenant).json()
        self.assertTrue(all(h["status"] == "available" for h in data["list"]))

    def test_rented_is_public(self):
        """已租是公开信息，用于展示出租率，不需要拦。"""
        data = self.client.get("/api/houses", params={"status": "rented"}).json()
        self.assertEqual(data["total"], 1)

    def test_admin_can_query_pending(self):
        self.publish()
        data = self.client.get("/api/admin/houses", params={"status": "pending"},
                               headers=self.admin).json()
        self.assertEqual(data["total"], 1)

    def test_admin_houses_requires_admin(self):
        response = self.client.get("/api/admin/houses", headers=self.landlord)
        self.assertEqual(response.status_code, 403)


class TestHouseReview(AdminCase):

    def test_approve_puts_house_online(self):
        house_no = self.publish()
        response = self.client.patch("/api/houses/%s/review" % house_no,
                                     headers=self.admin, json={"approved": True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["house"]["status"], "available")

        listed = self.client.get("/api/houses").json()
        self.assertIn(house_no, [h["house_no"] for h in listed["list"]])

    def test_reject_takes_house_offline(self):
        house_no = self.publish()
        response = self.client.patch("/api/houses/%s/review" % house_no,
                                     headers=self.admin,
                                     json={"approved": False, "reason": "描述不实"})
        self.assertEqual(response.json()["house"]["status"], "offline")

    def test_landlord_gets_notified(self):
        house_no = self.publish()
        self.client.patch("/api/houses/%s/review" % house_no,
                          headers=self.admin, json={"approved": True})
        messages = self.client.get("/api/messages", headers=self.landlord).json()
        titles = [m["title"] for m in messages["list"]]
        self.assertIn("房源审核通过", titles)

    def test_landlord_cannot_review(self):
        """房东不能自己审自己的房源。"""
        house_no = self.publish()
        response = self.client.patch("/api/houses/%s/review" % house_no,
                                     headers=self.landlord, json={"approved": True})
        self.assertEqual(response.status_code, 403)

    def test_review_available_house_rejected(self):
        response = self.client.patch("/api/houses/DL0001/review",
                                     headers=self.admin, json={"approved": True})
        self.assertEqual(response.status_code, 409)

    def test_unknown_house(self):
        response = self.client.patch("/api/houses/NOPE/review",
                                     headers=self.admin, json={"approved": True})
        self.assertEqual(response.status_code, 404)


class TestHouseUpdate(AdminCase):

    def test_landlord_edits_own(self):
        response = self.client.put("/api/houses/DL0001", headers=self.landlord,
                                   json={"rent_price": 3200, "title": "改了标题"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["house"]["rent_price"], 3200.0)

    def test_edit_sends_back_for_review(self):
        """改完关键信息要重新审核，防止先发合规内容、过审后再改。"""
        self.client.put("/api/houses/DL0001", headers=self.landlord,
                        json={"description": "改了描述"})
        house = self.client.get("/api/admin/houses", headers=self.admin).json()
        target = [h for h in house["list"] if h["house_no"] == "DL0001"][0]
        self.assertEqual(target["status"], "pending")

    def test_admin_edit_does_not_need_review(self):
        self.client.put("/api/houses/DL0001", headers=self.admin,
                        json={"description": "管理员改的"})
        house = self.client.get("/api/houses/DL0001").json()
        self.assertEqual(house["status"], "available")

    def test_tags_replaced(self):
        response = self.client.put("/api/houses/DL0001", headers=self.landlord,
                                   json={"tags": ["电梯", "近医院"]})
        self.assertEqual(response.json()["house"]["tags"], ["电梯", "近医院"])

    def test_tenant_cannot_edit(self):
        response = self.client.put("/api/houses/DL0001", headers=self.tenant,
                                   json={"title": "乱改"})
        self.assertEqual(response.status_code, 403)

    def test_cannot_edit_others_house(self):
        self.client.post("/api/auth/register", json={
            "username": "landlord2", "password": "test123", "role": "landlord"})
        other = auth_header(self.client, "landlord2", "test123")
        response = self.client.put("/api/houses/DL0001", headers=other,
                                   json={"title": "抢别人的"})
        self.assertEqual(response.status_code, 403)

    def test_invalid_price(self):
        response = self.client.put("/api/houses/DL0001", headers=self.landlord,
                                   json={"rent_price": -1})
        self.assertEqual(response.status_code, 422)


class TestLandlordDashboard(AdminCase):

    def test_counts(self):
        data = self.client.get("/api/landlord/dashboard",
                               headers=self.landlord).json()
        self.assertEqual(data["house_total"], 3)
        self.assertEqual(data["house_rented"], 1)
        self.assertEqual(data["house_vacant"], 2)
        self.assertGreater(data["occupancy_rate"], 0)

    def test_todo_counts_pending_appointments(self):
        self.client.post("/api/appointments", headers=self.tenant, json={
            "house_no": "DL0001", "viewing_time": future().isoformat()})
        data = self.client.get("/api/landlord/dashboard",
                               headers=self.landlord).json()
        self.assertEqual(data["todo"]["appointments"], 1)
        self.assertGreaterEqual(data["todo"]["total"], 1)

    def test_todo_counts_open_repairs(self):
        self.client.post("/api/repairs", json={
            "house_no": "DL0001", "content": "漏水", "urgency": "紧急"})
        data = self.client.get("/api/landlord/dashboard",
                               headers=self.landlord).json()
        self.assertEqual(data["todo"]["repairs"], 1)
        self.assertEqual(data["todo"]["urgent_repairs"], 1)

    def test_income_after_payment(self):
        contract_no = self.client.post("/api/contracts", headers=self.landlord, json={
            "house_no": "DL0001", "tenant_username": "tenant1",
            "start_date": future().isoformat()}).json()["contract"]["contract_no"]
        self.client.post("/api/contracts/%s/activate" % contract_no,
                         headers=self.tenant)
        bills = self.client.get("/api/bills/mine", headers=self.tenant).json()["list"]
        # 本月收入只认确认到账的钱，所以要走完回调那一段
        settle(self.client, bills[0]["bill_no"], self.tenant)

        data = self.client.get("/api/landlord/dashboard",
                               headers=self.landlord).json()
        self.assertGreater(data["month_income"], 0)
        self.assertEqual(data["contract_active"], 1)

    def test_tenant_cannot_access(self):
        response = self.client.get("/api/landlord/dashboard", headers=self.tenant)
        self.assertEqual(response.status_code, 403)

    def test_landlord_houses_scoped(self):
        data = self.client.get("/api/landlord/houses", headers=self.landlord).json()
        self.assertEqual(data["total"], 3)


class TestUserManagement(AdminCase):

    def _user_id(self, username):
        data = self.client.get("/api/admin/users", params={"keyword": username},
                               headers=self.admin).json()
        return data["list"][0]["id"]

    def test_list(self):
        data = self.client.get("/api/admin/users", headers=self.admin).json()
        self.assertGreaterEqual(data["total"], 3)

    def test_search_by_keyword(self):
        data = self.client.get("/api/admin/users", params={"keyword": "tenant"},
                               headers=self.admin).json()
        self.assertEqual(data["total"], 1)

    def test_filter_by_role(self):
        data = self.client.get("/api/admin/users", params={"role": "landlord"},
                               headers=self.admin).json()
        self.assertEqual(data["total"], 1)

    def test_list_never_leaks_password(self):
        response = self.client.get("/api/admin/users", headers=self.admin)
        self.assertNotIn("password", response.text)

    def test_disable_user(self):
        user_id = self._user_id("tenant1")
        response = self.client.patch("/api/admin/users/%d" % user_id,
                                     headers=self.admin, json={"status": "disabled"})
        self.assertEqual(response.status_code, 200)

        login = self.client.post("/api/auth/login",
                                 json={"username": "tenant1", "password": "test123"})
        self.assertEqual(login.status_code, 403, "被禁用的账号不能再登录")

    def test_change_role(self):
        user_id = self._user_id("tenant1")
        response = self.client.patch("/api/admin/users/%d" % user_id,
                                     headers=self.admin, json={"role": "landlord"})
        self.assertEqual(response.json()["user"]["role"], "landlord")

    def test_cannot_disable_self(self):
        """管理员把自己禁用会直接锁死后台，必须挡住。"""
        me = self.client.get("/api/auth/me", headers=self.admin).json()
        response = self.client.patch("/api/admin/users/%d" % me["id"],
                                     headers=self.admin, json={"status": "disabled"})
        self.assertEqual(response.status_code, 409)

    def test_cannot_demote_self(self):
        me = self.client.get("/api/auth/me", headers=self.admin).json()
        response = self.client.patch("/api/admin/users/%d" % me["id"],
                                     headers=self.admin, json={"role": "tenant"})
        self.assertEqual(response.status_code, 409)

    def test_invalid_status(self):
        user_id = self._user_id("tenant1")
        response = self.client.patch("/api/admin/users/%d" % user_id,
                                     headers=self.admin, json={"status": "冻结"})
        self.assertEqual(response.status_code, 400)

    def test_invalid_role(self):
        user_id = self._user_id("tenant1")
        response = self.client.patch("/api/admin/users/%d" % user_id,
                                     headers=self.admin, json={"role": "superuser"})
        self.assertEqual(response.status_code, 400)

    def test_landlord_cannot_manage_users(self):
        response = self.client.get("/api/admin/users", headers=self.landlord)
        self.assertEqual(response.status_code, 403)

    def test_unknown_user(self):
        response = self.client.patch("/api/admin/users/99999",
                                     headers=self.admin, json={"status": "disabled"})
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
