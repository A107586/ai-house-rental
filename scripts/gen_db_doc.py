"""从 models.py 生成《数据库设计说明书》的表结构章节。

**手抄一份表结构必然会和代码脱节** —— 改了字段忘了改文档，是这类文档
最常见的死法。这里直接从 SQLAlchemy 的元数据读，生成的内容天然与代码
一致，改完模型重跑一次即可。

表的中文名与字段说明在 `db_meta.py`：**同一份说明现在有三个出口**
（本文档、可建库的 DDL、交付用的数据库设计书），抄三份必然会散。
"""
import io
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT and os.path.join(_ROOT, 'scripts'))
sys.path.insert(0, os.path.join(_ROOT, 'server'))

from app.console import force_utf8  # noqa: E402
from db_meta import (COLUMN_NOTES, TABLE_INFO,  # noqa: E402
                     load_metadata, ordered_tables)

force_utf8()
metadata = load_metadata()


def type_name(col):
    t = str(col.type)
    return {
        "INTEGER": "int",
        "TEXT": "text",
        "DATETIME": "datetime",
        "BOOLEAN": "bool",
        "FLOAT": "float",
    }.get(t, t.lower())


lines = []
add = lines.append

add("## 三、表结构")
add("")
add("> 本章由 `scripts/gen_db_doc.py` 从 `server/app/models.py` 的模型定义")
add("> 直接生成。**手抄一份表结构必然会和代码脱节**——改了字段忘了改文档，")
add("> 是这类文档最常见的死法。改完模型重跑一次脚本即可。")
add("")

ordered = ordered_tables(metadata)

tables = {t.name: t for t in metadata.sorted_tables}

for i, name in enumerate(ordered, 1):
    t = tables[name]
    cn, purpose = TABLE_INFO.get(name, (name, ""))
    add("### 3.%d `%s` — %s" % (i, name, cn))
    add("")
    if purpose:
        add("> %s" % purpose)
        add("")
    add("| 字段 | 类型 | 空 | 键 | 说明 |")
    add("|---|---|:---:|:---:|---|")
    for c in t.columns:
        keys = []
        if c.primary_key:
            keys.append("PK")
        if c.foreign_keys:
            keys.append("FK→" + str(list(c.foreign_keys)[0].target_fullname))
        if c.unique:
            keys.append("UQ")
        if c.index:
            keys.append("IDX")
        note = COLUMN_NOTES.get((name, c.name), "")
        add("| `%s` | %s | %s | %s | %s |" % (
            c.name, type_name(c), "否" if not c.nullable else "是",
            " ".join(keys) or "—", note))
    add("")

out = "\n".join(lines)
# 只重写《数据库设计说明书》的第三章，前后各章是手写的分析内容，不动
doc_path = os.path.join(_ROOT, 'docs', '数据库设计说明书.md')
whole = io.open(doc_path, encoding='utf-8').read()
start = whole.index('## 三、表结构')
end = whole.index('## 四、索引设计')
io.open(doc_path, 'w', encoding='utf-8', newline='').write(
    whole[:start] + out.rstrip() + "\n\n---\n\n" + whole[end:])
print("生成 %d 张表，%d 行" % (len(ordered), len(lines)))
