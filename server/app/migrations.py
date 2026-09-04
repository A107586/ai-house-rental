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


@step("0005", "复合索引：房源筛选、账单、会话消息")
def _composite_indexes(connection, engine):
    """把热点查询真正走上索引。

    实测房源筛选的执行计划原先是：

        SEARCH houses USING INDEX ix_houses_status (status=?)
        USE TEMP B-TREE FOR ORDER BY

    只用上了 status 单列索引，district / rent_price 全靠回表过滤，
    排序还要另建一棵临时 B 树。单列索引在这种「多条件 + 排序」的查询里
    基本只起到第一层筛选的作用。

    列的顺序有讲究：**等值条件在前、范围条件在后、排序列最后**。
    status 与 district 是等值匹配，rent_price 是区间，所以按这个顺序建；
    反过来（把 rent_price 放前面）后面的等值条件就用不上索引了。
    """
    statements = [
        # 房源筛选：区域 + 价格区间，最常用的一条路径
        "CREATE INDEX IF NOT EXISTS ix_houses_filter "
        "ON houses (status, district, rent_price)",
        # 房源列表默认按发布时间倒序，单独给排序一条
        "CREATE INDEX IF NOT EXISTS ix_houses_status_published "
        "ON houses (status, published_at)",
        # 账单：查「某合同的账单」与「未缴/逾期」都很频繁
        "CREATE INDEX IF NOT EXISTS ix_bills_contract_status "
        "ON bills (contract_id, status)",
        "CREATE INDEX IF NOT EXISTS ix_bills_status_due "
        "ON bills (status, due_date)",
        # 工单按房源 + 状态查
        "CREATE INDEX IF NOT EXISTS ix_repairs_house_status "
        "ON repairs (house_id, status)",
        # 会话消息的轮询查的是 WHERE conversation_id=? AND id>?。
        # **这是会长得最快的表**，现在只有单列索引
        "CREATE INDEX IF NOT EXISTS ix_chat_lines_conv_id "
        "ON chat_lines (conversation_id, id)",
        # 预约的时段冲突判断
        "CREATE INDEX IF NOT EXISTS ix_appointments_house_time "
        "ON appointments (house_id, viewing_time)",
    ]
    for statement in statements:
        table = statement.split(" ON ")[1].split(" ")[0]
        if _table_exists(engine, table):
            connection.execute(text(statement))


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


@step("0006", "支付表：三段式支付的网关与回调字段（D-1）")
def _payments_gateway(connection, engine):
    """把「点一下就算付了」改成「发起 → 回调 → 确认」需要的六列。

    **老数据必须显式回填成 succeeded。** 模型层的默认值改成了 pending，
    但改造之前建出来的流水，钱在这个项目的语义下是真收到了的
    （账单已经是 paid）。不回填的话历史流水会集体显示「等待付款」——
    账单说已付、流水说没付，对账直接对不上，而且这种不一致
    在本地是看不出来的（本地库是新建的，根本没有历史流水）。
    """
    if not _table_exists(engine, "payments"):
        return
    _add_column(connection, engine, "payments", "gateway",
                "VARCHAR(20) NOT NULL DEFAULT 'mock'")
    _add_column(connection, engine, "payments", "gateway_trade_no", "VARCHAR(64)")
    _add_column(connection, engine, "payments", "notify_payload",
                "TEXT NOT NULL DEFAULT ''")
    _add_column(connection, engine, "payments", "notified_at", "DATETIME")
    _add_column(connection, engine, "payments", "expire_at", "DATETIME")
    _add_column(connection, engine, "payments", "created_at", "DATETIME")

    # 历史流水回填：状态置终态，created_at 用 paid_at 兜底
    connection.execute(text(
        "UPDATE payments SET status='succeeded' "
        "WHERE status IS NULL OR status='' OR status='pending'"))
    connection.execute(text(
        "UPDATE payments SET created_at=paid_at WHERE created_at IS NULL"))
    connection.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_payments_gateway_trade_no "
        "ON payments (gateway_trade_no)"))


