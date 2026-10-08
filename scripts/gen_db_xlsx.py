# -*- coding: utf-8 -*-
"""生成交付用的《数据库设计书》工作簿（每张表一个工作表）。

    python scripts/gen_db_xlsx.py            # 生成
    python scripts/gen_db_xlsx.py --check    # 只检查有没有过期

## 版式对标 `shengyadb.xls`

参考文档的版式是：一张表一个工作表，表头四行（表名、表描述、表名称、
列标题），然后逐字段一行，末尾一个 `END`。这里照搬，**但改了三处**，
每一处都写在工作簿的「说明」页里，免得评审时以为是抄漏了：

1. **去掉 `扩展 / 左 / 类型 / 右 / List显示` 五列。** 那是给界面生成器用的
   （某字段在页面上是 input 还是下拉、列表页显不显示）。我们的前端是
   手写 React，不从这份表生成，留着就是五列空白。
2. **加一列「键」**（PK / FK→目标 / UQ / IDX）。参考文档里表间关系
   全靠字段名对暗号，而**本项目建的是真外键**（设计原则 1.3），
   这是主要区别之一，得让它在表格里看得见。
3. **`id`、`created_at` 这类列照列不误。** 参考文档把它们省掉了，
   由工具补。我们没有那个工具，省掉就等于文档和库对不上。

## 数据从哪来

字段、类型、长度、非空、键——**全部从 `server/app/models.py` 的元数据读**。
表的中文名、用途、字段说明、所属模块、对应需求在 `scripts/db_meta.py`。
两头都不手抄，所以这份工作簿不会和库分叉。
"""

import argparse
import datetime
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db_meta import (COLUMN_NOTES, MODULES, REQUIREMENTS, ROOT,  # noqa: E402
                     TABLE_INFO, check_coverage, load_metadata, module_of,
                     ordered_tables)

import sqlalchemy as sa  # noqa: E402
from openpyxl import Workbook, load_workbook  # noqa: E402
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side  # noqa: E402
from openpyxl.utils import get_column_letter  # noqa: E402

OUT = os.path.join(ROOT, "docs", "评审",
                   "第6小组_基于AI智能体的房屋租赁系统_数据库设计.xlsx")

TITLE = "基于 AI 智能体的房屋租赁系统"
GROUP = "大连理工大学 实习实训 第 6 小组"

HEAD_FILL = PatternFill("solid", fgColor="D9E2F3")
NAME_FILL = PatternFill("solid", fgColor="F2F2F2")
THIN = Side(style="thin", color="AAAAAA")
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
WRAP = Alignment(wrap_text=True, vertical="top")

COLUMNS = ["类型", "大小", "名称", "非空", "键", "默认值", "描述",
           "特殊取值描述"]


def type_and_size(column):
    """拆成「类型」与「大小」两列——参考文档就是这么分的。"""
    kind = column.type
    if isinstance(kind, sa.String) and not isinstance(kind, sa.Text):
        return "VARCHAR", str(kind.length or "")
    if isinstance(kind, sa.Numeric) and not isinstance(kind, sa.Float):
        return "DECIMAL", "%d,%d" % (kind.precision, kind.scale)
    name = type(kind).__name__.upper()
    return {"TEXT": "TEXT", "INTEGER": "INTEGER", "DATETIME": "DATETIME",
            "DATE": "DATE", "BOOLEAN": "BOOL", "FLOAT": "FLOAT"}.get(
                name, name), ""


def key_marks(column):
    marks = []
    if column.primary_key:
        marks.append("PK")
    for fk in sorted(column.foreign_keys, key=lambda f: f.target_fullname):
        marks.append("FK→%s" % fk.target_fullname)
    if column.unique:
        marks.append("UQ")
    if column.index:
        marks.append("IDX")
    return " ".join(marks)


def python_default(column):
    """模型里的 Python 侧默认值，照实写出来。

    **建库脚本里没有它们**（那是 ORM 插入时填的），但设计书要写——
    读的人需要知道「这一列不填会是什么」。
    """
    default = column.default
    if default is None:
        return ""
    if getattr(default, "is_callable", False):
        func = getattr(default, "arg", None)
        name = getattr(func, "__name__", "")
        return "datetime.now()" if "now" in str(name) else "（函数）"
    value = getattr(default, "arg", None)
    if value is None or value == "":
        return "''" if isinstance(value, str) else ""
    return str(value)


