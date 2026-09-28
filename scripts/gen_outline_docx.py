# -*- coding: utf-8 -*-
"""把《概要设计说明书》导成交付用的 Word 文档，版式对齐《概要设计模板》。

    python scripts/gen_outline_docx.py              # 生成
    python scripts/gen_outline_docx.py --check      # 只检查 Word 版是否与源一致
    python scripts/gen_outline_docx.py --update-toc # 生成后让 Word 更新目录页码、填封面页数

## 为什么源是 Markdown，交付的是 Word

**用 Word 当源文件，这份文档就没法被 git 管**——看不出改了什么、谁改的、
为什么改。所以源是 `docs/概要设计说明书.md`，Word 版从它导出来，
**导出来的那一份不要手改**：改了下次导出就没了，而且和源分叉。

## 版式照模板（逐项从模板 .doc 里量出来的）

| 项 | 模板 | 这里 |
|---|---|---|
| 纸张与页边距 | A4；上下 2.0、左 2.5、右 1.5 cm；页眉页脚 1.3 cm | 同 |
| 封面 | 密级（黑体）、文档编号 / 项目名称 / 项目编号（楷体）、题目（楷体 42 磅）、版本与日期、单位、版权声明（黑体）、总页数 / 正文 / 附录 / 生效日期 / 编制 / 批准签署栏 | 同；签署栏的页数由 Word 分页后填 |
| 文件修改控制 | 黑体三号标题，表头楷体小四加底色 | 同 |
| 目录 | 「目  录」宋体小三加粗，目录项宋体五号 | 同 |
| 页眉 | 正文各页「概要设计书 …… 版本：x  第 n 页」，小五 | 同，封面、修改控制、目录三页不带页眉 |
| 正文 | 宋体 / Times New Roman 五号，1.5 倍行距 | 同，段首缩进两字 |
| 标题 | 一级宋体二号、二级黑体三号、三级宋体三号，均加粗 | 同 |
| 图题、表题 | 「图2-1  系统结构图」宋体五号，图题在图下、表题在表上 | 同 |
| 表格 | 宋体五号，表头加粗、灰底，全框线 | 同 |

模板写的「楷体_GB2312」是旧版 Windows 才有的字体，现在的系统里是「楷体」，
这里用后者——指定一个装不上的字体，打开时会被替换成什么就说不准了。

## 不留日文字体

python-docx 自带的底板是按日文环境做的：`settings.xml` 里东亚语言写的是
`ja-JP`，主题里东亚字体是「ＭＳ 明朝 / ＭＳ ゴシック」，字体表里还声明了
MS Mincho、MS Gothic。凡是没显式指定字体、落到主题字体上的文字（标题、
目录、列表符号），在 WPS 里就会显示成日文字体。这里三处都改成中文，
并给每个用到的样式显式写上字体，不让任何文字落到主题上。

## 图

源文件里 `<!-- DIAGRAM:名字:BEGIN -->` 标记段由 `gen_diagrams.py` 维护，
这里换成同名的 `docs/图/名字.png`。宽图（宽高比大于 1.25）单独放一个横向页。
"""

import argparse
import base64
import hashlib
import io
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from docx import Document  # noqa: E402
from docx.enum.section import WD_ORIENT, WD_SECTION  # noqa: E402
from docx.enum.style import WD_STYLE_TYPE  # noqa: E402
from docx.enum.table import WD_TABLE_ALIGNMENT  # noqa: E402
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK, WD_TAB_ALIGNMENT  # noqa: E402
from docx.oxml import OxmlElement  # noqa: E402
from docx.oxml.ns import qn  # noqa: E402
from docx.shared import Cm, Pt, RGBColor  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "docs", "概要设计说明书.md")
OUT = os.path.join(ROOT, "docs", "评审",
                   "第6小组_基于AI智能体的房屋租赁系统_概要设计.docx")
FIG_DIR = os.path.join(ROOT, "docs", "图")
DIAGRAM_BEGIN = re.compile(r"<!-- DIAGRAM:(\w+):BEGIN")

PAGE_W, PAGE_H = Cm(21.0), Cm(29.7)
MARGIN_TOP = MARGIN_BOTTOM = Cm(2.0)
MARGIN_LEFT, MARGIN_RIGHT = Cm(2.5), Cm(1.5)
HEADER_DISTANCE = Cm(1.3)

