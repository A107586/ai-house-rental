"""业务数据模型。

表结构参考团队《项目改造清单》第 6 节，并落实《需求分析说明书》
第 3.1 节的数据安全与数据质量要求：

* 密码只存哈希，不存明文（R-01）
* 金额一律 `Numeric(10, 2)`，不用字符串或整数（R-04）
* 表间用真外键关联，不靠业务编号字符串匹配（R-03、R-06）
* 看房时间用 `DateTime` 而非 `Date`，否则表达不了「下午两点」（AI-06）
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


def mask_phone(phone):
    """手机号中间四位掩码（R-02）。"""
    if not phone or len(phone) < 7:
        return phone or ""
    return phone[:3] + "****" + phone[-4:]
