# -*- coding: utf-8 -*-
"""把《概要设计说明书》导成交付用的 Word 文档。

    python scripts/gen_outline_docx.py

## 为什么源是 Markdown，交付的是 .doc

交付要的是 Word。但**用 Word 当源文件，这份文档就没法被 git 管**——
看不出改了什么、谁改的、为什么改，评审前一晚合并两个人的修改
只能靠「你先别动我来改」。

所以源是 `docs/概要设计说明书.md`：在 git 里逐行可比、可回滚，
里面的接口清单与模块统计表还能由脚本重新生成。
Word 版是从它导出来的，**导出来的那一份不要手改**——
改了下次导出就没了，而且和源分叉。

## 排版对着模板来

《概要设计模板》的版式：封面（密级、项目名称、文档名、版本、日期、
编制单位）→ 文件修改控制 → 目录 → 正文。这里照着做，
目录用 Word 的域（`TOC`），打开文档后按 F9 或「更新域」即可生成页码——
python-docx 插不了已经算好页码的目录，只能插域。
在装了 Word 的 Windows 上加 `--update-toc`，会让 Word 把页码算好再存。

封面上的版本、日期与「文件修改控制」表**从源文件开头那两张表读**，
不在这里另写一份——原先写死在脚本里，源文件升了版本，Word 版还是 1.0.0。

## 图

源文件里 `<!-- DIAGRAM:名字:BEGIN -->` 标记段是 `gen_diagrams.py` 写的
Mermaid 图。Word 渲染不了 Mermaid，这里换成同名的 `docs/图/名字.png`。
宽图（宽高比大于 1.25）单独放一个横向页，否则字小到看不清。

    python scripts/gen_outline_docx.py              # 生成
    python scripts/gen_outline_docx.py --check      # 只检查 Word 版是否与源一致
    python scripts/gen_outline_docx.py --update-toc # 生成后让 Word 更新目录页码
"""

import argparse
import io
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from docx import Document  # noqa: E402
from docx.enum.section import WD_ORIENT, WD_SECTION  # noqa: E402
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK  # noqa: E402
from docx.oxml import OxmlElement  # noqa: E402
from docx.oxml.ns import qn  # noqa: E402
from docx.shared import Cm, Pt, RGBColor  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "docs", "概要设计说明书.md")
OUT = os.path.join(ROOT, "docs", "评审",
                   "第6小组_基于AI智能体的房屋租赁系统_概要设计.docx")
FIG_DIR = os.path.join(ROOT, "docs", "图")
DIAGRAM_BEGIN = re.compile(r"<!-- DIAGRAM:(\w+):BEGIN")

PAGE_W, PAGE_H, MARGIN = Cm(21.0), Cm(29.7), Cm(2.2)

TITLE = "基于 AI 智能体的房屋租赁系统"
GROUP = "大连理工大学 实习实训 第 6 小组"
CODE_FONT = "Consolas"
BODY_FONT = "微软雅黑"

INLINE = re.compile(r"(\*\*.+?\*\*|`[^`]+`)")


def set_cjk(run, font=BODY_FONT):
    """中文字体要单独设 eastAsia，否则 Word 用默认宋体渲染中文。"""
    run.font.name = font
    rpr = run._element.get_or_add_rPr()
    fonts = rpr.find(qn("w:rFonts"))
    if fonts is None:
        fonts = OxmlElement("w:rFonts")
        rpr.append(fonts)
    fonts.set(qn("w:eastAsia"), font)


def add_runs(paragraph, text):
    """把 `**粗体**` 与 `` `等宽` `` 解析成 Word 的 run。"""
    for piece in INLINE.split(text):
        if not piece:
            continue
        if piece.startswith("**") and piece.endswith("**"):
            run = paragraph.add_run(piece[2:-2])
            run.bold = True
            set_cjk(run)
        elif piece.startswith("`") and piece.endswith("`"):
            run = paragraph.add_run(piece[1:-1])
            set_cjk(run, CODE_FONT)
            run.font.size = Pt(9.5)
            run.font.color.rgb = RGBColor(0xA0, 0x30, 0x20)
        else:
            run = paragraph.add_run(piece)
            set_cjk(run)


