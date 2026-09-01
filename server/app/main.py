"""业务后端 REST 接口。

职责边界：只做数据的增删改查与业务校验，不含任何大模型逻辑。
智能体服务通过这些接口取数，不直接连数据库
（对应非功能需求 N-06：禁止拼接执行任意 SQL）。
"""

import os
from datetime import datetime, timedelta

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import leasing
from app.auth import create_token, decode_token, using_dev_secret
from app.db import get_session, init_db
from app.models import (Appointment, Bill, ChatMessage, Contract, Favorite,
                        House, Message, Notice, Payment, Repair, User)
from app.security import hash_password, verify_password

app = FastAPI(
    title="房屋租赁系统 · 业务后端",
    description="房源、预约、报修、收藏、用户的增删改查接口",
    version="0.2.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:5173",
                   "http://localhost:8000", "http://127.0.0.1:3000",
                   "http://127.0.0.1:5173", "http://127.0.0.1:8000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 一次看房占用一小时，与智能体端的约定保持一致
VIEWING_MINUTES = 60


@app.on_event("startup")
def _startup():
    init_db()


# ------------------------------------------------------------------ 鉴权

def current_user(authorization: str = Header(default=""),
                 session: Session = Depends(get_session)):
    """从 Authorization 头解出当前用户。未登录或令牌无效返回 None。"""
    if not authorization.lower().startswith("bearer "):
        return None
    payload = decode_token(authorization[7:].strip())
    if not payload:
        return None
    user = session.get(User, payload["uid"])
    return user if user and user.status == "active" else None


def require_login(user: User = Depends(current_user)):
    if not user:
        raise HTTPException(status_code=401, detail="请先登录")
    return user


def require_staff(user: User = Depends(require_login)):
    """房东或管理员才能操作。"""
    if user.role not in ("landlord", "admin"):
        raise HTTPException(status_code=403, detail="没有操作权限")
    return user


# ------------------------------------------------------------------ 请求模型

class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=50)
    password: str = Field(min_length=6, max_length=64)
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
    deposit_months: int = Field(default=1, ge=0, le=6)
    pay_months: int = Field(default=1, ge=1, le=12)


class ContractEnd(BaseModel):
    reason: str = ""
    deduct: float = Field(default=0, ge=0)


class ContractRenew(BaseModel):
    months: int = Field(default=12, ge=1, le=60)


class PayRequest(BaseModel):
    method: str = "alipay"


class NoticeCreate(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1)
    summary: str = ""
    category: str = "公告"


class ChatLog(BaseModel):
    session_id: str
    role: str
    content: str
    intent: str = ""
    user_id: int | None = None


# ------------------------------------------------------------------ 通用

# 前端构建产物目录。跑过 `cd web && npm run build` 之后就会存在。
WEB_DIST = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "web", "dist")
HAS_WEB = os.path.isdir(WEB_DIST)


if not HAS_WEB:
    # 只有在没有构建产物时才注册这个路由。
    # 显式路由的优先级高于文件末尾挂载的静态目录，若无条件注册，
    # 它会把静态首页整个盖住，访问根路径永远只能看到这段 JSON。
    @app.get("/", include_in_schema=False)
    def root():
        """没有前端构建产物时的根路径引导。

        直接访问本服务根路径只看到一句 {"detail":"Not Found"} 太容易让人困惑，
        这里明确告诉使用者网站在哪、怎么把网站挂到本端口下。
        """
        return {
            "service": "房屋租赁系统 · 业务后端",
            "提示": "还没有前端构建产物，本端口当前只提供接口。",
            "看网站": "开发模式访问 http://localhost:5173"
                      "；或执行 cd web && npm run build 后刷新本页面",
            "接口文档": "/docs",
            "健康检查": "/health",
        }


@app.get("/health")
def health(session: Session = Depends(get_session)):
    house_count = len(session.execute(select(House.id)).all())
    return {
        "status": "ok",
        "houses": house_count,
        "seeded": house_count > 0,
        # 部署前必须设置 AUTH_SECRET，这里主动提醒，避免带着开发密钥上线
        "auth_secret_is_default": using_dev_secret(),
    }


# ------------------------------------------------------------------ 认证

