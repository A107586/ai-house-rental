"""交易链的业务规则：签约、排账单、缴费、退租。

单独成模块而不是塞进接口层，因为这些规则要被多处复用
（接口、定时任务、测试），而且**必须能脱离 HTTP 单独测试**。

一条完整的链路::

    看房通过 → 生成合同(pending) → 租客确认 → 合同生效(active)
                                        │
                                        ├─► 一次性排出整个租期的账单
                                        │   （押金 1 条 + 租金 N 条）
                                        └─► 房源置为 rented

    缴费 → 账单置 paid，产生流水

    退租 → 合同 ended，房源放回 available，
           未到期的租金账单作废，押金按扣款结算
"""

import uuid
from datetime import datetime, timedelta
from decimal import Decimal

from app.models import Bill, Contract, House, Message, Payment, Refund


def _serial(prefix):
    """生成业务单号。前缀 + 年月日 + 6 位随机，够用且可读。"""
    return "%s%s%s" % (prefix, datetime.now().strftime("%y%m%d"),
                       uuid.uuid4().hex[:6].upper())


def add_months(moment, months):
    """按月加日期，自动处理月末溢出。

    1 月 31 日加一个月应落在 2 月 28／29 日，直接换月份会抛异常。
    """
    year = moment.year + (moment.month - 1 + months) // 12
    month = (moment.month - 1 + months) % 12 + 1
    # 逐日回退到该月存在的最后一天
    day = moment.day
    while day > 0:
        try:
            return moment.replace(year=year, month=month, day=day)
        except ValueError:
            day -= 1
    raise ValueError("无法计算目标日期")


def months_between(start, end):
    """租期包含多少个计费月，不足一月按一月算。"""
    count = (end.year - start.year) * 12 + (end.month - start.month)
    if end.day > start.day:
        count += 1
    return max(count, 1)


# ---------------------------------------------------------------- 签约

def create_contract(session, house, tenant, start_date, months=12,
                    deposit_months=1, pay_months=1, content=""):
    """生成一份待确认合同。

    不在这里改房源状态——合同要等租客确认后才生效，
    在 `activate_contract` 里才占用房源。
    """
    if house.status != "available":
        raise ValueError("该房源当前不可签约")
    if months < 1:
        raise ValueError("租期至少一个月")

    rent = Decimal(str(house.rent_price))
    end_date = add_months(start_date, months)

    contract = Contract(
        contract_no=_serial("HT"),
        house_id=house.id,
        tenant_id=tenant.id,
        landlord_id=house.landlord_id,
        start_date=start_date,
        end_date=end_date,
        rent=rent,
        deposit=rent * deposit_months,
        pay_months=pay_months,
        status="pending",
        content=content or render_contract_text(house, tenant, start_date,
                                                end_date, rent,
                                                rent * deposit_months, pay_months),
    )
    session.add(contract)
    session.flush()
    return contract


def render_contract_text(house, tenant, start_date, end_date, rent,
                         deposit, pay_months):
    """生成合同正文。

    模板化生成即可——AI-02 合同智能审阅要读的就是这段文本，
    所以条款必须写全，包括故意留下的、对租客不利的条款供审阅演示。
    """
    return """房屋租赁合同

出租方（甲方）：{landlord}
承租方（乙方）：{tenant}

第一条 租赁物
甲方将坐落于 {address} 的房屋（编号 {house_no}，建筑面积 {area} 平方米，
户型 {layout}）出租给乙方居住使用。

第二条 租赁期限
租赁期自 {start} 起至 {end} 止。

第三条 租金及支付方式
月租金为人民币 {rent} 元，乙方按每 {pay_months} 个月支付一次，
应于每期首月 5 日前支付当期租金。

第四条 押金
乙方应于签约时支付押金人民币 {deposit} 元。租赁期满且无违约情形的，
甲方应于乙方腾房并结清费用后 7 日内退还押金。

第五条 维修责任
房屋主体结构及原有设施的自然损坏由甲方负责维修；
因乙方使用不当造成的损坏由乙方承担维修费用。

第六条 违约责任
任何一方提前解除合同的，应提前 30 日书面通知对方，
并向对方支付相当于一个月租金的违约金。

第七条 其他约定
乙方不得擅自转租、改变房屋结构或用于非居住用途。
本合同一式两份，甲乙双方各执一份，自双方确认之日起生效。
""".format(
        landlord=(house.landlord.real_name if house.landlord else "房东"),
        tenant=tenant.real_name or tenant.username,
        address=house.address or (house.district + house.community),
        house_no=house.house_no,
        area=int(house.area),
        layout=house.layout,
        start=start_date.strftime("%Y 年 %m 月 %d 日"),
        end=end_date.strftime("%Y 年 %m 月 %d 日"),
        rent=int(rent),
        pay_months=pay_months,
        deposit=int(deposit),
    )


