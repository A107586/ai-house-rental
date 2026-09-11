"""找回密码（K-11）。

`奇思妙想.docx` 第 1 条。改造前的答案是「处理不了」——
`/api/auth/password` 要求先登录且必须验原密码，密码忘了就是进不去。

这份测试里安全相关的用例占了一多半，因为**重置密码接口是整个系统里
最值钱的一扇门**：绕过它就等于拿到账号，不需要知道原密码。
"""

import hashlib
from datetime import datetime, timedelta

from tests.base import ApiTestCase, auth_header, make_admin
import app.db as db
from app import ratelimit
from app.models import PasswordReset, User


def token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class ResetCase(ApiTestCase):

    def setUp(self):
        super().setUp()
        # 限流按 IP，测试之间会互相影响，每次清干净
        ratelimit.reset()
        session = db.SessionLocal()
        try:
            user = session.query(User).filter_by(username="tenant1").one()
            user.email = "tenant1@example.com"
            session.commit()
        finally:
            session.close()

    def forgot(self, username="tenant1"):
        return self.client.post("/api/auth/forgot", json={"username": username})

    def issue_token(self, username="tenant1", minutes=30, used=False):
        """直接造一条重置记录，拿到原文令牌。

        走接口只能拿到「已发出」这个事实——令牌原文在邮件里，
        而测试环境没有邮件通道。造记录是唯一能测到「用令牌改密码」
        这一半的办法。
        """
        import secrets
        token = secrets.token_urlsafe(32)
        session = db.SessionLocal()
        try:
            user = session.query(User).filter_by(username=username).one()
            session.add(PasswordReset(
                user_id=user.id,
                token_hash=token_hash(token),
                expires_at=datetime.now() + timedelta(minutes=minutes),
                used_at=datetime.now() if used else None))
            session.commit()
        finally:
            session.close()
        return token

    def reset(self, token, password="newpass123"):
        return self.client.post("/api/auth/reset",
                                json={"token": token, "new_password": password})

    def can_login(self, password):
        return self.client.post("/api/auth/login", json={
            "username": "tenant1", "password": password}).status_code == 200


class TestForgotDoesNotLeak(ResetCase):
    """**不透露账号是否存在。**

    区别对待的话，这个接口就成了一个免费的「账号是否注册」查询器——
    撞库的第一步正是筛出哪些账号真实存在。
    """

    def test_same_answer_for_existing_and_missing(self):
        a = self.forgot("tenant1")
        b = self.forgot("this_user_does_not_exist")
        self.assertEqual(a.status_code, b.status_code)
        self.assertEqual(a.json()["message"], b.json()["message"])

    def test_same_answer_when_email_missing(self):
        session = db.SessionLocal()
        try:
            user = session.query(User).filter_by(username="landlord1").one()
            user.email = ""
            session.commit()
        finally:
            session.close()
        a = self.forgot("landlord1")
        b = self.forgot("tenant1")
        self.assertEqual(a.json()["message"], b.json()["message"])

    def test_no_token_issued_for_unknown_user(self):
        """说法一样，但**不能真的建记录**。"""
        self.forgot("this_user_does_not_exist")
        session = db.SessionLocal()
        try:
            self.assertEqual(session.query(PasswordReset).count(), 0)
        finally:
            session.close()

    def test_token_issued_for_real_user(self):
        self.forgot("tenant1")
        session = db.SessionLocal()
        try:
            self.assertEqual(session.query(PasswordReset).count(), 1)
        finally:
            session.close()

    def test_plaintext_token_is_never_stored(self):
        """库里只存哈希。库被读到也不能拿它去改别人的密码。"""
        self.forgot("tenant1")
        session = db.SessionLocal()
        try:
            row = session.query(PasswordReset).one()
            self.assertEqual(len(row.token_hash), 64)
            # 哈希是 16 进制，不可能是 token_urlsafe 那种带 - _ 的串
            int(row.token_hash, 16)
        finally:
            session.close()

    def test_rate_limited(self):
        """不限流的话，这个接口能被用来给任意用户的邮箱刷垃圾邮件。"""
        codes = {self.forgot().status_code for _ in range(12)}
        self.assertIn(429, codes)


class TestReset(ResetCase):

    def test_happy_path(self):
        token = self.issue_token()
        response = self.reset(token)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(self.can_login("newpass123"))
        self.assertFalse(self.can_login("test123"))

    def test_token_is_single_use(self):
        token = self.issue_token()
        self.reset(token)
        again = self.reset(token, "another123")
        self.assertEqual(again.status_code, 400)
        self.assertIn("已经用过", again.json()["detail"])
        # 第二次没有生效
        self.assertTrue(self.can_login("newpass123"))

    def test_expired_token_rejected(self):
        token = self.issue_token(minutes=-1)
        response = self.reset(token)
        self.assertEqual(response.status_code, 400)
        self.assertIn("过期", response.json()["detail"])
        self.assertTrue(self.can_login("test123"))

    def test_bogus_token_rejected(self):
        self.assertEqual(self.reset("not-a-real-token").status_code, 400)

    def test_used_and_unknown_are_told_apart(self):
        """「用过了」和「从来不存在」要分开提示。

        笼统一句「无效」会让用户反复点同一个失效链接，
        却始终不知道该重新申请。
        """
        used = self.issue_token(used=True)
        self.assertIn("已经用过", self.reset(used).json()["detail"])
        self.assertIn("无效或已过期", self.reset("bogus").json()["detail"])

    def test_weak_password_rejected(self):
        token = self.issue_token()
        self.assertEqual(self.reset(token, "123").status_code, 422)
        # 令牌没被消耗掉，用户可以再试一次
        self.assertEqual(self.reset(token, "goodpass1").status_code, 200)

    def test_password_with_space_rejected(self):
        token = self.issue_token()
        self.assertEqual(self.reset(token, "abc 12345").status_code, 422)


