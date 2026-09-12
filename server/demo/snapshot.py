# -*- coding: utf-8 -*-
"""把当前的 rental.db 做成新的演示快照。

    cd server
    python seed.py          # 先播一份干净的
    python demo/snapshot.py # 再快照

**顺序不能反。** 直接快照当前工作库的话，会把你调试时造的账号、
半途取消的合同、点坏的支付单一起带进仓库——那种数据进了演示库，
下一个人拿它做演示时会当场看到一堆解释不清的记录。
"""

import datetime
import hashlib
import io
import json
import os
import shutil
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.dirname(HERE)
LIVE = os.path.join(SERVER, "rental.db")
DEMO = os.path.join(HERE, "rental_demo.db")
SEED = os.path.join(SERVER, "seed.py")

TABLES = ("users", "houses", "contracts", "bills", "reviews",
          "appointments", "repairs", "payments", "house_images")


def main():
    if not os.path.exists(LIVE):
        print("找不到 %s，先跑 python seed.py" % LIVE)
        return 1

    # VACUUM 之后再复制：删除留下的空页不必进仓库，
    # 实测能把 1.8 MB 压到 850 KB 左右
    db = sqlite3.connect(LIVE)
    db.execute("VACUUM")
    db.close()

    shutil.copy(LIVE, DEMO)

    digest = hashlib.sha256(io.open(SEED, "rb").read()).hexdigest()[:16]
    snap = sqlite3.connect(DEMO)
    counts = {}
    for table in TABLES:
        try:
            counts[table] = snap.execute(
                "select count(*) from %s" % table).fetchone()[0]
        except sqlite3.Error:
            counts[table] = None
    snap.close()

    meta = {
        # 记下是哪一版 seed.py 生成的。快照和 seed.py 是两个可能漂移的
        # 真相源，**能对账它们才敢共存**
        "seed_sha256_16": digest,
        "generated_at": datetime.date.today().isoformat(),
        "random_seed": 20260901,
        "counts": counts,
        "size_bytes": os.path.getsize(DEMO),
    }
    io.open(os.path.join(HERE, "manifest.json"), "w", encoding="utf-8").write(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n")

    print("已生成 %s（%.0f KB）" % (os.path.relpath(DEMO, SERVER),
                                 meta["size_bytes"] / 1024))
    for table, value in counts.items():
        print("  %-14s %s" % (table, value))
    print("\n记得把 demo/README.md 里的条数表一起更新。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