@app.post("/api/auth/register")
def register(payload: RegisterRequest, session: Session = Depends(get_session)):
    if payload.role not in ("tenant", "landlord"):
        raise HTTPException(status_code=400, detail="只能注册租客或房东账号")
    exists = session.scalar(select(User).where(User.username == payload.username))
    if exists:
        raise HTTPException(status_code=409, detail="用户名已被占用")

    user = User(
        username=payload.username,
        password_hash=hash_password(payload.password),
        real_name=payload.real_name,
        phone=payload.phone,
        role=payload.role,
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return {"ok": True, "token": create_token(user), "user": user.to_dict()}


@app.post("/api/auth/login")
def login(payload: LoginRequest, session: Session = Depends(get_session)):
    user = session.scalar(select(User).where(User.username == payload.username))
    # 用户不存在与密码错误返回同一句提示，避免暴露账号是否存在
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    if user.status != "active":
        raise HTTPException(status_code=403, detail="账号已被禁用")
    return {"ok": True, "token": create_token(user), "user": user.to_dict()}


@app.get("/api/auth/me")
def me(user: User = Depends(require_login)):
    return user.to_dict()


# ------------------------------------------------------------------ 房源

@app.get("/api/houses")
def list_houses(
    district: str = "",
    layout: str = "",
    rent_type: str = "",
    status: str = "available",
    rent_price_min: float | None = None,
    rent_price_max: float | None = None,
    area_min: float | None = None,
    area_max: float | None = None,
    keyword: str = "",
    sort: str = "published_at",
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),
    session: Session = Depends(get_session),
):
    """房源列表。所有筛选条件都是可选的，未传即不过滤。"""
    stmt = select(House)

    if status:
        stmt = stmt.where(House.status == status)
    if district:
        stmt = stmt.where(House.district.like("%" + district + "%"))
    if layout:
        stmt = stmt.where(House.layout.like("%" + layout + "%"))
    if rent_type:
        stmt = stmt.where(House.rent_type == rent_type)
    if rent_price_min is not None:
        stmt = stmt.where(House.rent_price >= rent_price_min)
    if rent_price_max is not None:
        stmt = stmt.where(House.rent_price <= rent_price_max)
    if area_min is not None:
        stmt = stmt.where(House.area >= area_min)
    if area_max is not None:
        stmt = stmt.where(House.area <= area_max)
    if keyword:
        like = "%" + keyword + "%"
        stmt = stmt.where(House.title.like(like) | House.description.like(like)
                          | House.community.like(like))

    total = len(session.execute(stmt).all())

    order = {
        "rent_price": House.rent_price.asc(),
        "-rent_price": House.rent_price.desc(),
        "area": House.area.asc(),
        "-area": House.area.desc(),
        "view_count": House.view_count.desc(),
    }.get(sort, House.published_at.desc())

    rows = session.scalars(
        stmt.order_by(order).offset((page - 1) * limit).limit(limit)).all()

    return {"total": total, "page": page, "limit": limit,
            "list": [h.to_dict() for h in rows]}


@app.get("/api/houses/{house_no}")
def get_house(house_no: str, session: Session = Depends(get_session)):
    house = session.scalar(select(House).where(House.house_no == house_no))
    if not house:
        raise HTTPException(status_code=404, detail="房源不存在")
    house.view_count += 1
    session.commit()
    return house.to_dict()


@app.post("/api/houses")
def create_house(payload: HouseCreate, user: User = Depends(require_staff),
                 session: Session = Depends(get_session)):
    """房东发布房源。新房源默认待审核，管理员通过后才对外可见。"""
    last = session.scalar(select(House).order_by(House.id.desc()))
    next_no = "DL%04d" % ((last.id if last else 0) + 1)

    house = House(
        house_no=next_no, title=payload.title, landlord_id=user.id,
        district=payload.district, community=payload.community,
        address=payload.address, rent_price=payload.rent_price,
        deposit=payload.deposit or payload.rent_price, area=payload.area,
        layout=payload.layout, rent_type=payload.rent_type,
        floor=payload.floor, orientation=payload.orientation,
        description=payload.description, tags=",".join(payload.tags),
        status="available" if user.role == "admin" else "pending",
    )
    session.add(house)
    session.commit()
    session.refresh(house)
    return {"ok": True, "house": house.to_dict()}


