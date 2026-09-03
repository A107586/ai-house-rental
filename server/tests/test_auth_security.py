"""登录限流与令牌吊销测试（待完善清单 B-1 / B-2）。

这两条是答辩最容易被问到的安全题，也是原先完全没有的：

* 登录接口没有任何限制，撞库脚本可以不受限制地跑
* 令牌是无状态 HMAC，改完密码、账号被禁用之后**旧令牌还能用满七天**
"""

import time

from app import ratelimit
from tests.base import ApiTestCase, auth_header, make_admin


class RateLimitCase(ApiTestCase):

    def setUp(self):
        super().setUp()
        # 计数在进程内存里，用例之间必须清干净，否则互相污染
        ratelimit.reset()

    def tearDown(self):
        ratelimit.reset()
        super().tearDown()

    def login(self, username="tenant1", password="test123"):
        return self.client.post("/api/auth/login",
                                json={"username": username,
                                      "password": password})


class TestLoginRateLimit(RateLimitCase):

    def test_normal_login_unaffected(self):
        self.assertEqual(self.login().status_code, 200)

    def test_blocks_after_repeated_failures(self):
        for _ in range(ratelimit.FAIL_LIMIT):
            self.assertEqual(self.login(password="wrong").status_code, 401)
        blocked = self.login(password="wrong")
        self.assertEqual(blocked.status_code, 429)
        self.assertIn("过于频繁", blocked.json()["detail"])

    def test_correct_password_also_blocked_once_limited(self):
        """限流拦的是这个 IP，不是这次输入对不对——
        否则攻击者只要蒙对一次就能绕过。"""
        for _ in range(ratelimit.FAIL_LIMIT):
            self.login(password="wrong")
        self.assertEqual(self.login().status_code, 429)

    def test_success_clears_the_counter(self):
        """本人已经证明身份，之前的手误不该继续记账。"""
        for _ in range(ratelimit.FAIL_LIMIT - 1):
            self.login(password="wrong")
        self.assertEqual(self.login().status_code, 200)
        # 清零之后又能重新失败满额度
        for _ in range(ratelimit.FAIL_LIMIT):
            self.assertEqual(self.login(password="wrong").status_code, 401)

    def test_counts_by_ip_not_by_username(self):
        """按用户名限流会**给攻击者锁死他人账号的手段**——
        拿一个已知用户名连错八次，本人就登不上了。

        这里换着用户名失败，同一个 IP 仍然被拦，证明计数按 IP。
        """
        for index in range(ratelimit.FAIL_LIMIT):
            self.login(username="ghost%d" % index, password="wrong")
        self.assertEqual(self.login().status_code, 429)

    def test_missing_fields_do_not_count_as_attempts(self):
        """参数没填是手滑，不是在猜密码。"""
        for _ in range(ratelimit.FAIL_LIMIT * 2):
            self.client.post("/api/auth/login",
                             json={"username": "", "password": ""})
        self.assertEqual(self.login().status_code, 200)

    def test_window_expiry_releases(self):
        """窗口滑过之后自动解封，不需要人工干预。"""
        for _ in range(ratelimit.FAIL_LIMIT):
            self.login(password="wrong")
        self.assertEqual(self.login().status_code, 429)

        # 把失败时刻整体往前挪出窗口，等价于「时间过去了」
        old = time.time() - ratelimit.FAIL_WINDOW - 1
        for ip in list(ratelimit._failures):
            ratelimit._failures[ip] = [old] * ratelimit.FAIL_LIMIT
        self.assertEqual(self.login().status_code, 200)

    def test_wait_hint_is_positive(self):
        for _ in range(ratelimit.FAIL_LIMIT):
            self.login(password="wrong")
        detail = self.login(password="wrong").json()["detail"]
        seconds = int("".join(c for c in detail if c.isdigit()))
        self.assertGreater(seconds, 0)
        self.assertLessEqual(seconds, ratelimit.FAIL_WINDOW + 1)


