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

    def setUp(self):
        super().setUp()
        self.headers = auth_header(self.client)

    def _future(self, days=3, hour=14):
        moment = datetime.now() + timedelta(days=days)
        return moment.replace(hour=hour, minute=0, second=0, microsecond=0)

    def test_create(self):
        response = self.client.post("/api/appointments", headers=self.headers, json={
            "house_no": "DL0001",
            "viewing_time": self._future().isoformat(),
            "contact_name": "张三", "contact_phone": "13800009999"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["appointment"]["status"], "pending")

    def test_rejects_conflict(self):
        """服务端必须独立做冲突校验，不能只依赖智能体那边的检查。"""
        moment = self._future().isoformat()
        self.client.post("/api/appointments", headers=self.headers,
                         json={"house_no": "DL0001", "viewing_time": moment})
        second = self.client.post("/api/appointments", headers=self.headers,
                                  json={"house_no": "DL0001", "viewing_time": moment})
        self.assertEqual(second.status_code, 409)

    def test_rejects_overlapping_within_an_hour(self):
        base = self._future(hour=14)
        self.client.post("/api/appointments", headers=self.headers, json={
            "house_no": "DL0001", "viewing_time": base.isoformat()})
        overlap = self.client.post("/api/appointments", headers=self.headers, json={
            "house_no": "DL0001",
            "viewing_time": (base + timedelta(minutes=30)).isoformat()})
        self.assertEqual(overlap.status_code, 409)

    def test_allows_adjacent_hour(self):
        base = self._future(hour=14)
        self.client.post("/api/appointments", headers=self.headers, json={
            "house_no": "DL0001", "viewing_time": base.isoformat()})
        adjacent = self.client.post("/api/appointments", headers=self.headers, json={
            "house_no": "DL0001",
            "viewing_time": (base + timedelta(hours=1)).isoformat()})
        self.assertEqual(adjacent.status_code, 200)

    def test_rejects_past_time(self):
        past = (datetime.now() - timedelta(days=1)).isoformat()
        response = self.client.post("/api/appointments", headers=self.headers,
                                    json={"house_no": "DL0001", "viewing_time": past})
        self.assertEqual(response.status_code, 400)

    def test_rejects_rented_house(self):
        response = self.client.post("/api/appointments", headers=self.headers, json={
            "house_no": "DL0003", "viewing_time": self._future().isoformat()})
        self.assertEqual(response.status_code, 409)

    def test_rejects_unknown_house(self):
        response = self.client.post("/api/appointments", headers=self.headers, json={
            "house_no": "NOPE", "viewing_time": self._future().isoformat()})
        self.assertEqual(response.status_code, 404)

    def test_list_by_house(self):
        self.client.post("/api/appointments", headers=self.headers, json={
            "house_no": "DL0001", "viewing_time": self._future().isoformat()})
        data = self.client.get("/api/appointments",
                               params={"house_no": "DL0001"}).json()
        self.assertEqual(data["total"], 1)

    def test_list_masks_phone(self):
        self.client.post("/api/appointments", headers=self.headers, json={
            "house_no": "DL0001", "viewing_time": self._future().isoformat(),
            "contact_phone": "13800009999"})
        response = self.client.get("/api/appointments",
                                   params={"house_no": "DL0001"})
        self.assertNotIn("13800009999", response.text)

    def test_requires_login(self):
        response = self.client.post("/api/appointments", json={
            "house_no": "DL0001", "viewing_time": self._future().isoformat(),
            "contact_phone": "13800009999"})
        self.assertEqual(response.status_code, 401)

    def test_rejects_invalid_phone(self):
        response = self.client.post("/api/appointments", headers=self.headers, json={
            "house_no": "DL0001", "viewing_time": self._future().isoformat(),
            "contact_phone": "abc"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"], "请输入正确的 11 位手机号")


class TestOrientationVocabulary(ApiTestCase):
    """**朝向的两套写法都得收。**

    这个洞是给智能体做「发布房源」表单、真的点了确认才撞出来的：
    校验表只写了「南」「东南」，而前端下拉框给的是「朝南」「南北通透」，
    种子数据也是后一套——九个选项里五个一提交就 422，
    **房东在页面上选「朝南」根本发不出去**，编辑已有房源同样存不下。

    卡片好好的、页面也好好的，只有真的提交一次才看得见。
    """

    def setUp(self):
        super().setUp()
        self.headers = auth_header(self.client, "landlord1", "test123")

    BASE = {"title": "朝向测试房源", "district": "中山区", "community": "测试小区",
            "address": "测试路 1 号", "rent_price": 3000, "deposit": 3000,
            "area": 80, "layout": "两室一厅", "rent_type": "整租", "floor": "3/9"}

    def test_both_spellings_are_accepted(self):
        for value in ("南", "东南", "朝南", "朝东", "南北", "南北通透", ""):
            with self.subTest(orientation=value):
                payload = dict(self.BASE, orientation=value)
                response = self.client.post("/api/houses", headers=self.headers,
                                            json=payload)
                self.assertEqual(response.status_code, 200,
                                 "朝向「%s」被拒了：%s"
                                 % (value, response.text[:120]))

    def test_the_frontend_dropdown_is_fully_accepted(self):
        """前端下拉框里的每一项都得能提交——这才是用户真正会点的那些。

        写死在这里是有意的：改前端选项而忘了后端校验，正是原先出错的路径。
        """
        for value in ("朝南", "朝东", "朝西", "朝北",
                      "东南", "西南", "东北", "西北", "南北通透"):
            with self.subTest(orientation=value):
                response = self.client.post(
                    "/api/houses", headers=self.headers,
                    json=dict(self.BASE, orientation=value))
                self.assertEqual(response.status_code, 200, value)

    def test_seeded_houses_can_be_saved_back_unchanged(self):
        """已有房源原样保存一次必须成功。

        编辑表单会把朝向原样回填，用户什么都没改就点保存——
        这一步失败是最说不过去的。
        """
        house = self.client.get("/api/houses/DL0001").json()
        response = self.client.put(
            "/api/houses/DL0001", headers=self.headers,
            json={"orientation": house.get("orientation") or ""})
        self.assertEqual(response.status_code, 200, response.text[:150])

    def test_garbage_is_still_rejected(self):
        response = self.client.post("/api/houses", headers=self.headers,
                                    json=dict(self.BASE, orientation="朝里"))
        self.assertEqual(response.status_code, 422)


class TestDistricts(ApiTestCase):

    def test_counts_only_available(self):
        data = self.client.get("/api/districts").json()
        counts = {row["district"]: row["count"] for row in data}
        self.assertEqual(counts.get("中山区"), 1, "已租房源不应计入")


class TestMapEndpoint(ApiTestCase):
    """地图点位接口（/api/houses/map）。"""

    def test_map_endpoint_filters_points_by_bounds(self):
        """只返回视野矩形内的点。"""
        data = self.client.get("/api/houses/map", params={
            "west": 121.62, "south": 38.90,
            "east": 121.65, "north": 38.93,
        }).json()
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["list"][0]["house_no"], "DL0001")
        self.assertIn("lng", data["list"][0])
        # 地图一次要画几百个点，房源描述这种大字段不能跟着下发
        self.assertNotIn("description", data["list"][0])

    def test_map_endpoint_shares_the_list_filters(self):
        """**地图和列表必须用同一套筛选。**

        各写一份的话，用户勾了「有电梯」，列表里那套没电梯的房子
        消失了、地图上却还亮着——而他会以为是地图坏了。
        """
        data = self.client.get("/api/houses/map",
                               params={"district": "中山区"}).json()
        self.assertEqual([h["house_no"] for h in data["list"]], ["DL0001"])

        # K-04/K-05 补的维度也要在地图上生效，不能只支持最早那几个
        no_lift = self.client.get("/api/houses/map",
                                  params={"elevator": "false"}).json()
        self.assertNotIn("DL0001", [h["house_no"] for h in no_lift["list"]])

    def test_map_skips_houses_without_coordinates(self):
        """没定过位的房源不进地图。

        硬塞一个默认坐标会让它们全堆在市中心某一点上，
        比不显示更糟——用户会以为那里真有十几套房。
        """
        body = self.client.get("/api/houses/map").json()
        self.assertTrue(all(h["lng"] and h["lat"] for h in body["list"]))


if __name__ == "__main__":
    unittest.main()
