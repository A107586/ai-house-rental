"""业务后端接口测试（第二批）：鉴权、收藏、预约流转、报修、发布、统计。"""

import unittest
from datetime import datetime, timedelta

from tests.base import ApiTestCase, auth_header, make_admin


class TestToken(ApiTestCase):

    def test_login_returns_token(self):
        data = self.client.post("/api/auth/login",
                                json={"username": "tenant1", "password": "test123"}).json()
        self.assertIn("token", data)
        self.assertGreater(len(data["token"]), 20)

    def test_me_requires_token(self):
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)

    def test_me_with_token(self):
        data = self.client.get("/api/auth/me",
                               headers=auth_header(self.client)).json()
        self.assertEqual(data["username"], "tenant1")

    def test_tampered_token_rejected(self):
        """改动 payload 后签名对不上，必须拒绝。"""
        token = self.client.post(
            "/api/auth/login",
            json={"username": "tenant1", "password": "test123"}).json()["token"]
        payload, _, signature = token.rpartition(".")
        bad = payload[:-1] + ("A" if payload[-1] != "A" else "B") + "." + signature
        response = self.client.get("/api/auth/me",
                                   headers={"Authorization": "Bearer " + bad})
        self.assertEqual(response.status_code, 401)

    def test_garbage_token_rejected(self):
        response = self.client.get("/api/auth/me",
                                   headers={"Authorization": "Bearer not-a-token"})
        self.assertEqual(response.status_code, 401)