@app.patch("/api/houses/{house_no}/status")
def update_house_status(house_no: str, status: str,
                        user: User = Depends(require_staff),
                        session: Session = Depends(get_session)):
    """上下架或审核房源。房东只能改自己的房源。"""
    if status not in ("available", "rented", "offline", "pending"):
        raise HTTPException(status_code=400, detail="状态值不合法")

    house = session.scalar(select(House).where(House.house_no == house_no))
    if not house:
        raise HTTPException(status_code=404, detail="房源不存在")
    if user.role != "admin" and house.landlord_id != user.id:
        raise HTTPException(status_code=403, detail="只能修改自己发布的房源")

    house.status = status
    session.commit()
    return {"ok": True, "house": house.to_dict()}


@app.get("/api/districts")
def list_districts(session: Session = Depends(get_session)):
    """可选区域及其房源数量，供前端筛选器使用。"""
    rows = session.execute(
        select(House.district).where(House.status == "available")).all()
    counts = {}
    for (district,) in rows:
        counts[district] = counts.get(district, 0) + 1
    return [{"district": d, "count": c}
            for d, c in sorted(counts.items(), key=lambda kv: -kv[1])]


# ------------------------------------------------------------------ 收藏

@app.get("/api/favorites")
def list_favorites(user: User = Depends(require_login),
                   session: Session = Depends(get_session)):
    rows = session.scalars(
        select(Favorite).where(Favorite.user_id == user.id)
        .order_by(Favorite.created_at.desc())).all()
    houses = []
    for favorite in rows:
        house = session.get(House, favorite.house_id)
        if house:
            houses.append(house.to_dict())
    return {"total": len(houses), "list": houses}


@app.post("/api/favorites/{house_no}")
def add_favorite(house_no: str, user: User = Depends(require_login),
                 session: Session = Depends(get_session)):
    house = session.scalar(select(House).where(House.house_no == house_no))
    if not house:
        raise HTTPException(status_code=404, detail="房源不存在")

    exists = session.scalar(select(Favorite).where(
        Favorite.user_id == user.id, Favorite.house_id == house.id))
    if exists:
        # 重复收藏不算错误，直接返回成功，前端无需特殊处理
        return {"ok": True, "favorited": True}

    session.add(Favorite(user_id=user.id, house_id=house.id))
    session.commit()
    return {"ok": True, "favorited": True}


@app.delete("/api/favorites/{house_no}")
def remove_favorite(house_no: str, user: User = Depends(require_login),
                    session: Session = Depends(get_session)):
    house = session.scalar(select(House).where(House.house_no == house_no))
    if not house:
        raise HTTPException(status_code=404, detail="房源不存在")
    favorite = session.scalar(select(Favorite).where(
        Favorite.user_id == user.id, Favorite.house_id == house.id))
    if favorite:
        session.delete(favorite)
        session.commit()
    return {"ok": True, "favorited": False}


# ------------------------------------------------------------------ 预约

@app.get("/api/appointments")
def list_appointments(
    house_no: str = "",
    status: str = "",
    session: Session = Depends(get_session),
):
    """看房预约列表。智能体用它做时段冲突检测。"""
    stmt = select(Appointment)
    if house_no:
        house = session.scalar(select(House).where(House.house_no == house_no))
        if not house:
            return {"total": 0, "list": []}
        stmt = stmt.where(Appointment.house_id == house.id)
    if status:
        stmt = stmt.where(Appointment.status == status)
    else:
        # 默认排除已取消与已拒绝，它们不占用时段
        stmt = stmt.where(Appointment.status.in_(["pending", "confirmed"]))

    rows = session.scalars(stmt.order_by(Appointment.viewing_time)).all()
    return {"total": len(rows), "list": [a.to_dict() for a in rows]}


@app.get("/api/appointments/mine")
def my_appointments(user: User = Depends(require_login),
                    session: Session = Depends(get_session)):
    """我的预约。房东看自己房源上的预约，租客看自己提交的。"""
    if user.role in ("landlord", "admin"):
        house_ids = [row[0] for row in session.execute(
            select(House.id).where(House.landlord_id == user.id)).all()]
        if user.role == "admin":
            stmt = select(Appointment)
        else:
            stmt = select(Appointment).where(Appointment.house_id.in_(house_ids or [-1]))
    else:
        stmt = select(Appointment).where(Appointment.tenant_id == user.id)

    rows = session.scalars(stmt.order_by(Appointment.viewing_time.desc())).all()
    return {"total": len(rows), "list": [a.to_dict() for a in rows]}


