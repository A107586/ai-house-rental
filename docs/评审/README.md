# 评审交付物

这一目录放**交给评审看的成品**。除需求跟踪矩阵外，其余全部由脚本生成，
**不要手改**——改了下次生成就没了，而且会和代码分叉。

| 文件 | 内容 | 怎么来的 |
|---|---|---|
| `第6小组_..._需求跟踪矩阵.xlsx` | 需求跟踪矩阵（封面 / 变更履历 / 设计用 RTM 300 行 / 界面图 7 张） | 手工维护 |
| `第6小组_..._概要设计.docx` | 概要设计说明书（38 页、36 张表） | `python scripts/gen_outline_docx.py`，源是 `docs/概要设计说明书.md` |
| `第6小组_..._数据库设计.xlsx` | 数据库设计书（封面 / 说明 / 表清单 / 需求追溯 / 36 张表各一页） | `python scripts/gen_db_xlsx.py` |
| `第6小组_..._建库脚本_MySQL.sql` | MySQL 8 建库脚本，带表与字段注释 | `python scripts/gen_db_sql.py` |
| `第6小组_..._建库脚本_SQLite.sql` | SQLite 建库脚本（**实际在跑的那份结构**） | 同上 |

## 一条命令全部重新生成

```bash
python scripts/gen_db_doc.py && python scripts/gen_db_sql.py && python scripts/gen_db_xlsx.py && python scripts/gen_api_list.py && python scripts/gen_outline_docx.py
```

## 改了代码之后会怎样

**不用记得重新生成，测试会提醒。** `server/tests/test_design_artifacts.py`
把四个生成脚本的 `--check` 都跑一遍：模型加了字段、路由加了接口而交付物
没更新，测试直接红。

它还会**校验概要设计正文里引用的每一条接口真的存在**——
这一条抓到过三条写错的路径，其中一条连 HTTP 方法都是错的。

## 源在哪

| 交付物 | 源 |
|---|---|
| 表结构、类型、非空、主外键 | `server/app/models.py`（SQLAlchemy 模型） |
| 表的中文名、用途、字段说明、所属模块、对应需求 | `scripts/db_meta.py` |
| 概要设计的正文 | `docs/概要设计说明书.md` |
| 接口清单与模块统计表 | 两个 FastAPI 应用的路由表（生成时现问） |

**没有第二份表结构。** 交付要求「能导出 SQL 直接建库」，做法是
从模型生成 SQL，而不是另画一份设计稿再导出——两个源必然分叉，
而分叉的那一天没人会收到通知。

两份建库脚本都**真的跑过**：SQLite 建完逐表逐字段与模型比对一致；
MySQL 起了一个临时 mysqld 跑完整份脚本，可重复执行。