def activate_contract(session, contract):
    """租客确认，合同生效：排账单 + 占用房源。"""
    if contract.status != "pending":
        raise ValueError("只有待确认的合同可以生效")

    house = session.get(House, contract.house_id)
    if not house:
        raise ValueError("房源不存在")
    if house.status != "available":
        raise ValueError("该房源已被占用")

    contract.status = "active"
    contract.signed_at = datetime.now()
    house.status = "rented"

    bills = generate_bills(session, contract)
    notify(session, contract.tenant_id, "contract",
           "合同已生效", "合同 %s 已生效，共 %d 期账单待缴。"
           % (contract.contract_no, len(bills)), contract.contract_no)
    notify(session, contract.landlord_id, "contract",
           "新签约", "房源 %s 已签约，租期至 %s。"
           % (house.house_no, contract.end_date.strftime("%Y-%m-%d")),
           contract.contract_no)
    return bills


# ---------------------------------------------------------------- 账单

def generate_bills(session, contract):
    """一次性排出整个租期的账单。

    押金 1 条 + 租金 N 条。租客能提前看到完整缴费计划，
    房东也能预估现金流——这比「到期再生成」更贴近真实平台的做法。
    """
    bills = []

    bills.append(Bill(
        bill_no=_serial("ZD"),
        contract_id=contract.id,
        type="deposit",
        period="",
        amount=contract.deposit,
        due_date=contract.start_date,
        remark="押金",
    ))

    total_months = months_between(contract.start_date, contract.end_date)
    pay_months = max(contract.pay_months, 1)
    rent = Decimal(str(contract.rent))

    index = 0
    while index < total_months:
        # 最后一期可能不足一个完整付款周期，按实际月数收
        span = min(pay_months, total_months - index)
        due = add_months(contract.start_date, index)
        start_label = due.strftime("%Y-%m")
        end_label = add_months(contract.start_date, index + span - 1).strftime("%Y-%m")
        period = start_label if span == 1 else "%s ~ %s" % (start_label, end_label)

        bills.append(Bill(
            bill_no=_serial("ZD"),
            contract_id=contract.id,
            type="rent",
            period=period,
            amount=rent * span,
            due_date=due,
            remark="第 %d 期租金" % (index // pay_months + 1),
        ))
        index += span

    session.add_all(bills)
    session.flush()
    return bills


def pay_bill(session, bill, user=None, method="alipay", idempotency_key=None):
    """缴费。重复缴费直接拒绝，不能产生两条流水。"""
    if bill.status == "paid":
        raise ValueError("该账单已缴清")
    if bill.status == "cancelled":
        raise ValueError("该账单已作废")
    # 合同已经结束或作废的，不能再收钱。
    #
    # 原先这里只看账单状态：退租时只会作废「未到期的租金」账单，
    # 押金和已到期未缴的租金仍是 unpaid，于是**合同都结束了还能继续缴费**。
    # 收进来的钱既没有对应的租约，也不会再产生任何权利义务——
    # 退租结算已经做过了，这笔钱只能靠人工退回去。
    #
    # 遗留欠款要收，正确做法是走催缴与追偿，而不是让一个已终止的
    # 合同继续走缴费通道。
    if bill.contract and bill.contract.status in ("ended", "cancelled"):
        raise ValueError("该合同已结束，无法继续缴费；如有欠款请联系房东处理")

    bill.status = "paid"
    bill.paid_at = datetime.now()

    payment = Payment(
        trade_no=_serial("LS"),
        bill_id=bill.id,
        user_id=user.id if user else None,
        amount=bill.amount,
        method=method,
        idempotency_key=idempotency_key,
    )
    session.add(payment)
    session.flush()

    contract = bill.contract
    if contract:
        notify(session, contract.landlord_id, "bill",
               "收到租金", "账单 %s 已缴纳，金额 %d 元。"
               % (bill.bill_no, int(bill.amount)), bill.bill_no)
    return payment


# ---------------------------------------------------------------- 退租

def end_contract(session, contract, reason="", deduct=0):
    """退租或到期结束。

    未到期的租金账单一律作废——人已经搬走了还挂着待缴账单是不对的。
    押金按扣款结算，扣款不能超过押金本身。
    """
    if contract.status != "active":
        raise ValueError("只有生效中的合同可以退租")

    deduct = Decimal(str(deduct or 0))
    if deduct < 0:
        raise ValueError("扣款不能为负")
    deposit = Decimal(str(contract.deposit))
    if deduct > deposit:
        raise ValueError("扣款不能超过押金")

    today = datetime.now()
    cancelled = 0
    for bill in contract.bills:
        if bill.status == "unpaid" and bill.type == "rent" and bill.due_date > today:
            bill.status = "cancelled"
            bill.remark = (bill.remark + " · 退租作废").strip(" ·")
            cancelled += 1

    contract.status = "ended"
    contract.ended_at = today
    contract.end_reason = reason
    contract.deposit_refund = deposit - deduct

    # 押金退款：**只有真的收到过押金才生成退款记录**。
    #
    # 原先这段是无条件创建的，押金账单没付过时只把 payment_id 置空，
    # 退款记录照样生成——于是一分钱没收，账上却挂着一笔「已退 3000 元」。
    # 那不是记录不准，是凭空造了一笔支出，对账时对不平。
    #
    # 押金没收到时也不需要「退 0 元」的记录：没有发生的资金流动不该留痕，
    # 留了反而让对账的人以为退过。合同上的 deposit_refund 仍照常写，
    # 那是「应退多少」的结算结论，与「实际退了多少」是两回事。
    deposit_bill = next((b for b in contract.bills
                         if b.type == "deposit" and b.status == "paid"), None)
    refund = None
    if deposit_bill:
        refund = Refund(
            refund_no=_serial("TK"), contract_id=contract.id,
            payment_id=(deposit_bill.payment.id
                        if deposit_bill.payment else None),
            original_amount=deposit, deduct_amount=deduct,
            refund_amount=deposit - deduct, reason=reason or "退租押金结算",
        )
        session.add(refund)
        # 押金账单本身也要标记已退，否则重复退租会再退一次
        deposit_bill.remark = (deposit_bill.remark + " · 已退款").strip(" ·")

    house = session.get(House, contract.house_id)
    if house and house.status == "rented":
        house.status = "available"

    # 通知文案要与实际资金流动一致。押金从没收到过却告诉租客
    # 「退还押金 3000 元」，会直接引出「钱退哪去了」的质问。
    if refund:
        detail = "退还押金 %d 元%s。" % (
            int(deposit - deduct),
            ("，扣款 %d 元" % int(deduct)) if deduct else "")
    else:
        detail = "本合同的押金未曾缴纳，无需退款。"
    notify(session, contract.tenant_id, "contract", "合同已结束",
           "合同 %s 已结束，%s" % (contract.contract_no, detail),
           contract.contract_no)
    session.flush()
    return {"cancelled_bills": cancelled,
            # refund 是「应退金额」的结算结论，无论押金收没收到都算得出来；
            # refund_record 是「实际退款流水」，没收到押金就没有这条。
            "refund": float(deposit - deduct),
            "refund_record": refund.to_dict() if refund else None}


def renew_contract(session, contract, months=12):
    """续租：结束旧合同，基于同一房源生成新的待确认合同。"""
    if contract.status != "active":
        raise ValueError("只有生效中的合同可以续租")

    house = session.get(House, contract.house_id)
    tenant_id = contract.tenant_id

    # 先结束旧合同，房源才会放回 available，新合同才签得出来
    end_contract(session, contract, reason="续租")

    from app.models import User
    tenant = session.get(User, tenant_id)
    new_contract = create_contract(
        session, house, tenant,
        start_date=contract.end_date, months=months,
        deposit_months=1, pay_months=contract.pay_months)
    return new_contract


# ---------------------------------------------------------------- 消息

def notify(session, user_id, type_, title, content="", link_no=""):
    """写一条站内消息。用户不存在时静默跳过，不影响主流程。"""
    if not user_id:
        return None
    message = Message(user_id=user_id, type=type_, title=title,
                      content=content, link_no=link_no)
    session.add(message)
    return message
