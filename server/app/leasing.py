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
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

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


_CN_NUM = {8: "八", 9: "九", 10: "十", 11: "十一", 12: "十二",
           13: "十三", 14: "十四", 15: "十五"}


def house_specific_clauses(house, deposit, pay_months):
    """按这套房的实际情况拼出来的附加条款（E1）。

    ## 为什么要有它

    验收意见是「所有的审阅条款长得都一样，评分也是」。原因不在审阅那一侧：
    **每一份合同都是同一个模板渲染出来的**，正文一字不差，
    审阅读到的自然是同一段文字，找出同一批风险，给出同一个分数。

    合同审阅这个功能因此变成了摆设——它永远只能告诉你同一件事。

    ## 拼什么

    只拼**这套房真实具备、而且真会引起纠纷**的条款。判断标准是
    「不写清楚的话，事后会为这件事吵架吗」：

    - 有电梯 → 停梯期间怎么算
    - 带家具 → 清单与自然损耗的边界（退租时最常吵的一类）
    - 可养宠物 → 写进合同，否则房东事后反悔租客毫无依据
    - 供暖方式 → 取暖费谁承担，集中与自采暖的惯例不一样
    - 合租 → 公共区域与室友变更
    - 免押 → 免的是押金，不是赔偿责任，这一点必须点明
    - 付款周期长 → 一次付多期，提前退租那部分钱怎么退

    **没有的特征不写。** 给一套没电梯的房子写电梯条款比不写更糟——
    那说明这份合同是套出来的，不是为这套房拟的。
    """
    tags = set((house.tags or "").split(",")) if house.tags else set()
    clauses = []

    if getattr(house, "elevator", False):
        clauses.append(
            "电梯\n"
            "本房屋所在楼栋配有电梯。电梯因检修或故障连续停运超过 48 小时的，\n"
            "乙方可要求甲方按停运天数相应减免租金；乙方搬入搬出需使用电梯的，\n"
            "应提前一日告知物业。")
    elif (getattr(house, "floor_no", None) or 0) >= 4:
        clauses.append(
            "楼层\n"
            "本房屋位于第 %d 层且无电梯。乙方搬运大件物品的费用由乙方自理，\n"
            "甲方应予以必要配合。" % house.floor_no)

    if "有家具" in tags:
        clauses.append(
            "家具家电\n"
            "房屋内家具家电由甲方提供，双方应于交房时共同清点并签署\n"
            "《物品交接清单》，作为本合同附件。正常使用产生的自然损耗由甲方承担；\n"
            "乙方人为损坏或遗失的，按实际维修或重置费用赔偿。\n"
            "未签署清单的，退租时甲方不得就物品缺损主张扣款。")

    if "可养宠物" in tags:
        clauses.append(
            "饲养宠物\n"
            "甲方同意乙方在本房屋内饲养宠物。乙方应做好清洁与噪音管理，\n"
            "因饲养宠物造成的房屋损坏由乙方承担；甲方不得在租期内\n"
            "以饲养宠物为由要求乙方搬离。")
    else:
        clauses.append(
            "饲养宠物\n"
            "乙方饲养宠物前应取得甲方书面同意。")

    heating = getattr(house, "heating", "") or ""
    if "集中" in heating:
        clauses.append(
            "取暖费\n"
            "本房屋为集中供暖，取暖费由甲方承担。供暖期内室温不达标的，\n"
            "乙方可要求甲方协调供热单位处理。")
    elif heating:
        clauses.append(
            "取暖费\n"
            "本房屋为%s。取暖能耗费用由乙方按实际用量承担，\n"
            "设备本身的维修与更换由甲方负责。" % heating)

    if (getattr(house, "rent_type", "") or "") == "合租":
        clauses.append(
            "合租\n"
            "本房屋为合租，乙方承租其中一间卧室，享有厨房、卫生间、客厅等\n"
            "公共区域的共同使用权。甲方调整其他房间承租人的，不影响本合同的效力，\n"
            "但应提前告知乙方。")

    if float(deposit or 0) <= 0:
        clauses.append(
            "免押说明\n"
            "本合同不收取押金。免收押金不免除乙方对房屋及屋内物品的损坏\n"
            "赔偿责任，相关费用于退租时据实结算。")

    if (pay_months or 1) >= 3:
        clauses.append(
            "提前退租的租金退还\n"
            "乙方按每 %d 个月一次性支付租金。租期内经双方协商一致提前退租的，\n"
            "甲方应退还已付但尚未居住月份的租金，不得以「已缴不退」为由拒绝。"
            % pay_months)

    if not clauses:
        return ""

    parts = []
    for index, text in enumerate(clauses, start=8):
        parts.append("第%s条 %s" % (_CN_NUM.get(index, str(index)), text))
    return "\n\n" + "\n\n".join(parts)