class TestRegister(ApiTestCase):

    def test_register(self):
        response = self.client.post("/api/auth/register", json={
            "username": "newbie", "password": "secret123", "real_name": "新用户"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("token", response.json())

    def test_duplicate_username(self):
        response = self.client.post("/api/auth/register",
                                    json={"username": "tenant1", "password": "secret123"})
        self.assertEqual(response.status_code, 409)

    def test_short_password_rejected(self):
        response = self.client.post("/api/auth/register",
                                    json={"username": "abcd", "password": "123"})
        self.assertEqual(response.status_code, 422)

    def test_invalid_phone_rejected_with_friendly_message(self):
        response = self.client.post("/api/auth/register", json={
            "username": "newbie", "password": "secret123", "phone": "hxbjsa"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"], "请输入正确的 11 位手机号")

    def test_invalid_username_rejected(self):
        response = self.client.post("/api/auth/register", json={
            "username": "坏!", "password": "secret123"})
        self.assertEqual(response.status_code, 422)
        self.assertIn("3–20", response.json()["detail"])

    def test_blank_fields_have_specific_messages(self):
        username = self.client.post("/api/auth/register", json={
            "username": "", "password": "secret123"})
        password = self.client.post("/api/auth/register", json={
            "username": "newbie", "password": ""})
        self.assertEqual(username.json()["detail"], "请输入用户名")
        self.assertEqual(password.json()["detail"], "请输入密码")


class TestLoginValidation(ApiTestCase):

    def test_wrong_credentials_have_friendly_message(self):
        response = self.client.post("/api/auth/login", json={
            "username": "tenant1", "password": "wrong"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "用户名或密码错误")

    def test_blank_username_and_password_are_distinguished(self):
        no_username = self.client.post("/api/auth/login", json={
            "username": "", "password": "x"})
        no_password = self.client.post("/api/auth/login", json={
            "username": "tenant1", "password": ""})
        self.assertEqual(no_username.json()["detail"], "请输入用户名")
        self.assertEqual(no_password.json()["detail"], "请输入密码")

    def test_cannot_self_register_as_admin(self):
        """不能通过注册接口把自己变成管理员。"""
        response = self.client.post("/api/auth/register", json={
            "username": "hacker", "password": "secret123", "role": "admin"})
        self.assertEqual(response.status_code, 400)


class TestFavorites(ApiTestCase):

    def test_requires_login(self):
        self.assertEqual(self.client.get("/api/favorites").status_code, 401)

    def test_add_list_remove(self):
        headers = auth_header(self.client)
        self.client.post("/api/favorites/DL0001", headers=headers)
        data = self.client.get("/api/favorites", headers=headers).json()
        self.assertEqual(data["total"], 1)

        self.client.delete("/api/favorites/DL0001", headers=headers)
        data = self.client.get("/api/favorites", headers=headers).json()
        self.assertEqual(data["total"], 0)

    def test_duplicate_is_not_an_error(self):
        headers = auth_header(self.client)
        self.client.post("/api/favorites/DL0001", headers=headers)
        second = self.client.post("/api/favorites/DL0001", headers=headers)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(
            self.client.get("/api/favorites", headers=headers).json()["total"], 1)

    def test_unknown_house(self):
        response = self.client.post("/api/favorites/NOPE",
                                    headers=auth_header(self.client))
        self.assertEqual(response.status_code, 404)


class TestAppointmentWorkflow(ApiTestCase):

    def _future(self, days=3, hour=14):
        moment = datetime.now() + timedelta(days=days)
        return moment.replace(hour=hour, minute=0, second=0, microsecond=0)

    def _make(self):
        headers = auth_header(self.client)
        response = self.client.post("/api/appointments", headers=headers, json={
            "house_no": "DL0001", "viewing_time": self._future().isoformat()})
        return response.json()["appointment"]["id"], headers

    def test_tenant_can_cancel_own(self):
        appointment_id, headers = self._make()
        response = self.client.patch("/api/appointments/%d" % appointment_id,
                                     headers=headers, json={"status": "cancelled"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["appointment"]["status"], "cancelled")

    def test_tenant_cannot_confirm(self):
        """确认预约是房东的权限，租客不能自己确认。"""
        appointment_id, headers = self._make()
        response = self.client.patch("/api/appointments/%d" % appointment_id,
                                     headers=headers, json={"status": "confirmed"})
        self.assertEqual(response.status_code, 403)

    def test_landlord_can_confirm(self):
        appointment_id, _ = self._make()
        headers = auth_header(self.client, "landlord1", "test123")
        response = self.client.patch("/api/appointments/%d" % appointment_id,
                                     headers=headers,
                                     json={"status": "confirmed", "reply": "欢迎"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["appointment"]["reply"], "欢迎")

    def test_cancelled_slot_is_reusable(self):
        """取消后该时段应释放出来，可以被再次预约。"""
        appointment_id, headers = self._make()
        self.client.patch("/api/appointments/%d" % appointment_id,
                          headers=headers, json={"status": "cancelled"})
        again = self.client.post("/api/appointments", headers=headers, json={
            "house_no": "DL0001", "viewing_time": self._future().isoformat()})
        self.assertEqual(again.status_code, 200)

    def test_invalid_status(self):
        appointment_id, headers = self._make()
        response = self.client.patch("/api/appointments/%d" % appointment_id,
                                     headers=headers, json={"status": "已完成"})
        self.assertEqual(response.status_code, 400)

    def test_mine_for_tenant(self):
        _, headers = self._make()
        data = self.client.get("/api/appointments/mine", headers=headers).json()
        self.assertEqual(data["total"], 1)


class TestRepairs(ApiTestCase):

    def test_create_and_list(self):
        response = self.client.post("/api/repairs", json={
            "house_no": "DL0001", "content": "卫生间水管漏水",
            "category": "水暖", "urgency": "紧急", "assignee": "水暖工"})
        self.assertEqual(response.status_code, 200)

        data = self.client.get("/api/repairs", params={"house_no": "DL0001"}).json()
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["list"][0]["urgency"], "紧急")

    def test_filter_by_urgency(self):
        for content, urgency in (("漏水", "紧急"), ("灯泡坏了", "可延后")):
            self.client.post("/api/repairs", json={
                "house_no": "DL0001", "content": content, "urgency": urgency})
        # 不带房源号会列出全平台工单，含联系人与故障描述，必须登录
        data = self.client.get("/api/repairs", params={"urgency": "紧急"},
                               headers=auth_header(self.client, "landlord1",
                                                   "test123")).json()
        self.assertEqual(data["total"], 1)

    def test_platform_wide_list_requires_login(self):
        """匿名不能拉到全平台工单——那会暴露所有租客的报修内容。"""
        response = self.client.get("/api/repairs")
        self.assertEqual(response.status_code, 401)

    def test_empty_content_rejected(self):
        response = self.client.post("/api/repairs",
                                    json={"house_no": "DL0001", "content": ""})
        self.assertEqual(response.status_code, 422)

    def test_unknown_house(self):
        response = self.client.post("/api/repairs",
                                    json={"house_no": "NOPE", "content": "漏水"})
        self.assertEqual(response.status_code, 404)

    def test_status_update_requires_staff(self):
        repair_id = self.client.post("/api/repairs", json={
            "house_no": "DL0001", "content": "漏水"}).json()["repair"]["id"]
        tenant = self.client.patch("/api/repairs/%d" % repair_id,
                                   headers=auth_header(self.client),
                                   json={"status": "done"})
        self.assertEqual(tenant.status_code, 403)

        landlord = self.client.patch("/api/repairs/%d" % repair_id,
                                     headers=auth_header(self.client, "landlord1", "test123"),
                                     json={"status": "done"})
        self.assertEqual(landlord.status_code, 200)

    def test_phone_masked_in_list(self):
        self.client.post("/api/repairs", json={
            "house_no": "DL0001", "content": "漏水",
            "contact_phone": "13800009999"})
        response = self.client.get("/api/repairs")
        self.assertNotIn("13800009999", response.text)


class TestHousePublish(ApiTestCase):

    PAYLOAD = {"title": "测试房源", "district": "中山区", "rent_price": 3000,
               "area": 80, "layout": "两室一厅"}

    def test_requires_staff(self):
        response = self.client.post("/api/houses", json=self.PAYLOAD,
                                    headers=auth_header(self.client))
        self.assertEqual(response.status_code, 403)

    def test_landlord_publish_goes_pending(self):
        """房东发布的房源要等审核，不能直接对外可见。"""
        response = self.client.post("/api/houses", json=self.PAYLOAD,
                                    headers=auth_header(self.client, "landlord1", "test123"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["house"]["status"], "pending")

        listed = self.client.get("/api/houses").json()
        self.assertNotIn("测试房源", [h["title"] for h in listed["list"]])

    def test_status_update_scoped_to_owner(self):
        """房东能上下架自己的房源，别的房东不能；**待审核的不能自己点上架**。

        原先这条直接把刚发布（pending）的房源改成 available 并断言 200——
        那正是 `approved_at` 要堵的绕过审核的路。现在先由管理员审过，
        再验证房东本人的上下架与越权。
        """
        headers = auth_header(self.client, "landlord1", "test123")
        house_no = self.client.post("/api/houses", json=self.PAYLOAD,
                                    headers=headers).json()["house"]["house_no"]
        url = "/api/houses/%s/status" % house_no

        pending = self.client.patch(url, params={"status": "available"},
                                    headers=headers)
        self.assertEqual(pending.status_code, 409)

        admin = make_admin(self.client)
        self.client.patch("/api/houses/%s/review" % house_no, headers=admin,
                          json={"approved": True})

        down = self.client.patch(url, params={"status": "offline"}, headers=headers)
        self.assertEqual(down.status_code, 200)
        self.assertEqual(down.json()["house"]["status"], "offline")
        # 审过的内容没变，重新上架不必再审
        up = self.client.patch(url, params={"status": "available"}, headers=headers)
        self.assertEqual(up.status_code, 200)
        self.assertEqual(up.json()["house"]["status"], "available")

        self.client.post("/api/auth/register", json={
            "username": "landlord2", "password": "test123", "role": "landlord"})
        other = auth_header(self.client, "landlord2", "test123")
        stolen = self.client.patch(url, params={"status": "offline"}, headers=other)
        self.assertEqual(stolen.status_code, 403)

    def test_invalid_price_rejected(self):
        payload = dict(self.PAYLOAD, rent_price=-100)
        response = self.client.post("/api/houses", json=payload,
                                    headers=auth_header(self.client, "landlord1", "test123"))
        self.assertEqual(response.status_code, 422)


class TestStats(ApiTestCase):

    def test_counts(self):
        data = self.client.get("/api/stats").json()
        self.assertEqual(data["house_total"], 3)
        self.assertEqual(data["house_available"], 2)
        self.assertEqual(data["house_rented"], 1)
        self.assertGreater(data["avg_rent"], 0)

    def test_by_district(self):
        data = self.client.get("/api/stats").json()
        districts = {row["district"] for row in data["by_district"]}
        self.assertIn("中山区", districts)


if __name__ == "__main__":
    unittest.main()
