# -*- coding: utf-8 -*-
"""把已有库里的历史遗留数据修一遍（一次性）。

    cd server
    python ../scripts/repair_data.py            # 只看会改什么
    python ../scripts/repair_data.py --write    # 真的改

`seed.py` 里的毛病改掉之后，**新播的库是对的，已经在跑的库还是旧的**。
重新播种能一次解决，代价是把手动注册的测试账号一起抹掉；
所以这里逐条修，只动证据确凿的那些。

每一条都写明起因。修完一次就不会再匹配到东西，留着当记录。
"""

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

from app.console import force_utf8  # noqa: E402

force_utf8()

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import Contract, House  # noqa: E402


def _generated_title(house, area_text):
    return "%s%s %s %s平" % (house.district or "", house.community or "",
                             house.layout or "", area_text)


def fix_house_titles(session, write):
    """标题里的面积和下面那行对不上。

    `seed.py` 生成标题时用 `int(area)`（截断），而前端两处显示面积用的
    都是 `Math.round`：房源卡片的规格行、房东发布时自动生成的标题。
    于是 63.8 平的房子，**同一张卡片上标题写「63平」、下一行写「64 平米」**，
    98 套在架房源里 42 套对不上。

    房东可以自己改标题，所以只在标题**正好等于**那条生成规则的旧产物时
    才动它——那说明没人改过，而且确实停在旧算法上。
    """
    changed = skipped = 0
    for house in session.scalars(select(House)).all():
        area = float(house.area or 0)
        if area <= 0 or not house.title:
            continue
        stale = _generated_title(house, int(area))
        fresh = _generated_title(house, round(area))
        if stale == fresh or house.title == fresh:
            continue
        if house.title != stale:
            skipped += 1               # 不是生成的那条，别碰
            continue
        changed += 1
        if write:
            house.title = fresh
    return changed, "标题被人改过、没动 %d 条" % skipped


def settle_ended_contracts(session, write):
    """已结束的合同上挂着**永远缴不掉**的账单。

    两份历史合同建完直接 `end_contract`，中间没人缴费，于是 25 笔账单
    从 2025 年 7 月一路欠到现在。演示账号 tenant1 打开「我的账单」，
    看到的是「34 笔待缴，合计 44200 元，最早 2025-07-01 到期」。

    比数字难看更要命的是：`end_contract` 只作废**未到期**的租金账单，
    这些早就到期了，于是留在 unpaid；而缴费接口有一道
    「该合同已结束，无法继续缴费」的拦截。**界面上摆着「去缴费」，
    点下去必然失败**，而且没有任何一条路能让它变成已缴。

    所以这里标成 `cancelled`（退租结算时核销），不标 `paid`：
    没人真的付过钱，凭空造一笔支付记录会让对账对不平——
    这正是 `end_contract` 里那段押金退款注释说过的同一件事。

    `seed.py` 已经改成结束前先结清，新播的库不会再有这种账单。
    """
    changed = 0
    rows = session.scalars(
        select(Contract).where(Contract.status == "ended")).all()
    for contract in rows:
        for bill in contract.bills or []:
            if bill.status != "unpaid":
                continue
            changed += 1
            if write:
                bill.status = "cancelled"
                bill.remark = (
                    (bill.remark or "") + " · 退租结算时核销").strip(" ·")
    return changed, "已结束的合同 %d 份" % len(rows)


REPAIRS = (("房源标题里的面积", fix_house_titles),
           ("已结束合同的欠账", settle_ended_contracts))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true", help="真的写库")
    args = parser.parse_args()

    session = SessionLocal()
    try:
        for label, repair in REPAIRS:
            count, note = repair(session, args.write)
            print("%-14s 需要改 %3d 条%s（%s）"
                  % (label, count, "，已写入" if args.write and count else "", note))
        if args.write:
            session.commit()
    finally:
        session.close()
    print("\n预演结束，什么都没改。加 --write 才写库。" if not args.write else "\n完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
