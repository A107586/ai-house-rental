"""数据库连接。

本地开发默认用 SQLite——零安装、无密码、开箱即用，组员拉下代码就能跑。
部署到阿里云时改 `DATABASE_URL` 环境变量即可切到 MySQL，业务代码不用动：

    DATABASE_URL=mysql+pymysql://user:password@host:3306/rental?charset=utf8mb4
"""

import os

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker

from app.models import Base

DEFAULT_SQLITE = "sqlite:///" + os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "rental.db")

DATABASE_URL = os.environ.get("DATABASE_URL") or DEFAULT_SQLITE

# SQLite 默认禁止跨线程共享连接，而 FastAPI 的同步接口跑在线程池里
_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

# 连接池。**默认值在 100 并发下会直接把服务打垮**——
# SQLAlchemy 默认 pool_size=5 + max_overflow=10，上限只有 15 条连接，
# pool_timeout 是 30 秒。实测 100 并发时 85 个请求排队等满 30 秒后失败，
# 成功率一度只有 1/100，而需求 N-01 要求「100 并发下响应 < 1s」。
#
# pool_timeout 特意压到 5 秒：**排队 30 秒才失败是最糟的结果**——
# 用户早就走了，连接还占着不放。快速失败反而让系统更健康，
# 前端也能及时给出「服务繁忙」而不是干等着转圈。
#
# pool_pre_ping 防的是另一件事：MySQL 会掐掉空闲太久的连接，
# 池子里留着的就成了死连接，取出来第一次用才报错。
#
# 注意这些参数**不能无条件传**：内存库（测试用的 sqlite://）走的是
# SingletonThreadPool，它压根不接受 max_overflow / pool_timeout，
# 传了会在 create_engine 阶段直接抛 TypeError，整套测试都跑不起来。
_pool_options = {}
if DATABASE_URL != "sqlite://" and not DATABASE_URL.endswith(":memory:"):
    _pool_options = {
        "pool_size": 20,
        "max_overflow": 40,
        "pool_timeout": 5,
        "pool_pre_ping": True,
    }

engine = create_engine(DATABASE_URL, connect_args=_connect_args, future=True,
                       **_pool_options)


if DATABASE_URL.startswith("sqlite") and not DATABASE_URL.endswith(":memory:") \
        and DATABASE_URL != "sqlite://":

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, connection_record):
        """SQLite 的并发调优。只对文件库生效，内存库（测试用）跳过。

        默认的回滚日志模式下，**任何一个写事务都会把所有读者挡在门外**。
        房源详情接口每次访问都要写（浏览量 +1、记一条足迹），
        在并发下就成了整个系统的串行瓶颈——实测 100 并发时详情接口
        最慢一次要 1.4 秒，而它本身只是查一行数据。

        WAL 模式把写入记到单独的日志里，读者不再被写者阻塞。
        synchronous=NORMAL 在 WAL 下是官方推荐值：仍能保证数据库不损坏，
        只是极端断电时可能丢掉最后几个事务——对本项目可以接受。
        """
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        # 拿不到锁时等待而不是立刻报 database is locked
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, future=True)


def init_db():
    """建表。已存在的表不会被改动。"""
    Base.metadata.create_all(engine)
    _upgrade_sqlite_schema()


def _upgrade_sqlite_schema():
    """Apply small, backwards-compatible upgrades to existing SQLite databases.

    ``create_all`` creates missing tables but does not add columns to tables that
    already exist.  Keep the local demo database usable after pulling a newer
    version of the project without deleting the user's test data.
    """
    if not DATABASE_URL.startswith("sqlite"):
        return

    columns = {column["name"] for column in inspect(engine).get_columns("users")}
    additions = {
        "id_card_hash": "VARCHAR(64) NOT NULL DEFAULT ''",
        "id_card_masked": "VARCHAR(32) NOT NULL DEFAULT ''",
    }
    with engine.begin() as connection:
        for name, definition in additions.items():
            if name not in columns:
                connection.execute(
                    text(f"ALTER TABLE users ADD COLUMN {name} {definition}"))
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_users_id_card_hash "
            "ON users (id_card_hash)"))

        payment_columns = {
            column["name"] for column in inspect(engine).get_columns("payments")
        }
        payment_additions = {
            "status": "VARCHAR(20) NOT NULL DEFAULT 'succeeded'",
            "idempotency_key": "VARCHAR(64)",
            "failure_reason": "VARCHAR(200) NOT NULL DEFAULT ''",
        }
        for name, definition in payment_additions.items():
            if name not in payment_columns:
                connection.execute(text(
                    f"ALTER TABLE payments ADD COLUMN {name} {definition}"))
        connection.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_payments_bill_id "
            "ON payments (bill_id)"))
        connection.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_payments_idempotency_key "
            "ON payments (idempotency_key) WHERE idempotency_key IS NOT NULL"))


def get_session():
    """FastAPI 依赖：每个请求一个会话，用完即关。"""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