@app.post("/api/appointments")
def create_appointment(payload: AppointmentCreate,
                       user: User = Depends(current_user),
                       session: Session = Depends(get_session)):
    """提交看房预约。

    服务端必须再做一次时段冲突校验——智能体那边的检查可能因并发而失效，
    也可能有人绕过智能体直接调接口。
    """
    house = session.scalar(select(House).where(House.house_no == payload.house_no))
    if not house:
        raise HTTPException(status_code=404, detail="房源不存在")
    if house.status != "available":
        raise HTTPException(status_code=409, detail="该房源当前不可预约")
    if payload.viewing_time <= datetime.now():
        raise HTTPException(status_code=400, detail="看房时间必须晚于当前时间")

    window = timedelta(minutes=VIEWING_MINUTES)
    clash = session.scalar(
        select(Appointment).where(
            Appointment.house_id == house.id,
            Appointment.status.in_(["pending", "confirmed"]),
            Appointment.viewing_time > payload.viewing_time - window,
            Appointment.viewing_time < payload.viewing_time + window,
        ))
    if clash:
        raise HTTPException(status_code=409, detail="该时段已被预约")

    appointment = Appointment(
        house_id=house.id,
        tenant_id=user.id if user else None,
        contact_name=payload.contact_name or (user.real_name if user else ""),
        contact_phone=payload.contact_phone or (user.phone if user else ""),
        viewing_time=payload.viewing_time,
        remark=payload.remark,
        status="pending",
    )
    session.add(appointment)
    session.commit()
    session.refresh(appointment)
    return {"ok": True, "appointment": appointment.to_dict()}


@app.patch("/api/appointments/{appointment_id}")
def update_appointment(appointment_id: int, payload: AppointmentUpdate,
                       user: User = Depends(require_login),
                       session: Session = Depends(get_session)):
    """确认 / 拒绝 / 取消预约。

    房东与管理员可确认或拒绝；租客只能取消自己的预约。
    """
    if payload.status not in ("confirmed", "rejected", "cancelled"):
        raise HTTPException(status_code=400, detail="状态值不合法")

    appointment = session.get(Appointment, appointment_id)
    if not appointment:
        raise HTTPException(status_code=404, detail="预约不存在")

    house = session.get(House, appointment.house_id)
    is_staff = user.role == "admin" or (house and house.landlord_id == user.id)

    if payload.status == "cancelled":
        if not is_staff and appointment.tenant_id != user.id:
            raise HTTPException(status_code=403, detail="只能取消自己的预约")
    elif not is_staff:
        raise HTTPException(status_code=403, detail="只有房东或管理员可以处理预约")

    appointment.status = payload.status
    appointment.reply = payload.reply
    session.commit()
    return {"ok": True, "appointment": appointment.to_dict()}


# ------------------------------------------------------------------ 报修

@app.get("/api/repairs")
def list_repairs(house_no: str = "", status: str = "", urgency: str = "",
                 session: Session = Depends(get_session)):
    stmt = select(Repair)
    if house_no:
        house = session.scalar(select(House).where(House.house_no == house_no))
        if not house:
            return {"total": 0, "list": []}
        stmt = stmt.where(Repair.house_id == house.id)
    if status:
        stmt = stmt.where(Repair.status == status)
    if urgency:
        stmt = stmt.where(Repair.urgency == urgency)

    rows = session.scalars(stmt.order_by(Repair.created_at.desc())).all()
    return {"total": len(rows), "list": [r.to_dict() for r in rows]}


@app.post("/api/repairs")
def create_repair(payload: RepairCreate, user: User = Depends(current_user),
                  session: Session = Depends(get_session)):
    """提交报修工单。

    `category` / `urgency` / `assignee` 由调用方（智能体）分析后填入；
    直接调用本接口而不填这几项也允许，此时工单进入人工分派流程。
    """
    house = session.scalar(select(House).where(House.house_no == payload.house_no))
    if not house:
        raise HTTPException(status_code=404, detail="房源不存在")

    repair = Repair(
        house_id=house.id,
        tenant_id=user.id if user else None,
        contact_name=payload.contact_name or (user.real_name if user else ""),
        contact_phone=payload.contact_phone or (user.phone if user else ""),
        content=payload.content,
        category=payload.category,
        urgency=payload.urgency,
        assignee=payload.assignee,
        analysis=payload.analysis,
    )
    session.add(repair)
    session.commit()
    session.refresh(repair)
    return {"ok": True, "repair": repair.to_dict()}


