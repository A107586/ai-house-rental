"""操作日志测试（M-07）。

采集口径是这批用例的重点：**只记改变状态的操作，且不记请求体**。
前者决定日志能不能翻，后者决定日志本身会不会变成新的泄露面——
改密码接口的请求体里是明文密码。
"""

from tests.base import ApiTestCase, auth_header, make_admin
from app.oplog import action_of, should_log


class TestActionNaming(ApiTestCase):
    """日志要给人看：「PATCH /api/admin/houses/12」谁也读不出发生了什么。"""

    def test_business_names(self):
        cases = [
            ("POST", "/api/houses", "发布房源"),
            ("PATCH", "/api/houses/DL0001/status", "上下架房源"),
            ("PATCH", "/api/houses/DL0001", "修改房源"),
            ("POST", "/api/contracts/HT001/activate", "确认合同生效"),
            ("POST", "/api/bills/ZD001/pay", "缴纳账单"),
            ("PATCH", "/api/admin/users/3", "修改用户状态或角色"),
        ]
        for method, path, name in cases:
            self.assertEqual(action_of(method, path), name, path)

    def test_status_route_beats_generic_route(self):
        """规则按顺序匹配，更具体的必须排在前面，
        否则上下架会被记成「修改房源」。"""
        self.assertEqual(action_of("PATCH", "/api/houses/DL0001/status"),
                         "上下架房源")

    def test_unknown_route_falls_back_to_raw(self):
        self.assertEqual(action_of("POST", "/api/whatever"),
                         "POST /api/whatever")


class TestLogScope(ApiTestCase):

    def test_get_is_not_logged(self):
        """全量记访问会让日志以每天几万条的速度涨，真要追溯时反而翻不动。"""
        self.assertFalse(should_log("GET", "/api/houses"))

    def test_write_is_logged(self):
        for method in ("POST", "PATCH", "PUT", "DELETE"):
            self.assertTrue(should_log(method, "/api/houses"))

    def test_static_is_not_logged(self):
        self.assertFalse(should_log("POST", "/static/x.png"))

    def test_login_is_skipped(self):
        self.assertFalse(should_log("POST", "/api/auth/login"))

    def test_chat_is_skipped(self):
        """一轮对话写两条，会把日志淹掉。"""
        self.assertFalse(should_log("POST", "/api/chat-messages"))


class TestLogCollection(ApiTestCase):

    def setUp(self):
        super().setUp()
        self.admin = make_admin(self.client)
        self.tenant = auth_header(self.client, "tenant1", "test123")

    def _logs(self, **params):
        return self.client.get("/api/admin/logs", headers=self.admin,
                               params=params).json()

    def test_write_request_is_recorded(self):
        self.client.post("/api/favorites", headers=self.tenant,
                         json={"house_no": "DL0001"})
        body = self._logs()
        self.assertGreaterEqual(body["total"], 1)
        top = body["list"][0]
        self.assertEqual(top["action"], "收藏房源")
        self.assertEqual(top["username"], "tenant1")
        self.assertEqual(top["role"], "tenant")

    def test_read_request_is_not_recorded(self):
        before = self._logs()["total"]
        self.client.get("/api/houses")
        self.client.get("/api/houses/DL0001")
        self.assertEqual(self._logs()["total"], before)

    def test_failed_request_is_recorded(self):
        """一串 403 正是需要被看见的信号。"""
        self.client.patch("/api/admin/users/1", headers=self.tenant,
                          json={"status": "disabled"})
        body = self._logs(failed_only=True)
        self.assertGreaterEqual(body["total"], 1)
        self.assertGreaterEqual(body["list"][0]["status_code"], 400)

    def test_newest_first(self):
        self.client.post("/api/favorites", headers=self.tenant,
                         json={"house_no": "DL0001"})
        self.client.delete("/api/favorites/DL0001", headers=self.tenant)
        self.assertEqual(self._logs()["list"][0]["action"], "取消收藏")

    def test_filter_by_role(self):
        self.client.post("/api/favorites", headers=self.tenant,
                         json={"house_no": "DL0001"})
        body = self._logs(role="tenant")
        self.assertTrue(all(row["role"] == "tenant" for row in body["list"]))
        self.assertGreaterEqual(body["total"], 1)

    def test_filter_by_keyword(self):
        self.client.post("/api/favorites", headers=self.tenant,
                         json={"house_no": "DL0001"})
        self.assertGreaterEqual(self._logs(keyword="收藏")["total"], 1)
        self.assertEqual(self._logs(keyword="不存在的动作")["total"], 0)

    def test_action_list(self):
        self.client.post("/api/favorites", headers=self.tenant,
                         json={"house_no": "DL0001"})
        body = self.client.get("/api/admin/logs/actions",
                               headers=self.admin).json()
        self.assertIn("收藏房源", body["list"])

    def test_password_is_never_in_the_log(self):
        """日志是给管理员看的，不该成为一个新的密码泄露面。"""
        self.client.post("/api/password", headers=self.tenant, json={
            "old_password": "test123", "new_password": "verysecret999"})
        dump = str(self._logs(limit=200))
        self.assertNotIn("verysecret999", dump)
        self.assertNotIn("test123", dump)

    def test_anonymous_write_is_recorded_without_identity(self):
        self.client.post("/api/auth/register", json={
            "username": "newbie", "password": "test123", "role": "tenant"})
        top = self._logs()["list"][0]
        self.assertEqual(top["action"], "注册账号")
        self.assertEqual(top["username"], "匿名")


class TestLogAccess(ApiTestCase):
    """日志里有全站所有人的操作轨迹，房东能翻到就等于能看别的房东在做什么。"""

    def setUp(self):
        super().setUp()
        self.tenant = auth_header(self.client, "tenant1", "test123")
        self.landlord = auth_header(self.client, "landlord1", "test123")

    def test_anonymous_denied(self):
        self.assertEqual(self.client.get("/api/admin/logs").status_code, 401)

    def test_tenant_denied(self):
        self.assertEqual(
            self.client.get("/api/admin/logs", headers=self.tenant).status_code,
            403)

    def test_landlord_denied(self):
        self.assertEqual(
            self.client.get("/api/admin/logs", headers=self.landlord).status_code,
            403)

    def test_action_list_denied(self):
        self.assertEqual(
            self.client.get("/api/admin/logs/actions",
                            headers=self.landlord).status_code, 403)
