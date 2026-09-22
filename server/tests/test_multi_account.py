# -*- coding: utf-8 -*-
"""一台电脑同时登录多个账号。

## 为什么值得单独一组用例

这不是假想的场景，是**每天都在发生的**：答辩要同时开租客、房东、
管理员三个窗口；开发自己也一直在两个账号之间来回切。

而这条路上踩过的坑有过完整的一串。令牌原先只存 `localStorage`，
那是**同源共享**的——A 标签页登录 landlord1、B 登录 landlord2，
第二次登录会把 A 的令牌一起换掉。于是冒出三个看着毫无关联的「bug」：

* 「所有房东共享房源」——A 页请求带的是 landlord2 的令牌
* 「谁发的消息都显示成自己发的」——`mine` 由服务端按令牌算
* 「私信只能发一条」——第二条以 B 的身份发，而 B 不是当事人 → 403

三条都不在业务逻辑里，**错的是「现在是谁」这件事被共享了**。
前端那一层已经改成 `sessionStorage` 按标签页隔离（见 `web/src/api.js`），
本地缓存也按账号分开了（见 `web/src/storage.js`）。

**这一组用例盯的是服务端**：无论前端怎么错，同一个进程里同时处理
两个账号的请求，谁也不能看到谁的数据。前端隔离是一道防线，
服务端按令牌判身份是另一道——两道都得立着。

## 并发那几条

不止顺序交替，还要**真并发**。串行跑不出来的是进程级共享状态：
一个按身份填的模块级缓存、一个忘了清的全局变量，串行时后写覆盖前写
看不出问题，并发时就会互相串。
"""

import os
import shutil
import tempfile
import threading
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tests.base import ApiTestCase, auth_header, make_admin
import app.db as db
from app.main import app as fastapi_app
from app.models import Base, House, User
from app.security import hash_password

# 两个账号刻意用同一个会话编号。接口要求至少 8 个字符。
SHARED_SESSION = "shared-session-id"


class MultiAccountTestCase(ApiTestCase):
    """两个房东 + 一个租客，各自有各自的房源。"""

    def setUp(self):
        super().setUp()
        session = db.SessionLocal()
        try:
            second = User(username="landlord2",
                          password_hash=hash_password("test123"),
                          real_name="房东乙", phone="13800009999",
                          role="landlord")
            session.add(second)
            session.flush()
            session.add(House(
                house_no="DL9001", title="房东乙的房", landlord_id=second.id,
                district="沙河口区", community="黑石礁", rent_price=2600,
                area=60, layout="两室一厅", rent_type="整租",
                status="available"))
            session.commit()
        finally:
            session.close()


class TestTokensDoNotBleed(MultiAccountTestCase):
    """两个令牌同时在手，各查各的。"""

    def test_two_landlords_see_only_their_own_houses(self):
        one = auth_header(self.client, "landlord1", "test123")
        two = auth_header(self.client, "landlord2", "test123")

        # 交替发，模拟两个标签页来回切
        rows_one = self.client.get("/api/landlord/houses", headers=one).json()
        rows_two = self.client.get("/api/landlord/houses", headers=two).json()
        again = self.client.get("/api/landlord/houses", headers=one).json()

        nos_one = {r["house_no"] for r in rows_one["list"]}
        nos_two = {r["house_no"] for r in rows_two["list"]}
        self.assertIn("DL0001", nos_one)
        self.assertNotIn("DL9001", nos_one)
        self.assertEqual(nos_two, {"DL9001"})
        # 第二个人查过之后，第一个人再查还是原来那批
        self.assertEqual({r["house_no"] for r in again["list"]}, nos_one)

    def test_whoami_is_per_token(self):
        one = auth_header(self.client, "landlord1", "test123")
        two = auth_header(self.client, "tenant1", "test123")
        self.assertEqual(
            self.client.get("/api/auth/me", headers=one).json()["username"],
            "landlord1")
        self.assertEqual(
            self.client.get("/api/auth/me", headers=two).json()["username"],
            "tenant1")
        # 再问一遍，确认没有被后一次调用带偏
        self.assertEqual(
            self.client.get("/api/auth/me", headers=one).json()["username"],
            "landlord1")

    def test_logging_in_again_does_not_invalidate_the_other_session(self):
        """第二个账号登录，第一个账号手里那份令牌必须还能用。

        这是「一台电脑开两个窗口」的底线：后登录的把先登录的挤下线，
        多账号就无从谈起。
        """
        one = auth_header(self.client, "landlord1", "test123")
        auth_header(self.client, "landlord2", "test123")
        auth_header(self.client, "tenant1", "test123")
        still = self.client.get("/api/auth/me", headers=one)
        self.assertEqual(still.status_code, 200)
        self.assertEqual(still.json()["username"], "landlord1")

    def test_same_account_twice_gets_two_usable_tokens(self):
        """同一个账号在两个窗口登录，两份令牌都得能用。"""
        first = auth_header(self.client, "tenant1", "test123")
        second = auth_header(self.client, "tenant1", "test123")
        for headers in (first, second):
            self.assertEqual(
                self.client.get("/api/auth/me", headers=headers).status_code,
                200)