def render_contract_text(house, tenant, start_date, end_date, rent,
                         deposit, pay_months):
    """生成合同正文。

    前七条是所有合同共有的主干；**第八条起按这套房的实际情况拼**
    （见 `house_specific_clauses`）——不然每份合同一字不差，
    合同审阅永远只能告诉你同一件事。

    模板里故意留了几条对租客不利的条款，供 AI-02 审阅演示。
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
应于每期起始日（每月 {due_day} 日{due_day_note}）当日或之前支付当期租金。

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
本合同一式两份，甲乙双方各执一份，自双方确认之日起生效。{extra}
""".format(
        extra=house_specific_clauses(house, deposit, pay_months),
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
        # 缴费日写成系统真正用的那一天。原先写「每期首月 5 日前」，而账单的
        # 到期日是每期的起始日（_rent_bills），逾期也按它算——20 日起租的合同，
        # 正文说 5 日前交，系统 21 日才算逾期，两边对不上（2026-10-22）
        due_day=start_date.day,
        due_day_note="，当月没有这一天的为当月最后一天" if start_date.day > 28 else "",
        deposit=int(deposit),
    )


def settle_delegation(session, contract, house):
    """合同生效时结算委托：记佣金、标促成中介、结束委托。

    **不复用账单那条链**——佣金是「房东付给中介」，和账单的
    「租客付给房东」付款方收款方都不同（见 AgentCommission 的注释）。

    佣金基数取**合同上的月租**而不是房源当前定价：房源可以在签约之后
    改价，而这一单的佣金该按当时谈成的价算。
    """
    from app.models import AgentCommission, HouseDelegation

    delegation = session.scalar(select(HouseDelegation).where(
        HouseDelegation.house_id == house.id,
        HouseDelegation.status == "active"))
    if not delegation:
        return None

    contract.agent_id = delegation.agent_id
    rate = Decimal(str(delegation.commission_rate))
    base = Decimal(str(contract.rent))
    commission = AgentCommission(
        contract_id=contract.id, delegation_id=delegation.id,
        agent_id=delegation.agent_id, base_amount=base, rate=rate,
        amount=(base * rate / 100).quantize(Decimal("0.01")))
    session.add(commission)

    # 房子租出去了，委托自然结束，不留悬空关系
    delegation.status = "ended"
    delegation.end_at = datetime.now()
    delegation.end_reason = "房源已签约"

    notify(session, delegation.agent_id, "system", "成交，佣金已入账",
           "房源 %s 已签约，佣金 %d 元待结算。"
           % (house.house_no, int(commission.amount)), contract.contract_no)
    return commission


def close_open_viewings(session, house, reason):
    """房子租出去了，还没到时间的看房预约一并取消，并告诉约的人为什么。

    原先签约生效只改房源状态，别人约的看房还挂着「待确认」：约的人一直等，
    房东的待办里也一直躺着一批已经没意义的申请，点「确认」还能确认成功
    （2026-10-22 补）。已经过了时间的不动，那是历史记录。
    """
    from app.models import Appointment

    rows = session.scalars(select(Appointment).where(
        Appointment.house_id == house.id,
        Appointment.status.in_(("pending", "confirmed")),
        Appointment.viewing_time > datetime.now())).all()
    for appointment in rows:
        appointment.status = "cancelled"
        appointment.reply = reason
        notify(session, appointment.tenant_id, "system", "看房预约已取消",
               "%s %s 的看房已取消：%s。" % (
                   house.house_no, appointment.viewing_time.strftime("%m-%d %H:%M"), reason),
               house.house_no)
    return len(rows)