def read_front(text):
    """源文件第 1 章之前的两张表：元信息（项 / 内容）与文件修改控制。"""
    head = text[:text.index("## 1 文档概述")]
    tables, current = [], []
    for line in head.split("\n"):
        if line.strip().startswith("|"):
            current.append(split_row(line))
        elif current:
            tables.append(current)
            current = []
    if current:
        tables.append(current)
    meta = {row[0]: row[1] for row in tables[0][2:]}
    revisions = [row for row in tables[1][2:]]
    return meta, revisions


def add_cover(doc, meta, revisions):
    author, _, approver = meta.get("编制 / 审批", "张健 / 张健").partition(" / ")
    for text, size, bold in (("密级：%s" % meta.get("密级", "机密"), 10.5, False),
                             ("", 12, False), ("", 12, False),
                             (TITLE, 22, True),
                             ("概要设计说明书", 18, True),
                             ("", 12, False),
                             ("版本 %s" % meta["版本"], 12, False),
                             (meta["日期"], 12, False),
                             ("", 12, False), ("", 12, False),
                             (GROUP, 14, False),
                             ("编制：%s　　审批：%s" % (author, approver), 11, False)):
        paragraph = doc.add_paragraph()
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = paragraph.add_run(text)
        run.bold = bold
        run.font.size = Pt(size)
        set_cjk(run)

    doc.add_paragraph()
    heading = doc.add_paragraph()
    run = heading.add_run("文件修改控制")
    run.bold = True
    set_cjk(run)
    table = doc.add_table(rows=1 + len(revisions), cols=4)
    table.style = "Table Grid"
    for i, text in enumerate(("修改编号", "版本", "修改条款及内容", "修改日期")):
        cell = table.rows[0].cells[i]
        cell.text = ""
        add_runs(cell.paragraphs[0], "**%s**" % text)
    for r, row in enumerate(revisions, 1):
        for i in range(4):
            cell = table.rows[r].cells[i]
            cell.text = ""
            add_runs(cell.paragraphs[0], row[i] if i < len(row) else "")

    doc.add_paragraph()
    paragraph = doc.add_paragraph()
    run = paragraph.add_run("目　录")
    run.bold = True
    run.font.size = Pt(14)
    set_cjk(run)
    add_toc_field(doc)
    doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)


def add_toc_field(doc):
    """插一个 TOC 域。

    python-docx **生成不了带页码的目录**——页码要排版引擎算。
    插域之后，Word 打开时选「更新域」就能生成；否则那里会显示一行提示。
    """
    paragraph = doc.add_paragraph()
    run = paragraph.add_run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = r'TOC \o "1-3" \h \z \u'
    separate = OxmlElement("w:fldChar")
    separate.set(qn("w:fldCharType"), "separate")
    placeholder = OxmlElement("w:t")
    placeholder.text = "（在 Word 里右键「更新域」生成目录与页码）"
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    for node in (begin, instr, separate, placeholder, end):
        run._element.append(node)


def set_page(section, landscape):
    section.orientation = WD_ORIENT.LANDSCAPE if landscape else WD_ORIENT.PORTRAIT
    section.page_width, section.page_height = ((PAGE_H, PAGE_W) if landscape
                                               else (PAGE_W, PAGE_H))
    for side in ("left_margin", "right_margin", "top_margin", "bottom_margin"):
        setattr(section, side, MARGIN)


def image_size(path):
    from PIL import Image
    with Image.open(path) as image:
        return image.size


