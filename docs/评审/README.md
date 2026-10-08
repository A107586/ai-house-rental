# 评审交付物

这一目录放**交给评审看的成品**。除需求跟踪矩阵外，其余全部由脚本生成，
**不要手改**——改了下次生成就没了，而且会和代码分叉。

| 文件 | 内容 | 怎么来的 |
|---|---|---|
| `第6小组_..._需求跟踪矩阵.xlsx` | 需求跟踪矩阵（封面 / 变更履历 / 设计用 RTM 300 行 / 界面图 7 张） | 手工维护 |
| `第6小组_..._概要设计.docx` | 概要设计说明书（59 页、44 张表、24 张图：分层系统结构图、10 张模块结构图、11 张模块类图、2 张 E-R 图；目录带页码，封面签署栏的页数由 Word 分页后填） | `python scripts/gen_outline_docx.py --update-toc`，源是 `docs/概要设计说明书.md` 与 `docs/图/` |
| `第6小组_..._数据库设计.xlsx` | 数据库设计书（封面 / 说明 / 表清单 / **E-R 图** / 需求追溯 / 36 张表各一页；全部宋体 12 号，同参考工作簿） | `python scripts/gen_db_xlsx.py` |
| `第6小组_..._建库脚本_MySQL.sql` | MySQL 8 建库脚本，带表与字段注释 | `python scripts/gen_db_sql.py` |
| `第6小组_..._建库脚本_SQLite.sql` | SQLite 建库脚本（**实际在跑的那份结构**） | 同上 |

与课程给的参考文件的对应关系：

| 参考文件 | 本目录里对应的 |
|---|---|
| `概要设计模板.doc` / `概要设计demo.doc` | 概要设计.docx：章节逐条对齐模板；模板要求的「系统结构图」、每个模块的「模块结构（图形描述）」与「类图」、「数据库表间关系图」都有。**版式也照模板**：页边距、封面与签署栏、文件修改控制、目录、页眉，正文宋体五号，标题宋体二号 / 黑体三号 / 宋体三号，图题表题「图2-1  …」的写法。不含任何日文字体（python-docx 底板自带的日文主题与字体已去掉） |
| `shengyadb.xls`（每表一页的数据库设计） | 数据库设计.xlsx：同一版式，改动三处写在它的「说明」页；另加一页 E-R 图 |
| `ticketServer.sql`（建库脚本） | 建库脚本_MySQL.sql：同样 `DROP TABLE IF EXISTS` + 表与字段 `COMMENT`，另有 60 个真外键、utf8mb4 |

## 一条命令全部重新生成

```bash
python scripts/gen_diagrams.py && python scripts/gen_db_doc.py && python scripts/gen_db_sql.py && python scripts/gen_db_xlsx.py && python scripts/gen_api_list.py && python scripts/gen_test_counts.py && python scripts/gen_outline_docx.py --update-toc
```

**顺序有讲究**：图要先出（数据库设计书与 Word 版都要贴图），
Word 版要最后出（它读的 .md 会被前面几个脚本改写）。

`gen_diagrams.py` 渲染图要用 mermaid-cli（Node），第一次先装：

```bash
cd scripts && set PUPPETEER_SKIP_DOWNLOAD=1 && npm install
```

浏览器用本机的 Edge 或 Chrome，不另下 Chromium。

**渲染要 Node ≥ 22.13**（mermaid-cli 12 与 puppeteer-core 的 `engines` 就是这么写的；
Node 16 上的报错是 `ReadableStream is not defined`，看不出和版本有关）。
不必替换系统里的 Node——装一个便携版，出图时放到 PATH 最前面即可：

```bat
set PATH=C:\Users\<你>\tools\node-v24.21.0-win-x64;%PATH%
python scripts/gen_diagrams.py
```

系统结构图与十张模块结构图的源是 HTML，用同一个本机浏览器截图，**不需要 Node**；
Node 只管 Mermaid 画的类图与 E-R 图。

Node 不够新时 `gen_diagrams.py` 会在**改动任何文件之前**停下来并说明原因。只改了文字、
图的源没变时不会重新渲染；没装 mermaid-cli 也可以加 `--no-render`
只更新文档里的 Mermaid 块。

## 改了代码之后会怎样

**不用记得重新生成，测试会提醒。** `server/tests/test_design_artifacts.py`
把七个生成脚本的 `--check` 都跑一遍：模型加了字段、路由加了接口、
服务函数改了名，而交付物或图没更新，测试直接红。

`--check` 都**不需要渲染环境**：图比的是 Mermaid 源与 PNG 的指纹，
Word 版比的是段落、表格与图片内容，所以任何一台机器都能跑。

它还会**校验概要设计正文里引用的每一条接口真的存在**——
这一条抓到过三条写错的路径，其中一条连 HTTP 方法都是错的。

## 源在哪

| 交付物 | 源 |
|---|---|
| 表结构、类型、非空、主外键 | `server/app/models.py`（SQLAlchemy 模型） |
| 表的中文名、用途、字段说明、所属模块、对应需求 | `scripts/db_meta.py` |
| 概要设计的正文 | `docs/概要设计说明书.md` |
| 接口清单与模块统计表 | 两个 FastAPI 应用的路由表（生成时现问） |
| 系统结构图 | 分层架构图（`docs/图/system.html` 截图）：各模块的接口数、表数与子智能体、工具数现数；模块里的业务取自第 3 章「功能定义」的归纳 |
| 模块结构图 | 《概要设计说明书》第 3 章各模块的「功能定义」表（`docs/图/struct_*.html` 截图） |
| 类图 | 模型类、路由表，以及**处理函数的语法树**（它实际调用了哪个服务函数、哪个 DTO） |
| E-R 图 | 模型的外键 |

**没有第二份表结构，也没有手画的图。** 交付要求「能导出 SQL 直接建库」，
做法是从模型生成 SQL 与 E-R 图，而不是另画一份设计稿再导出——
两个源必然分叉，而分叉的那一天没人会收到通知。本目录之前就吃过一次：
《数据库设计说明书》里那张手写的 ER 图只画了 36 张表中的 22 张。

两份建库脚本都**真的跑过**：SQLite 整份脚本连跑两遍、逐表逐字段与模型比对一致
（2026-09-28）；MySQL 在 2026-09-21 起了一个临时 mysqld 跑完整份脚本、
可重复执行，此后模型只加了一列可空的 `houses.approved_at`，MySQL 未重跑。