@app.patch("/api/repairs/{repair_id}")
def update_repair(repair_id: int, payload: RepairUpdate,
                  user: User = Depends(require_staff),
                  session: Session = Depends(get_session)):
    if payload.status not in ("pending", "processing", "done"):
        raise HTTPException(status_code=400, detail="状态值不合法")
    repair = session.get(Repair, repair_id)
    if not repair:
        raise HTTPException(status_code=404, detail="工单不存在")
    repair.status = payload.status
    session.commit()
    return {"ok": True, "repair": repair.to_dict()}


# ------------------------------------------------------------------ 统计

@app.get("/api/stats")
def stats(session: Session = Depends(get_session)):
    """平台概况。供管理端仪表盘与 AI-05 经营分析取数。"""
    houses = session.scalars(select(House)).all()
    appointments = session.scalars(select(Appointment)).all()
    repairs = session.scalars(select(Repair)).all()

    available = [h for h in houses if h.status == "available"]
    prices = [float(h.rent_price) for h in available]

    by_district = {}
    for house in available:
        entry = by_district.setdefault(house.district, {"count": 0, "total": 0.0})
        entry["count"] += 1
        entry["total"] += float(house.rent_price)

    contracts = session.scalars(select(Contract)).all()
    bills = session.scalars(select(Bill)).all()
    paid = [b for b in bills if b.status == "paid"]
    unpaid = [b for b in bills if b.status == "unpaid"]

    # 按月汇总已收租金，对应需求 F-07 收租统计
    monthly = {}
    for bill in paid:
        if bill.type != "rent" or not bill.paid_at:
            continue
        key = bill.paid_at.strftime("%Y-%m")
        monthly[key] = monthly.get(key, 0.0) + float(bill.amount)

    return {
        "house_total": len(houses),
        "house_available": len(available),
        "house_rented": sum(1 for h in houses if h.status == "rented"),
        "avg_rent": round(sum(prices) / len(prices), 2) if prices else 0,
        "occupancy_rate": (round(sum(1 for h in houses if h.status == "rented")
                                 / len(houses) * 100, 1) if houses else 0),
        "appointment_total": len(appointments),
        "appointment_pending": sum(1 for a in appointments if a.status == "pending"),
        "repair_total": len(repairs),
        "repair_pending": sum(1 for r in repairs if r.status == "pending"),
        "repair_urgent": sum(1 for r in repairs if r.urgency == "紧急"),
        "contract_total": len(contracts),
        "contract_active": sum(1 for c in contracts if c.status == "active"),
        "income_received": round(sum(float(b.amount) for b in paid), 2),
        "income_pending": round(sum(float(b.amount) for b in unpaid), 2),
        "overdue_count": sum(1 for b in unpaid if b.overdue),
        "monthly_income": [{"month": m, "amount": round(v, 2)}
                           for m, v in sorted(monthly.items())],
        "by_district": [
            {"district": d, "count": v["count"],
             "avg_rent": round(v["total"] / v["count"], 2)}
            for d, v in sorted(by_district.items(), key=lambda kv: -kv[1]["count"])
        ],
    }


# ------------------------------------------------------------------ 合同

def _visible_contract(session, contract_no, user):
    """取出合同并校验可见性：只有当事双方与管理员能看。"""
    contract = session.scalar(
        select(Contract).where(Contract.contract_no == contract_no))
    if not contract:
        raise HTTPException(status_code=404, detail="合同不存在")
    if user.role != "admin" and user.id not in (contract.tenant_id,
                                                contract.landlord_id):
        raise HTTPException(status_code=403, detail="无权查看该合同")
    return contract


@app.get("/api/contracts/mine")
def my_contracts(status: str = "", user: User = Depends(require_login),
                 session: Session = Depends(get_session)):
    """我的合同。房东看自己出租的，租客看自己承租的，管理员看全部。"""
    stmt = select(Contract)
    if user.role == "admin":
        pass
    elif user.role == "landlord":
        stmt = stmt.where(Contract.landlord_id == user.id)
    else:
        stmt = stmt.where(Contract.tenant_id == user.id)
    if status:
        stmt = stmt.where(Contract.status == status)

    rows = session.scalars(stmt.order_by(Contract.created_at.desc())).all()
    return {"total": len(rows), "list": [c.to_dict() for c in rows]}


