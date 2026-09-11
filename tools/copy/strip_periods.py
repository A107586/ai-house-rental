# -*- coding: utf-8 -*-
"""E3：把 UI 文案末尾的句号去掉，正文保留。

## 分界写死成一条可执行的规则

用户给的口径是「按钮、标签、菜单项、表单占位符、单行提示不带句号；
成段的说明、对话内容、错误解释保留标点」。

「单行提示」和「成段说明」的分界不能靠感觉，否则改到一半就开始
自相矛盾。这里落成一条机械规则：

    **句子内部还有别的标点 → 它是成段的，保留。**
    **通篇只有末尾一个句号，且不长 → 它是一句提示，去掉。**

理由：「没有待审的资质。」内部没有断句，它就是一个标签式的陈述；
而「密码已重置，请用新密码登录。」里有个逗号——它是在讲两件事，
读起来是一句话，句号该留着。

长度上限 20 个字是个保守值：超过 20 个字的单句在界面上几乎总是
说明性文字，不是标签。宁可少改。

## 不碰的东西

- 带 `${...}` 的：改它要重新数坑位，不值得为一个句号冒这个险
- 智能体的回话：那是对话内容，本来就该有标点
- 以问号、感叹号结尾的：那是语气，不是句读
"""
import io
import json
import os
import re

ROWS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "copy_rows.json")
SRC = "C:/Competition1/rental/web/src"

# 句子内部的标点。出现任意一个，就说明这句话不止一个分句。
INNER = "，,；;。！？!?、：:"

MAX_LEN = 20

# **只去「状态陈述」的句号，不去「说明」的。**
#
# 只按长度和标点判断不够——「流水号是与客服核对这笔缴费时要报的号码。」
# 和「没有待审的资质。」一样短、一样只有一个分句，可前者是在解释一件事，
# 后者是一个状态标签。去掉前者的句号，它会读起来像半句话。
#
# 状态陈述的形状很稳定，用开头就能认出来：
#   没有… 还没有… 暂无… 暂时…   —— 空态
#   已…                        —— 操作完成
#   请…                        —— 单行指令
# 除此之外一律不碰。宁可漏改，不能改坏。
STATUS_PREFIXES = ("没有", "还没有", "暂无", "暂时", "已", "请")

# 这些界面里的文字是**对话内容**，不是界面标签——助手和房东说的话
# 本来就该有标点。
SKIP_SCREENS = {"AI 助手对话", "与房东咨询", "助手思考中"}


def candidates():
    rows = json.load(io.open(ROWS, encoding="utf-8"))
    for index, row in enumerate(rows, start=1):
        row["id"] = index

    out = []
    for row in rows:
        text = row["text"]
        if row["screen"] in SKIP_SCREENS:
            continue
        if "${" in text or "{" in text:
            continue
        if not text.endswith("。"):
            continue
        body = text[:-1]
        if len(body) > MAX_LEN:
            continue
        if any(ch in body for ch in INNER):
            continue
        if not body.strip():
            continue
        if not body.lstrip("　 ").startswith(STATUS_PREFIXES):
            continue
        out.append((row, body))
    return out


def main():
    import sys
    apply = "--apply" in sys.argv
    rows = candidates()
    print("符合「单行提示」规则的：%d 条\n" % len(rows))

    files = {}
    changed = skipped = 0
    for row, body in rows:
        for loc in dict.fromkeys(row["lines"]):
            name, line_no = loc.rsplit(":", 1)
            path = None
            for root, _, names in os.walk(SRC):
                if name in names:
                    path = os.path.join(root, name)
                    break
            if not path:
                skipped += 1
                continue
            if path not in files:
                files[path] = io.open(path, encoding="utf-8").read().split("\n")
            lines = files[path]
            idx = int(line_no) - 1
            hit = None
            for probe in range(max(0, idx - 4), min(len(lines), idx + 5)):
                if row["text"] in lines[probe]:
                    hit = probe
                    break
            if hit is None:
                skipped += 1
                continue
            lines[hit] = lines[hit].replace(row["text"], body)
            changed += 1
            print("  %s:%d  %s" % (name, hit + 1, row["text"]))

    print("\n改 %d 处，跳过 %d 处（找不到原文）" % (changed, skipped))
    if not apply:
        print("（没有加 --apply，没有写入任何文件）")
        return
    for path, lines in files.items():
        io.open(path, "w", encoding="utf-8", newline="").write("\n".join(lines))
    print("已写入 %d 个文件" % len(files))


if __name__ == "__main__":
    main()