class TestRoleBoundariesHoldWithMixedTokens(MultiAccountTestCase):
    """手里同时有三种角色的令牌时，越权仍然要被挡住。"""

    def test_tenant_token_cannot_reach_landlord_or_admin_data(self):
        tenant = auth_header(self.client, "tenant1", "test123")
        make_admin(self.client)                       # 让库里确实有个管理员
        for path in ("/api/landlord/houses", "/api/admin/users"):
            self.assertEqual(self.client.get(path, headers=tenant).status_code,
                             403, path)

    def test_landlord_token_cannot_reach_admin_data(self):
        landlord = auth_header(self.client, "landlord1", "test123")
        make_admin(self.client)
        self.assertEqual(
            self.client.get("/api/admin/users", headers=landlord).status_code,
            403)

    def test_admin_token_still_works_after_others_logged_in(self):
        admin = make_admin(self.client)
        auth_header(self.client, "landlord1", "test123")
        auth_header(self.client, "tenant1", "test123")
        self.assertEqual(
            self.client.get("/api/admin/users", headers=admin).status_code, 200)


class TestConcurrentRequests(unittest.TestCase):
    """真并发。串行测不出进程级共享状态的串号。

    ## 这一组为什么自带一个库

    `tests/base.py` 用的是内存 SQLite + `StaticPool`——**整个进程共用
    一条连接**。那是为了让每个用例拿到同一个内存库，单线程下完全正确，
    可一旦多个线程各拿一个 `Session` 同时读写，这条连接上的事务就会
    互相打断：实测三个账号并发打 `/api/auth/me`，36 次里偶尔冒出一两次
    401「请先登录」。

    **那不是产品的问题。** 同一份并发压力换成文件库 + `QueuePool`
    （每个 Session 一条连接，和线上一样）复跑，36/36 全部 200、
    身份一个都没错。所以这一组自己建一个带池的库，
    不去动共享装置——否则测出来的是装置的毛病，还会让人去"修"
    一个根本不存在的并发 bug。

    复跑那次对照实验：见日志 2026-09-22「踩的坑」。
    """

    @classmethod
    def setUpClass(cls):
        cls._dir = tempfile.mkdtemp(prefix="multiacct-")
        cls._engine = create_engine(
            "sqlite:///" + os.path.join(cls._dir, "t.db"),
            connect_args={"check_same_thread": False}, future=True)
        cls._maker = sessionmaker(bind=cls._engine, autoflush=False, future=True)
        # 换掉模块级的那两个名字：`get_session` 在调用时才查它们
        cls._saved = (db.engine, db.SessionLocal)
        db.engine, db.SessionLocal = cls._engine, cls._maker
        Base.metadata.create_all(cls._engine)

        session = cls._maker()
        try:
            for name, role in (("landlord1", "landlord"),
                               ("landlord2", "landlord"),
                               ("tenant1", "tenant")):
                session.add(User(username=name,
                                 password_hash=hash_password("test123"),
                                 real_name=name, phone="1380000%04d" % len(name),
                                 role=role))
            session.flush()
            one = session.query(User).filter_by(username="landlord1").one()
            two = session.query(User).filter_by(username="landlord2").one()
            session.add_all([
                House(house_no="DL0001", title="房东甲的房", landlord_id=one.id,
                      district="中山区", community="青泥洼桥", rent_price=2800,
                      area=70, layout="两室一厅", rent_type="整租",
                      status="available"),
                House(house_no="DL9001", title="房东乙的房", landlord_id=two.id,
                      district="沙河口区", community="黑石礁", rent_price=2600,
                      area=60, layout="两室一厅", rent_type="整租",
                      status="available"),
            ])
            session.commit()
        finally:
            session.close()
        cls._client = TestClient(fastapi_app)

    @classmethod
    def tearDownClass(cls):
        """**一定要还回去。** 不还的话，后面所有用例都会打到这个临时库上。"""
        db.engine, db.SessionLocal = cls._saved
        cls._engine.dispose()
        shutil.rmtree(cls._dir, ignore_errors=True)

    @property
    def client(self):
        return self._client

    def _hammer(self, header_by_name, path, rounds=12):
        """几个账号同时打同一个接口，收集 (账号, 响应体)。"""
        results = []
        errors = []
        lock = threading.Lock()

        def run(name, headers):
            for _ in range(rounds):
                try:
                    body = self.client.get(path, headers=headers).json()
                except Exception as exc:                     # noqa: BLE001
                    with lock:
                        errors.append("%s: %r" % (name, exc))
                    return
                with lock:
                    results.append((name, body))

        threads = [threading.Thread(target=run, args=(n, h))
                   for n, h in header_by_name.items()]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        self.assertEqual(errors, [])
        return results

    def test_concurrent_whoami_never_returns_the_wrong_person(self):
        headers = {
            "landlord1": auth_header(self.client, "landlord1", "test123"),
            "landlord2": auth_header(self.client, "landlord2", "test123"),
            "tenant1": auth_header(self.client, "tenant1", "test123"),
        }
        got = self._hammer(headers, "/api/auth/me")
        self.assertEqual(len(got), 3 * 12)
        wrong = [(n, b.get("username")) for n, b in got
                 if b.get("username") != n]
        self.assertEqual(wrong, [], "并发下 /api/auth/me 返回了别人的身份")

    def test_concurrent_landlord_houses_never_leak(self):
        headers = {
            "landlord1": auth_header(self.client, "landlord1", "test123"),
            "landlord2": auth_header(self.client, "landlord2", "test123"),
        }
        got = self._hammer(headers, "/api/landlord/houses")
        leaks = []
        for name, body in got:
            nos = {r["house_no"] for r in body.get("list", [])}
            if name == "landlord2" and nos != {"DL9001"}:
                leaks.append((name, sorted(nos)))
            if name == "landlord1" and "DL9001" in nos:
                leaks.append((name, sorted(nos)))
        self.assertEqual(leaks, [], "并发下房东看到了别人的房源")


