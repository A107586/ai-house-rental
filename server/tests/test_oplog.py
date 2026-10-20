"""操作日志测试（M-07）。

采集口径是这批用例的重点：**只记改变状态的操作，且不记请求体**。
前者决定日志能不能翻，后者决定日志本身会不会变成新的泄露面——
改密码接口的请求体里是明文密码。
"""

import re

from tests.base import ApiTestCase, auth_header, make_admin
from app import oplog
from app.oplog import action_of, should_log


def _routes():
    """(方法, 示例路径)。新版 FastAPI 把 include_router 进来的路由包了一层，要钻进去。"""
    from app.main import app

    def walk(routes):
        for r in routes:
            inner = getattr(r, "original_router", None)
            if inner is not None:
                yield from walk(inner.routes)
            else:
                yield r
    return [(m, re.sub(r"\{[^}]*\}", "1", r.path))
            for r in walk(app.routes) for m in (getattr(r, "methods", None) or [])]


class TestActionNaming(ApiTestCase):
    """日志要给人看：「PATCH /api/admin/houses/12」谁也读不出发生了什么。"""

    def test_business_names(self):
        cases = [
            ("POST", "/api/houses", "发布房源"),
            ("PATCH", "/api/houses/DL0001/status", "上下架房源"),
            ("PUT", "/api/houses/DL0001", "修改房源"),
            ("POST", "/api/houses/DL0001/images/3/cover", "设置房源封面"),
            ("POST", "/api/delegations/5/end", "解除房源委托"),
            ("PATCH", "/api/admin/config", "修改系统配置"),
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

    def test_every_logged_route_has_a_name(self):
        """管理员反馈日志里一堆「POST /api/delegations」看不懂。新接口忘了起名，这里就过不去。"""
        unnamed = sorted({"%s %s" % (m, p) for m, p in _routes()
                          if should_log(m, p) and action_of(m, p).startswith(m + " ")})
        self.assertEqual(unnamed, [], "这些接口在操作日志里没有动作名，去 app/oplog.py 补一条")

    def test_no_stale_rules(self):
        """规则对不上任何接口，说明接口改过路径或方法——原先「修改个人资料」写的是
        PATCH /api/profile，实际是 PUT /api/auth/profile，日志里一直是原始路径。"""
        routes = _routes()
        stale = [(m, pat.pattern, name) for m, pat, name in oplog._COMPILED
                 if not any(rm == m and pat.match(rp) for rm, rp in routes)]
        self.assertEqual(stale, [])

    def test_automatic_read_marks_are_not_logged(self):
        self.assertFalse(should_log("PATCH", "/api/messages/12/read"))
        self.assertFalse(should_log("POST", "/api/conversations/3/read"))
        self.assertTrue(should_log("POST", "/api/messages/read-all"))     # 这个是人点的

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
