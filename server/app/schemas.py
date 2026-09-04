"""接口的请求体模型。

从 `main.py` 搬出来的——那个文件曾经 3400 行、105 个接口，读一遍要翻很久，
组员同时改不同模块时也总在同一个文件上冲突。

集中放还有个好处：**所有入参校验规则一眼可见**。原先想确认「密码到底
限制了多长」，得先在三千行里找到那个类。

只放请求模型。响应结构由各 `to_dict()` 决定，跟着数据模型走更合适；
另有少数几个模型仍留在 `main.py` 里贴着自己的接口——它们只被一处使用，
搬过来反而要在两个文件之间来回跳。
"""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


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


# 可选枚举。**空串一律合法**——表示房东没填，不是填错了。
# 把「没填」当成非法值会逼着房东为了发布而瞎选一个，那比留空更糟。
DECORATIONS = ("", "精装", "简装", "毛坯")
HEATINGS = ("", "集中供暖", "自采暖", "无供暖")
ORIENTATIONS = ("", "东", "南", "西", "北", "东南", "西南", "东北", "西北", "南北")


class HouseFacets(BaseModel):
    """K-04 新增的筛选维度。发布与编辑共用，两处必须收一样的字段，
    否则会出现「发布时填得了、编辑时改不了」这种半截功能。"""

    elevator: bool | None = None
    decoration: str | None = None
    floor_no: int | None = Field(default=None, ge=1, le=200)
    floor_total: int | None = Field(default=None, ge=1, le=200)
    heating: str | None = None
    available_from: date | None = None
    min_months: int | None = Field(default=None, ge=1, le=60)
    # 押 N 付 M。填了就按「月租 × N」把 deposit 一并算好，
    # 押金始终只有 deposit 一个真相（K-02）
    deposit_months: int | None = Field(default=None, ge=0, le=6)
    pay_months: int | None = Field(default=None, ge=1, le=12)
    service_fee: float | None = Field(default=None, ge=0, le=100000)
    business_area: str | None = Field(default=None, max_length=30)
    # 大连市域范围。写死边界是为了挡住「把经纬度填反了」这种最常见的错误
    # ——纬度填成 121 的话点位会掉到地图外面，而页面上看不出哪里不对
    lng: float | None = Field(default=None, ge=120.5, le=123.5)
    lat: float | None = Field(default=None, ge=38.5, le=40.5)

    @field_validator("decoration")
    @classmethod
    def _check_decoration(cls, value):
        if value is not None and value not in DECORATIONS:
            raise ValueError("装修情况只能是：精装 / 简装 / 毛坯")
        return value

    @field_validator("heating")
    @classmethod
    def _check_heating(cls, value):
        if value is not None and value not in HEATINGS:
            raise ValueError("供暖方式只能是：集中供暖 / 自采暖 / 无供暖")
        return value

    @model_validator(mode="after")
    def _check_floor(self):
        if (self.floor_no is not None and self.floor_total is not None
                and self.floor_no > self.floor_total):
            raise ValueError("所在楼层不能高于总楼层")
        return self


class HouseCreate(HouseFacets):
    title: str = Field(min_length=2, max_length=60)
    district: str = Field(min_length=2, max_length=30)
    community: str = Field(default="", max_length=60)
    address: str = Field(default="", max_length=200)
    # 上限不是摆设：没有它就能发出「月租 9999999」的房源，
    # 而列表按价格排序时这一条会永远霸占榜首（K-12）
    rent_price: float = Field(gt=0, le=1000000)
    deposit: float = Field(default=0, ge=0, le=1000000)
    area: float = Field(gt=0, le=10000)
    layout: str = Field(default="", max_length=20)
    rent_type: str = "整租"
    floor: str = Field(default="", max_length=20)
    orientation: str = ""
    description: str = Field(default="", max_length=2000)
    tags: list[str] = []

    @field_validator("orientation")
    @classmethod
    def _check_orientation(cls, value):
        if value not in ORIENTATIONS:
            raise ValueError("朝向只能是东/南/西/北及其组合")
        return value


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


class HouseUpdate(HouseFacets):
    title: str | None = Field(default=None, min_length=2, max_length=60)
    community: str | None = Field(default=None, max_length=60)
    address: str | None = Field(default=None, max_length=200)
    rent_price: float | None = Field(default=None, gt=0, le=1000000)
    deposit: float | None = Field(default=None, ge=0, le=1000000)
    area: float | None = Field(default=None, gt=0, le=10000)
    layout: str | None = Field(default=None, max_length=20)
    rent_type: str | None = None
    floor: str | None = Field(default=None, max_length=20)
    orientation: str | None = None
    description: str | None = Field(default=None, max_length=2000)
    tags: list[str] | None = None

    @field_validator("orientation")
    @classmethod
    def _check_orientation(cls, value):
        if value is not None and value not in ORIENTATIONS:
            raise ValueError("朝向只能是东/南/西/北及其组合")
        return value


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