def activate_contract(session, contract):
    """租客确认，合同生效：排账单 + 占用房源。"""
    if contract.status != "pending":
        raise ValueError("只有待确认的合同可以生效")

    house = session.get(House, contract.house_id)
    if not house:
        raise ValueError("房源不存在")
    if house.status != "available":
        # 说清楚是哪一种：原先一律「已被占用」，房东下架了或房源在重审时，
        # 租客会以为被别人抢先签了
        raise ValueError({"rented": "该房源已被占用",
                          "offline": "房东已下架这套房，合同暂时无法生效，请联系房东",
                          "pending": "这套房源正在重新审核，审核通过后才能确认签约",
                          }.get(house.status, "该房源当前不可签约"))

    contract.status = "active"
    contract.signed_at = datetime.now()
    house.status = "rented"
    close_open_viewings(session, house, "这套房已经租出去了")

    # 这套房如果是委托给中介带看的，成交这一刻结算佣金并结束委托（J-06）。
    # 放在这里而不是签约那一步：合同生效才算成交，pending 的合同
    # 随时可能作废，那时佣金已经记上了就得再撤回一次
    settle_delegation(session, contract, house)

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

    # **免押就不排押金账单。**
    #
    # 原先无条件排一条。合同选了「押零付一」时，这条账单的金额是 0——
    # 它在库里是一条正常记录，可在用户眼里是一张「押金 0 元，待缴」的
    # 单子：个人中心给它配了「立即缴费」按钮，点开收银台写着
    # 「确认支付 ¥0.00」；智能助手说「帮我交房租」时，它还因为到期日
    # 最早而被排在第一位，助手于是问「要我帮你缴纳押金 0 元吗」。
    #
    # 一笔不用付的钱不该长成一张待缴账单。真正免押的合同，
    # 账单列表里就不该有押金这一行。
    if Decimal(str(contract.deposit or 0)) > 0:
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
    bills += _rent_bills(contract, contract.start_date, total_months, "第 %d 期租金")

    session.add_all(bills)
    session.flush()
    return bills