_ENUM_PAIR = re.compile(r"[A-Za-z_][A-Za-z_0-9]*\s+[一-鿿]{1,8}")


def split_note(note):
    """字段说明拆成「描述」和「特殊取值描述」两列。

    参考文档把取值枚举单列一栏（`0：支付宝，1：微信`），这一列评审时
    最常被问到，混在大段描述里会被淹掉。

    **判据要死板，不要聪明。** 第一版写的是「含 `/` 且出现了
    待/已/未/租客/房东 之类的字眼就算枚举」，结果
    `deposit 押金 / rent 租金` 不在那张字眼表里，整条落进了描述列。
    现在改成数「英文 + 中文」这种取值对出现了几次：**两次以上才算枚举**，
    一次可能只是随口提到一个状态名。
    """
    note = (note or "").replace("**", "")
    if not note:
        return "", ""
    head, _, tail = note.partition("。")
    if len(_ENUM_PAIR.findall(head)) >= 2:
        return tail.strip(), head.strip()          # 枚举在前，补充说明在后
    if len(_ENUM_PAIR.findall(note)) >= 2 and not tail:
        return "", note.strip()
    return note.strip(), ""


def sheet_title(name):
    """工作表名 31 字符上限，且不能有 : \\ / ? * [ ]。表名都是小写下划线，
    只需管长度。"""
    return name[:31]


def write_cover(book, table_count, column_count, fk_count):
    sheet = book.active
    sheet.title = "封面"
    rows = [
        ("", ""),
        ("密级：", "机密"),
        ("", ""),
        (TITLE, ""),
        ("数据库设计书", ""),
        ("", ""),
        (GROUP, ""),
        ("", ""),
        ("表数量", table_count),
        ("字段数量", column_count),
        ("外键数量", fk_count),
        ("数据库引擎", "开发：SQLite 3　　部署：MySQL 8.0+（InnoDB / utf8mb4）"),
        ("建库脚本", "第6小组_..._建库脚本_MySQL.sql　/　..._SQLite.sql（同目录）"),
        ("生成方式", "python scripts/gen_db_sql.py；python scripts/gen_db_xlsx.py"),
        ("生成日期", datetime.date.today().isoformat()),
        ("编制", "张健"),
        ("审批", "张健"),
    ]
    for row in rows:
        sheet.append(list(row))
    sheet["A4"].font = Font(size=18, bold=True)
    sheet["A5"].font = Font(size=16, bold=True)
    sheet.column_dimensions["A"].width = 16
    sheet.column_dimensions["B"].width = 70
    return sheet


def write_readme(book):
    sheet = book.create_sheet("说明")
    lines = [
        ("这份工作簿是生成的，不要手改", ""),
        ("", "改了也会被下一次生成覆盖，而且会和真正在跑的结构分叉。"),
        ("", "源：server/app/models.py（字段、类型、非空、键）"),
        ("", "　　scripts/db_meta.py（中文名、用途、字段说明、模块、对应需求）"),
        ("", "生成：python scripts/gen_db_xlsx.py"),
        ("", "校验：python scripts/gen_db_xlsx.py --check（测试套件里也跑）"),
        ("", ""),
        ("与参考版式（shengyadb.xls）的三处不同", ""),
        ("1. 去掉 扩展/左/类型/右/List显示 五列",
         "那五列是给界面生成器用的（某字段在页面上是 input 还是下拉）。"
         "本项目前端是手写 React，不从这份表生成，留着就是五列空白。"),
        ("2. 增加「键」列",
         "PK / FK→目标表.列 / UQ / IDX。参考文档表间关系靠字段名对暗号，"
         "而本项目建的是真外键（设计原则 1.3），这是主要区别之一，"
         "得让它在表格里看得见。"),
        ("3. id、created_at 这类列照列不误",
         "参考文档把它们省掉、由工具补。我们没有那个工具，省掉就等于"
         "文档和库对不上。"),
        ("", ""),
        ("默认值这一列，和建库脚本对不上是故意的", ""),
        ("", "这里写的是模型里的 Python 侧默认值（ORM 插入时填）。"),
        ("", "建库脚本的 DDL 里不写它们：写了就等于多一份由数据库执行的"),
        ("", "语义，两边不一致时谁说了算会说不清。"),
        ("", ""),
        ("E-R 图页", ""),
        ("", "参考工作簿的 Sheet1 要求「划分主表和从表，确定主外键关系」。"),
        ("", "本工作簿的「E-R图」页把全部表的主外键关系画了出来，"),
        ("", "图由 scripts/gen_diagrams.py 从模型的外键生成，与本工作簿同源。"),
    ]
    for row in lines:
        sheet.append(list(row))
    for cell in ("A1", "A8", "A13", "A18"):         # 各段标题所在行
        sheet[cell].font = Font(bold=True)
    sheet.column_dimensions["A"].width = 34
    sheet.column_dimensions["B"].width = 86
    for row in sheet.iter_rows():
        for cell in row:
            cell.alignment = WRAP
    return sheet