SONG, HEI, KAI = "宋体", "黑体", "楷体"
LATIN = "Times New Roman"
CODE = "Consolas"
BLACK = RGBColor(0, 0, 0)

HEADER_SHADE = "CCCCCC"          # 正文表格表头，模板里是灰底
REVISION_SHADE = "99CCFF"        # 文件修改控制表头，模板里是浅蓝底

INLINE = re.compile(r"(\*\*.+?\*\*|`[^`]+`)")
CAPTION = re.compile(r"^\*\*([图表]) (\d+-\d+)　(.+)\*\*$")


# ============================================================== 字体与样式

def set_fonts(target, east=SONG, latin=LATIN):
    """给 run 或样式写死四种字体槽位，并删掉主题字体引用。

    只设 `font.name` 管不住中文：中文走的是 eastAsia 槽位；留着
    `asciiTheme` 这类属性，Word 会优先用主题字体——而底板的主题是日文的。"""
    element = target.element if hasattr(target, "element") else target._element
    rpr = element.get_or_add_rPr()
    fonts = rpr.find(qn("w:rFonts"))
    if fonts is None:
        fonts = OxmlElement("w:rFonts")
        rpr.insert(0, fonts)
    for attr in ("w:asciiTheme", "w:hAnsiTheme", "w:eastAsiaTheme", "w:cstheme"):
        if fonts.get(qn(attr)) is not None:
            del fonts.attrib[qn(attr)]
    fonts.set(qn("w:ascii"), latin)
    fonts.set(qn("w:hAnsi"), latin)
    fonts.set(qn("w:cs"), latin)
    fonts.set(qn("w:eastAsia"), east)


def style_font(style, east, latin, size, bold=False):
    set_fonts(style, east, latin)
    style.font.size = Pt(size)
    style.font.bold = bold
    style.font.italic = False
    style.font.color.rgb = BLACK
    rpr = style.element.get_or_add_rPr()
    color = rpr.find(qn("w:color"))
    if color is not None:
        for attr in ("w:themeColor", "w:themeShade", "w:themeTint"):
            if color.get(qn(attr)) is not None:
                del color.attrib[qn(attr)]


def para_format(style, before=0, after=0, line=1.5, first=None, left=None, keep=False):
    pf = style.paragraph_format
    pf.space_before = Pt(before)
    pf.space_after = Pt(after)
    pf.line_spacing = line
    if first is not None:
        pf.first_line_indent = first
    if left is not None:
        pf.left_indent = left
    pf.keep_with_next = keep


def ensure_style(doc, name):
    try:
        return doc.styles[name]
    except KeyError:
        style = doc.styles.add_style(name, WD_STYLE_TYPE.PARAGRAPH)
        style.base_style = doc.styles["Normal"]
        return style


def setup_styles(doc):
    styles = doc.styles

    # 文档默认：东亚语言中文，四个字体槽位写死
    defaults = doc.styles.element.find(qn("w:docDefaults"))
    rpr = defaults.find(qn("w:rPrDefault")).find(qn("w:rPr"))
    fonts = rpr.find(qn("w:rFonts"))
    if fonts is None:
        fonts = OxmlElement("w:rFonts")
        rpr.insert(0, fonts)
    for attr in list(fonts.attrib):
        del fonts.attrib[attr]
    fonts.set(qn("w:ascii"), LATIN)
    fonts.set(qn("w:hAnsi"), LATIN)
    fonts.set(qn("w:cs"), LATIN)
    fonts.set(qn("w:eastAsia"), SONG)
    lang = rpr.find(qn("w:lang"))
    if lang is None:
        lang = OxmlElement("w:lang")
        rpr.append(lang)
    lang.set(qn("w:val"), "en-US")
    lang.set(qn("w:eastAsia"), "zh-CN")

    normal = styles["Normal"]
    style_font(normal, SONG, LATIN, 10.5)
    para_format(normal, line=1.5)

    for level, (east, latin, size, before, after, line) in {
            1: (SONG, LATIN, 22, 17, 16.5, 2.41),
            2: (HEI, "Arial", 16, 13, 13, 1.73),
            3: (SONG, LATIN, 16, 13, 13, 1.73),
            4: (HEI, "Arial", 14, 14, 14.5, 1.57)}.items():
        style = styles["Heading %d" % level]
        style_font(style, east, latin, size, bold=True)
        para_format(style, before, after, line, keep=True)
        style.paragraph_format.first_line_indent = Pt(0)

    for level in (1, 2, 3):
        style = ensure_style(doc, "toc %d" % level)
        style_font(style, SONG, LATIN, 10.5)
        para_format(style, line=1.5, left=Pt(21 * (level - 1)))

    for name in ("List Bullet", "List Number", "Header", "Footer", "Caption"):
        if name in [s.name for s in styles]:
            style_font(styles[name], SONG, LATIN, 9 if name in ("Header", "Footer") else 10.5)
    # 底板的页眉样式自带居中、右对齐两个制表位（按 Letter 纸宽算的），
    # 不清掉的话「版本：x 第 n 页」会停在页面正中间
    for name in ("Header", "Footer"):
        styles[name].paragraph_format.tab_stops.clear_all()


