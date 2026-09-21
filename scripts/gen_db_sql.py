# -*- coding: utf-8 -*-
"""从 models.py 生成可直接建库的 DDL。

    python scripts/gen_db_sql.py            # 生成
    python scripts/gen_db_sql.py --check    # 只检查有没有过期（CI / 测试用）

## 为什么不画 ER 图再导出

课程给的参考做法是「在工具里画表 → 导出 SQL」（ER-Studio 或那份
`shengyadb.xls`）。**那套流程有一个前提：设计稿是唯一的源。**
我们这个项目不满足——表结构早就在 `server/app/models.py` 里跑着了，
测试、接口、迁移全都依赖它。这时候再摆一份设计稿进来，就有了两个源，
而两个源必然分叉：**这次生成文档时就发现，文档里少了 `role_requests`
整张表，还少了 `users.accept_contact`、`houses.building/unit`、
`reviews` 的五个字段**——都是模型改了、文档没跟上。

所以方向反过来：**模型是唯一的源，SQL 和文档都从它生成。**
交付要求「能导出 SQL 直接建库」照样满足，而且多一条保证——
导出的 SQL 与正在跑的代码永远是同一份结构。

## 两份 SQL，分别是什么

- **SQLite**：`server/app/db.py` 实际在用的引擎，也是 `seed.py` 播种、
  测试套件跑的那一个。**这份是真在跑的结构。**
- **MySQL 8**：交付格式（对标 `ticketServer.sql`），InnoDB + utf8mb4，
  表注释与字段注释都写进 `COMMENT`。生成后**真的起了一个 mysqld 跑过**，
  不是「看着像能跑」。

## 有意不写进 DDL 的东西

**列默认值。** 模型里的 `default=datetime.now`、`default=""` 是
**Python 侧**的默认值，由 ORM 在插入时填。DDL 里不写它们是故意的：
写了就等于多出一份由数据库执行的语义，两边规则不一致时，
到底谁说了算会变成一个没人能回答的问题。建库脚本负责结构，
插数据的事归应用。
"""

import argparse
import io
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db_meta import (COLUMN_NOTES, ROOT, TABLE_INFO,  # noqa: E402
                     load_metadata, ordered_tables)

from sqlalchemy.dialects import mysql, sqlite  # noqa: E402
from sqlalchemy.schema import CreateIndex, CreateTable  # noqa: E402

OUT_DIR = os.path.join(ROOT, "docs", "评审")
MYSQL_SQL = os.path.join(OUT_DIR, "第6小组_基于AI智能体的房屋租赁系统_建库脚本_MySQL.sql")
SQLITE_SQL = os.path.join(OUT_DIR, "第6小组_基于AI智能体的房屋租赁系统_建库脚本_SQLite.sql")
DB_NAME = "rental"

# MySQL 的注释上限是 1024 字符（表注释 2048）。中文一个字算一个字符，
# 留出余量截到 500——注释是给人看的提示，不是把设计文档塞进库里。
MAX_COMMENT = 500


def plain(text):
    """把 Markdown 味道去掉，压成一行，能放进 SQL 注释。

    说明里带着 `**粗体**`、反引号、换行，直接塞进 COMMENT 会很难读，
    引号还会把语句截断。
    """
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text or "")
    text = text.replace("`", "").replace("\n", " ")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > MAX_COMMENT:
        text = text[:MAX_COMMENT - 1] + "…"
    return text


def annotate(metadata):
    """把中文名与说明挂到元数据上，让方言自己渲染成 COMMENT。

    只在生成脚本的进程里改，**不动 models.py**：注释是文档层的事，
    让它去污染模型定义，模型就变成了半份文档。
    """
    for table in metadata.sorted_tables:
        cn, purpose = TABLE_INFO.get(table.name, (table.name, ""))
        table.comment = plain("%s — %s" % (cn, purpose) if purpose else cn)
        for column in table.columns:
            note = COLUMN_NOTES.get((table.name, column.name))
            if note:
                column.comment = plain(note)


def ddl(metadata, dialect, with_comment):
    """按建表依赖顺序生成 DDL。

    用 `sorted_tables`（依赖拓扑序）而不是业务顺序：**被引用的表必须先建**，
    否则外键指向一张还不存在的表。文档里的顺序是给人读的，这里的是给库跑的。
    """
    parts = []
    for table in metadata.sorted_tables:
        statement = str(CreateTable(table).compile(dialect=dialect)).strip()
        if with_comment:
            # 方言把表注释紧贴在右括号后面（`)COMMENT='…'`），能跑但难读
            statement = statement.replace(")COMMENT=", ") COMMENT=")
            statement += (" ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 "
                          "COLLATE=utf8mb4_unicode_ci")
        parts.append(statement + ";")
        for index in sorted(table.indexes, key=lambda i: i.name):
            parts.append(str(CreateIndex(index).compile(dialect=dialect)).strip()
                         + ";")
    return parts