def add_figure(doc, name, caption):
    """插一张图。宽图单开一个横向页，插完再切回纵向。

    **缺图就报错**：静默跳过的话，Word 版会少一张图而没人发现——
    这正是本次补图之前的状态：模板要求的类图与 E-R 图一张都没有。"""
    path = os.path.join(FIG_DIR, name + ".png")
    if not os.path.exists(path):
        raise SystemExit("缺少 %s，先运行 python scripts/gen_diagrams.py" % path)
    width, height = image_size(path)
    landscape = width / float(height) > 1.25
    if landscape:
        set_page(doc.add_section(WD_SECTION.NEW_PAGE), True)
    section = doc.sections[-1]
    max_w = section.page_width - 2 * MARGIN
    max_h = section.page_height - 2 * MARGIN - Cm(2.0)       # 给图题留位置
    scale = min(max_w / float(width), max_h / float(height))
    paragraph = doc.add_paragraph()
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    paragraph.add_run().add_picture(path, width=int(width * scale),
                                    height=int(height * scale))
    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    add_runs(title, caption)
    for run in title.runs:
        run.bold = True
        run.font.size = Pt(10)
    if landscape:
        set_page(doc.add_section(WD_SECTION.NEW_PAGE), False)


def add_code(doc, lines):
    paragraph = doc.add_paragraph()
    paragraph.paragraph_format.left_indent = Pt(18)
    paragraph.paragraph_format.space_after = Pt(6)
    run = paragraph.add_run("\n".join(lines))
    set_cjk(run, CODE_FONT)
    run.font.size = Pt(9)


def split_row(line):
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def add_table(doc, rows):
    header, body = rows[0], rows[2:]          # rows[1] 是 |---|---| 分隔行
    table = doc.add_table(rows=1 + len(body), cols=len(header))
    table.style = "Table Grid"
    for i, text in enumerate(header):
        cell = table.rows[0].cells[i]
        cell.text = ""
        add_runs(cell.paragraphs[0], "**%s**" % text)
    for r, row in enumerate(body, 1):
        for i in range(len(header)):
            cell = table.rows[r].cells[i]
            cell.text = ""
            add_runs(cell.paragraphs[0], row[i] if i < len(row) else "")
    for row in table.rows:
        for cell in row.cells:
            for paragraph in cell.paragraphs:
                for run in paragraph.runs:
                    run.font.size = run.font.size or Pt(9)
    doc.add_paragraph()


def convert(doc, text):
    lines = text.split("\n")
    i = 0
    table_buffer = []

    def flush_table():
        if table_buffer:
            add_table(doc, [split_row(line) for line in table_buffer])
            table_buffer.clear()

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        figure = DIAGRAM_BEGIN.match(stripped)
        if figure:                                # Mermaid 块 → 渲染好的 PNG
            flush_table()
            name = figure.group(1)
            end = "<!-- DIAGRAM:%s:END -->" % name
            caption = name
            i += 1
            while i < len(lines) and lines[i].strip() != end:
                text = lines[i].strip()
                if text.startswith("**图") and text.endswith("**"):
                    caption = text.strip("*")
                i += 1
            add_figure(doc, name, caption)
            i += 1
            continue

        if stripped.startswith("<!--"):           # 生成标记，不进 Word
            i += 1
            continue

        if stripped.startswith("|") and stripped.endswith("|"):
            table_buffer.append(stripped)
            i += 1
            continue
        flush_table()

        if stripped.startswith("```"):
            block = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                block.append(lines[i])
                i += 1
            add_code(doc, block)
            i += 1
            continue

        if stripped.startswith("#"):
            level = len(stripped) - len(stripped.lstrip("#"))
            heading = doc.add_heading(level=min(level, 4))
            heading.text = ""
            add_runs(heading, stripped.lstrip("# ").strip())
            for run in heading.runs:
                run.font.color.rgb = RGBColor(0x14, 0x3C, 0x38)
            i += 1
            continue

        if stripped in ("---", ""):
            if stripped == "":
                doc.add_paragraph()
            i += 1
            continue

        if stripped.startswith(">"):
            paragraph = doc.add_paragraph()
            paragraph.paragraph_format.left_indent = Pt(18)
            add_runs(paragraph, stripped.lstrip("> ").strip())
            for run in paragraph.runs:
                run.font.size = Pt(10)
                run.font.color.rgb = RGBColor(0x44, 0x4A, 0x52)
            i += 1
            continue

        if re.match(r"^[-*] ", stripped):
            paragraph = doc.add_paragraph(style="List Bullet")
            add_runs(paragraph, stripped[2:])
            i += 1
            continue
        if re.match(r"^\d+[.、] ", stripped):
            paragraph = doc.add_paragraph(style="List Number")
            add_runs(paragraph, re.sub(r"^\d+[.、] ", "", stripped))
            i += 1
            continue

        paragraph = doc.add_paragraph()
        add_runs(paragraph, stripped)
        if re.match(r"^\*\*表 \d", stripped):     # 表题居中，与模板一致
            paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        i += 1

    flush_table()