def strip_japanese(doc):
    """底板里的三处日文设置：东亚主题语言、主题字体、字体表。"""
    settings = doc.settings.element
    lang = settings.find(qn("w:themeFontLang"))
    if lang is not None:
        lang.set(qn("w:eastAsia"), "zh-CN")

    for part in doc.part.package.parts:
        name = str(part.partname)
        if name.endswith("/theme/theme1.xml"):
            xml = part.blob.decode("utf-8")
            xml = re.sub(r'<a:font script="Jpan"[^>]*/>', "", xml)
            xml = re.sub(r'<a:ea typeface="[^"]*"/>', '<a:ea typeface="宋体"/>', xml)
            part._blob = xml.encode("utf-8")
        elif name.endswith("/fontTable.xml"):
            xml = part.blob.decode("utf-8")
            xml = re.sub(r'<w:font w:name="MS (Mincho|Gothic)">.*?</w:font>', "", xml,
                         flags=re.S)
            part._blob = xml.encode("utf-8")


# ============================================================== 文字

def add_runs(paragraph, text, east=SONG, size=None, bold=False):
    """把 `**粗体**` 与 `` `等宽` `` 解析成 run；每个 run 都写死字体。"""
    for piece in INLINE.split(text):
        if not piece:
            continue
        if piece.startswith("**") and piece.endswith("**"):
            # 粗体里可能夹着 `代码`，反引号本身不该印出来
            run = paragraph.add_run(piece[2:-2].replace("`", ""))
            run.bold = True
            set_fonts(run, east)
        elif piece.startswith("`") and piece.endswith("`"):
            run = paragraph.add_run(piece[1:-1])
            set_fonts(run, SONG, CODE)
            run.bold = bold or None
        else:
            run = paragraph.add_run(piece)
            set_fonts(run, east)
            run.bold = bold or None
        if size:
            run.font.size = Pt(size)
    return paragraph


def styled_paragraph(doc_or_cell, text, east, size, bold=False, align=None, latin=LATIN):
    paragraph = doc_or_cell.add_paragraph()
    paragraph.paragraph_format.first_line_indent = Pt(0)
    if align is not None:
        paragraph.alignment = align
    if text:
        run = paragraph.add_run(text)
        set_fonts(run, east, latin)
        run.font.size = Pt(size)
        run.bold = bold
    return paragraph


def shade(cell, color):
    tcpr = cell._element.get_or_add_tcPr()
    fill = OxmlElement("w:shd")
    fill.set(qn("w:val"), "clear")
    fill.set(qn("w:color"), "auto")
    fill.set(qn("w:fill"), color)
    tcpr.append(fill)


def cell_text(cell, text, east=SONG, size=10.5, bold=False,
              align=WD_ALIGN_PARAGRAPH.LEFT, latin=LATIN, markdown=False):
    cell.text = ""
    paragraph = cell.paragraphs[0]
    paragraph.alignment = align
    paragraph.paragraph_format.first_line_indent = Pt(0)
    if markdown:
        add_runs(paragraph, text, east, size, bold)
    elif text:
        run = paragraph.add_run(text)
        set_fonts(run, east, latin)
        run.font.size = Pt(size)
        run.bold = bold


def set_widths(table, widths):
    table.autofit = False
    for row in table.rows:
        for cell, width in zip(row.cells, widths):
            cell.width = width


# ============================================================== 页面

