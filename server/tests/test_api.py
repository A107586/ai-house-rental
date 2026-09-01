"""业务后端接口测试（第一批）：密码安全、登录、房源、预约冲突。"""

import unittest
from datetime import datetime, timedelta

from tests.base import ApiTestCase, auth_header  # noqa: F401
from app.security import hash_password, verify_password


class TestPasswordSecurity(unittest.TestCase):
    """R-01：密码必须加盐哈希存储。"""

    def test_hash_is_not_plaintext(self):
        hashed = hash_password("test123")
        self.assertNotIn("test123", hashed)

    def test_same_password_different_hash(self):
        """加了盐，同一密码两次哈希结果必须不同。"""
        self.assertNotEqual(hash_password("test123"), hash_password("test123"))

    def test_verify(self):
        hashed = hash_password("test123")
        self.assertTrue(verify_password("test123", hashed))
        self.assertFalse(verify_password("test124", hashed))
        self.assertFalse(verify_password("", hashed))


class TestAuth(ApiTestCase):

    def test_login_ok(self):
        response = self.client.post("/api/auth/login",
                                    json={"username": "tenant1", "password": "test123"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["user"]["username"], "tenant1")

    def test_login_wrong_password(self):
        response = self.client.post("/api/auth/login",
                                    json={"username": "tenant1", "password": "bad"})
        self.assertEqual(response.status_code, 401)

    def test_response_never_leaks_password_hash(self):
        """接口返回里绝不能出现密码哈希。"""
        response = self.client.post("/api/auth/login",
                                    json={"username": "tenant1", "password": "test123"})
        self.assertNotIn("password", response.text)

    def test_phone_is_masked(self):
        """R-02：手机号必须脱敏后返回。"""
        response = self.client.post("/api/auth/login",
                                    json={"username": "tenant1", "password": "test123"})
        phone = response.json()["user"]["phone"]
        self.assertEqual(phone, "139****5678")
        self.assertNotIn("13900005678", response.text)


class TestHouseList(ApiTestCase):

    def test_default_only_available(self):
        """默认只返回可租房源，已租的不该出现在搜索结果里。"""
        data = self.client.get("/api/houses").json()
        self.assertEqual(data["total"], 2)
        self.assertNotIn("DL0003", [h["house_no"] for h in data["list"]])

    def test_filter_by_district(self):
        data = self.client.get("/api/houses", params={"district": "中山区"}).json()
        self.assertEqual(data["total"], 1)

    def test_filter_by_price_range(self):
        data = self.client.get("/api/houses",
                               params={"rent_price_max": 3000}).json()
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["list"][0]["house_no"], "DL0001")

    def test_filter_by_area(self):
        data = self.client.get("/api/houses", params={"area_min": 90}).json()
        self.assertEqual(data["total"], 1)

    def test_combined_filters(self):
        data = self.client.get("/api/houses", params={
            "district": "中山区", "layout": "两室一厅", "rent_price_max": 3000}).json()
        self.assertEqual(data["total"], 1)

    def test_no_match_returns_empty_not_error(self):
        data = self.client.get("/api/houses",
                               params={"district": "旅顺口区"}).json()
        self.assertEqual(data["total"], 0)
        self.assertEqual(data["list"], [])

    def test_pagination(self):
        data = self.client.get("/api/houses", params={"limit": 1, "page": 1}).json()
        self.assertEqual(len(data["list"]), 1)
        self.assertEqual(data["total"], 2, "total 应是符合条件的总数，不是本页条数")

    def test_sort_by_price(self):
        data = self.client.get("/api/houses", params={"sort": "rent_price"}).json()
        prices = [h["rent_price"] for h in data["list"]]
        self.assertEqual(prices, sorted(prices))

    def test_tags_are_list(self):
        data = self.client.get("/api/houses", params={"district": "中山区"}).json()
        self.assertEqual(data["list"][0]["tags"], ["电梯", "地铁"])


