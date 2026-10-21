"""个人资料、改密码与房源相册测试。

改密码是最容易被做错的接口之一：不验证原密码就等于拿到令牌即可夺号。
"""

import unittest

from tests.base import ApiTestCase, auth_header


class TestProfile(ApiTestCase):

    def setUp(self):
        super().setUp()
        self.tenant = auth_header(self.client, "tenant1", "test123")

    def test_update_name_and_phone(self):
        response = self.client.put("/api/auth/profile", headers=self.tenant,
                                   json={"real_name": "新名字",
                                         "phone": "13700001111"})
        self.assertEqual(response.status_code, 200)
        user = response.json()["user"]
        self.assertEqual(user["real_name"], "新名字")
        self.assertEqual(user["phone"], "137****1111", "返回的手机号要脱敏")

    def test_partial_update_keeps_other_fields(self):
        """只传姓名时，手机号不能被清空。"""
        self.client.put("/api/auth/profile", headers=self.tenant,
                        json={"real_name": "只改名字"})
        user = self.client.get("/api/auth/me", headers=self.tenant).json()
        self.assertEqual(user["real_name"], "只改名字")
        self.assertEqual(user["phone"], "139****5678")

    def test_cannot_change_role_via_profile(self):
        """资料接口不接受 role 字段，避免绕过管理员提权。"""
        self.client.put("/api/auth/profile", headers=self.tenant,
                        json={"real_name": "新名字", "role": "admin"})
        user = self.client.get("/api/auth/me", headers=self.tenant).json()
        self.assertEqual(user["role"], "tenant")

    def test_requires_login(self):
        response = self.client.put("/api/auth/profile", json={"real_name": "新名字"})
        self.assertEqual(response.status_code, 401)

    def test_name_cannot_carry_contact_details(self):
        """姓名会写进合同、给房东看全名；聊天里留联系方式被拦，姓名不能成为绕过去的路。
        演示库里就有一个叫「微信abc888」的租客——原先接口与改资料都不查。"""
        for bad in ("微信abc888", "13900001234", "加V:abc", "张"):
            response = self.client.put("/api/auth/profile", headers=self.tenant,
                                       json={"real_name": bad})
            self.assertEqual(response.status_code, 422, bad)
        for good in ("欧阳娜娜", "Tom Lee", "阿依古丽·买买提"):
            response = self.client.put("/api/auth/profile", headers=self.tenant,
                                       json={"real_name": good})
            self.assertEqual(response.status_code, 200, good)

    def test_register_checks_the_name_too(self):
        response = self.client.post("/api/auth/register", json={
            "username": "newbie1", "password": "secret123", "real_name": "微信abc888"})
        self.assertEqual(response.status_code, 422)
        ok = self.client.post("/api/auth/register", json={
            "username": "newbie2", "password": "secret123"})
        self.assertEqual(ok.status_code, 200, "姓名可以先不填，签约前再补")


class TestPassword(ApiTestCase):

    def setUp(self):
        super().setUp()
        self.tenant = auth_header(self.client, "tenant1", "test123")

    def test_change_and_relogin(self):
        response = self.client.post("/api/auth/password", headers=self.tenant,
                                    json={"old_password": "test123",
                                          "new_password": "newpass123"})
        self.assertEqual(response.status_code, 200)

        old = self.client.post("/api/auth/login",
                               json={"username": "tenant1", "password": "test123"})
        self.assertEqual(old.status_code, 401, "旧密码必须失效")

        new = self.client.post("/api/auth/login",
                               json={"username": "tenant1", "password": "newpass123"})
        self.assertEqual(new.status_code, 200)

    def test_wrong_old_password_rejected(self):
        """不验原密码就等于拿到令牌即可夺号。"""
        response = self.client.post("/api/auth/password", headers=self.tenant,
                                    json={"old_password": "wrong",
                                          "new_password": "newpass123"})
        self.assertEqual(response.status_code, 403)

    def test_same_password_rejected(self):
        response = self.client.post("/api/auth/password", headers=self.tenant,
                                    json={"old_password": "test123",
                                          "new_password": "test123"})
        self.assertEqual(response.status_code, 400)

    def test_short_password_rejected(self):
        response = self.client.post("/api/auth/password", headers=self.tenant,
                                    json={"old_password": "test123",
                                          "new_password": "123"})
        self.assertEqual(response.status_code, 422)

    def test_requires_login(self):
        response = self.client.post("/api/auth/password",
                                    json={"old_password": "a", "new_password": "bbbbbb"})
        self.assertEqual(response.status_code, 401)


class TestHouseImages(ApiTestCase):

    def test_empty_album(self):
        data = self.client.get("/api/houses/DL0001/images").json()
        self.assertEqual(data["total"], 0)

    def test_unknown_house(self):
        response = self.client.get("/api/houses/NOPE/images")
        self.assertEqual(response.status_code, 404)

    def test_house_dict_has_images_field(self):
        """前端靠这个字段渲染相册，缺了会直接白屏。"""
        house = self.client.get("/api/houses/DL0001").json()
        self.assertIn("images", house)
        self.assertIsInstance(house["images"], list)


class TestHouseFilters(ApiTestCase):
    """传统筛选器用到的后端参数组合。"""

    def test_price_range(self):
        data = self.client.get("/api/houses", params={
            "rent_price_min": 2000, "rent_price_max": 3000}).json()
        self.assertEqual(data["total"], 1)

    def test_area_range(self):
        data = self.client.get("/api/houses", params={
            "area_min": 60, "area_max": 90}).json()
        self.assertEqual(data["total"], 1)

    def test_rent_type(self):
        data = self.client.get("/api/houses", params={"rent_type": "整租"}).json()
        self.assertEqual(data["total"], 2)

    def test_combined(self):
        data = self.client.get("/api/houses", params={
            "district": "中山区", "layout": "两室一厅",
            "rent_price_max": 3000, "rent_type": "整租"}).json()
        self.assertEqual(data["total"], 1)

    def test_sort_desc(self):
        data = self.client.get("/api/houses", params={"sort": "-rent_price"}).json()
        prices = [h["rent_price"] for h in data["list"]]
        self.assertEqual(prices, sorted(prices, reverse=True))

    def test_no_match_returns_empty(self):
        data = self.client.get("/api/houses", params={
            "district": "中山区", "rent_price_min": 99999}).json()
        self.assertEqual(data["total"], 0)


if __name__ == "__main__":
    unittest.main()