def write_index(book, metadata, ordered):
    sheet = book.create_sheet("表清单")
    header = ["序号", "所属模块", "表名", "中文名", "字段数", "索引数",
              "对应需求", "用途"]
    sheet.append(header)
    tables = {t.name: t for t in metadata.sorted_tables}
    for i, name in enumerate(ordered, 1):
        table = tables[name]
        cn, purpose = TABLE_INFO.get(name, (name, ""))
        sheet.append([i, module_of(name), name, cn, len(table.columns),
                      len(table.indexes),
                      " ".join(REQUIREMENTS.get(name, [])),
                      purpose.replace("**", "")])
    style_header(sheet, len(header))
    for width, letter in zip((6, 14, 24, 16, 8, 8, 30, 90), "ABCDEFGH"):
        sheet.column_dimensions[letter].width = width
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = WRAP
    sheet.freeze_panes = "A2"
    return sheet


ER_IMAGES = [("er_overview", "图 1　E-R 总览：只有实体与关系，线上写外键列名"),
             ("er", "图 2　E-R 全图：每张表的主键（PK）、外键（FK）、唯一键（UK）")]


def write_er(book):
    """E-R 图页。参考工作簿的 Sheet1 写着「划分主表和从表，确定主外键关系」，
    这一页把关系画出来；图由 gen_diagrams.py 从模型生成，这里只是贴进来。

    **缺图就报错，不静默跳过**：少一页 E-R 图的设计书照样能打开，
    没人会发现它少了。"""
    from openpyxl.drawing.image import Image
    sheet = book.create_sheet("E-R图")
    sheet["A1"] = "表间关系（E-R 图）"
    sheet["A1"].font = Font(size=14, bold=True)
    sheet["A2"] = ("由 scripts/gen_diagrams.py 从 server/app/models.py 的外键生成；"
                   "Crow's Foot 记法：|| 恰好一个，|o 零或一个，o{ 零或多个。"
                   "图片可放大查看。")
    row = 4
    for name, caption in ER_IMAGES:
        path = os.path.join(ROOT, "docs", "图", name + ".png")
        if not os.path.exists(path):
            raise SystemExit("缺少 %s，先运行 python scripts/gen_diagrams.py" % path)
        sheet.cell(row=row, column=1, value=caption).font = Font(bold=True)
        image = Image(path)
        width = 1400
        image.height = int(image.height * width / image.width)
        image.width = width
        sheet.add_image(image, "A%d" % (row + 1))
        row += 2 + int(image.height / 20) + 2      # 默认行高约 20 像素
    sheet.column_dimensions["A"].width = 120
    return sheet