def text_width(section):
    return section.page_width - section.left_margin - section.right_margin


def set_page(section, landscape):
    section.orientation = WD_ORIENT.LANDSCAPE if landscape else WD_ORIENT.PORTRAIT
    section.page_width, section.page_height = ((PAGE_H, PAGE_W) if landscape
                                               else (PAGE_W, PAGE_H))
    section.top_margin, section.bottom_margin = MARGIN_TOP, MARGIN_BOTTOM
    section.left_margin, section.right_margin = MARGIN_LEFT, MARGIN_RIGHT
    section.header_distance = section.footer_distance = HEADER_DISTANCE


def add_field(paragraph, instr, size=9):
    run = paragraph.add_run()
    set_fonts(run)
    run.font.size = Pt(size)
    for kind, text in (("begin", None), (None, instr), ("separate", None),
                       ("text", "1"), ("end", None)):
        if kind in ("begin", "separate", "end"):
            node = OxmlElement("w:fldChar")
            node.set(qn("w:fldCharType"), kind)
        elif kind == "text":
            node = OxmlElement("w:t")
            node.text = text
        else:
            node = OxmlElement("w:instrText")
            node.set(qn("xml:space"), "preserve")
            node.text = " %s " % text
        run._element.append(node)


def set_header(section, version):
    """模板的页眉：左「概要设计书」，右「版本：x  第 n 页」，下面一条细线。"""
    section.header.is_linked_to_previous = False
    header = section.header
    paragraph = header.paragraphs[0]
    for run in list(paragraph.runs):
        run._element.getparent().remove(run._element)
    paragraph.paragraph_format.first_line_indent = Pt(0)
    paragraph.paragraph_format.tab_stops.add_tab_stop(text_width(section),
                                                      WD_TAB_ALIGNMENT.RIGHT)
    for text in ("概要设计书", "\t版本：%s　第 " % version):
        run = paragraph.add_run(text)
        set_fonts(run)
        run.font.size = Pt(9)
    add_field(paragraph, "PAGE")
    run = paragraph.add_run(" 页")
    set_fonts(run)
    run.font.size = Pt(9)
    ppr = paragraph._element.get_or_add_pPr()
    borders = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    for key, value in (("w:val", "single"), ("w:sz", "6"), ("w:space", "1"),
                       ("w:color", "000000")):
        bottom.set(qn(key), value)
    borders.append(bottom)
    ppr.append(borders)


def page_break(doc):
    doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)


# ============================================================== 封面、修改控制、目录

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


PAGE_PLACEHOLDER = "（分页后填）"


def add_cover(doc, meta):
    left, center = WD_ALIGN_PARAGRAPH.LEFT, WD_ALIGN_PARAGRAPH.CENTER
    styled_paragraph(doc, "密级：%s" % meta["密级"], HEI, 18, align=left)
    for key in ("文档编号", "项目名称", "项目编号"):
        styled_paragraph(doc, "%s：%s" % (key, meta[key]), KAI, 14, align=left)
    for _ in range(5):
        styled_paragraph(doc, "", KAI, 14)
    styled_paragraph(doc, meta["文档名称"], KAI, 42, align=center)
    styled_paragraph(doc, "", KAI, 14)
    styled_paragraph(doc, "版本：%s" % meta["版本"], KAI, 14, align=center)
    styled_paragraph(doc, meta["日期"], KAI, 14, align=center)
    for _ in range(6):
        styled_paragraph(doc, "", KAI, 14)
    styled_paragraph(doc, meta["编制单位"], KAI, 15, align=center)
    styled_paragraph(doc, "（版权所有，翻版必究）", HEI, 15, align=center)
    for _ in range(3):
        styled_paragraph(doc, "", KAI, 14)

    # 签署栏：2 行 8 列，第二行合并成「编制 | 姓名 | 批准 | 姓名」，同模板
    author, _, approver = meta["编制 / 审批"].partition(" / ")
    table = doc.add_table(rows=2, cols=8)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    set_widths(table, [Cm(w) for w in (2.0, 2.0, 1.9, 1.9, 1.9, 2.2, 2.4, 2.7)])
    first = ("总页数", PAGE_PLACEHOLDER, "正文", PAGE_PLACEHOLDER, "附录", PAGE_PLACEHOLDER,
             "生效日期", meta["日期"])
    for i, text in enumerate(first):
        cell_text(table.rows[0].cells[i], text, KAI, 10.5, align=WD_ALIGN_PARAGRAPH.CENTER)
    row = table.rows[1].cells
    name1 = row[1].merge(row[3])
    name2 = row[5].merge(row[7])
    cell_text(row[0], "编制", KAI, 10.5, align=WD_ALIGN_PARAGRAPH.CENTER)
    cell_text(name1, author, KAI, 12, align=WD_ALIGN_PARAGRAPH.CENTER)
    cell_text(row[4], "批准", KAI, 10.5, align=WD_ALIGN_PARAGRAPH.CENTER)
    cell_text(name2, approver, KAI, 12, align=WD_ALIGN_PARAGRAPH.CENTER)