def _rent_bills(contract, start, total_months, remark):
    """从 start 起排 total_months 个月的租金账单，按付款周期合并成期。续租也用它。"""
    bills = []
    pay_months = max(contract.pay_months, 1)
    rent = Decimal(str(contract.rent))
    index = 0
    while index < total_months:
        # 最后一期可能不足一个完整付款周期，按实际月数收
        span = min(pay_months, total_months - index)
        due = add_months(start, index)
        start_label = due.strftime("%Y-%m")
        end_label = add_months(start, index + span - 1).strftime("%Y-%m")
        period = start_label if span == 1 else "%s ~ %s" % (start_label, end_label)
        bills.append(Bill(
            bill_no=_serial("ZD"),
            contract_id=contract.id,
            type="rent",
            period=period,
            amount=rent * span,
            due_date=due,
            remark=remark % (index // pay_months + 1),
        ))
        index += span
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
    # **关掉的、失败的那次都留着**，新的一次另起一条（2026-10-22）。原先 bill_id 唯一：
    # 关单只改状态，旧行照样占着约束，重新发起撞约束，账单从此付不了。
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
    session.add(payment)
    bill.payments.append(payment)
    payment.pay_url = order["pay_url"]   # 仅本次响应用，不落库
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

def _bill_period(bill, contract):
    """一张租金账单覆盖的区间 [起, 止)。押一付三的一张覆盖三个月。"""
    rent = Decimal(str(contract.rent or 0))
    span = 1
    if rent > 0:
        span = max(int((Decimal(str(bill.amount)) / rent).to_integral_value(ROUND_HALF_UP)), 1)
    return bill.due_date, add_months(bill.due_date, span)


def _split_by_days(amount, start, end, moment):
    """按天拆一笔钱：住过的部分、没住的部分、住了几天、一共几天。"""
    amount = Decimal(str(amount))
    total = (end.date() - start.date()).days
    if total <= 0:
        return amount, Decimal("0"), 0, 0
    lived = min(max((moment.date() - start.date()).days, 0), total)
    lived_part = (amount * lived / total).quantize(Decimal("0.01"), ROUND_HALF_UP)
    return lived_part, amount - lived_part, lived, total


def plan_settlement(contract, deduct=0, now=None):
    """退租结算算一遍，**不落库**。结算预览和真正退租用的是同一份结果。

    以「今天」为退租日，逐张账单：

    - 押金：已缴的按扣款退；没缴的作废（押金是担保，不是欠款）；
    - 租金，没缴的：整段在今天之后 → 作废；今天正处在这一期里 → **按住过的天数折算**，
      剩下的作废；整段已经住完 → 原样保留，是真欠款（合同结束后只能线下结）；
    - 租金，已缴的：整段在今天之后 → **全额退回**；今天正处在这一期里 → 没住的天数退回；
      住完的不动。

    原先只作废未缴的、未到期的租金：押一付三的租客月初交了三个月、月中搬走，
    后两个半月的钱就这么留在房东那边——而系统生成的合同里写着
    「甲方应退还已付但尚未居住月份的租金」（2026-10-22）。

    违约金不在这里自动扣：提前退租谁违约、扣多少是双方的事，
    房东要扣就从押金里扣（deduct），租客同意的也是那个数。
    """
    now = now or datetime.now()
    deduct = Decimal(str(deduct or 0))
    if deduct < 0:
        raise ValueError("扣款不能为负")
    deposit = Decimal(str(contract.deposit or 0))
    if deduct > deposit:
        raise ValueError("扣款不能超过押金")

    plan = {"deposit": {"amount": deposit, "deduct": deduct, "refund": deposit - deduct,
                        "bill": None, "paid": False},
            "cancel": [], "prorate": [], "rent_refunds": [], "arrears": []}
    for bill in sorted(contract.bills, key=lambda b: (b.due_date, b.id)):
        if bill.type == "deposit":
            if bill.status == "paid":
                plan["deposit"].update(bill=bill, paid=True)
            elif bill.status == "unpaid":
                plan["cancel"].append(bill)
            continue
        if bill.type != "rent" or bill.status not in ("paid", "unpaid"):
            continue
        start, end = _bill_period(bill, contract)
        lived_part, unlived_part, lived, total = _split_by_days(bill.amount, start, end, now)
        if bill.status == "unpaid":
            if lived == 0:
                plan["cancel"].append(bill)
            elif lived < total:
                plan["prorate"].append({"bill": bill, "amount": lived_part,
                                        "lived": lived, "total": total})
            else:
                plan["arrears"].append(bill)
        elif unlived_part > 0 and lived < total:
            plan["rent_refunds"].append({"bill": bill, "refund": unlived_part,
                                         "kept": lived_part, "lived": lived, "total": total})
    return plan


def settlement_summary(plan):
    """把结算方案转成接口能直接返回的样子（金额转 float，账单换成编号）。"""
    deposit = plan["deposit"]
    rent_refund = sum((r["refund"] for r in plan["rent_refunds"]), Decimal("0"))
    arrears = (sum((Decimal(str(b.amount)) for b in plan["arrears"]), Decimal("0"))
               + sum((p["amount"] for p in plan["prorate"]), Decimal("0")))
    deposit_refund = deposit["refund"] if deposit["paid"] else Decimal("0")
    return {
        "deposit": float(deposit["amount"]), "deposit_paid": deposit["paid"],
        "deduct": float(deposit["deduct"]),
        "deposit_refund": float(deposit_refund),
        "rent_refunds": [{"bill_no": r["bill"].bill_no, "period": r["bill"].period,
                          "paid": float(r["bill"].amount), "refund": float(r["refund"]),
                          "lived_days": r["lived"], "total_days": r["total"]}
                         for r in plan["rent_refunds"]],
        "rent_refund": float(rent_refund),
        "cancelled": [{"bill_no": b.bill_no, "type": b.type, "amount": float(b.amount)}
                      for b in plan["cancel"]],
        "prorated": [{"bill_no": p["bill"].bill_no, "period": p["bill"].period,
                      "original": float(p["bill"].amount), "amount": float(p["amount"]),
                      "lived_days": p["lived"], "total_days": p["total"]}
                     for p in plan["prorate"]],
        "arrears": float(arrears),
        "refund_total": float(deposit_refund + rent_refund),
    }


def end_contract(session, contract, reason="", deduct=0):
    """退租或到期结束：按 plan_settlement 结算，**每一笔退款都落一条流水**。"""
    if contract.status != "active":
        raise ValueError("只有生效中的合同可以退租")

    today = datetime.now()
    plan = plan_settlement(contract, deduct, today)
    summary = settlement_summary(plan)
    deposit_info = plan["deposit"]
    deduct, deposit = deposit_info["deduct"], deposit_info["amount"]

    for bill in plan["cancel"]:
        bill.status = "cancelled"
        note = "退租作废，押金未缴无需再缴" if bill.type == "deposit" else "退租作废"
        bill.remark = (bill.remark + " · " + note).strip(" ·")
    for item in plan["prorate"]:
        bill = item["bill"]
        bill.remark = (bill.remark + " · 按实际居住 %d/%d 天折算，原 %s 元" % (
            item["lived"], item["total"], _money(bill.amount))).strip(" ·")
        bill.amount = item["amount"]

    contract.status = "ended"
    contract.ended_at = today
    contract.end_reason = reason
    _clear_request(contract)
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
    refund = None
    deposit_bill = deposit_info["bill"]
    if deposit_bill:
        refund = Refund(
            refund_no=_serial("TK"), contract_id=contract.id, type="deposit",
            bill_id=deposit_bill.id,
            payment_id=(deposit_bill.payment.id if deposit_bill.payment else None),
            original_amount=deposit, deduct_amount=deduct,
            refund_amount=deposit - deduct, reason=reason or "退租押金结算",
        )
        session.add(refund)
        deposit_bill.refunds.append(refund)
        # 押金账单本身也要标记已退，否则重复退租会再退一次
        deposit_bill.remark = (deposit_bill.remark + " · 已退款").strip(" ·")

    # 已付未住的租金按天退回，每张账单一条流水
    rent_records = []
    for item in plan["rent_refunds"]:
        bill = item["bill"]
        record = Refund(
            refund_no=_serial("TK"), contract_id=contract.id, type="rent", bill_id=bill.id,
            payment_id=(bill.payment.id if bill.payment else None),
            original_amount=bill.amount, deduct_amount=item["kept"],
            refund_amount=item["refund"],
            reason="退租日 %s，这一期住了 %d/%d 天，退回没住的部分" % (
                today.strftime("%Y-%m-%d"), item["lived"], item["total"]))
        session.add(record)
        bill.refunds.append(record)
        bill.remark = (bill.remark + " · 退租退回 %s 元" % _money(item["refund"])).strip(" ·")
        rent_records.append(record)

    house = session.get(House, contract.house_id)
    if house and house.status == "rented":
        # 出租期间改过房源内容的，审核记录已经清掉——回到待审，不直接上架
        house.status = "available" if house.approved_at else "pending"

    # 通知文案要与实际资金流动一致。押金从没收到过却告诉租客
    # 「退还押金 3000 元」，会直接引出「钱退哪去了」的质问。
    parts = []
    if refund:
        parts.append("退还押金 %d 元%s" % (
            int(deposit - deduct), ("（扣款 %d 元）" % int(deduct)) if deduct else ""))
    else:
        parts.append("本合同的押金未曾缴纳，无需退款")
    if summary["rent_refund"]:
        parts.append("退回已付未住的租金 %s 元" % _money(summary["rent_refund"]))
    if summary["arrears"]:
        parts.append("还有 %s 元住过期间的租金未缴，请与房东线下结清" % _money(summary["arrears"]))
    notify(session, contract.tenant_id, "contract", "合同已结束",
           "合同 %s 已结束，%s。" % (contract.contract_no, "；".join(parts)),
           contract.contract_no)
    session.flush()
    return {"cancelled_bills": len(plan["cancel"]),
            # refund 是「应退押金」的结算结论，无论押金收没收到都算得出来；
            # refund_record 是「实际退款流水」，没收到押金就没有这条。
            "refund": float(deposit - deduct),
            "refund_record": refund.to_dict() if refund else None,
            "rent_refund_records": [r.to_dict() for r in rent_records],
            "settlement": summary}


def renew_contract(session, contract, months=12):
    """续租：**在本合同上延长租期**，按原租金补排新增月份的账单。

    原先是「结束旧合同、另起一份待确认的新合同」。结束旧合同走的是退租那一套：
    押金当场全额退、后面几个月的租金账单作废、房源重新上架——人还住着，
    房子已经能被别人约看、签约了；新合同还要租客再点一次确认。
    延长本合同，押金、房源状态都不动。
    """
    if contract.status != "active":
        raise ValueError("只有生效中的合同可以续租")
    months = int(months or 12)
    if months < 1:
        raise ValueError("续租至少 1 个月")

    old_end = contract.end_date
    contract.end_date = add_months(old_end, months)
    bills = _rent_bills(contract, old_end, months, "续租第 %d 期租金")
    session.add_all(bills)
    contract.content = (contract.content or "") + (
        "\n\n【续租补充条款】双方同意续租 %d 个月，租期延长至 %s，"
        "月租金 %s 元，其余条款不变。（%s）" % (
            months, contract.end_date.strftime("%Y年%m月%d日"),
            _money(contract.rent), datetime.now().strftime("%Y-%m-%d")))
    _clear_request(contract)
    for user_id in (contract.tenant_id, contract.landlord_id):
        notify(session, user_id, "contract", "已续租",
               "合同 %s 已续租 %d 个月，租期至 %s，新增 %d 期租金账单。" % (
                   contract.contract_no, months,
                   contract.end_date.strftime("%Y-%m-%d"), len(bills)),
               contract.contract_no)
    session.flush()
    return contract


_REQUEST_LABEL = {"end": "退租", "renew": "续租"}
_SIDE_LABEL = {"tenant": "租客", "landlord": "房东"}


def request_change(session, contract, kind, by, months=0, reason="", deduct=0):
    """一方提出退租 / 续租，等另一方确认。

    `deduct` 只用于**房东提出的退租**：押金拟扣多少当场写明，租客同意的就是这个数。
    租客提的退租不带扣款——扣款由房东确认时填（他要先验房）。
    """
    if contract.status != "active":
        raise ValueError("只有生效中的合同可以申请%s" % _REQUEST_LABEL[kind])
    if contract.request_type and contract.request_type != kind:
        raise ValueError("这份合同已有一个%s申请在等处理，先处理或撤回它"
                         % _REQUEST_LABEL[contract.request_type])
    deduct = Decimal(str(deduct or 0)) if kind == "end" and by == "landlord" else Decimal("0")
    if deduct < 0:
        raise ValueError("扣款不能为负")
    if deduct > Decimal(str(contract.deposit or 0)):
        raise ValueError("扣款不能超过押金")
    contract.request_type = kind
    contract.request_by = by
    contract.request_months = int(months or 12) if kind == "renew" else 0
    contract.request_reason = (reason or "")[:100]
    contract.request_deduct = deduct
    contract.requested_at = datetime.now()

    other = contract.landlord_id if by == "tenant" else contract.tenant_id
    what = "续租 %d 个月" % contract.request_months if kind == "renew" else "退租"
    if kind == "end" and by == "landlord":
        what = "提前退租"
    detail = "，原因：%s" % reason if reason and kind == "end" else ""
    if deduct > 0:
        detail += "，押金拟扣 %s 元" % _money(deduct)
    notify(session, other, "contract", "%s申请%s" % (_SIDE_LABEL[by], _REQUEST_LABEL[kind]),
           "合同 %s：%s申请%s%s。请到合同里确认或拒绝。" % (
               contract.contract_no, _SIDE_LABEL[by], what, detail),
           contract.contract_no)
    session.flush()
    return contract


def withdraw_request(session, contract, side):
    """撤回（提出方）或拒绝（另一方）待处理的申请。"""
    if not contract.request_type:
        raise ValueError("没有待处理的申请")
    kind, by = contract.request_type, contract.request_by
    _clear_request(contract)
    if side == by:
        other = contract.landlord_id if by == "tenant" else contract.tenant_id
        title, verb = "%s撤回了%s申请" % (_SIDE_LABEL[by], _REQUEST_LABEL[kind]), "已撤回"
    else:
        other = contract.tenant_id if by == "tenant" else contract.landlord_id
        title, verb = "%s申请未通过" % _REQUEST_LABEL[kind], "对方没有同意"
    notify(session, other, "contract", title,
           "合同 %s 的%s申请%s，合同照常履行。" % (contract.contract_no,
                                            _REQUEST_LABEL[kind], verb),
           contract.contract_no)
    session.flush()
    return contract


def _clear_request(contract):
    contract.request_type = ""
    contract.request_by = ""
    contract.request_months = 0
    contract.request_reason = ""
    contract.request_deduct = 0
    contract.requested_at = None


def _money(value):
    value = Decimal(str(value or 0))
    return str(int(value)) if value == value.to_integral() else str(value)


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
