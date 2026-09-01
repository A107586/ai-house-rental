"""业务数据模型。

表结构参考团队《项目改造清单》第 6 节，并落实《需求分析说明书》
第 3.1 节的数据安全与数据质量要求：

* 密码只存哈希，不存明文（R-01）
* 金额一律 `Numeric(10, 2)`，不用字符串或整数（R-04）
* 表间用真外键关联，不靠业务编号字符串匹配（R-03、R-06）
* 看房时间用 `DateTime` 而非 `Date`，否则表达不了「下午两点」（AI-06）

交易链三张表 `contracts` / `bills` / `payments` 是主干：
看房通过后生成合同，合同生效时一次性排出整个租期的账单，缴费产生流水。
"""

from datetime import datetime

from sqlalchemy import (Boolean, DateTime, ForeignKey, Integer, Numeric,
                        String, Text, UniqueConstraint)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    """用户。三种角色共用一张表，用 role 区分。"""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    # 只存 BCrypt 哈希，任何情况下不落明文（R-01）
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    real_name: Mapped[str] = mapped_column(String(50), default="")
    phone: Mapped[str] = mapped_column(String(20), default="")
    email: Mapped[str] = mapped_column(String(100), default="")
    # tenant 租客 / landlord 房东 / admin 管理员
    role: Mapped[str] = mapped_column(String(20), default="tenant", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    houses: Mapped[list["House"]] = relationship(back_populates="landlord")

    def to_dict(self):
        """对外输出。**绝不包含 password_hash。**"""
        return {
            "id": self.id,
            "username": self.username,
            "real_name": self.real_name,
            "phone": mask_phone(self.phone),
            "role": self.role,
            "status": self.status,
        }


class House(Base):
    """房源。"""

    __tablename__ = "houses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    house_no: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    landlord_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)

    province: Mapped[str] = mapped_column(String(30), default="辽宁省")
    city: Mapped[str] = mapped_column(String(30), default="大连市")
    district: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    community: Mapped[str] = mapped_column(String(60), default="")
    address: Mapped[str] = mapped_column(String(200), default="")

    # 金额统一 Numeric，不用 varchar / int（R-04）
    rent_price: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, index=True)
    deposit: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    area: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, index=True)

    layout: Mapped[str] = mapped_column(String(20), default="", index=True)
    rent_type: Mapped[str] = mapped_column(String(20), default="整租", index=True)
    floor: Mapped[str] = mapped_column(String(20), default="")
    orientation: Mapped[str] = mapped_column(String(20), default="")

    description: Mapped[str] = mapped_column(Text, default="")
    # 房源特征以逗号分隔存储，如「电梯,地铁,近学校」
    tags: Mapped[str] = mapped_column(String(200), default="")
    cover_image: Mapped[str] = mapped_column(String(255), default="")

    # available 可租 / rented 已租 / offline 已下架 / pending 待审核
    status: Mapped[str] = mapped_column(String(20), default="available", index=True)
    view_count: Mapped[int] = mapped_column(Integer, default=0)
    published_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    landlord: Mapped["User"] = relationship(back_populates="houses")
    appointments: Mapped[list["Appointment"]] = relationship(back_populates="house")

    def to_dict(self):
        return {
            "id": self.id,
            "house_no": self.house_no,
            "title": self.title,
            "district": self.district,
            "community": self.community,
            "address": self.address,
            "rent_price": float(self.rent_price),
            "deposit": float(self.deposit),
            "area": float(self.area),
            "layout": self.layout,
            "rent_type": self.rent_type,
            "floor": self.floor,
            "orientation": self.orientation,
            "description": self.description,
            "tags": [t for t in self.tags.split(",") if t],
            "cover_image": self.cover_image,
            "status": self.status,
            "view_count": self.view_count,
            "landlord_name": self.landlord.real_name if self.landlord else "",
        }


class Appointment(Base):
    """看房预约。

    `viewing_time` 必须是 `DateTime`：AI-06 要处理「这周六下午两点」，
    只存日期就丢掉了时刻，也无法做时段冲突检测。
    """

    __tablename__ = "appointments"
    # 刻意不加 UNIQUE(house_id, viewing_time)：
    # 该约束既太严——取消掉的预约会永久占住那个时段，与「取消后释放」的
    # 业务规则冲突；又太松——管不了「一次看房占用一小时」的重叠判断。
    # 真正的规则由接口层的时段冲突校验实现，那里会排除已取消与已拒绝的记录。

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    house_id: Mapped[int] = mapped_column(ForeignKey("houses.id"), nullable=False, index=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=True)

    contact_name: Mapped[str] = mapped_column(String(50), default="")
    contact_phone: Mapped[str] = mapped_column(String(20), default="")
    viewing_time: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    remark: Mapped[str] = mapped_column(Text, default="")

    # pending 待确认 / confirmed 已确认 / rejected 已拒绝 / cancelled 已取消
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    reply: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    house: Mapped["House"] = relationship(back_populates="appointments")

    def to_dict(self):
        return {
            "id": self.id,
            "house_id": self.house_id,
            "house_no": self.house.house_no if self.house else "",
            "contact_name": self.contact_name,
            "contact_phone": mask_phone(self.contact_phone),
            "viewing_time": self.viewing_time.strftime("%Y-%m-%d %H:%M:%S"),
            "remark": self.remark,
            "status": self.status,
            "reply": self.reply,
        }