class TestHouseDetail(ApiTestCase):

    def test_detail(self):
        data = self.client.get("/api/houses/DL0001").json()
        self.assertEqual(data["district"], "中山区")

    def test_view_count_increases(self):
        before = self.client.get("/api/houses/DL0001").json()["view_count"]
        after = self.client.get("/api/houses/DL0001").json()["view_count"]
        self.assertEqual(after, before + 1)

    def test_missing_returns_404(self):
        self.assertEqual(self.client.get("/api/houses/NOPE").status_code, 404)


class TestAppointments(ApiTestCase):

    def _future(self, days=3, hour=14):
        moment = datetime.now() + timedelta(days=days)
        return moment.replace(hour=hour, minute=0, second=0, microsecond=0)

    def test_create(self):
        response = self.client.post("/api/appointments", json={
            "house_no": "DL0001",
            "viewing_time": self._future().isoformat(),
            "contact_name": "张三", "contact_phone": "13800009999"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["appointment"]["status"], "pending")

    def test_rejects_conflict(self):
        """服务端必须独立做冲突校验，不能只依赖智能体那边的检查。"""
        moment = self._future().isoformat()
        self.client.post("/api/appointments",
                         json={"house_no": "DL0001", "viewing_time": moment})
        second = self.client.post("/api/appointments",
                                  json={"house_no": "DL0001", "viewing_time": moment})
        self.assertEqual(second.status_code, 409)

    def test_rejects_overlapping_within_an_hour(self):
        base = self._future(hour=14)
        self.client.post("/api/appointments", json={
            "house_no": "DL0001", "viewing_time": base.isoformat()})
        overlap = self.client.post("/api/appointments", json={
            "house_no": "DL0001",
            "viewing_time": (base + timedelta(minutes=30)).isoformat()})
        self.assertEqual(overlap.status_code, 409)

    def test_allows_adjacent_hour(self):
        base = self._future(hour=14)
        self.client.post("/api/appointments", json={
            "house_no": "DL0001", "viewing_time": base.isoformat()})
        adjacent = self.client.post("/api/appointments", json={
            "house_no": "DL0001",
            "viewing_time": (base + timedelta(hours=1)).isoformat()})
        self.assertEqual(adjacent.status_code, 200)

    def test_rejects_past_time(self):
        past = (datetime.now() - timedelta(days=1)).isoformat()
        response = self.client.post("/api/appointments",
                                    json={"house_no": "DL0001", "viewing_time": past})
        self.assertEqual(response.status_code, 400)

    def test_rejects_rented_house(self):
        response = self.client.post("/api/appointments", json={
            "house_no": "DL0003", "viewing_time": self._future().isoformat()})
        self.assertEqual(response.status_code, 409)

    def test_rejects_unknown_house(self):
        response = self.client.post("/api/appointments", json={
            "house_no": "NOPE", "viewing_time": self._future().isoformat()})
        self.assertEqual(response.status_code, 404)

    def test_list_by_house(self):
        self.client.post("/api/appointments", json={
            "house_no": "DL0001", "viewing_time": self._future().isoformat()})
        data = self.client.get("/api/appointments",
                               params={"house_no": "DL0001"}).json()
        self.assertEqual(data["total"], 1)

    def test_list_masks_phone(self):
        self.client.post("/api/appointments", json={
            "house_no": "DL0001", "viewing_time": self._future().isoformat(),
            "contact_phone": "13800009999"})
        response = self.client.get("/api/appointments",
                                   params={"house_no": "DL0001"})
        self.assertNotIn("13800009999", response.text)


class TestDistricts(ApiTestCase):

    def test_counts_only_available(self):
        data = self.client.get("/api/districts").json()
        counts = {row["district"]: row["count"] for row in data}
        self.assertEqual(counts.get("中山区"), 1, "已租房源不应计入")


if __name__ == "__main__":
    unittest.main()