def add_revisions(doc, revisions, rows_at_least=6):
    styled_paragraph(doc, "文件修改控制", HEI, 16, align=WD_ALIGN_PARAGRAPH.CENTER)
    styled_paragraph(doc, "", SONG, 10.5)
    total = max(len(revisions), rows_at_least)
    table = doc.add_table(rows=1 + total, cols=4)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    set_widths(table, [Cm(w) for w in (2.3, 2.3, 10.0, 2.4)])
    center = WD_ALIGN_PARAGRAPH.CENTER
    for i, text in enumerate(("修改编号", "版本", "修改条款及内容", "修改日期")):
        cell = table.rows[0].cells[i]
        cell_text(cell, text, KAI, 12, bold=True, align=center)
        shade(cell, REVISION_SHADE)
    for r in range(total):
        row = revisions[r] if r < len(revisions) else ["", "", "", ""]
        cells = table.rows[r + 1].cells
        cell_text(cells[0], row[0], SONG, 12, align=center)
        cell_text(cells[1], row[1], SONG, 12, align=center)
        cell_text(cells[2], row[2], KAI, 12, align=WD_ALIGN_PARAGRAPH.JUSTIFY, markdown=True)
        cell_text(cells[3], row[3], SONG, 12, align=center)


def add_toc(doc):
    styled_paragraph(doc, "目  录", SONG, 15, bold=True, align=WD_ALIGN_PARAGRAPH.CENTER)
    paragraph = doc.add_paragraph()
    paragraph.paragraph_format.first_line_indent = Pt(0)
    run = paragraph.add_run()
    set_fonts(run)
    for kind, text in (("begin", None), ("instr", r'TOC \o "1-3" \h \z \u'),
                       ("separate", None), ("text", "（在 Word 里右键「更新域」生成目录与页码）"),
                       ("end", None)):
        if kind == "instr":
            node = OxmlElement("w:instrText")
            node.set(qn("xml:space"), "preserve")
            node.text = text
        elif kind == "text":
            node = OxmlElement("w:t")
            node.text = text
        else:
            node = OxmlElement("w:fldChar")
            node.set(qn("w:fldCharType"), kind)
        run._element.append(node)


# ============================================================== 正文

def image_size(path):
    from PIL import Image
    with Image.open(path) as image:
        return image.size


def caption_text(text):
    """「图 3-1　账号…」→ 模板写法「图3-1  账号…」。"""
    m = CAPTION.match(text.strip())
    return "%s%s  %s" % (m.group(1), m.group(2), m.group(3)) if m else text.strip("*")


def add_caption(doc, text):
    paragraph = doc.add_paragraph()
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    paragraph.paragraph_format.first_line_indent = Pt(0)
    paragraph.paragraph_format.keep_with_next = text.startswith("表")
    run = paragraph.add_run(text)
    set_fonts(run)
    run.font.size = Pt(10.5)