class Favorite(Base):
    """收藏。"""

    __tablename__ = "favorites"
    __table_args__ = (
        UniqueConstraint("user_id", "house_id", name="uq_user_house"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    house_id: Mapped[int] = mapped_column(ForeignKey("houses.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class Repair(Base):
    """房屋报修工单。

    `category` / `urgency` / `assignee` 三个字段由 AI-04 报修智能体
    从租客的自由文本描述中分析得出，不需要租客自己填。
    """

    __tablename__ = "repairs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    house_id: Mapped[int] = mapped_column(ForeignKey("houses.id"), nullable=False, index=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=True)

    contact_name: Mapped[str] = mapped_column(String(50), default="")
    contact_phone: Mapped[str] = mapped_column(String(20), default="")
    content: Mapped[str] = mapped_column(Text, nullable=False)

    # ---- 以下三项由智能体分析填充 ----
    # 水电 / 家具 / 门窗 / 家电 / 其他
    category: Mapped[str] = mapped_column(String(30), default="", index=True)
    # 紧急 / 一般 / 可延后
    urgency: Mapped[str] = mapped_column(String(20), default="", index=True)
    assignee: Mapped[str] = mapped_column(String(50), default="")
    analysis: Mapped[str] = mapped_column(Text, default="")

    # pending 待处理 / processing 处理中 / done 已完成
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    house: Mapped["House"] = relationship()

    def to_dict(self):
        return {
            "id": self.id,
            "house_id": self.house_id,
            "house_no": self.house.house_no if self.house else "",
            "contact_name": self.contact_name,
            "contact_phone": mask_phone(self.contact_phone),
            "content": self.content,
            "category": self.category,
            "urgency": self.urgency,
            "assignee": self.assignee,
            "analysis": self.analysis,
            "status": self.status,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M") if self.created_at else "",
        }


class ChatMessage(Base):
    """AI 对话记录，供智能体保存会话上下文与用户偏好。"""

    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False)  # user / assistant
    content: Mapped[str] = mapped_column(Text, nullable=False)
    intent: Mapped[str] = mapped_column(String(30), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class HouseImage(Base):
    """房源相册。

    `is_cover` 标记封面图，列表页只取封面，详情页取全部。
    """

    __tablename__ = "house_images"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    house_id: Mapped[int] = mapped_column(ForeignKey("houses.id"), nullable=False, index=True)
    url: Mapped[str] = mapped_column(String(255), nullable=False)
    caption: Mapped[str] = mapped_column(String(100), default="")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_cover: Mapped[bool] = mapped_column(Boolean, default=False)

    def to_dict(self):
        return {
            "id": self.id,
            "url": self.url,
            "caption": self.caption,
            "is_cover": self.is_cover,
        }


class Contract(Base):
    """租赁合同。

    这是整条交易链的枢纽：看房通过后生成合同，合同生效后按月产生账单，
    退租时结算押金。房源的 `rented` 状态也由合同状态驱动。

    状态流转::

        pending ──租客确认──► active ──到期/退租──► ended
           │                    │
           └──任一方取消──► cancelled

    `deposit_refund` 记录退租时实际退还的押金，与 `deposit` 的差额即扣款。
    """

    __tablename__ = "contracts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    contract_no: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    house_id: Mapped[int] = mapped_column(ForeignKey("houses.id"), nullable=False, index=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    landlord_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)

    start_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # 金额一律 Numeric，不用 varchar / int（需求 R-04）
    rent: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    deposit: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    # 押一付三这类付款方式，数字表示每期几个月
    pay_months: Mapped[int] = mapped_column(Integer, default=1)

    # pending 待确认 / active 生效中 / ended 已结束 / cancelled 已取消
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    content: Mapped[str] = mapped_column(Text, default="")
    # AI-02 合同智能审阅的结果，暂未接入
    review_result: Mapped[str] = mapped_column(Text, default="")

    signed_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    ended_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    end_reason: Mapped[str] = mapped_column(String(100), default="")
    deposit_refund: Mapped[float] = mapped_column(Numeric(10, 2), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    house: Mapped["House"] = relationship()
    bills: Mapped[list["Bill"]] = relationship(back_populates="contract")

    @property
    def days_left(self):
        """剩余天数。已结束的合同返回 0。"""
        if self.status != "active":
            return 0
        return max((self.end_date.date() - datetime.now().date()).days, 0)

    def to_dict(self):
        return {
            "id": self.id,
            "contract_no": self.contract_no,
            "house_id": self.house_id,
            "house_no": self.house.house_no if self.house else "",
            "house_title": self.house.title if self.house else "",
            "tenant_id": self.tenant_id,
            "landlord_id": self.landlord_id,
            "start_date": self.start_date.strftime("%Y-%m-%d"),
            "end_date": self.end_date.strftime("%Y-%m-%d"),
            "rent": float(self.rent),
            "deposit": float(self.deposit),
            "pay_months": self.pay_months,
            "status": self.status,
            "content": self.content,
            "days_left": self.days_left,
            "end_reason": self.end_reason,
            "deposit_refund": (float(self.deposit_refund)
                               if self.deposit_refund is not None else None),
            "signed_at": self.signed_at.strftime("%Y-%m-%d %H:%M") if self.signed_at else "",
        }


class Bill(Base):
    """账单。

    合同生效时一次性按租期生成全部租金账单加一条押金账单，
    这样租客能提前看到整个租期的缴费计划，房东也能预估现金流。
    """

    __tablename__ = "bills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bill_no: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    contract_id: Mapped[int] = mapped_column(ForeignKey("contracts.id"),
                                             nullable=False, index=True)

    # rent 租金 / deposit 押金 / utility 水电 / other 其它
    type: Mapped[str] = mapped_column(String(20), default="rent", index=True)
    period: Mapped[str] = mapped_column(String(20), default="")  # 如 2026-10
    amount: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    due_date: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)

    # unpaid 待缴 / paid 已缴 / cancelled 已作废
    status: Mapped[str] = mapped_column(String(20), default="unpaid", index=True)
    paid_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    remark: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    contract: Mapped["Contract"] = relationship(back_populates="bills")

    @property
    def overdue(self):
        """是否逾期。已缴与已作废的不算。"""
        return (self.status == "unpaid"
                and self.due_date.date() < datetime.now().date())

    def to_dict(self):
        return {
            "id": self.id,
            "bill_no": self.bill_no,
            "contract_id": self.contract_id,
            "contract_no": self.contract.contract_no if self.contract else "",
            "house_no": (self.contract.house.house_no
                         if self.contract and self.contract.house else ""),
            "type": self.type,
            "period": self.period,
            "amount": float(self.amount),
            "due_date": self.due_date.strftime("%Y-%m-%d"),
            "status": self.status,
            "overdue": self.overdue,
            "paid_at": self.paid_at.strftime("%Y-%m-%d %H:%M") if self.paid_at else "",
            "remark": self.remark,
        }


class Payment(Base):
    """缴费流水。

    与账单一对一，但单独成表——真实系统里退款、对账、第三方回调
    都挂在流水上，账单只表达「该交多少」。
    """

    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trade_no: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    bill_id: Mapped[int] = mapped_column(ForeignKey("bills.id"), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=True)

    amount: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    # 课程项目不接真实支付，method 仅作展示：alipay / wechat / bank / offline
    method: Mapped[str] = mapped_column(String(20), default="alipay")
    paid_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    def to_dict(self):
        return {
            "id": self.id,
            "trade_no": self.trade_no,
            "bill_id": self.bill_id,
            "amount": float(self.amount),
            "method": self.method,
            "paid_at": self.paid_at.strftime("%Y-%m-%d %H:%M:%S"),
        }


class Notice(Base):
    """平台公告。同时作为 AI-03 智能客服的检索语料。"""

    __tablename__ = "notices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    summary: Mapped[str] = mapped_column(String(255), default="")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(30), default="公告", index=True)
    publisher_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=True)
    is_published: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    view_count: Mapped[int] = mapped_column(Integer, default=0)
    published_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    def to_dict(self):
        return {
            "id": self.id,
            "title": self.title,
            "summary": self.summary,
            "content": self.content,
            "category": self.category,
            "view_count": self.view_count,
            "published_at": self.published_at.strftime("%Y-%m-%d"),
        }


class Message(Base):
    """站内消息。

    预约被确认、账单到期、工单进度变化都往这里写一条，
    解决目前「房东确认了预约但租客毫不知情」的问题。
    """

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    # appointment 预约 / bill 账单 / repair 报修 / contract 合同 / system 系统
    type: Mapped[str] = mapped_column(String(20), default="system", index=True)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    content: Mapped[str] = mapped_column(Text, default="")
    # 关联对象编号，前端据此跳转
    link_no: Mapped[str] = mapped_column(String(32), default="")
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    def to_dict(self):
        return {
            "id": self.id,
            "type": self.type,
            "title": self.title,
            "content": self.content,
            "link_no": self.link_no,
            "is_read": self.is_read,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M"),
        }


def mask_phone(phone):
    """手机号中间四位掩码（R-02）。"""
    if not phone or len(phone) < 7:
        return phone or ""
    return phone[:3] + "****" + phone[-4:]