def write_matrix(book, ordered):
    """需求 → 表 的反向追溯。

    表清单是「这张表为哪条需求而建」，这一页是反过来的
    「这条需求落在哪几张表上」。评审问的往往是后者：
    **哪条需求没有任何表接着，就是漏做了。**
    """
    sheet = book.create_sheet("需求追溯")
    sheet.append(["需求编号", "涉及表数", "涉及的表"])
    reverse = {}
    for name in ordered:
        for code in REQUIREMENTS.get(name, []):
            reverse.setdefault(code, []).append(name)

    def sort_key(code):
        prefix, _, number = code.partition("-")
        return (prefix, int(number) if number.isdigit() else 0)

    for code in sorted(reverse, key=sort_key):
        tables = reverse[code]
        sheet.append([code, len(tables), "、".join(tables)])
    style_header(sheet, 3)
    for width, letter in zip((12, 10, 110), "ABC"):
        sheet.column_dimensions[letter].width = width
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = WRAP
    sheet.freeze_panes = "A2"
    return sheet


def style_header(sheet, width):
    for i in range(1, width + 1):
        cell = sheet.cell(row=1, column=i)
        cell.font = Font(bold=True)
        cell.fill = HEAD_FILL
        cell.border = BOX
        cell.alignment = Alignment(horizontal="center", vertical="center")


def write_table_sheet(book, table, today):
    name = table.name
    cn, purpose = TABLE_INFO.get(name, (name, ""))
    sheet = book.create_sheet(sheet_title(name))

    sheet["A1"] = name
    sheet["A1"].font = Font(size=14, bold=True)
    sheet["A2"], sheet["B2"] = "表描述", cn
    sheet["D2"], sheet["E2"] = "所属模块", module_of(name)
    sheet["G2"], sheet["H2"] = "做成日期", today
    sheet["A3"], sheet["B3"] = "表名称", name
    sheet["D3"], sheet["E3"] = "对应需求", " ".join(REQUIREMENTS.get(name, []))
    sheet["G3"], sheet["H3"] = "备注", purpose.replace("**", "")
    for cell in ("A2", "D2", "G2", "A3", "D3", "G3"):
        sheet[cell].font = Font(bold=True)
        sheet[cell].fill = NAME_FILL

    sheet.append([])
    sheet.append(COLUMNS)
    for i in range(1, len(COLUMNS) + 1):
        cell = sheet.cell(row=5, column=i)
        cell.font = Font(bold=True)
        cell.fill = HEAD_FILL
        cell.border = BOX
        cell.alignment = Alignment(horizontal="center", vertical="center")

    for column in table.columns:
        kind, size = type_and_size(column)
        note, values = split_note(COLUMN_NOTES.get((name, column.name), ""))
        sheet.append([kind, size, column.name, "" if column.nullable else "Y",
                      key_marks(column), python_default(column), note, values])
    for row in sheet.iter_rows(min_row=6, max_row=5 + len(table.columns)):
        for cell in row:
            cell.border = BOX
            cell.alignment = WRAP

    sheet.append([])
    sheet.append(["END"])

    for width, letter in zip((12, 8, 26, 6, 26, 14, 60, 42), "ABCDEFGH"):
        sheet.column_dimensions[letter].width = width
    sheet.freeze_panes = "A6"
    return sheet


def build(metadata):
    book = Workbook()
    tables = {t.name: t for t in metadata.sorted_tables}
    ordered = ordered_tables(metadata)
    today = datetime.date.today().isoformat()

    write_cover(book, len(tables),
                sum(len(t.columns) for t in tables.values()),
                sum(len(t.foreign_keys) for t in tables.values()))
    write_readme(book)
    write_index(book, metadata, ordered)
    write_er(book)
    write_matrix(book, ordered)
    for name in ordered:
        write_table_sheet(book, tables[name], today)
    apply_song_font(book)
    return book


def apply_song_font(book):
    """全部单元格用宋体 12 号，与参考工作簿 `shengyadb.xls` 一致；
    主题里的日文字体条目去掉。

    openpyxl 默认是 Calibri 11，中文落到主题的东亚字体上；而它自带的主题
    里写着日文字体「ＭＳ Ｐゴシック」，在 WPS 里就可能显示成日文字体。
    所以每个格子显式写宋体，主题里的日文条目也一并删掉。"""
    from openpyxl.writer.theme import theme_xml
    for sheet in book.worksheets:
        for row in sheet.iter_rows():
            for cell in row:
                old = cell.font
                size = old.sz if old.sz and float(old.sz) != 11 else 12
                cell.font = Font(name="宋体", size=size, bold=old.b,
                                 italic=old.i, color=old.color)
    theme = re.sub(r'<a:font script="Jpan"[^>]*/>', "", theme_xml)
    book.loaded_theme = re.sub(r'<a:ea typeface="[^"]*"/>', '<a:ea typeface="宋体"/>', theme)