def add_figure(doc, name, caption, version):
    """插一张图，图题在图下。宽图单开一个横向页，插完再切回纵向。

    **缺图就报错**：静默跳过的话，Word 版会少一张图而没人发现。"""
    path = os.path.join(FIG_DIR, name + ".png")
    if not os.path.exists(path):
        raise SystemExit("缺少 %s，先运行 python scripts/gen_diagrams.py" % path)
    width, height = image_size(path)
    landscape = width / float(height) > 1.25 and width > 2600
    if landscape:
        section = doc.add_section(WD_SECTION.NEW_PAGE)
        set_page(section, True)
        set_header(section, version)
    section = doc.sections[-1]
    max_w = text_width(section)
    max_h = section.page_height - section.top_margin - section.bottom_margin - Cm(2.5)
    scale = min(max_w / float(width), max_h / float(height))
    paragraph = doc.add_paragraph()
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    paragraph.paragraph_format.first_line_indent = Pt(0)
    paragraph.paragraph_format.line_spacing = 1.0
    paragraph.paragraph_format.keep_with_next = True
    paragraph.add_run().add_picture(path, width=int(width * scale),
                                    height=int(height * scale))
    add_caption(doc, caption_text(caption))
    if landscape:
        section = doc.add_section(WD_SECTION.NEW_PAGE)
        set_page(section, False)
        set_header(section, version)


def add_code(doc, lines):
    paragraph = doc.add_paragraph()
    pf = paragraph.paragraph_format
    pf.left_indent = Pt(21)
    pf.first_line_indent = Pt(0)
    pf.line_spacing = 1.0
    pf.space_before = Pt(3)
    pf.space_after = Pt(6)
    run = paragraph.add_run("\n".join(lines))
    set_fonts(run, SONG, CODE)
    run.font.size = Pt(9)


def split_row(line):
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def display_width(text):
    """估计一段文字的显示宽度：汉字、全角符号算 2，其余算 1。"""
    plain = re.sub(r"\*\*|`", "", text)
    return sum(2 if ord(ch) > 0x2E7F else 1 for ch in plain)


UNIT = Cm(0.19)          # 五号字下半个汉字（一个英文字符）大约的宽度
CELL_PADDING = Cm(0.5)
SHORT_COLUMN = 16        # 最长一格不超过 8 个汉字的列算「短列」


def column_widths(rows, total):
    """列宽按内容分配：模板里「序号」列是窄的，说明列最宽。

    **短列给到恰好放下最长一格**（序号、功能点、方法这类，折行就难看了）；
    剩下的宽度按需求量分给长列，需求量取「最长一格」与「平均长度两倍」
    中较小的那个——只看最长一格的话，一格超长的说明会把整列撑到挤扁别的列。"""
    cols = len(rows[0])
    fixed, need = {}, {}
    for c in range(cols):
        cells = [display_width(r[c]) if c < len(r) else 0 for r in rows]
        header = display_width(rows[0][c])
        longest, average = max(cells), sum(cells) / float(len(cells))
        if longest <= SHORT_COLUMN:
            fixed[c] = int(max(longest, header) * UNIT + CELL_PADDING)
        else:
            need[c] = max(header, min(longest, average * 2 + 4))
    left = total - sum(fixed.values())
    if not need or left < Cm(3) * len(need):
        widths = [fixed.get(c, 0) or int(Cm(3)) for c in range(cols)]
        scale = total / float(sum(widths))
        return [int(w * scale) for w in widths]
    units = float(sum(need.values()))
    return [fixed[c] if c in fixed else int(left * need[c] / units) for c in range(cols)]


def add_table(doc, rows):
    header, body = rows[0], rows[2:]          # rows[1] 是 |---|---| 分隔行
    table = doc.add_table(rows=1 + len(body), cols=len(header))
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    set_widths(table, column_widths([header] + body, text_width(doc.sections[-1])))
    for i, text in enumerate(header):
        cell = table.rows[0].cells[i]
        cell_text(cell, text.replace("**", ""), SONG, 10.5, bold=True,
                  align=WD_ALIGN_PARAGRAPH.CENTER)
        shade(cell, HEADER_SHADE)
    for r, row in enumerate(body, 1):
        for i in range(len(header)):
            cell_text(table.rows[r].cells[i], row[i] if i < len(row) else "",
                      SONG, 10.5, markdown=True)
    # 表头在跨页时重复；一行不许拆到两页（否则下一页顶上会孤零零剩半行）
    for index, row in enumerate(table.rows):
        trpr = row._tr.get_or_add_trPr()
        cant = OxmlElement("w:cantSplit")
        cant.set(qn("w:val"), "true")
        trpr.append(cant)
        if index == 0:
            repeat = OxmlElement("w:tblHeader")
            repeat.set(qn("w:val"), "true")
            trpr.append(repeat)


