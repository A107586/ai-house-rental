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
    images: Mapped[list["HouseImage"]] = relationship(
        back_populates="house", order_by="HouseImage.sort_order")
    reviews: Mapped[list["Review"]] = relationship()

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
            "images": [i.url for i in sorted(
                self.images, key=lambda x: (not x.is_cover, x.sort_order))],
            "status": self.status,
            "view_count": self.view_count,
            "landlord_name": self.landlord.real_name if self.landlord else "",
            "rating": self.avg_rating,
            "review_count": len(self.reviews),
        }

    @property
    def avg_rating(self):
        """平均评分。没有评价时返回 0，前端据此显示「暂无评价」。"""
        if not self.reviews:
            return 0
        return round(sum(r.rating for r in self.reviews) / len(self.reviews), 1)


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

    house: Mapped["House"] = relationship(back_populates="images")

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


class BrowsingHistory(Base):
    """浏览足迹。

    每个用户对同一房源只保留一条，重复浏览更新时间与次数——
    否则一个用户刷十次详情页就会把足迹列表刷满。
    """

    __tablename__ = "browsing_history"
    __table_args__ = (
        UniqueConstraint("user_id", "house_id", name="uq_history_user_house"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    house_id: Mapped[int] = mapped_column(ForeignKey("houses.id"), nullable=False)
    view_count: Mapped[int] = mapped_column(Integer, default=1)
    viewed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)

    house: Mapped["House"] = relationship()


class Review(Base):
    """租后评价。

    只有租约正常结束的租客才能评价，且一份合同只能评一次——
    不然任何人都能刷分，评分就没有参考价值了。
    """

    __tablename__ = "reviews"
    __table_args__ = (
        UniqueConstraint("contract_id", name="uq_review_contract"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("contracts.id"), nullable=False)
    house_id: Mapped[int] = mapped_column(ForeignKey("houses.id"), nullable=False, index=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)

    # 1–5 星
    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    # 分项评分，便于展示「房源真实度」「房东响应」这类维度
    rating_house: Mapped[int] = mapped_column(Integer, default=0)
    rating_landlord: Mapped[int] = mapped_column(Integer, default=0)
    content: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    tenant: Mapped["User"] = relationship()

    def to_dict(self):
        name = ""
        if self.tenant:
            raw = self.tenant.real_name or self.tenant.username
            # 评价对所有人可见，只露姓氏，其余打码
            name = raw[0] + "*" * max(len(raw) - 1, 1)
        return {
            "id": self.id,
            "rating": self.rating,
            "rating_house": self.rating_house,
            "rating_landlord": self.rating_landlord,
            "content": self.content,
            "tenant_name": name,
            "created_at": self.created_at.strftime("%Y-%m-%d"),
        }


class OperationLog(Base):
    """操作日志（M-07）。

    只记录**改变了状态的操作**：GET 请求不落库。全量记访问会让日志
    以每天几万条的速度增长，真正需要追溯时反而翻不动；而审计要回答的
    问题从来是「谁改了什么」，不是「谁看了什么」。

    用户名与角色是**冗余存下来的**，不靠外键 join 取。用户改名或被删
    之后，日志仍要能说清当时是谁做的——这正是审计日志存在的意义。
    """

    __tablename__ = "operation_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=True)
    username: Mapped[str] = mapped_column(String(50), default="")
    role: Mapped[str] = mapped_column(String(20), default="")
    action: Mapped[str] = mapped_column(String(60), default="")
    method: Mapped[str] = mapped_column(String(10), default="")
    path: Mapped[str] = mapped_column(String(255), default="")
    status_code: Mapped[int] = mapped_column(Integer, default=0)
    ip: Mapped[str] = mapped_column(String(45), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now,
                                                 index=True)

    def to_dict(self):
        return {
            "id": self.id,
            "username": self.username or "匿名",
            "role": self.role or "-",
            "action": self.action,
            "method": self.method,
            "path": self.path,
            "status_code": self.status_code,
            "ip": self.ip,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M:%S"),
        }


class RepairWorker(Base):
    """维修人员档案（F-09）。"""

    __tablename__ = "repair_workers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    phone: Mapped[str] = mapped_column(String(20), default="")
    # 擅长的故障类型，与报修工单的 category 取值一致，便于派单时匹配
    skill: Mapped[str] = mapped_column(String(30), default="")
    status: Mapped[str] = mapped_column(String(20), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            # 维修人员的电话只有房东与管理员看得到，同样按 R-02 掩码
            "phone": mask_phone(self.phone),
            "skill": self.skill,
            "status": self.status,
        }


class RepairItem(Base):
    """维修物品／耗材（F-09）。"""

    __tablename__ = "repair_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    unit: Mapped[str] = mapped_column(String(10), default="个")
    unit_price: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    stock: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "unit": self.unit,
            "unit_price": float(self.unit_price or 0),
            "stock": self.stock,
        }


class RepairRecord(Base):
    """维修记录：一张工单完成时登记的人、物、钱（F-09）。

    `repair_id` 上有唯一约束——一张工单只能结案一次。少了它，重复提交
    结案会把物料重复扣一遍库存、成本也重复计一遍，统计就废了。
    """

    __tablename__ = "repair_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    repair_id: Mapped[int] = mapped_column(ForeignKey("repairs.id"),
                                           unique=True, nullable=False)
    worker_id: Mapped[int] = mapped_column(ForeignKey("repair_workers.id"),
                                           nullable=True)
    hours: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    labor_cost: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    item_cost: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    note: Mapped[str] = mapped_column(Text, default="")
    finished_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    repair: Mapped["Repair"] = relationship()
    worker: Mapped["RepairWorker"] = relationship()
    usages: Mapped[list["RepairItemUsage"]] = relationship(
        back_populates="record", cascade="all, delete-orphan")

    @property
    def total_cost(self):
        return float(self.labor_cost or 0) + float(self.item_cost or 0)

    def to_dict(self):
        return {
            "id": self.id,
            "repair_id": self.repair_id,
            "house_no": (self.repair.house.house_no
                         if self.repair and self.repair.house else ""),
            "category": self.repair.category if self.repair else "",
            "worker_name": self.worker.name if self.worker else "未指派",
            "hours": float(self.hours or 0),
            "labor_cost": float(self.labor_cost or 0),
            "item_cost": float(self.item_cost or 0),
            "total_cost": round(self.total_cost, 2),
            "note": self.note,
            "items": [usage.to_dict() for usage in self.usages],
            "finished_at": self.finished_at.strftime("%Y-%m-%d %H:%M"),
        }


