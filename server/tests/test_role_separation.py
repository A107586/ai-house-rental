"""身份分清：谁能以租客身份对一套房做事（tenant_side_block）。

实测房东给自己的房源约了三次看房（一次还确认了）、跟自己签了一份合同；
中介、管理员也都能以租客身份预约、咨询、收藏。2026-10-22 定下的规则：

* 管理员只做平台管理，中介只做经纪业务——都不以租客身份做任何事；
* 房东账号可以租别人的房（单账号双身份），但不能对自己的房这么做。
"""

from datetime import datetime, timedelta

from tests.base import ApiTestCase, auth_header, make_admin


def slot(days=5):
    return (datetime.now() + timedelta(days=days)).replace(
        hour=10, minute=0, second=0, microsecond=0).isoformat()


class RoleCase(ApiTestCase):

    def setUp(self):
        super().setUp()
        self.landlord = auth_header(self.client, "landlord1", "test123")
        self.tenant = auth_header(self.client, "tenant1", "test123")
        self.admin = make_admin(self.client)
        self.client.post("/api/auth/register", json={
            "username": "landlord2", "password": "test123", "real_name": "赵房东",
            "phone": "13800000002", "role": "landlord"})
        self.landlord2 = auth_header(self.client, "landlord2", "test123")
        self.client.post("/api/auth/register", json={
            "username": "agent9", "password": "test123", "real_name": "中介丙",
            "phone": "13700000009", "role": "agent"})
        self.agent = auth_header(self.client, "agent9", "test123")

    def book(self, headers, house_no="DL0001"):
        return self.client.post("/api/appointments", headers=headers, json={
            "house_no": house_no, "viewing_time": slot(), "contact_name": "某人",
            "contact_phone": "13800000099"})


class TestBooking(RoleCase):

    def test_landlord_cannot_book_own_house(self):
        r = self.book(self.landlord)
        self.assertEqual(r.status_code, 400)
        self.assertIn("自己的房源", r.json()["detail"])

    def test_agent_and_admin_cannot_book(self):
        self.assertIn("中介", self.book(self.agent).json()["detail"])
        self.assertIn("管理员", self.book(self.admin).json()["detail"])
        self.assertEqual(self.book(self.agent).status_code, 400)
        self.assertEqual(self.book(self.admin).status_code, 400)

    def test_landlord_can_still_rent_someone_elses_house(self):
        # 单账号双身份：landlord2 去约 landlord1 的房，照常可以
        self.assertEqual(self.book(self.landlord2).status_code, 200)
        self.assertEqual(self.book(self.tenant, "DL0002").status_code, 200)


class TestConsultAndFavorite(RoleCase):

    def test_consult(self):
        for who in (self.landlord, self.agent, self.admin):
            r = self.client.post("/api/conversations", headers=who, json={"house_no": "DL0001"})
            self.assertEqual(r.status_code, 400, r.text)
        ok = self.client.post("/api/conversations", headers=self.tenant, json={"house_no": "DL0001"})
        self.assertEqual(ok.status_code, 200)

    def test_favorite(self):
        for who in (self.landlord, self.agent, self.admin):
            self.assertEqual(self.client.post("/api/favorites/DL0001", headers=who).status_code, 403)
        self.assertEqual(self.client.post("/api/favorites/DL0001", headers=self.tenant).status_code, 200)


class TestSigning(RoleCase):

    def sign(self, tenant_username):
        return self.client.post("/api/contracts", headers=self.landlord, json={
            "house_no": "DL0001", "tenant_username": tenant_username,
            "start_date": slot(3), "months": 12})

    def test_cannot_sign_with_self_agent_or_admin(self):
        own = self.sign("landlord1")
        self.assertEqual(own.status_code, 409)
        self.assertIn("自己", own.json()["detail"])
        for name in ("agent9", "root"):
            r = self.sign(name)
            self.assertEqual(r.status_code, 409, name)
            self.assertIn("乙方不能是", r.json()["detail"])
        self.assertEqual(self.sign("tenant1").status_code, 200)


class TestHouseDetailFlags(RoleCase):

    def test_detail_tells_the_page_what_to_hide(self):
        own = self.client.get("/api/houses/DL0001", headers=self.landlord).json()
        self.assertTrue(own["can_manage"])
        self.assertIn("自己的房源", own["tenant_block"])
        other = self.client.get("/api/houses/DL0001", headers=self.tenant).json()
        self.assertFalse(other["can_manage"])
        self.assertEqual(other["tenant_block"], "")
        # 中介看一套不归他管的房：管不了，也不能以租客身份做事——页面靠 tenant_block 收起按钮
        agent = self.client.get("/api/houses/DL0001", headers=self.agent).json()
        self.assertFalse(agent["can_manage"])
        self.assertIn("中介", agent["tenant_block"])
        admin = self.client.get("/api/houses/DL0001", headers=self.admin).json()
        self.assertIn("管理员", admin["tenant_block"])
        anon = self.client.get("/api/houses/DL0001").json()
        self.assertEqual((anon["can_manage"], anon["tenant_block"]), (False, ""))
