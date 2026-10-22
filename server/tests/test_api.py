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

    def _other(self):
        """第二个租客。时段冲突要拿两个人测——同一个人约同一套房第二次，
        先撞上的是「同一套房只留一条」那条规矩，测不到时段。"""
        self.client.post("/api/auth/register", json={
            "username": "tenant2", "password": "test123", "real_name": "租客乙",
            "phone": "13900002222"})
        return auth_header(self.client, "tenant2", "test123")

    def test_rejects_conflict(self):
        """服务端必须独立做冲突校验，不能只依赖智能体那边的检查。"""
        moment = self._future().isoformat()
        self.client.post("/api/appointments", headers=self.headers,
                         json={"house_no": "DL0001", "viewing_time": moment})
        second = self.client.post("/api/appointments", headers=self._other(),
                                  json={"house_no": "DL0001", "viewing_time": moment})
        self.assertEqual(second.status_code, 409)
        self.assertIn("时段", second.json()["detail"])

    def test_rejects_overlapping_within_an_hour(self):
        base = self._future(hour=14)
        self.client.post("/api/appointments", headers=self.headers, json={
            "house_no": "DL0001", "viewing_time": base.isoformat()})
        overlap = self.client.post("/api/appointments", headers=self._other(), json={
            "house_no": "DL0001",
            "viewing_time": (base + timedelta(minutes=30)).isoformat()})
        self.assertEqual(overlap.status_code, 409)
        self.assertIn("时段", overlap.json()["detail"])

    def test_allows_adjacent_hour(self):
        base = self._future(hour=14)
        self.client.post("/api/appointments", headers=self.headers, json={
            "house_no": "DL0001", "viewing_time": base.isoformat()})
        adjacent = self.client.post("/api/appointments", headers=self._other(), json={
            "house_no": "DL0001",
            "viewing_time": (base + timedelta(hours=1)).isoformat()})
        self.assertEqual(adjacent.status_code, 200)

    def test_one_open_viewing_per_house(self):
        """原先同一个人能连占几个时段，别人约不进来。"""
        self.client.post("/api/appointments", headers=self.headers, json={
            "house_no": "DL0001", "viewing_time": self._future(hour=10).isoformat()})
        again = self.client.post("/api/appointments", headers=self.headers, json={
            "house_no": "DL0001", "viewing_time": self._future(hour=15).isoformat()})
        self.assertEqual(again.status_code, 409)
        self.assertIn("先取消原来的", again.json()["detail"])
        # 另一套房不受影响
        other = self.client.post("/api/appointments", headers=self.headers, json={
            "house_no": "DL0002", "viewing_time": self._future(hour=15).isoformat()})
        self.assertEqual(other.status_code, 200, other.text)

    def test_reschedule_swaps_in_one_step(self):
        """改期带 replaces：新的约上、旧的同时取消。挪半小时也不算和自己冲突。"""
        first = self.client.post("/api/appointments", headers=self.headers, json={
            "house_no": "DL0001", "viewing_time": self._future(hour=10).isoformat()})
        old_id = first.json()["appointment"]["id"]
        moved = self.client.post("/api/appointments", headers=self.headers, json={
            "house_no": "DL0001", "replaces": old_id,
            "viewing_time": self._future(hour=10).replace(minute=30).isoformat()})
        self.assertEqual(moved.status_code, 200, moved.text)
        rows = {a["id"]: a for a in self.client.get(
            "/api/appointments/mine?side=tenant", headers=self.headers).json()["list"]}
        self.assertEqual(rows[old_id]["status"], "cancelled")
        self.assertEqual(rows[moved.json()["appointment"]["id"]]["status"], "pending")

    def test_cannot_replace_someone_elses_viewing(self):
        first = self.client.post("/api/appointments", headers=self.headers, json={
            "house_no": "DL0001", "viewing_time": self._future(hour=10).isoformat()})
        hijack = self.client.post("/api/appointments", headers=self._other(), json={
            "house_no": "DL0001", "replaces": first.json()["appointment"]["id"],
            "viewing_time": self._future(hour=15).isoformat()})
        self.assertEqual(hijack.status_code, 409)

    def test_taken_slots_only_lists_upcoming_ones(self):
        """过去的时段不会再冲突；摆在详情页里，一条上周的「待确认」像房东一直没理人。"""
        import app.db as db
        from app.models import Appointment
        booked = self.client.post("/api/appointments", headers=self.headers, json={
            "house_no": "DL0001", "viewing_time": self._future().isoformat()})
        session = db.SessionLocal()
        try:
            session.get(Appointment, booked.json()["appointment"]["id"]).viewing_time = \
                datetime.now() - timedelta(days=2)
            session.commit()
        finally:
            session.close()
        slots = self.client.get("/api/appointments?house_no=DL0001").json()
        self.assertEqual(slots["total"], 0)

    def test_viewing_hours_and_horizon(self):
        """这几条原先只有智能体那一侧守着，直接调接口凌晨三点、三年以后都能约。"""
        for moment in (self._future(hour=3), self._future(hour=19),
                       self._future(days=45, hour=10)):
            response = self.client.post("/api/appointments", headers=self.headers, json={
                "house_no": "DL0001", "viewing_time": moment.isoformat()})
            self.assertEqual(response.status_code, 400, moment)

    def test_landlord_cannot_book_his_own_house(self):
        landlord = auth_header(self.client, "landlord1", "test123")
        response = self.client.post("/api/appointments", headers=landlord, json={
            "house_no": "DL0001", "viewing_time": self._future().isoformat()})
        self.assertEqual(response.status_code, 400)

    def _book(self, headers, moment):
        response = self.client.post("/api/appointments", headers=headers, json={
            "house_no": "DL0001", "viewing_time": moment.isoformat(),
            "contact_phone": "13800009999"})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["appointment"]["id"]

    def _set(self, headers, appointment_id, status):
        return self.client.patch("/api/appointments/%d" % appointment_id,
                                 headers=headers, json={"status": status})

    def test_cancelled_slot_cannot_be_confirmed_back(self):
        """取消后让出的时段别人约了并确认，原先房东还能把取消的那条「确认」回来——
        同一时段两条已确认的看房。状态只能往前走。"""
        landlord = auth_header(self.client, "landlord1", "test123")
        moment = self._future()
        first = self._book(self.headers, moment)
        self._set(self.headers, first, "cancelled")
        self.client.post("/api/auth/register", json={
            "username": "tenant9", "password": "secret123", "real_name": "李四"})
        second = self._book(auth_header(self.client, "tenant9", "secret123"), moment)
        self.assertEqual(self._set(landlord, second, "confirmed").status_code, 200)

        back = self._set(landlord, first, "confirmed")
        self.assertEqual(back.status_code, 409)
        slots = self.client.get("/api/appointments?house_no=DL0001").json()["list"]
        self.assertEqual(sum(1 for s in slots if s["status"] == "confirmed"), 1)

    def test_confirmed_can_only_be_cancelled(self):
        landlord = auth_header(self.client, "landlord1", "test123")
        appointment = self._book(self.headers, self._future())
        self._set(landlord, appointment, "confirmed")
        self.assertEqual(self._set(landlord, appointment, "rejected").status_code, 409)
        self.assertEqual(self._set(self.headers, appointment, "cancelled").status_code, 200)

    def test_a_viewing_that_already_happened_cannot_be_cancelled(self):
        """确认过、时间也过了的是一次发生过的看房，带看记录挂在它上面。"""
        import app.db as db
        from app.models import Appointment
        landlord = auth_header(self.client, "landlord1", "test123")
        appointment = self._book(self.headers, self._future())
        self._set(landlord, appointment, "confirmed")
        session = db.SessionLocal()
        try:
            session.get(Appointment, appointment).viewing_time = \
                datetime.now() - timedelta(hours=2)
            session.commit()
        finally:
            session.close()
        self.assertEqual(self._set(self.headers, appointment, "cancelled").status_code, 409)

    def test_double_click_is_harmless(self):
        landlord = auth_header(self.client, "landlord1", "test123")
        appointment = self._book(self.headers, self._future())
        self.assertEqual(self._set(landlord, appointment, "confirmed").status_code, 200)
        self.assertEqual(self._set(landlord, appointment, "confirmed").status_code, 200)

    def test_landlord_hears_when_the_tenant_cancels(self):
        """租客取消已确认的看房，房东要知道——否则他照样跑一趟。"""
        from app import db
        from app.models import Message, User
        landlord = auth_header(self.client, "landlord1", "test123")
        appointment = self._book(self.headers, self._future())
        self._set(landlord, appointment, "confirmed")
        session = db.SessionLocal()
        try:
            landlord_id = session.query(User).filter_by(username="landlord1").one().id
            self._set(self.headers, appointment, "cancelled")
            titles = [m.title for m in session.query(Message).filter_by(user_id=landlord_id)]
        finally:
            session.close()
        self.assertIn("看房预约已被租客取消", titles)

    def test_renting_the_house_cancels_open_viewings(self):
        """房子租出去了，别人约的看房不能还挂着「待确认」。"""
        from datetime import date
        from app import db
        from app.models import Appointment, Message, User
        landlord = auth_header(self.client, "landlord1", "test123")
        waiting = self._book(self.headers, self._future())

        self.client.post("/api/auth/register", json={
            "username": "tenant9", "password": "secret123", "real_name": "李四"})
        signer = auth_header(self.client, "tenant9", "secret123")
        no = self.client.post("/api/contracts", headers=landlord, json={
            "house_no": "DL0001", "tenant_username": "tenant9",
            "start_date": (date.today() + timedelta(days=10)).isoformat(),
            "months": 12, "pay_months": 1}).json()["contract"]["contract_no"]
        activated = self.client.post("/api/contracts/%s/activate" % no, headers=signer)
        self.assertEqual(activated.status_code, 200, activated.text)

        session = db.SessionLocal()
        try:
            self.assertEqual(session.get(Appointment, waiting).status, "cancelled")
            tenant_id = session.query(User).filter_by(username="tenant1").one().id
            notes = [m.content for m in session.query(Message).filter_by(user_id=tenant_id)]
        finally:
            session.close()
        self.assertTrue(any("已经租出去了" in n for n in notes), notes)

    def test_taking_the_house_offline_cancels_open_viewings(self):
        from app import db
        from app.models import Appointment
        landlord = auth_header(self.client, "landlord1", "test123")
        waiting = self._book(self.headers, self._future())
        off = self.client.patch("/api/houses/DL0001/status?status=offline", headers=landlord)
        self.assertEqual(off.status_code, 200, off.text)
        session = db.SessionLocal()
        try:
            appointment = session.get(Appointment, waiting)
            self.assertEqual(appointment.status, "cancelled")
            self.assertIn("下架", appointment.reply)
        finally:
            session.close()

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