def convert(doc, text, version):
    lines = text.split("\n")
    i = 0
    table_buffer = []

    def flush_table():
        if table_buffer:
            add_table(doc, [split_row(line) for line in table_buffer])
            table_buffer.clear()
            styled_paragraph(doc, "", SONG, 10.5)

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        figure = DIAGRAM_BEGIN.match(stripped)
        if figure:                                # 图 → 渲染好的 PNG
            flush_table()
            name = figure.group(1)
            end = "<!-- DIAGRAM:%s:END -->" % name
            caption = name
            i += 1
            while i < len(lines) and lines[i].strip() != end:
                candidate = lines[i].strip()
                if candidate.startswith("**图") and candidate.endswith("**"):
                    caption = candidate
                i += 1
            add_figure(doc, name, caption, version)
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
            # 源文件的「## 1 文档概述」是一级标题（「#」只用于文档名，已做进封面）
            level = len(stripped) - len(stripped.lstrip("#")) - 1
            heading = doc.add_heading(level=max(1, min(level, 4)))
            heading.text = ""
            size = {1: 22, 2: 16, 3: 16, 4: 14}[max(1, min(level, 4))]
            east = {1: SONG, 2: HEI, 3: SONG, 4: HEI}[max(1, min(level, 4))]
            add_runs(heading, stripped.lstrip("# ").strip(), east, size, bold=True)
            i += 1
            continue

        if stripped in ("---", ""):
            i += 1
            continue

        if CAPTION.match(stripped):                # 表题在表上，图题已随图处理
            add_caption(doc, caption_text(stripped))
            i += 1
            continue

        # 源文件为了好读会把一段话折成几行；Word 里要拼回一段，
        # 否则每一行都成了一个段落，句子在半中间断开
        if stripped.startswith(">"):
            parts = [stripped.lstrip(">").strip()]
            i += 1
            while (i < len(lines) and lines[i].strip().startswith(">")
                   and lines[i].strip().lstrip(">").strip()):
                parts.append(lines[i].strip().lstrip(">").strip())
                i += 1
            if parts[0]:
                paragraph = doc.add_paragraph()
                paragraph.paragraph_format.left_indent = Pt(21)
                paragraph.paragraph_format.first_line_indent = Pt(0)
                add_runs(paragraph, join_lines(parts), KAI)
            continue

        bullet = re.match(r"^[-*] ", stripped)
        number = re.match(r"^\d+[.、] ", stripped)
        if bullet or number:
            parts = [stripped[bullet.end():] if bullet else stripped[number.end():]]
            i += 1
            while (i < len(lines) and lines[i].startswith((" ", "\t"))
                   and lines[i].strip() and not starts_block(lines[i].strip())):
                parts.append(lines[i].strip())
                i += 1
            paragraph = doc.add_paragraph(style="List Bullet" if bullet else "List Number")
            add_runs(paragraph, join_lines(parts))
            continue

        parts = [stripped]
        i += 1
        while i < len(lines) and lines[i].strip() and not starts_block(lines[i].strip()):
            parts.append(lines[i].strip())
            i += 1
        paragraph = doc.add_paragraph()
        paragraph.paragraph_format.first_line_indent = Pt(21)   # 段首缩进两字
        add_runs(paragraph, join_lines(parts))

    flush_table()


def starts_block(stripped):
    """这一行是不是另起一个块（标题、表格、代码、引用、列表、图、表题）。"""
    return bool(stripped.startswith(("#", "|", "```", ">", "<!--", "---"))
                or re.match(r"^[-*] |^\d+[.、] ", stripped)
                or CAPTION.match(stripped))


def join_lines(parts):
    """把折行拼回去：中文之间直接相连，英文单词之间补一个空格。"""
    text = parts[0]
    for part in parts[1:]:
        if text and part and re.match(r"[A-Za-z0-9`*)]", text[-1]) \
                and re.match(r"[A-Za-z0-9`*(]", part[0]):
            text += " " + part
        else:
            text += part
    return text


# ============================================================== 组装

def build():
    text = io.open(SRC, encoding="utf-8").read()
    meta, revisions = read_front(text)
    body = text[text.index("## 1 文档概述"):]

    doc = Document()
    setup_styles(doc)
    strip_japanese(doc)
    set_page(doc.sections[0], False)

    # 第 1 节：封面、文件修改控制、目录——不带页眉
    add_cover(doc, meta)
    page_break(doc)
    add_revisions(doc, revisions)
    page_break(doc)
    add_toc(doc)

    # 第 2 节起：正文，带页眉
    section = doc.add_section(WD_SECTION.NEW_PAGE)
    set_page(section, False)
    set_header(section, meta["版本"])
    convert(doc, body, meta["版本"])
    return doc


