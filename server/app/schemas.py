"""接口的请求体模型。

从 `main.py` 搬出来的——那个文件曾经 3400 行、105 个接口，读一遍要翻很久，
组员同时改不同模块时也总在同一个文件上冲突。

集中放还有个好处：**所有入参校验规则一眼可见**。原先想确认「密码到底
限制了多长」，得先在三千行里找到那个类。

只放请求模型。响应结构由各 `to_dict()` 决定，跟着数据模型走更合适；
另有少数几个模型仍留在 `main.py` 里贴着自己的接口——它们只被一处使用，
搬过来反而要在两个文件之间来回跳。
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class RegisterRequest(BaseModel):
    username: str = ""
    password: str = ""
    real_name: str = ""
    phone: str = ""
    role: str = "tenant"


class LoginRequest(BaseModel):
    username: str
    password: str


class AppointmentCreate(BaseModel):
    house_no: str
    viewing_time: datetime
    contact_name: str = ""
    contact_phone: str = ""
    remark: str = ""


class AppointmentUpdate(BaseModel):
    status: str
    reply: str = ""


class RepairCreate(BaseModel):
    house_no: str
    content: str = Field(min_length=1)
    contact_name: str = ""
    contact_phone: str = ""
    category: str = ""
    urgency: str = ""
    assignee: str = ""
    analysis: str = ""


class RepairUpdate(BaseModel):
    status: str


class HouseCreate(BaseModel):
    title: str
    district: str
    community: str = ""
    address: str = ""
    rent_price: float = Field(gt=0)
    deposit: float = 0
    area: float = Field(gt=0)
    layout: str = ""
    rent_type: str = "整租"
    floor: str = ""
    orientation: str = ""
    description: str = ""
    tags: list[str] = []


class ContractCreate(BaseModel):
    house_no: str
    tenant_username: str
    start_date: datetime
    months: int = Field(default=12, ge=1, le=60)
    # 不填就用系统配置里的默认押金月数（M-09）。这里必须是 None 而不是
    # 具体数字：0 本身是合法取值（免押），用 0 当哨兵就没法表达免押了。
    deposit_months: int | None = Field(default=None, ge=0, le=6)
    pay_months: int = Field(default=1, ge=1, le=12)


class ContractEnd(BaseModel):
    reason: str = ""
    deduct: float = Field(default=0, ge=0)


class ContractRenew(BaseModel):
    months: int = Field(default=12, ge=1, le=60)


class PayRequest(BaseModel):
    method: Literal["alipay", "wechat", "bank"] = "alipay"
    idempotency_key: str = Field(default="", max_length=64)


class NoticeCreate(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1)
    summary: str = ""
    category: str = "公告"


class HouseUpdate(BaseModel):
    title: str | None = None
    community: str | None = None
    address: str | None = None
    rent_price: float | None = Field(default=None, gt=0)
    deposit: float | None = Field(default=None, ge=0)
    area: float | None = Field(default=None, gt=0)
    layout: str | None = None
    rent_type: str | None = None
    floor: str | None = None
    orientation: str | None = None
    description: str | None = None
    tags: list[str] | None = None


class HouseReview(BaseModel):
    approved: bool
    reason: str = ""


class UserUpdate(BaseModel):
    status: str | None = None
    role: str | None = None


class ProfileUpdate(BaseModel):
    # 身份证号。**只在请求里出现，绝不落库**——服务端算出哈希与掩码
    # 之后原文就丢掉了，见 app/idcard.py 的说明。
    id_card: str | None = Field(default=None, max_length=18)
    real_name: str | None = Field(default=None, max_length=50)
    phone: str | None = Field(default=None, max_length=20)
    email: str | None = Field(default=None, max_length=100)


class PasswordChange(BaseModel):
    old_password: str
    new_password: str = Field(min_length=6, max_length=64)


class ReviewCreate(BaseModel):
    contract_no: str
    rating: int = Field(ge=1, le=5)
    rating_house: int = Field(default=0, ge=0, le=5)
    rating_landlord: int = Field(default=0, ge=0, le=5)
    content: str = Field(default="", max_length=500)


class ChatLog(BaseModel):
    # 刻意没有 user_id：身份只能来自令牌，见 log_chat 的说明
    session_id: str = Field(min_length=8, max_length=64)
    role: str
    content: str = Field(max_length=1000)
    intent: str = ""
