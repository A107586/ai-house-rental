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

from sqlalchemy import select

from app import gateway, notifier
from app.models import Bill, Contract, House, Message, Payment, Refund, User


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

def resolve_deposit(house, deposit_months=None, default_deposit_months=1):
    """算这份合同的押金。**这是押金唯一的口径，别处不许再算一遍**（K-02）。

    原先这里写死 `rent * deposit_months`，完全没读 `house.deposit`——
    房东发布时填「押金 800」，租客在详情页看到的是 800，
    合同上签出来却是「租金 × 押金月数」，两个数对不上。
    详情页和合同对同一套房给出两个押金，这是**用户会当成欺骗的那类错误**。

    优先级从高到低：

    1. **显式传了 `deposit_months`**——房东在签约时主动选了「押二付一」，
       这是针对这一单的决定，优先于房源上的默认值；
    2. **房源上选了押付方式**（`house.deposit_months`）——发布时选的
       「押一付三」。它和 `house.deposit` 是同一个数：发布与编辑接口
       都会按「月租 × 押金月数」把 `deposit` 一并写好。
       这里之所以还要单独看一眼月数，是为了**把「免押」和「没填」分开**：
       两者的 `deposit` 都是 0，只有月数能区分「明确不收押金」
       和「这一栏留空了」；
    3. **房源上填了押金金额**——房东填的是不成整数倍的数（月租 2800、
       押金 800）时走这条，也是租客在详情页看到的数字；
    4. 都没有，才回落到系统配置的押金月数。
    """
    rent = Decimal(str(house.rent_price))
    if deposit_months is not None:
        return rent * deposit_months
    if house.deposit_months is not None:
        return rent * house.deposit_months
    if house.deposit:
        return Decimal(str(house.deposit))
    return rent * default_deposit_months


