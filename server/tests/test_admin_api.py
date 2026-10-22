"""房东端与管理员端接口测试：工作台、房源编辑、审核、用户管理。

重点在两处容易出事的地方：
1. 房源状态的可见性——待审与已下架不能被匿名用户查到
2. 管理员的自我保护——不能把自己禁用或降级，否则后台直接锁死
"""

import unittest
from datetime import datetime, timedelta

from tests.base import ApiTestCase, auth_header, make_resident, settle


def future(days=7):
    # 看房只能约 9:00–18:00
    return (datetime.now() + timedelta(days=days)).replace(
        hour=10, minute=0, second=0, microsecond=0)


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

    def test_detail_of_an_unlisted_house_is_not_public(self):
        """列表早就拦了，详情原先没拦：被复核下架的违规房源，知道编号照样打得开。"""
        house_no = self.publish()
        self.assertEqual(self.client.get("/api/houses/%s" % house_no).status_code, 404)
        self.assertEqual(self.client.get("/api/houses/%s" % house_no,
                                         headers=self.tenant).status_code, 404)
        mine = self.client.get("/api/houses/%s" % house_no, headers=self.landlord)
        self.assertEqual(mine.status_code, 200)
        self.assertTrue(mine.json()["can_manage"])
        self.assertTrue(mine.json()["is_owner"])
        self.assertEqual(self.client.get("/api/houses/%s" % house_no,
                                         headers=self.admin).status_code, 200)
        # 相册同一道门
        self.assertEqual(self.client.get("/api/houses/%s/images" % house_no).status_code, 404)
        self.assertEqual(self.client.get("/api/houses/%s/images" % house_no,
                                         headers=self.landlord).status_code, 200)

    def test_unlisted_house_cannot_be_favorited_by_number(self):
        """收藏列表返回整套房源资料，从这里收进来就绕过了详情那道门。"""
        house_no = self.publish()
        response = self.client.post("/api/favorites/%s" % house_no, headers=self.tenant)
        self.assertEqual(response.status_code, 404)

    def test_tenant_who_signed_can_still_open_it(self):
        """合同详情要链到这套房，下架了也得打得开。"""
        make_resident("tenant1", "DL0001")
        self.client.patch("/api/houses/DL0001/status?status=offline", headers=self.landlord)
        detail = self.client.get("/api/houses/DL0001", headers=self.tenant)
        self.assertEqual(detail.status_code, 200)
        self.assertFalse(detail.json()["can_manage"])

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

    def test_past_pending_viewing_is_not_a_todo(self):
        """过了点的待确认已经确认不了（后端会拒），算进待办就是一个永远消不掉的数。"""
        import app.db as db
        from app.models import Appointment
        booked = self.client.post("/api/appointments", headers=self.tenant, json={
            "house_no": "DL0001", "viewing_time": future().isoformat()})
        session = db.SessionLocal()
        try:
            session.get(Appointment, booked.json()["appointment"]["id"]).viewing_time = \
                datetime.now() - timedelta(hours=3)
            session.commit()
        finally:
            session.close()
        data = self.client.get("/api/landlord/dashboard", headers=self.landlord).json()
        self.assertEqual(data["todo"]["appointments"], 0)

    def test_todo_counts_open_repairs(self):
        make_resident("tenant1", "DL0001")   # 报修只收在住租客
        self.client.post("/api/repairs", headers=auth_header(self.client), json={
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

    def test_disabling_a_landlord_takes_his_listings_down(self):
        """原先只吊销令牌：房源照常对外展示、照常能约看，约的人等一个登不上来的房东。"""
        from datetime import datetime, timedelta
        moment = (datetime.now() + timedelta(days=3)).replace(hour=15, minute=0, second=0,
                                                               microsecond=0)
        booked = self.client.post("/api/appointments", headers=self.tenant, json={
            "house_no": "DL0001", "viewing_time": moment.isoformat(),
            "contact_phone": "13900005678"})
        self.assertEqual(booked.status_code, 200, booked.text)

        response = self.client.patch("/api/admin/users/%d" % self._user_id("landlord1"),
                                     headers=self.admin, json={"status": "disabled"})
        self.assertEqual(response.json()["houses_offlined"], 2)   # DL0001、DL0002；DL0003 已出租不动
        listed = self.client.get("/api/houses").json()["list"]
        self.assertNotIn("DL0001", [h["house_no"] for h in listed])
        mine = self.client.get("/api/appointments/mine", headers=self.tenant).json()["list"]
        self.assertEqual(mine[0]["status"], "cancelled")

    def test_disabling_a_tenant_cancels_his_viewings(self):
        """他约的看房原先照常挂在房东待办里，房东确认了、按点到楼下，等一个登不上来的人。"""
        moment = (datetime.now() + timedelta(days=3)).replace(hour=15, minute=0, second=0,
                                                               microsecond=0)
        booked = self.client.post("/api/appointments", headers=self.tenant, json={
            "house_no": "DL0001", "viewing_time": moment.isoformat(),
            "contact_phone": "13900005678"})
        self.assertEqual(booked.status_code, 200, booked.text)

        self.client.patch("/api/admin/users/%d" % self._user_id("tenant1"),
                          headers=self.admin, json={"status": "disabled"})
        rows = self.client.get("/api/appointments/mine?side=landlord",
                               headers=self.landlord).json()["list"]
        self.assertEqual(rows[0]["status"], "cancelled")
        self.assertIn("停用", rows[0]["reply"])

    def test_cannot_demote_a_landlord_with_live_business(self):
        """和房东自己点「退回租客」同一道前置。原先管理员这条路什么都不查，
        合同的甲方一下子成了没有房东权限的账号。"""
        response = self.client.patch("/api/admin/users/%d" % self._user_id("landlord1"),
                                     headers=self.admin, json={"role": "tenant"})
        self.assertEqual(response.status_code, 409)
        self.assertIn("仍在架", response.json()["detail"])
        self.assertEqual(self.client.get("/api/auth/me", headers=self.landlord)
                         .json()["role"], "landlord")

    def test_approve_cannot_relist_a_house_the_landlord_took_down(self):
        """审核只管待审和被驳回的；房东自己下架的点「通过」等于替他重新挂出去。"""
        import app.db as db
        from app.models import House
        session = db.SessionLocal()
        try:      # 装置里的房源没有审核时间，补上才是「审过、上过架」的那种
            session.query(House).filter_by(house_no="DL0001").one().approved_at = datetime.now()
            session.commit()
        finally:
            session.close()
        self.client.patch("/api/houses/DL0001/status?status=offline", headers=self.landlord)
        response = self.client.patch("/api/houses/DL0001/review", headers=self.admin,
                                     json={"approved": True})
        self.assertEqual(response.status_code, 409)

    def test_house_of_a_disabled_landlord_cannot_go_back_up(self):
        """上架了也签不了约——签约要房东本人点。管理员也不例外。"""
        self.client.patch("/api/admin/users/%d" % self._user_id("landlord1"),
                          headers=self.admin, json={"status": "disabled"})
        response = self.client.patch("/api/houses/DL0001/status?status=available",
                                     headers=self.admin)
        self.assertEqual(response.status_code, 409)
        self.assertIn("停用", response.json()["detail"])

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


class TestUserOverview(AdminCase):
    """用户管理、户主档案、中介档案点进去看一个人的全貌。"""

    def user_id(self, username):
        rows = self.client.get("/api/admin/users", headers=self.admin,
                               params={"keyword": username}).json()["list"]
        return next(r["id"] for r in rows if r["username"] == username)

    def test_landlord_section(self):
        body = self.client.get("/api/admin/users/%d/overview" % self.user_id("landlord1"),
                               headers=self.admin).json()
        self.assertEqual(body["user"]["username"], "landlord1")
        self.assertNotIn("password_hash", body["user"])
        self.assertGreater(body["as_landlord"]["house_count"], 0)
        self.assertNotIn("as_agent", body)

    def test_tenant_without_contracts_has_no_tenant_section(self):
        body = self.client.get("/api/admin/users/%d/overview" % self.user_id("tenant1"),
                               headers=self.admin).json()
        self.assertNotIn("as_tenant", body)            # 没签过约就不摆一块空的
        self.assertIn("recent_ops", body)

    def test_only_admin(self):
        response = self.client.get("/api/admin/users/%d/overview" % self.user_id("tenant1"),
                                   headers=self.landlord)
        self.assertEqual(response.status_code, 403)

    def test_missing_user(self):
        response = self.client.get("/api/admin/users/99999/overview", headers=self.admin)
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
