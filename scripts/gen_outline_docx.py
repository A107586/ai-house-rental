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
"""

import io
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from docx import Document  # noqa: E402
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK  # noqa: E402
from docx.oxml import OxmlElement  # noqa: E402
from docx.oxml.ns import qn  # noqa: E402
from docx.shared import Pt, RGBColor  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "docs", "概要设计说明书.md")
OUT = os.path.join(ROOT, "docs", "评审",
                   "第6小组_基于AI智能体的房屋租赁系统_概要设计.docx")

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


def add_cover(doc):
    for text, size, bold in (("密级：机密", 10.5, False),
                             ("", 12, False), ("", 12, False),
                             (TITLE, 22, True),
                             ("概要设计说明书", 18, True),
                             ("", 12, False),
                             ("版本 1.0.0", 12, False),
                             ("2026-09-21", 12, False),
                             ("", 12, False), ("", 12, False),
                             (GROUP, 14, False),
                             ("编制：张健　　审批：张健", 11, False)):
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
    table = doc.add_table(rows=2, cols=4)
    table.style = "Table Grid"
    for i, text in enumerate(("修改编号", "版本", "修改条款及内容", "修改日期")):
        cell = table.rows[0].cells[i]
        cell.text = ""
        add_runs(cell.paragraphs[0], "**%s**" % text)
    for i, text in enumerate(("1", "1.0.0",
                              "创建。按《概要设计模板》的章节结构，"
                              "依据已实现的系统写成", "2026-09-21")):
        cell = table.rows[1].cells[i]
        cell.text = ""
        add_runs(cell.paragraphs[0], text)

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
        i += 1

    flush_table()


def main():
    text = io.open(SRC, encoding="utf-8").read()
    # 源文件开头的元信息表与修改控制表已经做进封面，正文从第 1 章开始
    body = text[text.index("## 1 文档概述"):]

    doc = Document()
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

    add_cover(doc)
    convert(doc, body)
    doc.save(OUT)
    print("生成 %s（%d 段、%d 张表）"
          % (os.path.basename(OUT), len(doc.paragraphs), len(doc.tables)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