def create_contract(session, house, tenant, start_date, months=12,
                    deposit_months=None, pay_months=1, content="",
                    default_deposit_months=1):
    """生成一份待确认合同。

    不在这里改房源状态——合同要等租客确认后才生效，
    在 `activate_contract` 里才占用房源。

    `deposit_months` 默认是 **None 而不是 1**：None 表示「签约方没有指定，
    按房源上的押金走」，1 表示「明确要求押一」。用 1 当默认值就无法
    区分这两种情况，房源上填的押金也就永远读不到了（K-02）。
    """
    if house.status != "available":
        raise ValueError("该房源当前不可签约")
    if months < 1:
        raise ValueError("租期至少一个月")

    # 合同是法律文本，甲乙双方必须是真实姓名。租客没实名就签，
    # 正文里的承租方会写成 `tenant1` 这种登录名（K-03）。
    # 这一步拦在生成之前，而不是生成完再补——合同一旦生成就有了编号、
    # 有了通知、租客那边已经看得见，事后改姓名等于改一份已经发出的合同。
    landlord = house.landlord or session.get(User, house.landlord_id)
    if not (landlord and landlord.real_name.strip()):
        raise ValueError("出租方尚未实名，请先在「个人中心 → 编辑资料」填写真实姓名")
    if not tenant.real_name.strip():
        raise ValueError("租客尚未实名，请让对方在「个人中心 → 编辑资料」"
                         "填写真实姓名后再签约")

    rent = Decimal(str(house.rent_price))
    deposit = resolve_deposit(house, deposit_months, default_deposit_months)
    end_date = add_months(start_date, months)

    contract = Contract(
        contract_no=_serial("HT"),
        house_id=house.id,
        tenant_id=tenant.id,
        landlord_id=house.landlord_id,
        start_date=start_date,
        end_date=end_date,
        rent=rent,
        deposit=deposit,
        pay_months=pay_months,
        status="pending",
        content=content or render_contract_text(house, tenant, start_date,
                                                end_date, rent,
                                                deposit, pay_months),
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
        # 姓名不再有 `or username` 兜底。到得了这一行，双方实名都已经
        # 在 create_contract 里校验过了；留着兜底只会把「没实名」这个
        # 问题藏进一份看起来正常的合同里（K-03）
        landlord=house.landlord.real_name,
        tenant=tenant.real_name,
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


def _guard_payable(bill):
    """发起支付前的共同校验。发起与确认两段都要用，抽出来免得只改一处。"""
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


def start_payment(session, bill, user=None, method="alipay", idempotency_key=None):
    """第一段：发起支付。

    **这一步不改账单状态。** 返回的是一张「等待付款」的支付单，
    钱还没到——账单要等网关回调确认之后才置 paid（见 confirm_payment）。

    改造之前这里是一步到位的：把 bill.status 改成 paid、建一条 succeeded
    的流水就返回了。那样写在本地能跑，但它假设了一件真实系统里不成立的事——
    **发起支付等于收到钱**。真实链路里用户跳到收银台之后可能不付、付失败、
    或者付了但回调晚到几秒，这三种情况在旧写法里都会被记成已付款。
    """
    _guard_payable(bill)

    # 同一张账单上如果压着一张过期的 pending 单，先关掉。
    # 不关的话它会一直占着 bill_id 的唯一约束，用户重新发起时被挡住，
    # 表现为「点了没反应」——这是三段式最容易踩的坑。
    existing = bill.payment
    if existing is not None:
        if existing.status == "succeeded":
            raise ValueError("该账单已缴清")
        if existing.status == "pending":
            if existing.expire_at and existing.expire_at > datetime.now():
                # 还没过期，直接把原单还回去，不重复建单
                return existing
            close_payment(session, existing, "超时未支付，自动关单")

    trade_no = _serial("LS")
    order = gateway.get_gateway().create(trade_no, bill.amount,
                                         "账单 %s" % bill.bill_no)

    payment = Payment(
        trade_no=trade_no,
        bill_id=bill.id,
        user_id=user.id if user else None,
        amount=bill.amount,
        method=method,
        status="pending",
        idempotency_key=idempotency_key,
        gateway=gateway.get_gateway().name,
        gateway_trade_no=order["gateway_trade_no"],
        expire_at=datetime.fromtimestamp(order["expire_at"]),
    )
    payment.pay_url = order["pay_url"]   # 仅本次响应用，不落库
    session.add(payment)
    session.flush()
    return payment


def confirm_payment(session, payment, payload=None, result="success"):
    """第二段：网关回调确认。**账单是在这里才变成已付的。**

    幂等是这个函数的核心要求：支付宝对同一笔交易会重复推送回调
    （最多 8 次，间隔递增），处理两次就多记一笔收入。所以已经是终态的
    支付单直接原样返回，不再动账。
    """
    if payment.status == "succeeded":
        return payment, False        # 重复回调，什么都不做
    if payment.status in ("failed", "closed"):
        raise ValueError("支付单已关闭，请重新发起支付")

    bill = payment.bill
    if payload is not None:
        payment.notify_payload = gateway.dumps(payload)
    payment.notified_at = datetime.now()

    if result != "success":
        payment.status = "failed"
        payment.failure_reason = (payload or {}).get("reason", "网关返回支付失败")
        session.flush()
        return payment, True

    # 到账前再校验一次合同状态：从发起到回调之间可能已经退租了。
    # 这段时间窗在本地是毫秒，真实网关里是几分钟——足够合同状态变化。
    _guard_payable(bill)

    payment.status = "succeeded"
    payment.paid_at = datetime.now()
    bill.status = "paid"
    bill.paid_at = payment.paid_at
    session.flush()

    contract = bill.contract
    if contract:
        notify(session, contract.landlord_id, "bill",
               "收到租金", "账单 %s 已缴纳，金额 %d 元。"
               % (bill.bill_no, int(bill.amount)), bill.bill_no)
    return payment, True


def close_payment(session, payment, reason="超时未支付"):
    """第三段的反面：关单。

    回调可能永远不来（用户关掉了收银台）。没有关单机制的话，
    这张 pending 单会永久占住账单，用户再也付不了钱。
    """
    if payment.status != "pending":
        return payment
    payment.status = "closed"
    payment.failure_reason = reason
    session.flush()
    return payment


def sweep_expired_payments(session, now=None):
    """扫描并关掉所有过期的 pending 单。

    真实系统里这是一个定时任务。这里在缴费相关接口被调用时顺带跑一次，
    没有引入调度框架——课程环境跑不起常驻的定时进程，
    而「顺带扫一次」在单机上足够，且不会漏（下次有人缴费就会清）。
    """
    now = now or datetime.now()
    rows = session.scalars(select(Payment).where(
        Payment.status == "pending", Payment.expire_at < now)).all()
    for row in rows:
        close_payment(session, row, "超时未支付，自动关单")
    return len(rows)


def pay_bill(session, bill, user=None, method="alipay", idempotency_key=None):
    """一步缴清：发起 + 立即确认。

    保留这个函数是因为**线下缴费和管理员代收是真实存在的场景**——
    钱已经通过别的方式收到了，系统只需要如实记一笔，不该走网关。
    它内部仍然走三段式的两个函数，所以状态流转和在线支付完全一致。
    """
    payment = start_payment(session, bill, user, method, idempotency_key)
    payment, _ = confirm_payment(session, payment, None, "success")
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
    # 押金不写死一个月：续租的押金该和这套房现在挂着的押金一致，
    # 否则续一次租押金就变了一个数（K-02）
    new_contract = create_contract(
        session, house, tenant,
        start_date=contract.end_date, months=months,
        pay_months=contract.pay_months)
    return new_contract


# ---------------------------------------------------------------- 消息

# 需要往外推的通知类型（C-2）。
#
# **不是所有站内信都值得发邮件。** 每条消息都推一封，用户很快就会把
# 整个域名拉黑，真正要紧的那封反而看不见了。这里只留三类「错过了会
# 有实际损失」的：账单到期、合同状态变化、工单进度。
EXTERNAL_TYPES = ("bill", "contract", "repair")


def notify(session, user_id, type_, title, content="", link_no=""):
    """写一条站内消息，必要时同步推到外部通道。

    站内信是**必达**的那一条，直接落库；邮件是尽力而为的补充，
    发不出去只当没发——合同已经生效、账单已经生成，不能因为邮件
    服务器连不上就把整个请求回滚，那会让用户以为操作失败又点一遍。
    """
    if not user_id:
        return None
    message = Message(user_id=user_id, type=type_, title=title,
                      content=content, link_no=link_no)
    session.add(message)

    if type_ in EXTERNAL_TYPES:
        user = session.get(User, user_id)
        if user:
            notifier.notify_external(user, title, content)
    return message