class TestResetRevokesSessions(ResetCase):
    """重置成功即吊销全部旧令牌。

    用户来重置密码，通常正是因为怀疑账号被人拿了。不吊销的话，
    攻击者手上那条七天有效的令牌照样能用，这次重置等于什么都没做。
    """

    def test_old_token_stops_working(self):
        old = auth_header(self.client, "tenant1", "test123")
        self.assertEqual(
            self.client.get("/api/auth/me", headers=old).status_code, 200)

        self.reset(self.issue_token())

        self.assertEqual(
            self.client.get("/api/auth/me", headers=old).status_code, 401)

    def test_other_pending_links_are_invalidated(self):
        """连点几次「忘记密码」会有好几个有效链接。用掉一个，
        其余的一并作废——否则邮箱里那几封旧邮件仍然是活的钥匙。"""
        first = self.issue_token()
        second = self.issue_token()
        self.reset(first)
        self.assertEqual(self.reset(second, "third1234").status_code, 400)


class TestDisabledAccount(ResetCase):

    def test_disabled_user_cannot_reset(self):
        token = self.issue_token()
        session = db.SessionLocal()
        try:
            user = session.query(User).filter_by(username="tenant1").one()
            user.status = "disabled"
            session.commit()
        finally:
            session.close()
        self.assertEqual(self.reset(token).status_code, 400)


class TestAdminIssuedCode(ResetCase):
    """管理员签发的一次性重置码（B1 的人工兜底）。

    邮箱通道是「尽力而为」的：没配 SMTP、用户没绑邮箱，自助流程就只会
    返回一句「如果该账号存在……」然后什么也不发生——**对忘了密码的人
    来说那等于没有找回密码功能**。真实平台这时走人工客服，这条路径
    就是它。

    它必须和邮件那条走**同一套校验**，否则就成了一个绕过限制的后门。
    """

    def setUp(self):
        super().setUp()
        # 测试库里没有 seed 出来的 admin，注册一个再提权
        self.admin = make_admin(self.client, "rootA")

    def _uid(self, username):
        session = db.SessionLocal()
        try:
            return session.query(User).filter_by(username=username).one().id
        finally:
            session.close()

    def issue(self, username="tenant1", headers=None):
        return self.client.post(
            "/api/admin/users/%d/reset-code" % self._uid(username),
            headers=headers if headers is not None else self.admin)

    def test_admin_gets_a_usable_code(self):
        code = self.issue().json()["code"]
        r = self.client.post("/api/auth/reset",
                             json={"token": code, "new_password": "brandnew1"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(
            self.client.post("/api/auth/login",
                             json={"username": "tenant1",
                                   "password": "brandnew1"}).status_code, 200)

    def test_code_is_single_use(self):
        """和邮件那条同一套校验——不是一个绕过限制的后门。"""
        code = self.issue().json()["code"]
        self.client.post("/api/auth/reset",
                         json={"token": code, "new_password": "brandnew1"})
        again = self.client.post("/api/auth/reset",
                                 json={"token": code, "new_password": "other12"})
        self.assertEqual(again.status_code, 400)

    def test_response_carries_the_warning(self):
        """**必须连用法和警告一起给。** 只甩一串字符，管理员既不知道
        该让用户拿它去哪儿填，也不会意识到这东西等于一把临时钥匙。"""
        data = self.issue().json()
        self.assertIn("核实身份", data["warning"])
        self.assertTrue(data["expires_in_minutes"] > 0)

    def test_non_admin_denied(self):
        tenant = auth_header(self.client, "tenant1", "test123")
        self.assertEqual(self.issue(headers=tenant).status_code, 403)

    def test_anonymous_denied(self):
        self.assertIn(self.issue(headers={}).status_code, (401, 403))

    def test_cannot_target_another_admin(self):
        """**防内鬼最基本的一条**：管理员之间不能互相签发，
        否则任何一个管理员都能悄悄接管另一个管理员的账号。"""
        make_admin(self.client, "rootB")
        self.assertEqual(self.issue("rootB").status_code, 403)

    def test_disabled_account_refused(self):
        self.client.patch("/api/admin/users/%d" % self._uid("tenant1"),
                          headers=self.admin, json={"status": "disabled"})
        self.assertEqual(self.issue().status_code, 409)
