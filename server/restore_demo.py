# -*- coding: utf-8 -*-
"""把标准演示数据库装回 rental.db。

    python restore_demo.py          # 会先问一句
    python restore_demo.py --yes    # 不问，直接覆盖

## 为什么要问一句

它**覆盖的是你当前的工作库**。里面可能有你刚注册的测试账号、
刚发布的房源、刚跑通的那条支付流水——装回去之后一个都不剩。

而这条命令最常用的场合恰恰是「我把数据搞乱了」，那时候手快按回车
是常事。所以除非明确加了 `--yes`，都要先看一眼将要丢掉什么。
"""

import argparse
import io
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime

from app.console import force_utf8  # noqa: E402

force_utf8()

HERE = os.path.dirname(os.path.abspath(__file__))
DEMO = os.path.join(HERE, "demo", "rental_demo.db")
LIVE = os.path.join(HERE, "rental.db")
MANIFEST = os.path.join(HERE, "demo", "manifest.json")

# 覆盖前先备份。**不是为了谨慎，是为了后悔**——
# 装回去之后才想起来「刚才那个账号还有用」是很常见的
BACKUP_DIR = os.path.join(HERE, "demo", "backups")


def summarize(path):
    """数一遍这个库里有什么。数不出来就说数不出来，不猜。"""
    if not os.path.exists(path):
        return None
    try:
        db = sqlite3.connect(path)
        out = {}
        for table in ("users", "houses", "contracts", "bills", "reviews"):
            try:
                out[table] = db.execute(
                    "select count(*) from %s" % table).fetchone()[0]
            except sqlite3.Error:
                out[table] = "?"
        db.close()
        return out
    except sqlite3.Error as exc:
        return {"打不开": str(exc)}


def main():
    parser = argparse.ArgumentParser(description="把标准演示数据库装回 rental.db")
    parser.add_argument("--yes", action="store_true", help="不询问，直接覆盖")
    parser.add_argument("--no-backup", action="store_true",
                        help="不备份当前的 rental.db")
    args = parser.parse_args()

    if not os.path.exists(DEMO):
        print("找不到 %s" % DEMO)
        return 1

    if os.path.exists(MANIFEST):
        meta = json.load(io.open(MANIFEST, encoding="utf-8"))
        print("演示库：生成于 %s，来自 seed.py（%s）"
              % (meta.get("generated_at", "?"),
                 meta.get("seed_sha256_16", "?")))

    live = summarize(LIVE)
    if live:
        print("\n当前的 rental.db 里有：")
        for key, value in live.items():
            print("  %-10s %s" % (key, value))
        print("\n**这些会被全部覆盖。**")
    else:
        print("\n当前没有 rental.db，直接装。")

    if not args.yes and live:
        try:
            answer = input("\n确定要覆盖吗？输入 yes 继续：").strip().lower()
        except EOFError:
            # 非交互环境（CI、管道）里读不到输入。**默认不覆盖**——
            # 一条会删数据的命令，不该因为没人回答就自己往下走
            print("非交互环境，没有确认。要覆盖请加 --yes")
            return 2
        if answer != "yes":
            print("已取消，什么都没动。")
            return 0

    if live and not args.no_backup:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = os.path.join(BACKUP_DIR, "rental-%s.db" % stamp)
        shutil.copy(LIVE, backup)
        print("\n原库已备份到 %s" % os.path.relpath(backup, HERE))

    shutil.copy(DEMO, LIVE)
    print("已装回 rental.db")

    got = summarize(LIVE)
    if got:
        print("\n现在库里有：")
        for key, value in got.items():
            print("  %-10s %s" % (key, value))
    print("\n演示账号：admin/admin123 · landlord1/test123 · "
          "tenant1/test123 · agent1/test123")
    return 0


if __name__ == "__main__":
    sys.exit(main())