@app.get("/api/contracts/{contract_no}")
def get_contract(contract_no: str, user: User = Depends(require_login),
                 session: Session = Depends(get_session)):
    return _visible_contract(session, contract_no, user).to_dict()


@app.post("/api/contracts")
def create_contract(payload: ContractCreate, user: User = Depends(require_staff),
                    session: Session = Depends(get_session)):
    """房东为某位租客发起签约，生成待确认合同。"""
    house = session.scalar(select(House).where(House.house_no == payload.house_no))
    if not house:
        raise HTTPException(status_code=404, detail="房源不存在")
    if user.role != "admin" and house.landlord_id != user.id:
        raise HTTPException(status_code=403, detail="只能为自己的房源签约")

    tenant = session.scalar(
        select(User).where(User.username == payload.tenant_username))
    if not tenant:
        raise HTTPException(status_code=404, detail="租客不存在")

    try:
        contract = leasing.create_contract(
            session, house, tenant, payload.start_date,
            months=payload.months, deposit_months=payload.deposit_months,
            pay_months=payload.pay_months)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    leasing.notify(session, tenant.id, "contract", "有一份合同待确认",
                   "房源 %s 的租赁合同已生成，请确认。" % house.house_no,
                   contract.contract_no)
    session.commit()
    session.refresh(contract)
    return {"ok": True, "contract": contract.to_dict()}


@app.post("/api/contracts/{contract_no}/activate")
def activate_contract(contract_no: str, user: User = Depends(require_login),
                      session: Session = Depends(get_session)):
    """租客确认合同，合同生效并排出整个租期的账单。"""
    contract = _visible_contract(session, contract_no, user)
    if user.role != "admin" and user.id != contract.tenant_id:
        raise HTTPException(status_code=403, detail="只有承租方可以确认合同")

    try:
        bills = leasing.activate_contract(session, contract)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    session.commit()
    session.refresh(contract)
    return {"ok": True, "contract": contract.to_dict(), "bill_count": len(bills)}


@app.post("/api/contracts/{contract_no}/end")
def end_contract(contract_no: str, payload: ContractEnd,
                 user: User = Depends(require_login),
                 session: Session = Depends(get_session)):
    """退租或到期结束。押金扣款只有房东与管理员能填。"""
    contract = _visible_contract(session, contract_no, user)
    is_staff = user.role == "admin" or user.id == contract.landlord_id
    if payload.deduct and not is_staff:
        raise HTTPException(status_code=403, detail="只有出租方可以扣除押金")

    try:
        result = leasing.end_contract(session, contract,
                                      reason=payload.reason, deduct=payload.deduct)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    session.commit()
    session.refresh(contract)
    return {"ok": True, "contract": contract.to_dict(), **result}


@app.post("/api/contracts/{contract_no}/renew")
def renew_contract(contract_no: str, payload: ContractRenew,
                   user: User = Depends(require_login),
                   session: Session = Depends(get_session)):
    """续租：结束当前合同，生成一份新的待确认合同。"""
    contract = _visible_contract(session, contract_no, user)
    try:
        new_contract = leasing.renew_contract(session, contract,
                                              months=payload.months)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    session.commit()
    session.refresh(new_contract)
    return {"ok": True, "contract": new_contract.to_dict()}


# ------------------------------------------------------------------ 账单

@app.get("/api/bills/mine")
def my_bills(status: str = "", user: User = Depends(require_login),
             session: Session = Depends(get_session)):
    """我的账单。租客看自己要交的，房东看自己该收的。"""
    if user.role == "admin":
        stmt = select(Bill)
    else:
        field = (Contract.landlord_id if user.role == "landlord"
                 else Contract.tenant_id)
        contract_ids = [row[0] for row in session.execute(
            select(Contract.id).where(field == user.id)).all()]
        stmt = select(Bill).where(Bill.contract_id.in_(contract_ids or [-1]))
    if status:
        stmt = stmt.where(Bill.status == status)

    rows = session.scalars(stmt.order_by(Bill.due_date)).all()
    unpaid = [b for b in rows if b.status == "unpaid"]
    return {
        "total": len(rows),
        "unpaid_count": len(unpaid),
        "unpaid_amount": round(sum(float(b.amount) for b in unpaid), 2),
        "overdue_count": sum(1 for b in unpaid if b.overdue),
        "list": [b.to_dict() for b in rows],
    }