@step("0007", "房源表：筛选维度、费用、坐标与核验字段（K-04）")
def _houses_facets(connection, engine):
    """把「电梯」「精装」这些从 tags 字符串里拆成独立列。

    原先它们全揉在逗号分隔的 `tags` 里，用 `tags LIKE '%电梯%'` 去筛，
    **「无电梯」会被「电梯」命中**——这不是没做筛选，是做了会错。
    高级筛选、费用明细、地图打点、房源核验四件事共用这一组字段，
    字段不落库，四件事都做不出来。

    存量房源的处理：新列一律给中性默认值（未填/未核验），
    **不去猜**。从 `tags` 里反推「有没有电梯」看着聪明，实际上
    「电梯」和「无电梯」两种写法都可能出现在旧数据里，猜错了
    比留空更糟——留空前端显示「暂无」，猜错就是明确的错误信息。
    """
    if not _table_exists(engine, "houses"):
        return

    columns = (
        ("elevator", "BOOLEAN NOT NULL DEFAULT 0"),
        ("decoration", "VARCHAR(20) NOT NULL DEFAULT ''"),
        ("floor_no", "INTEGER"),
        ("floor_total", "INTEGER"),
        ("heating", "VARCHAR(20) NOT NULL DEFAULT ''"),
        ("available_from", "DATE"),
        ("min_months", "INTEGER NOT NULL DEFAULT 1"),
        ("deposit_months", "INTEGER"),
        ("pay_months", "INTEGER"),
        ("service_fee", "NUMERIC(10, 2) NOT NULL DEFAULT 0"),
        ("business_area", "VARCHAR(30) NOT NULL DEFAULT ''"),
        ("lng", "FLOAT"),
        ("lat", "FLOAT"),
        ("verify_status", "VARCHAR(20) NOT NULL DEFAULT 'unverified'"),
        ("verify_no", "VARCHAR(32) NOT NULL DEFAULT ''"),
        ("verified_at", "DATETIME"),
        ("verify_remark", "VARCHAR(200) NOT NULL DEFAULT ''"),
        ("maintained_at", "DATETIME"),
    )
    for name, definition in columns:
        _add_column(connection, engine, "houses", name, definition)

    # 维护时间回填成发布时间。留空的话所有存量房源都显示「暂无维护记录」，
    # 而它们其实是有一个明确时间点的——发布那天。
    connection.execute(text(
        "UPDATE houses SET maintained_at = published_at "
        "WHERE maintained_at IS NULL"))

    for statement in (
        "CREATE INDEX IF NOT EXISTS ix_houses_elevator ON houses (elevator)",
        "CREATE INDEX IF NOT EXISTS ix_houses_decoration ON houses (decoration)",
        "CREATE INDEX IF NOT EXISTS ix_houses_available_from "
        "ON houses (available_from)",
        "CREATE INDEX IF NOT EXISTS ix_houses_business_area "
        "ON houses (business_area)",
        "CREATE INDEX IF NOT EXISTS ix_houses_verify_status "
        "ON houses (verify_status)",
        "CREATE INDEX IF NOT EXISTS ix_houses_verify_no ON houses (verify_no)",
    ):
        connection.execute(text(statement))


@step("0008", "相册表：图片尺寸、体积、内容哈希与审核状态（K-04）")
def _house_images_meta(connection, engine):
    """图片元数据。

    `audit_status` 默认 approved 而不是 pending：本项目的图片审核是
    **事后**的——先展示，被举报或被管理员发现再下架。默认 pending
    会让所有存量图片一夜之间从页面上消失。
    """
    if not _table_exists(engine, "house_images"):
        return
    _add_column(connection, engine, "house_images", "width",
                "INTEGER NOT NULL DEFAULT 0")
    _add_column(connection, engine, "house_images", "height",
                "INTEGER NOT NULL DEFAULT 0")
    _add_column(connection, engine, "house_images", "file_size",
                "INTEGER NOT NULL DEFAULT 0")
    _add_column(connection, engine, "house_images", "hash",
                "VARCHAR(64) NOT NULL DEFAULT ''")
    _add_column(connection, engine, "house_images", "audit_status",
                "VARCHAR(20) NOT NULL DEFAULT 'approved'")
    connection.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_house_images_hash "
        "ON house_images (hash)"))


@step("0009", "房源表：坐标来源与地图边界索引（地图找房）")
def _houses_location_source(connection, engine):
    """接入高德之后补的两列，外加一条按视野取点的索引。

    **经纬度本身在 0007 就加了**（K-04），这里只补「这坐标是怎么来的」。
    地理编码出来的坐标能精确到门牌，而演示库的门牌号是随机生成的、
    只能按商圈中心做稳定偏移——两者精度差一个数量级，界面上要不要
    标「大致位置」、能不能拿它算通勤距离，全看这个字段。

    **步骤号是 0009 而不是 0006。** 同事那版原本写的是 0006，
    但 0006 已经被三段式支付（D-1）占了：`schema_migrations` 里一旦
    记下 0006，这一步就会被当成执行过而**永远跳过**，坐标列建不出来，
    地图接口在老库上直接 500。版本号一经发布不能重排，只能往后追加。

    索引放 (status, lng, lat)：地图取点永远带着「只看可租的」这个前提，
    等值条件在前、范围条件在后。
    """
    if not _table_exists(engine, "houses"):
        return
    _add_column(connection, engine, "houses", "location_source",
                "VARCHAR(20) NOT NULL DEFAULT ''")
    _add_column(connection, engine, "houses", "geocoded_at", "DATETIME")
    for statement in (
        "CREATE INDEX IF NOT EXISTS ix_houses_map_bounds "
        "ON houses (status, lng, lat)",
        "CREATE INDEX IF NOT EXISTS ix_houses_lng ON houses (lng)",
        "CREATE INDEX IF NOT EXISTS ix_houses_lat ON houses (lat)",
    ):
        connection.execute(text(statement))
