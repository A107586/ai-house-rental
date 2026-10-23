"""定时提醒：快到期的账单、已逾期的账单、快到期的租约（2026-10-23 补）。

## 为什么要有它

原先平台上所有提醒都是**被动**的：账单到没到期、租约还剩几天，只在用户自己打开
个人中心或工作台时才看得到；唯一主动发出的「催缴」要房东一笔一笔去点。
真实的租房平台里，「明天该交房租了」「租约还有一个月到期」是租客收到最多的两类通知——
没人会每天打开 App 数日子。

## 三条约束

1. **同一件事只提醒一次。** 每类提醒以「谁、什么事、哪张单」去重：账单号或合同号记在
   站内信的 link_no 上，已经发过就不再发。没有这一条，每跑一轮就多一条，用户第二天就关通知。
2. **只提醒还能办的事。** 合同结束了的欠款在线上已经交不了，不发「即将到期」；
   已经有退租 / 续租申请在走的租约，不再发「快到期了」。
3. **跑多少次都一样。** 进程重启会立刻跑一轮、多个 worker 会各跑一轮——
   因为有第 1 条，结果都相同。真要上多 worker，去重应改成数据库唯一约束，这里先靠查询。
"""

import os
import threading
import time
from datetime import datetime, timedelta

from sqlalchemy import select

from app.leasing import START_BACK_DAYS, cancel_contract, notify
from app.models import Bill, Contract, Message

DUE_SOON_DAYS = 3          # 到期前几天提醒
LEASE_SOON_DAYS = 30       # 租约到期前几天提醒

TITLE_DUE_SOON = "账单快到期了"
TITLE_OVERDUE = "账单已逾期"
TITLE_LEASE_SOON = "租约快到期了"
TITLE_LEASE_OVER = "租约已到期"
TITLE_CONTRACT_WAITING = "有一份合同等你确认"
CONTRACT_WAIT_DAYS = 2     # 待确认合同发出几天后提醒租客


def _sent(session, user_id, title, link_no):
    return session.scalar(select(Message.id).where(
        Message.user_id == user_id, Message.title == title,
        Message.link_no == link_no).limit(1)) is not None


def _once(session, user_id, type_, title, content, link_no):
    if not user_id or _sent(session, user_id, title, link_no):
        return 0
    notify(session, user_id, type_, title, content, link_no)
    return 1


def _money(value):
    text = "%.2f" % float(value or 0)
    return text.rstrip("0").rstrip(".")


def run(session, now=None):
    """跑一轮，返回各类发了几条。不提交——调用方决定什么时候 commit。"""
    now = now or datetime.now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    counts = {"due_soon": 0, "overdue": 0, "lease_soon": 0, "lease_over": 0,
              "contract_waiting": 0, "contract_stale": 0}

    # ---- 待确认的合同：等太久的提醒租客一次；起租日已经过去太久的，确认不了了，自动作废
    for contract in session.scalars(select(Contract).where(Contract.status == "pending")).all():
        if (today - contract.start_date).days > START_BACK_DAYS:
            # 租客确认时会再查一次起租日（往前最多补 30 天），过了线就永远确认不了，
            # 原先它就一直挂在两边的列表里（2026-10-24）
            cancel_contract(session, contract, "system", "起租日已经过去太久，这份合同自动失效")
            notify(session, contract.landlord_id, "contract", "待确认的合同已失效",
                   "合同 %s 起租日 %s 已经过去太久，租客一直没有确认，已自动失效；要签的话请重新发起。"
                   % (contract.contract_no, contract.start_date.strftime("%Y-%m-%d")),
                   contract.contract_no)
            counts["contract_stale"] += 1
        elif contract.created_at and (now - contract.created_at).days >= CONTRACT_WAIT_DAYS:
            counts["contract_waiting"] += _once(
                session, contract.tenant_id, "contract", TITLE_CONTRACT_WAITING,
                "房东发起的合同 %s 还在等你确认，起租日 %s。可以在「我的租约」里确认、"
                "或者点「不签了」。" % (contract.contract_no,
                                       contract.start_date.strftime("%Y-%m-%d")),
                contract.contract_no)

    bills = session.scalars(select(Bill).join(Contract, Bill.contract_id == Contract.id).where(
        Bill.status == "unpaid", Contract.status == "active",
        Bill.due_date < today + timedelta(days=DUE_SOON_DAYS + 1))).all()
    for bill in bills:
        contract = bill.contract
        label = bill.period or bill.remark or "账单"
        if bill.due_date >= today:
            counts["due_soon"] += _once(
                session, contract.tenant_id, "bill", TITLE_DUE_SOON,
                "%s %s 元，%s 到期，可以在「我的账单」里缴。"
                % (label, _money(bill.amount), bill.due_date.strftime("%m-%d")),
                bill.bill_no)
        else:
            counts["overdue"] += _once(
                session, contract.tenant_id, "bill", TITLE_OVERDUE,
                "%s %s 元已于 %s 到期，还没有缴。有困难可以先和房东沟通。"
                % (label, _money(bill.amount), bill.due_date.strftime("%m-%d")),
                bill.bill_no)

    contracts = session.scalars(select(Contract).where(
        Contract.status == "active",
        Contract.end_date < today + timedelta(days=LEASE_SOON_DAYS + 1))).all()
    for contract in contracts:
        if contract.end_date < now:
            # 到期了还没办退租：这一步要房东办（押金结算、验房）
            counts["lease_over"] += _once(
                session, contract.landlord_id, "contract", TITLE_LEASE_OVER,
                "合同 %s 已于 %s 到期，请办理退租结算，或者和租客续租。"
                % (contract.contract_no, contract.end_date.strftime("%Y-%m-%d")),
                contract.contract_no)
            continue
        if contract.request_type:
            continue          # 已经有退租 / 续租在走了，不必再催
        days = (contract.end_date.date() - today.date()).days
        text = ("合同 %s 还有 %d 天到期（%s）。要续租可以在合同上点「申请续租」，不续的话记得提前约好退租。"
                % (contract.contract_no, days, contract.end_date.strftime("%Y-%m-%d")))
        for user_id in (contract.tenant_id, contract.landlord_id):
            counts["lease_soon"] += _once(session, user_id, "contract", TITLE_LEASE_SOON,
                                          text, contract.contract_no)
    session.flush()
    return counts


# ---------------------------------------------------------------- 后台定时

def start_background(session_factory, interval_hours=None):
    """起一个守护线程，进程启动时跑一轮，之后每隔几小时一轮。

    内存库（测试）和 REMINDER_INTERVAL_HOURS=0 都不起。
    """
    hours = interval_hours
    if hours is None:
        try:
            hours = float(os.environ.get("REMINDER_INTERVAL_HOURS", "6"))
        except ValueError:
            hours = 6
    if hours <= 0:
        return None

    def loop():
        while True:
            session = session_factory()
            try:
                run(session)
                session.commit()
            except Exception:                     # noqa: BLE001 - 提醒挂了不能拖垮服务
                session.rollback()
            finally:
                session.close()
            time.sleep(hours * 3600)

    thread = threading.Thread(target=loop, name="reminders", daemon=True)
    thread.start()
    return thread
