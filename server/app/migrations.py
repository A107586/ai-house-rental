"""轻量数据库迁移（待完善清单 B-5）。

`create_all` 只建缺失的表，**不会给已存在的表加列**。所以每次改模型，
组员要么删库重建（丢掉本地测试数据），要么手工补一句 ALTER TABLE。
本项目已经因此让全组删过一次库。

**为什么不是 Alembic。** 本该用它，但这台机器装不上——pip 到不了任何
可用的源（`Could not find a version that satisfies the requirement`）。
在联网环境里应当换成 Alembic，接口是兼容的：把下面 `STEPS` 里的每一项
翻译成一个 revision 即可。

这里实现的是 Alembic 的三个核心性质，够用且零依赖：

1. **有序**——按列表顺序执行，后面的可以依赖前面的
2. **只跑一次**——`schema_migrations` 表记下已执行的版本号
3. **幂等**——每步自己也做存在性判断，即使记录表被清掉重跑也不会炸

新增一次改表时：在 `STEPS` 末尾追加一项，**不要修改或删除已有项**——
已经跑过的机器不会重跑它们，改了只会造成各台机器结构不一致。
"""

from sqlalchemy import inspect, text

# 每项为 (版本号, 说明, 执行函数)。版本号一旦发布就不能再改。
_STEPS = []


def step(version, description):
    def register(func):
        _STEPS.append((version, description, func))
        return func
    return register


def _columns(engine, table):
    """表的列名集合。**表不存在时返回 None，不是空集合。**

    这两者必须分开：空集合表示「表在、但没有这一列」，该执行 ALTER；
    None 表示「表根本不存在」，该整步跳过。混为一谈会去 ALTER 一张
    不存在的表，直接报 no such table。
    """
    try:
        return {c["name"] for c in inspect(engine).get_columns(table)}
    except Exception:
        return None


def _add_column(connection, engine, table, name, definition):
    columns = _columns(engine, table)
    if columns is None:
        # 表还没建（全新库尚未 create_all，或这张表是后加的）——
        # 建表时字段本来就带上了，无需补
        return
    if name not in columns:
        connection.execute(
            text("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, definition)))


# ---------------------------------------------------------------- 迁移步骤

def _table_exists(engine, table):
    return _columns(engine, table) is not None


@step("0001", "用户表：身份证哈希与掩码（R-02）")
def _users_id_card(connection, engine):
    if not _table_exists(engine, "users"):
        return
    _add_column(connection, engine, "users", "id_card_hash",
                "VARCHAR(64) NOT NULL DEFAULT ''")
    _add_column(connection, engine, "users", "id_card_masked",
                "VARCHAR(32) NOT NULL DEFAULT ''")
    connection.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_users_id_card_hash "
        "ON users (id_card_hash)"))


@step("0002", "支付表：状态、幂等键与唯一索引")
def _payments_idempotency(connection, engine):
    if not _table_exists(engine, "payments"):
        return
    _add_column(connection, engine, "payments", "status",
                "VARCHAR(20) NOT NULL DEFAULT 'succeeded'")
    _add_column(connection, engine, "payments", "idempotency_key",
                "VARCHAR(64)")
    _add_column(connection, engine, "payments", "failure_reason",
                "VARCHAR(200) NOT NULL DEFAULT ''")
    connection.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_payments_bill_id "
        "ON payments (bill_id)"))
    # 部分索引：idempotency_key 允许为空，而多个 NULL 不该互相冲突
    connection.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_payments_idempotency_key "
        "ON payments (idempotency_key) WHERE idempotency_key IS NOT NULL"))


@step("0003", "用户表：令牌失效基线（B-2）")
def _users_token_baseline(connection, engine):
    # 允许为空——空表示从未吊销过。存量用户的令牌不该因为加了这个字段
    # 就集体失效
    _add_column(connection, engine, "users", "token_valid_after", "DATETIME")


@step("0004", "评价表：软删除（C-5）")
def _reviews_soft_delete(connection, engine):
    _add_column(connection, engine, "reviews", "deleted_at", "DATETIME")


# ---------------------------------------------------------------- 执行

def _applied(engine, connection):
    connection.execute(text(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version VARCHAR(20) PRIMARY KEY,"
        " description VARCHAR(200),"
        " applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"))
    rows = connection.execute(text("SELECT version FROM schema_migrations"))
    return {row[0] for row in rows}


def upgrade(engine, database_url):
    """把库升到最新。返回本次执行了哪些版本号。

    只对 SQLite 生效。部署到 MySQL 时表结构由 DBA 或正式的迁移工具管理，
    应用启动时自动改生产库的表**是很危险的**——一次误发布就可能改坏数据。
    """
    if not database_url.startswith("sqlite"):
        return []

    executed = []
    with engine.begin() as connection:
        done = _applied(engine, connection)
        for version, description, func in _STEPS:
            if version in done:
                continue
            func(connection, engine)
            connection.execute(
                text("INSERT INTO schema_migrations (version, description) "
                     "VALUES (:v, :d)"), {"v": version, "d": description})
            executed.append(version)
    return executed


def pending(engine, database_url):
    """还没执行的版本号。供健康检查与排障使用。"""
    if not database_url.startswith("sqlite"):
        return []
    with engine.begin() as connection:
        done = _applied(engine, connection)
    return [v for v, _, _ in _STEPS if v not in done]