class TestTokenRevocation(RateLimitCase):

    def token(self, username="tenant1", password="test123"):
        return self.login(username, password).json()["token"]

    def header(self, token):
        return {"Authorization": "Bearer " + token}

    def test_token_works_before_revocation(self):
        headers = self.header(self.token())
        self.assertEqual(self.client.get("/api/auth/me",
                                         headers=headers).status_code, 200)

    def test_password_change_kills_old_tokens(self):
        """改密码在用户心里等于「把别人踢下去」。旧令牌还能用满七天的话，
        这个动作实际上什么都没做——而密码泄露后改密码正是第一反应。"""
        old = self.header(self.token())
        self.client.post("/api/auth/password", headers=old, json={
            "old_password": "test123", "new_password": "newpass123"})

        self.assertEqual(self.client.get("/api/auth/me",
                                         headers=old).status_code, 401)
        # 新密码登录拿到的新令牌照常可用
        fresh = self.header(self.token(password="newpass123"))
        self.assertEqual(self.client.get("/api/auth/me",
                                         headers=fresh).status_code, 200)

    def test_logout_all_kills_old_tokens(self):
        """手机丢了、在网吧登录过忘了退——用户需要一个「全部踢下线」的
        按钮，而不该被迫换掉自己记得住的密码。"""
        old = self.header(self.token())
        self.assertEqual(self.client.post("/api/auth/logout-all",
                                          headers=old).status_code, 200)
        self.assertEqual(self.client.get("/api/auth/me",
                                         headers=old).status_code, 401)

    def test_disabling_account_kills_tokens(self):
        admin = make_admin(self.client)
        victim = self.header(self.token())
        target = self.client.get("/api/admin/users", headers=admin).json()
        uid = next(u["id"] for u in target["list"] if u["username"] == "tenant1")

        self.client.patch("/api/admin/users/%d" % uid, headers=admin,
                          json={"status": "disabled"})
        self.assertEqual(self.client.get("/api/auth/me",
                                         headers=victim).status_code, 401)

    def test_role_downgrade_kills_tokens(self):
        """**角色写在令牌里**。不吊销的话，一个刚被降为租客的人拿着旧令牌
        还能以房东身份操作满七天——这才是更危险的那一半。"""
        admin = make_admin(self.client)
        landlord = self.header(self.token("landlord1", "test123"))
        users = self.client.get("/api/admin/users", headers=admin).json()
        uid = next(u["id"] for u in users["list"]
                   if u["username"] == "landlord1")

        self.client.patch("/api/admin/users/%d" % uid, headers=admin,
                          json={"role": "tenant"})
        self.assertEqual(self.client.get("/api/auth/me",
                                         headers=landlord).status_code, 401)

    def test_unchanged_update_does_not_revoke(self):
        """把状态改成它本来就是的值，不该把人踢下线。"""
        admin = make_admin(self.client)
        headers = self.header(self.token())
        users = self.client.get("/api/admin/users", headers=admin).json()
        uid = next(u["id"] for u in users["list"] if u["username"] == "tenant1")

        self.client.patch("/api/admin/users/%d" % uid, headers=admin,
                          json={"status": "active"})
        self.assertEqual(self.client.get("/api/auth/me",
                                         headers=headers).status_code, 200)

    def test_other_users_are_unaffected(self):
        """吊销只作用于目标本人。"""
        admin = make_admin(self.client)
        landlord = self.header(self.token("landlord1", "test123"))
        tenant = self.header(self.token("tenant1", "test123"))
        users = self.client.get("/api/admin/users", headers=admin).json()
        uid = next(u["id"] for u in users["list"] if u["username"] == "tenant1")

        self.client.patch("/api/admin/users/%d" % uid, headers=admin,
                          json={"status": "disabled"})
        self.assertEqual(self.client.get("/api/auth/me",
                                         headers=landlord).status_code, 200)
        self.assertEqual(self.client.get("/api/auth/me",
                                         headers=tenant).status_code, 401)

    def test_token_carries_issued_at(self):
        """iat 是吊销的基础，缺了它整套机制无从判断。"""
        from app.auth import decode_token
        payload = decode_token(self.token())
        self.assertIn("iat", payload)
        # 带小数，所以和当前时刻直接比，不能截成整秒
        self.assertLessEqual(payload["iat"], time.time())

    def test_legacy_token_without_iat_is_rejected_after_revocation(self):
        """加 iat 之前签发的老令牌一律当作「早于任何基线」。"""
        import app.db as db
        from app.auth import _b64encode, _sign
        from app.models import User
        import json as _json

        session = db.SessionLocal()
        try:
            user = session.query(User).filter_by(username="tenant1").one()
            body = _b64encode(_json.dumps(
                {"uid": user.id, "username": user.username,
                 "role": user.role, "exp": int(time.time()) + 3600},
                separators=(",", ":")).encode())
            legacy = body + "." + _sign(body)
        finally:
            session.close()

        # 吊销之前老令牌仍可用（没有基线就无从比较）
        self.assertEqual(self.client.get(
            "/api/auth/me", headers=self.header(legacy)).status_code, 200)

        self.client.post("/api/auth/logout-all",
                         headers=self.header(self.token()))
        self.assertEqual(self.client.get(
            "/api/auth/me", headers=self.header(legacy)).status_code, 401)