def fingerprint(doc):
    """文档内容的指纹：段落文字、表格文字、图片数与各图的字节摘要。

    **不比 docx 的字节**：每次保存的 zip 都不一样。不比目录（`--update-toc`
    让 Word 把它展开成带页码的段落），也不比封面签署栏里的页数——
    那两处都是分页之后才有的排版结果，不是内容。"""
    parts = []
    for paragraph in doc.paragraphs:
        style = (paragraph.style.name or "").lower() if paragraph.style is not None else ""
        if style.startswith("toc") or "更新域" in paragraph.text:
            continue
        if paragraph.text.strip():
            parts.append(paragraph.text.strip())
    for index, table in enumerate(doc.tables):
        if index == 0:
            continue                              # 封面签署栏
        for row in table.rows:
            parts.append("|".join(cell.text.strip() for cell in row.cells))
    blobs = sorted(hashlib.sha256(part.blob).hexdigest()
                   for part in doc.part.package.parts
                   if str(part.partname).startswith("/word/media/"))
    parts.append("images:%d:%s" % (len(blobs), ",".join(blobs)))
    return "\n".join(parts)


# 让 Word 更新目录、按分页结果填封面签署栏的页数。
# 走 -EncodedCommand（UTF-16LE 的 base64）：脚本里有中文，直接用 -Command
# 传会被当成系统代码页解释，中文全乱。
WORD_FINISH = r"""
$ErrorActionPreference = 'Stop'
$w = New-Object -ComObject Word.Application
$w.Visible = $false
$d = $w.Documents.Open('%(path)s')
foreach ($t in $d.TablesOfContents) { $t.Update() }
$d.Repaginate()
$total = $d.ComputeStatistics(2)
$bodyStart = 0; $appendixStart = 0
foreach ($p in $d.Paragraphs) {
  if ($p.OutlineLevel -ne 1) { continue }
  $text = $p.Range.Text.Trim()
  if ($bodyStart -eq 0 -and $text.StartsWith('%(first)s')) { $bodyStart = $p.Range.Information(3) }
  if ($text.StartsWith('%(appendix)s')) { $appendixStart = $p.Range.Information(3); break }
}
if ($appendixStart -eq 0) { $appendixStart = $total + 1 }
$t = $d.Tables(1)
$t.Cell(1, 2).Range.Text = [string]$total
$t.Cell(1, 4).Range.Text = [string]($appendixStart - $bodyStart)
$t.Cell(1, 6).Range.Text = [string]($total - $appendixStart + 1)
foreach ($t in $d.TablesOfContents) { $t.Update() }
$d.Save(); $d.Close(); $w.Quit()
Write-Output ("pages total={0} body={1} appendix={2}" -f $total, ($appendixStart - $bodyStart), ($total - $appendixStart + 1))
"""


def finish_with_word(path):
    script = WORD_FINISH % {"path": path.replace("'", "''"),
                            "first": "1 文档概述", "appendix": "附录"}
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    done = subprocess.run(["powershell", "-NoProfile", "-EncodedCommand", encoded],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    if done.returncode != 0:
        print("  ! Word 后处理失败（需要本机装有 Word）：%s" % (done.stderr or "").strip()[-400:])
        return False
    print("  %s" % done.stdout.strip())
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
            print("重新生成：python scripts/gen_outline_docx.py --update-toc")
            return 1
        print("Word 版是最新的。")
        return 0

    try:
        doc.save(OUT)
    except PermissionError:
        print("  ! 写不进 %s：文件正被 Word / WPS 打开，关掉再生成。" % os.path.basename(OUT))
        return 1
    images = len([p for p in doc.part.package.parts
                  if str(p.partname).startswith("/word/media/")])
    print("生成 %s（%d 段、%d 张表、%d 张图）"
          % (os.path.basename(OUT), len(doc.paragraphs), len(doc.tables), images))
    if args.update_toc and os.name == "nt":
        if finish_with_word(OUT):
            print("目录页码与封面页数已由 Word 更新。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