def build():
    text = io.open(SRC, encoding="utf-8").read()
    meta, revisions = read_front(text)
    # 源文件开头的元信息表与修改控制表已经做进封面，正文从第 1 章开始
    body = text[text.index("## 1 文档概述"):]

    doc = Document()
    set_page(doc.sections[0], False)          # python-docx 默认是 Letter，交付用 A4
    style = doc.styles["Normal"]
    style.font.size = Pt(10.5)
    style.font.name = BODY_FONT
    # 正文样式的中文字体要写在 style 的 rPr 上，逐 run 设只管得住自己那一段
    rpr = style.element.get_or_add_rPr()
    fonts = rpr.find(qn("w:rFonts"))
    if fonts is None:
        fonts = OxmlElement("w:rFonts")
        rpr.append(fonts)
    fonts.set(qn("w:eastAsia"), BODY_FONT)

    add_cover(doc, meta, revisions)
    convert(doc, body)
    return doc


def fingerprint(doc):
    """文档内容的指纹：段落文字、表格文字、图片数与各图的字节摘要。

    **不比 docx 的字节**：每次保存的 zip 都不一样。也不比目录——
    `--update-toc` 让 Word 把目录展开成一串带页码的段落，
    那是排版结果，不是内容。"""
    import hashlib
    parts = []
    for paragraph in doc.paragraphs:
        style = (paragraph.style.name or "").lower() if paragraph.style is not None else ""
        if style.startswith("toc") or "更新域" in paragraph.text:
            continue
        if paragraph.text.strip():
            parts.append(paragraph.text.strip())
    for table in doc.tables:
        for row in table.rows:
            parts.append("|".join(cell.text.strip() for cell in row.cells))
    blobs = sorted(hashlib.sha256(part.blob).hexdigest()
                   for part in doc.part.package.parts
                   if part.partname.startswith("/word/media/"))
    parts.append("images:%d:%s" % (len(blobs), ",".join(blobs)))
    return "\n".join(parts)


def update_toc(path):
    """让 Word 打开文档、更新目录页码、保存。只在装了 Word 的 Windows 上可用。"""
    script = (
        "$w = New-Object -ComObject Word.Application; $w.Visible = $false;"
        "$d = $w.Documents.Open('%s');"
        "foreach ($t in $d.TablesOfContents) { $t.Update() };"
        "$d.Save(); $d.Close(); $w.Quit()" % path.replace("'", "''"))
    done = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                          capture_output=True, text=True)
    if done.returncode != 0:
        print("  ! 更新目录失败（需要本机装有 Word）：%s" % done.stderr.strip()[-300:])
        return False
    return True


def main():
    parser = argparse.ArgumentParser(description="概要设计说明书导出 Word")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--update-toc", action="store_true")
    args = parser.parse_args()

    doc = build()
    if args.check:
        if not os.path.exists(OUT):
            print("Word 版还没生成：python scripts/gen_outline_docx.py")
            return 1
        if fingerprint(Document(OUT)) != fingerprint(doc):
            print("Word 版与 docs/概要设计说明书.md 或 docs/图/ 对不上了。")
            print("重新生成：python scripts/gen_outline_docx.py")
            return 1
        print("Word 版是最新的。")
        return 0

    doc.save(OUT)
    images = len([p for p in doc.part.package.parts
                  if p.partname.startswith("/word/media/")])
    print("生成 %s（%d 段、%d 张表、%d 张图）"
          % (os.path.basename(OUT), len(doc.paragraphs), len(doc.tables), images))
    if args.update_toc and os.name == "nt":
        if update_toc(OUT):
            print("目录页码已由 Word 更新。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