def header(engine, table_count, column_count):
    cn = {"mysql": "MySQL 8.0+", "sqlite": "SQLite 3"}[engine]
    return [
        "-- 基于 AI 智能体的房屋租赁系统（大连智租） 建库脚本 · %s" % cn,
        "-- 大连理工大学 实习实训 第 6 小组",
        "--",
        "-- **这个文件是生成的，不要手改。** 改了也会被下一次生成覆盖，",
        "-- 而且会和真正在跑的结构分叉。",
        "--   源：server/app/models.py（SQLAlchemy 模型）",
        "--   生成：python scripts/gen_db_sql.py",
        "--   校验：python scripts/gen_db_sql.py --check（测试套件里也跑这一条）",
        "--",
        "-- 共 %d 张表、%d 个字段。建表顺序是依赖拓扑序，被引用的表先建。" % (
            table_count, column_count),
        "--",
        "-- 列默认值有意不写进 DDL：模型里的 default 是 Python 侧的，由 ORM 插入时填。",
        "-- 写进来就等于多一份由数据库执行的语义，两边不一致时谁说了算会说不清。",
        "",
    ]


def build_mysql(metadata):
    tables = metadata.sorted_tables
    lines = header("mysql", len(tables),
                   sum(len(t.columns) for t in tables))
    lines += [
        "CREATE DATABASE IF NOT EXISTS `%s`" % DB_NAME,
        "  DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;",
        "USE `%s`;" % DB_NAME,
        "",
        "-- 重复执行时先删干净。关掉外键检查是因为删除顺序与建表顺序相反，",
        "-- 逐张删会被外键挡住。",
        "SET FOREIGN_KEY_CHECKS = 0;",
    ]
    for table in reversed(tables):
        lines.append("DROP TABLE IF EXISTS `%s`;" % table.name)
    lines += ["SET FOREIGN_KEY_CHECKS = 1;", ""]
    for statement in ddl(metadata, mysql.dialect(), with_comment=True):
        lines += [statement, ""]
    return "\n".join(lines).rstrip() + "\n"


def build_sqlite(metadata):
    tables = metadata.sorted_tables
    lines = header("sqlite", len(tables),
                   sum(len(t.columns) for t in tables))
    lines += [
        "-- SQLite 默认不校验外键，必须每个连接都打开一次。",
        "-- 应用侧在 db.py 的 connect 事件里做了同样的事。",
        "PRAGMA foreign_keys = ON;",
        "",
    ]
    for table in reversed(tables):
        lines.append("DROP TABLE IF EXISTS \"%s\";" % table.name)
    lines.append("")
    for statement in ddl(metadata, sqlite.dialect(), with_comment=False):
        lines += [statement, ""]
    return "\n".join(lines).rstrip() + "\n"


def write(path, text, check_only):
    """写文件；`--check` 时只比对，不落盘。

    比对这一步是给测试用的：**模型改了而没重新生成，测试就该红**，
    不能靠人记得。
    """
    old = None
    if os.path.exists(path):
        old = io.open(path, encoding="utf-8").read()
    if check_only:
        return old == text
    if old != text:
        io.open(path, "w", encoding="utf-8", newline="\n").write(text)
    return True


def main():
    parser = argparse.ArgumentParser(description="生成可直接建库的 DDL")
    parser.add_argument("--check", action="store_true",
                        help="只检查文件是不是最新的，不写盘")
    args = parser.parse_args()

    metadata = load_metadata()
    annotate(metadata)
    ordered_tables(metadata)          # 顺带校验：每张表都登记过中文名

    results = [
        (MYSQL_SQL, build_mysql(metadata)),
        (SQLITE_SQL, build_sqlite(metadata)),
    ]
    stale = []
    for path, text in results:
        if not write(path, text, args.check):
            stale.append(os.path.basename(path))

    if args.check:
        if stale:
            print("建库脚本过期了：%s" % "、".join(stale))
            print("模型改过之后要重新生成：python scripts/gen_db_sql.py")
            return 1
        print("建库脚本是最新的。")
        return 0

    for path, text in results:
        print("生成 %s（%d 行）" % (os.path.basename(path),
                                    text.count("\n")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