class TestChatHistoryIsPerUser(MultiAccountTestCase):
    """对话记录：**同一个会话编号**也不能串。

    前端现在按账号分开存会话编号了，但服务端不能指望这件事——
    编号是客户端给的，撞上（或被猜到）就不该读得出别人的内容。
    """

    def _say(self, headers, session_id, content):
        return self.client.post("/api/chat-messages", headers=headers, json={
            "session_id": session_id, "role": "user", "content": content})

    def test_same_session_id_two_accounts_read_only_their_own(self):
        one = auth_header(self.client, "landlord1", "test123")
        two = auth_header(self.client, "tenant1", "test123")
        self._say(one, SHARED_SESSION, "房东说的话")
        self._say(two, SHARED_SESSION, "租客说的话")

        got_one = self.client.get("/api/chat-messages?session_id=" + SHARED_SESSION + "",
                                  headers=one).json()
        got_two = self.client.get("/api/chat-messages?session_id=" + SHARED_SESSION + "",
                                  headers=two).json()
        self.assertEqual([m["content"] for m in got_one["list"]], ["房东说的话"])
        self.assertEqual([m["content"] for m in got_two["list"]], ["租客说的话"])

    def test_clearing_one_account_does_not_clear_the_other(self):
        one = auth_header(self.client, "landlord1", "test123")
        two = auth_header(self.client, "tenant1", "test123")
        self._say(one, SHARED_SESSION, "房东说的话")
        self._say(two, SHARED_SESSION, "租客说的话")

        self.client.delete("/api/chat-messages?session_id=" + SHARED_SESSION + "", headers=one)
        left = self.client.get("/api/chat-messages?session_id=" + SHARED_SESSION + "",
                               headers=two).json()
        self.assertEqual([m["content"] for m in left["list"]], ["租客说的话"])


if __name__ == "__main__":
    unittest.main()