def fingerprint(metadata):
    """把「内容」压成一串文本，用来判断过期。

    直接比 xlsx 的字节没用：**openpyxl 每次写出来的 zip 都不一样**
    （时间戳），比字节会天天报过期。所以比的是结构与说明本身。
    """
    parts = []
    for name in ordered_tables(metadata):
        table = {t.name: t for t in metadata.sorted_tables}[name]
        parts.append("#%s|%s|%s|%s" % (
            name, TABLE_INFO.get(name, ("", ""))[0], module_of(name),
            ",".join(REQUIREMENTS.get(name, []))))
        for column in table.columns:
            kind, size = type_and_size(column)
            parts.append("%s|%s|%s|%s|%s|%s" % (
                column.name, kind, size, "" if column.nullable else "Y",
                key_marks(column),
                COLUMN_NOTES.get((name, column.name), "")))
    return "\n".join(parts)


def read_fingerprint(path):
    """从已生成的工作簿里把同样的指纹重新拼出来。

    **不另存一份指纹文件**：那样就有两个真相，文件被改了而指纹没改时，
    校验会说「没过期」。这里是直接读回工作簿的内容自己拼。
    """
    if not os.path.exists(path):
        return None
    book = load_workbook(path, read_only=True, data_only=True)
    parts = []
    skip = {"封面", "说明", "表清单", "E-R图", "需求追溯"}
    if "E-R图" not in book.sheetnames:
        book.close()
        return None                     # 缺 E-R 图页也算过期
    for sheet in book.worksheets:
        if sheet.title in skip:
            continue
        rows = list(sheet.iter_rows(values_only=True))
        if len(rows) < 5:
            return None
        name = rows[0][0]
        cn = rows[1][1] or ""
        module = rows[1][4] or ""
        codes = (rows[2][4] or "").replace(" ", ",")
        parts.append("#%s|%s|%s|%s" % (name, cn, module, codes))
        for row in rows[5:]:
            if not row or not row[2] or row[0] == "END":
                continue
            parts.append("%s|%s|%s|%s|%s|%s" % (
                row[2], row[0] or "", row[1] or "", row[3] or "",
                row[4] or "", ""))
    book.close()
    return "\n".join(parts)


def strip_notes(text):
    """比对时去掉「描述」——它在工作簿里被拆成了两列又做了清洗，
    还原不回原文。结构（表、字段、类型、键、需求）是能逐字比的，比这些。"""
    return "\n".join(line.rsplit("|", 1)[0] if not line.startswith("#")
                     else line for line in text.split("\n"))


def main():
    parser = argparse.ArgumentParser(description="生成交付用的数据库设计书")
    parser.add_argument("--check", action="store_true",
                        help="只检查有没有过期，不写盘")
    args = parser.parse_args()

    metadata = load_metadata()
    problems = check_coverage(metadata)
    if problems:
        for problem in problems:
            print("  ! %s" % problem)
        print("先把上面这些补到 scripts/db_meta.py，再生成。")
        return 1

    if args.check:
        current = strip_notes(fingerprint(metadata))
        stored = read_fingerprint(OUT)
        if stored is None:
            print("数据库设计书还没生成：python scripts/gen_db_xlsx.py")
            return 1
        if strip_notes(stored) != current:
            print("数据库设计书过期了（表或字段与模型对不上）。")
            print("重新生成：python scripts/gen_db_xlsx.py")
            return 1
        print("数据库设计书是最新的。")
        return 0

    book = build(metadata)
    book.save(OUT)
    print("生成 %s（%d 个工作表）" % (os.path.basename(OUT),
                                      len(book.worksheets)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
