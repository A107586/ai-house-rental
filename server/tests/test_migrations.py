"""数据库迁移测试（待完善清单 B-5）。

`create_all` 只建缺失的表，不会给已存在的表加列——本项目已经因此让全组
删过一次库。这套迁移要保证组员拉了新代码之后**不用删库**。

**本该用 Alembic**，但这台机器 pip 到不了任何可用的源。这里实现的是它的
三个核心性质，这批用例就是在守这三条。
"""

import os
import sqlite3
import tempfile

from sqlalchemy import create_engine

from app import migrations
from tests.base import ApiTestCase


class MigrationCase(ApiTestCase):

    def setUp(self):
        super().setUp()
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        os.unlink(self.path)
        self.url = "sqlite:///" + self.path.replace("\\", "/")
        self.engine = create_engine(self.url, future=True)

    def tearDown(self):
        self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.path + suffix)
            except OSError:
                pass
        super().tearDown()

    def raw(self):
        return sqlite3.connect(self.path)

    def make_legacy_users(self):
        """造一张「旧版」users 表——只有加字段之前的那几列。"""
        conn = self.raw()
        conn.execute("CREATE TABLE users ("
                     " id INTEGER PRIMARY KEY,"
                     " username VARCHAR(50),"
                     " password_hash VARCHAR(255))")
        conn.execute("INSERT INTO users (username, password_hash) "
                     "VALUES ('old_user', 'x')")
        conn.commit()
        conn.close()

    def columns(self, table):
        conn = self.raw()
        try:
            return [row[1] for row in conn.execute(
                "PRAGMA table_info(%s)" % table)]
        finally:
            conn.close()


class TestUpgrade(MigrationCase):

    def test_adds_missing_columns_without_dropping_data(self):
        """这是整套迁移存在的理由：加列而不丢数据。"""
        self.make_legacy_users()
        migrations.upgrade(self.engine, self.url)

        columns = self.columns("users")
        self.assertIn("id_card_hash", columns)
        self.assertIn("token_valid_after", columns)

        conn = self.raw()
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)
            self.assertEqual(
                conn.execute("SELECT username FROM users").fetchone()[0],
                "old_user")
        finally:
            conn.close()

    def test_runs_each_step_once(self):
        self.make_legacy_users()
        first = migrations.upgrade(self.engine, self.url)
        second = migrations.upgrade(self.engine, self.url)
        self.assertTrue(first)
        self.assertEqual(second, [], "已执行过的步骤不该重复执行")

    def test_records_what_was_applied(self):
        self.make_legacy_users()
        migrations.upgrade(self.engine, self.url)
        conn = self.raw()
        try:
            rows = conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version").fetchall()
        finally:
            conn.close()
        self.assertEqual([r[0] for r in rows],
                         [v for v, _, _ in migrations._STEPS])

    def test_idempotent_even_if_record_table_is_lost(self):
        """记录表被清掉后重跑也不能炸——每步自己还要做一次存在性判断。

        误删 schema_migrations 是真会发生的事（手工排障时最容易），
        那时重跑不该把整个服务启动流程打挂。
        """
        self.make_legacy_users()
        migrations.upgrade(self.engine, self.url)
        conn = self.raw()
        conn.execute("DROP TABLE schema_migrations")
        conn.commit()
        conn.close()

        # 不抛异常
        migrations.upgrade(self.engine, self.url)
        self.assertIn("id_card_hash", self.columns("users"))

    def test_missing_table_is_skipped_not_crashed(self):
        """表根本不存在时整步跳过。

        原先 _columns 表不存在时返回空集合，于是「列不在空集合里」成立，
        照样去 ALTER 一张不存在的表，直接报 no such table。
        全新库或后加的表都会踩到。
        """
        # 空库，一张表都没有
        applied = migrations.upgrade(self.engine, self.url)
        self.assertTrue(applied)
        self.assertEqual(self.columns("users"), [])

    def test_pending_reports_remaining(self):
        self.make_legacy_users()
        self.assertEqual(migrations.pending(self.engine, self.url),
                         [v for v, _, _ in migrations._STEPS])
        migrations.upgrade(self.engine, self.url)
        self.assertEqual(migrations.pending(self.engine, self.url), [])

    def test_non_sqlite_is_left_alone(self):
        """MySQL 的表结构由 DBA 或正式迁移工具管，
        **应用启动时自动改生产库是很危险的**——一次误发布就可能改坏数据。
        """
        self.assertEqual(
            migrations.upgrade(self.engine, "mysql+pymysql://x/y"), [])
        self.assertEqual(
            migrations.pending(self.engine, "mysql+pymysql://x/y"), [])


class TestStepRegistry(MigrationCase):

    def test_versions_are_unique_and_sorted(self):
        """版本号一旦发布就不能改。重复或乱序都会让各台机器结构不一致。"""
        versions = [v for v, _, _ in migrations._STEPS]
        self.assertEqual(len(versions), len(set(versions)), "版本号重复")
        self.assertEqual(versions, sorted(versions), "版本号必须递增")

    def test_every_step_has_a_description(self):
        for version, description, _ in migrations._STEPS:
            self.assertTrue(description.strip(), version)