class RepairItemUsage(Base):
    """一次维修用掉的物品与数量（F-09）。

    单价是**下单当时的快照**，不是外键跟着物品表走。物品调价之后，
    历史维修的成本不能跟着一起变——那等于把已经发生的账改了。
    """

    __tablename__ = "repair_item_usages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    record_id: Mapped[int] = mapped_column(ForeignKey("repair_records.id"),
                                           nullable=False, index=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("repair_items.id"),
                                         nullable=False, index=True)
    item_name: Mapped[str] = mapped_column(String(50), default="")
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    unit_price: Mapped[float] = mapped_column(Numeric(10, 2), default=0)

    record: Mapped["RepairRecord"] = relationship(back_populates="usages")

    @property
    def amount(self):
        return float(self.unit_price or 0) * (self.quantity or 0)

    def to_dict(self):
        return {
            "item_id": self.item_id,
            "item_name": self.item_name,
            "quantity": self.quantity,
            "unit_price": float(self.unit_price or 0),
            "amount": round(self.amount, 2),
        }


class SystemConfig(Base):
    """系统配置（M-09）。

    值统一用字符串存，类型由 `value_type` 描述、读取时再转换。理由是
    配置项会随需求增删，用一张宽表给每种类型开一列，加一个布尔配置就
    要改表结构；字符串加类型标记则只是插一行数据。

    **配置必须真的生效**——存了值却没人读，那只是个假功能。当前每一项
    都接进了对应的业务逻辑，见 `app/config_store.py` 的说明。
    """

    __tablename__ = "system_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(60), unique=True, nullable=False)
    value: Mapped[str] = mapped_column(String(255), default="")
    value_type: Mapped[str] = mapped_column(String(10), default="text")
    label: Mapped[str] = mapped_column(String(60), default="")
    description: Mapped[str] = mapped_column(String(200), default="")
    group_name: Mapped[str] = mapped_column(String(30), default="通用")
    is_public: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now,
                                                 onupdate=datetime.now)

    def to_dict(self):
        return {
            "key": self.key,
            "value": self.value,
            "value_type": self.value_type,
            "label": self.label,
            "description": self.description,
            "group": self.group_name,
            "is_public": self.is_public,
            "updated_at": self.updated_at.strftime("%Y-%m-%d %H:%M"),
        }


def mask_phone(phone):
    """手机号中间四位掩码（R-02）。"""
    if not phone or len(phone) < 7:
        return phone or ""
    return phone[:3] + "****" + phone[-4:]
