"""接口的请求体模型。

从 `main.py` 搬出来的——那个文件曾经 3400 行、105 个接口，读一遍要翻很久，
组员同时改不同模块时也总在同一个文件上冲突。

集中放还有个好处：**所有入参校验规则一眼可见**。原先想确认「密码到底
限制了多长」，得先在三千行里找到那个类。

只放请求模型。响应结构由各 `to_dict()` 决定，跟着数据模型走更合适；
另有少数几个模型仍留在 `main.py` 里贴着自己的接口——它们只被一处使用，
搬过来反而要在两个文件之间来回跳。
"""

import re
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


# 真实姓名：2–20 位中英文（可带间隔号与空格），和注册页前端那条规则一致。
#
# 原先只有前端在注册时查，**接口和改资料都不查**（2026-10-22）。演示库里就有一个
# 叫「微信abc888」的租客——姓名会原样写进合同的承租方，也会原样给要签约的房东看；
# 而聊天里留联系方式是被拦的，姓名就成了绕过去的那条路。
_REAL_NAME = re.compile(r"^[A-Za-z一-鿿·• ]{2,20}$")


def _valid_real_name(value):
    if value is None:
        return value
    value = value.strip()
    if value and not _REAL_NAME.match(value):
        raise ValueError("真实姓名需 2–20 位中文或英文，不能包含数字和符号")
    return value


def _local_naive(value):
    """带时区的时间换成本地时间再去掉时区，库里一律存本地的「墙上时间」。

    前端原先把本地零点 toISOString() 成 UTC 再发：北京时间 10-25 00:00 到了这里是
    10-24 16:00Z，原样存下去起租日就早了一天，账单都排在每月 24 号（2026-10-22）。
    前端已经改成发本地时间；这里兜底，别的调用方再这么发也不会错。
    """
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone().replace(tzinfo=None)
    return value


class RegisterRequest(BaseModel):
    username: str = ""
    password: str = ""
    real_name: str = ""
    phone: str = ""
    role: str = "tenant"

    _name = field_validator("real_name")(_valid_real_name)


class LoginRequest(BaseModel):
    username: str
    password: str


class AppointmentCreate(BaseModel):
    house_no: str
    viewing_time: datetime
    contact_name: str = ""
    contact_phone: str = ""
    remark: str = ""
    # 改期：新的一条约成功时，把这一条（自己的、同一套房的）同时取消。
    # 同一套房只能留一条待看的预约，先约新的再退旧的会被那条规矩挡住；
    # 先退旧的再约新的，新时段约不上时人就一条都没有了。所以两步放进一个事务
    replaces: int | None = None

    _viewing_time_local = field_validator("viewing_time")(_local_naive)


class ExtraBillIn(BaseModel):
    """房东在生效中的合同上加收一笔水电燃气 / 其它费用。"""
    type: Literal["utility", "other"] = "utility"
    amount: float = Field(gt=0, le=100000)
    title: str = Field(default="", max_length=60)
    due_date: datetime | None = None

    _due_date_local = field_validator("due_date")(_local_naive)


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


class RepairMessage(BaseModel):
    """管理员就一张工单联系房东或报修人。"""
    to: Literal["landlord", "tenant"]
    content: str = Field(min_length=1, max_length=500)


