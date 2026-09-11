# -*- coding: utf-8 -*-
"""把清单里填好的「改为」写回代码。

用法：先 `--dry` 看一遍要改什么，确认无误再去掉这个参数。

## 三条不肯让步的规则

**一、按 `#序号` 定位，不按文本匹配。**
同一句「确定」在界面上出现十几次，文本匹配分不清改的是哪一处；
而且填表的人很可能顺手把「原文」那一栏也动了，那样匹配当场失效。
序号对应的是 copy_rows.json 里记着的**文件 + 行号 + 原文**三件套。

**二、写回前先核对原文还在不在。**
从生成清单到写回之间，代码可能已经改过了。这时候按行号硬替换会写到
别的句子上——而且不会报错，只是悄悄改错一处。对不上就跳过并报出来。

**三、`${...}` 的数量必须一致。**
少一个坑位，运行时那一屏就白了；而且这类错误编译期发现不了，
要等用户点到那一屏才暴露。数量对不上直接拒绝写入这一条。

## 「改为」是哪一格

**永远是最后一格**，按位置取，不按列数数。

第一版写的是「去掉末尾的空串再取最后一个」——在主表（4 列）上是对的，
可「带变量」和「不要改」两张表是 5 列，空的「改为」被去掉之后，
**「位置」那一列顶上来变成了新文案**。dry-run 里于是冒出 168 条
根本没人填的「修改」，差点把 `App.jsx:828` 这种字符串写进界面。
"""

import io
import json
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(_ROOT, "web", "src")
DOC = os.path.join(_ROOT, "docs", "前端文案清单_2026-09-12.md")
ROWS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "copy_rows.json")

SLOT_RE = re.compile(r"\$\{[^}]*\}")
ARROW = " \u23ce "          # 渲染时把换行写成了这个符号


def unescape(text):
    return text.replace("\\|", "|").replace(ARROW, "\n").strip()


def read_edits():
    """把清单里填了「改为」的行读出来。"""
    edits = {}
    for raw in io.open(DOC, encoding="utf-8"):
        line = raw.rstrip("\n")
        if not line.startswith("|"):
            continue
        cells = line.split("|")
        # `| 1 | 原文 | 位置 | 改为 |` 切开后首尾各有一个空串
        if len(cells) < 4:
            continue
        rid = cells[1].strip()
        if not rid.isdigit():
            continue
        new = unescape(cells[-2])
        if new:
            edits[int(rid)] = new
    return edits


def _locate(lines, idx, old, window=4):
    """在记下的行号附近找这句话，找不到返回 None。

    **为什么要允许偏几行**：这份清单人要改几天，而这几天里代码也在动。
    上面加三行注释，下面所有行号就整体后移三行——按行号硬找会全军覆没，
    而那些句子明明还在原地。

    **为什么只找附近几行、不全文搜**：全文搜会撞上重名。「确定」这两个字
    在一个文件里可能有十几处，搜到哪个算哪个，改错了还不报错。
    行号在这里的作用是「大致位置」，原文才是身份证。
    """
    if 0 <= idx < len(lines) and old in lines[idx]:
        return idx
    for offset in range(1, window + 1):
        for probe in (idx - offset, idx + offset):
            if 0 <= probe < len(lines) and old in lines[probe]:
                return probe
    return None


def find_file(name, cache={}):
    if name in cache:
        return cache[name]
    for root, _, files in os.walk(SRC):
        if name in files:
            cache[name] = os.path.join(root, name)
            return cache[name]
    cache[name] = None
    return None


def main():
    dry = "--dry" in sys.argv
    rows = json.load(io.open(ROWS, encoding="utf-8"))
    for i, r in enumerate(rows, start=1):
        r["id"] = i
    by_id = {r["id"]: r for r in rows}

    edits = read_edits()
    if not edits:
        print("清单里没有填写任何「改为」，没有可写的内容")
        return

    print("清单里填了 %d 条\n" % len(edits))
    files, skipped, applied = {}, [], []

    for rid, new in sorted(edits.items()):
        row = by_id.get(rid)
        if not row:
            skipped.append((rid, "序号不存在"))
            continue

        old = row["text"]
        if old == new:
            continue
        n_old, n_new = len(SLOT_RE.findall(old)), len(SLOT_RE.findall(new))
        if n_old != n_new:
            skipped.append((rid, "${...} 坑位数量对不上：原 %d 个，新 %d 个"
                            % (n_old, n_new)))
            continue

        # 同一行里同一句话出现两次时，lines 里会有两条一样的位置。
        # 替换是整行 replace，第一次就把两处都改了，第二次自然对不上——
        # 去重，免得报一堆假的「已对不上原文」
        for loc in dict.fromkeys(row["lines"]):
            name, line_no = loc.rsplit(":", 1)
            path = find_file(name)
            if not path:
                skipped.append((rid, "找不到文件 %s" % name))
                continue
            if path not in files:
                files[path] = io.open(path, encoding="utf-8").read().split("\n")
            lines = files[path]
            idx = _locate(lines, int(line_no) - 1, old)
            if idx is None:
                # 代码已经变了。**按行号硬替换会写到别的句子上**，
                # 而且不报错——只是悄悄改错一处
                skipped.append((rid, "%s 第 %s 行附近找不到原文「%s」"
                                % (name, line_no, old[:20])))
                continue
            lines[idx] = lines[idx].replace(old, new)
            applied.append((name, idx + 1, old, new))

    for name, line_no, old, new in applied:
        print("  %s:%s" % (name, line_no))
        print("    - %s" % old)
        print("    + %s" % new)

    if skipped:
        print("\n跳过 %d 条：" % len(skipped))
        for rid, why in skipped:
            print("  #%d %s" % (rid, why))

    if dry:
        print("\n--dry：没有写入任何文件（实际会改 %d 处）" % len(applied))
        return

    for path, lines in files.items():
        io.open(path, "w", encoding="utf-8", newline="").write("\n".join(lines))
    print("\n写入 %d 处，涉及 %d 个文件" % (len(applied), len(files)))


if __name__ == "__main__":
    main()