@app.post("/api/bills/{bill_no}/pay")
def pay_bill(bill_no: str, payload: PayRequest,
             user: User = Depends(require_login),
             session: Session = Depends(get_session)):
    """缴费。只有承租方与管理员能付。"""
    bill = session.scalar(select(Bill).where(Bill.bill_no == bill_no))
    if not bill:
        raise HTTPException(status_code=404, detail="账单不存在")

    contract = bill.contract
    if user.role != "admin" and (not contract or user.id != contract.tenant_id):
        raise HTTPException(status_code=403, detail="只有承租方可以缴费")

    try:
        payment = leasing.pay_bill(session, bill, user, method=payload.method)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    session.commit()
    session.refresh(bill)
    return {"ok": True, "bill": bill.to_dict(), "payment": payment.to_dict()}


# ------------------------------------------------------------------ 站内消息

@app.get("/api/messages")
def list_messages(unread_only: bool = False, user: User = Depends(require_login),
                  session: Session = Depends(get_session)):
    stmt = select(Message).where(Message.user_id == user.id)
    if unread_only:
        stmt = stmt.where(Message.is_read.is_(False))
    rows = session.scalars(stmt.order_by(Message.created_at.desc()).limit(100)).all()
    unread = session.scalars(
        select(Message).where(Message.user_id == user.id,
                              Message.is_read.is_(False))).all()
    return {"total": len(rows), "unread": len(unread),
            "list": [m.to_dict() for m in rows]}


@app.patch("/api/messages/{message_id}/read")
def read_message(message_id: int, user: User = Depends(require_login),
                 session: Session = Depends(get_session)):
    message = session.get(Message, message_id)
    if not message or message.user_id != user.id:
        raise HTTPException(status_code=404, detail="消息不存在")
    message.is_read = True
    session.commit()
    return {"ok": True}


@app.post("/api/messages/read-all")
def read_all_messages(user: User = Depends(require_login),
                      session: Session = Depends(get_session)):
    rows = session.scalars(
        select(Message).where(Message.user_id == user.id,
                              Message.is_read.is_(False))).all()
    for message in rows:
        message.is_read = True
    session.commit()
    return {"ok": True, "updated": len(rows)}


# ------------------------------------------------------------------ 公告

@app.get("/api/notices")
def list_notices(session: Session = Depends(get_session)):
    rows = session.scalars(
        select(Notice).where(Notice.is_published.is_(True))
        .order_by(Notice.published_at.desc())).all()
    return {"total": len(rows), "list": [n.to_dict() for n in rows]}


@app.post("/api/notices")
def create_notice(payload: NoticeCreate, user: User = Depends(require_login),
                  session: Session = Depends(get_session)):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="只有管理员可以发布公告")
    notice = Notice(title=payload.title, content=payload.content,
                    summary=payload.summary or payload.content[:80],
                    category=payload.category, publisher_id=user.id)
    session.add(notice)
    session.commit()
    session.refresh(notice)
    return {"ok": True, "notice": notice.to_dict()}


# ------------------------------------------------------------------ 对话记录

@app.post("/api/chat-messages")
def log_chat(payload: ChatLog, session: Session = Depends(get_session)):
    """保存智能体对话记录，供多轮上下文与后续分析使用。"""
    message = ChatMessage(
        session_id=payload.session_id, user_id=payload.user_id,
        role=payload.role, content=payload.content, intent=payload.intent)
    session.add(message)
    session.commit()
    return {"ok": True, "id": message.id}


@app.get("/api/chat-messages")
def list_chat(session_id: str, limit: int = Query(50, ge=1, le=200),
              session: Session = Depends(get_session)):
    rows = session.scalars(
        select(ChatMessage).where(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.created_at.desc()).limit(limit)).all()
    return {"total": len(rows), "list": [
        {"role": m.role, "content": m.content, "intent": m.intent,
         "created_at": m.created_at.strftime("%Y-%m-%d %H:%M:%S")}
        for m in reversed(rows)]}


# ------------------------------------------------------------------ 前端托管
#
# 必须放在文件最末尾：挂载到 "/" 会接管所有未匹配的路径，
# 若放在 API 路由之前，/api/* 请求会被静态文件处理器吞掉。
#
# 有构建产物时，访问 http://localhost:8080 就是完整网站，
# 不需要额外启动前端开发服务器。

if HAS_WEB:
    app.mount("/", StaticFiles(directory=WEB_DIST, html=True), name="web")