# 可选枚举。**空串一律合法**——表示房东没填，不是填错了。
# 把「没填」当成非法值会逼着房东为了发布而瞎选一个，那比留空更糟。
DECORATIONS = ("", "精装", "简装", "毛坯")
HEATINGS = ("", "集中供暖", "自采暖", "无供暖")
# 朝向有两套写法在同时用，**必须都认**：
#
# - 校验表原先只写了「南」「东南」这种；
# - 而前端发布表单的下拉框给的是「朝南」「朝东」「南北通透」，
#   种子数据里 88 套房源用的也是这一套。
#
# 结果是九个选项里有五个一提交就 422——**房东在页面上选「朝南」
# 根本发不出去**，编辑已有房源时朝向原样回填，保存同样 422。
# 这个坑是给智能体做「发布房源」表单、真的点了确认才撞出来的：
# 卡片好好的，后端不收。
#
# 修法选「两种都认」而不是「统一成一种」：库里已经有一套写法的存量数据，
# 改数据要写迁移、还要同步前端展示，而这里只是入参校验，放宽不丢信息。
ORIENTATIONS = ("", "东", "南", "西", "北", "东南", "西南", "东北", "西北",
                "南北",
                "朝东", "朝南", "朝西", "朝北",
                "朝东南", "朝西南", "朝东北", "朝西北", "南北通透")


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
    # A5：楼层展示串由 floor_no/floor_total 拼，房东不再手填。
    # 字段保留是为了**不破坏老客户端**——传上来照收，但入库时
    # 会被拼出来的那个覆盖
    floor: str = Field(default="", max_length=20)
    building: str = Field(default="", max_length=20)
    unit: str = Field(default="", max_length=20)
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
    # 以下三项都是「签约时谈定的数」，不填就按房源与委托上的走。
    # 月租：议价后的成交价，不填取房源挂牌价
    rent: float | None = Field(default=None, gt=0, le=1_000_000)
    # 押金金额：填了就用它，优先于押金月数
    deposit: float | None = Field(default=None, ge=0, le=6_000_000)
    # 佣金比例（月租的百分比），只对有受托中介的房源有意义
    commission_rate: float | None = Field(default=None, ge=0, le=100)

    _start_date_local = field_validator("start_date")(_local_naive)


class ContractUpdate(BaseModel):
    """改一份**还没生效**的合同（B4）。

    每一项都可以不填——不填就是不动。用 None 当「没填」而不是 0：
    押金月数 0 是合法取值（免押），拿 0 当哨兵就表达不了免押了，
    和 ContractCreate 同一个理由。

    **刻意不收房源与租客。** 换房子或者换人签，那不是「改条款」，
    是另一份合同——取消这份重开一份，留下的记录也更说得清。
    """

    start_date: datetime | None = None
    months: int | None = Field(default=None, ge=1, le=60)
    deposit_months: int | None = Field(default=None, ge=0, le=6)
    pay_months: int | None = Field(default=None, ge=1, le=12)
    rent: float | None = Field(default=None, gt=0, le=1_000_000)
    deposit: float | None = Field(default=None, ge=0, le=6_000_000)
    commission_rate: float | None = Field(default=None, ge=0, le=100)

    _start_date_local = field_validator("start_date")(_local_naive)


class ContractEnd(BaseModel):
    reason: str = ""
    deduct: float = Field(default=0, ge=0)


class ContractRenew(BaseModel):
    months: int = Field(default=12, ge=1, le=60)
    # 续租时提出的新月租，不填就按原租金续；只对续租之后新排的账单生效
    rent: float | None = Field(default=None, gt=0, le=1_000_000)


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
    building: str | None = Field(default=None, max_length=20)
    unit: str | None = Field(default=None, max_length=20)
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

    _name = field_validator("real_name")(_valid_real_name)
    # 免打扰（J-07）。用 None 表示「这次不改」，而不是默认 False——
    # 只改手机号的请求不该顺手把免打扰关掉
    accept_contact: bool | None = None


class PasswordChange(BaseModel):
    old_password: str
    new_password: str = Field(min_length=6, max_length=64)


class ReviewCreate(BaseModel):
    contract_no: str
    rating: int = Field(ge=1, le=5)
    rating_house: int = Field(default=0, ge=0, le=5)
    rating_landlord: int = Field(default=0, ge=0, le=5)
    # 0 = 这一栏没评。不是「0 分」——聚合时要能区分「评了很差」
    # 和「没评」，两者混在一起算出来的均分没有意义
    rating_agent: int = Field(default=0, ge=0, le=5)
    content_landlord: str = Field(default="", max_length=300)
    content_agent: str = Field(default="", max_length=300)
    content: str = Field(default="", max_length=500)
    # C1：实拍图。**上限写在 schema 里而不是只在前端拦**——
    # 前端的 6 张限制是体验，后端的 6 张限制才是约束
    images: list[str] = Field(default_factory=list, max_length=6)


class ChatLog(BaseModel):
    # 刻意没有 user_id：身份只能来自令牌，见 log_chat 的说明
    session_id: str = Field(min_length=8, max_length=64)
    role: str
    content: str = Field(max_length=1000)
    intent: str = ""
