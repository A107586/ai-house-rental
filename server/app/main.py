"""业务后端 REST 接口。

职责边界：只做数据的增删改查与业务校验，不含任何大模型逻辑。
智能体服务通过这些接口取数，不直接连数据库
（对应非功能需求 N-06：禁止拼接执行任意 SQL）。
"""

from datetime import datetime, timedelta

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_session, init_db
from app.models import Appointment, ChatMessage, House, User
from app.security import verify_password

app = FastAPI(
    title="房屋租赁系统 · 业务后端",
    description="房源、预约、用户的增删改查接口",
    version="0.1.0",
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


@app.on_event("startup")
def _startup():
    init_db()


# ------------------------------------------------------------------ 模型

class LoginRequest(BaseModel):
    username: str
    password: str


class AppointmentCreate(BaseModel):
    house_no: str
    viewing_time: datetime
    contact_name: str = ""
    contact_phone: str = ""
    remark: str = ""


class ChatLog(BaseModel):
    session_id: str
    role: str
    content: str
    intent: str = ""
    user_id: int | None = None


# 一次看房占用一小时，与智能体端的约定保持一致
VIEWING_MINUTES = 60


# ------------------------------------------------------------------ 通用

@app.get("/health")
def health(session: Session = Depends(get_session)):
    total = session.scalar(select(House).with_only_columns(
        House.id).order_by(None).limit(1))
    house_count = len(session.execute(select(House.id)).all())
    return {"status": "ok", "houses": house_count, "seeded": total is not None}


# ------------------------------------------------------------------ 认证

@app.post("/api/auth/login")
def login(payload: LoginRequest, session: Session = Depends(get_session)):
    user = session.scalar(select(User).where(User.username == payload.username))
    # 用户不存在与密码错误返回同一句提示，避免暴露账号是否存在
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    if user.status != "active":
        raise HTTPException(status_code=403, detail="账号已被禁用")
    return {"ok": True, "user": user.to_dict()}


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


@app.post("/api/appointments")
def create_appointment(payload: AppointmentCreate,
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
        contact_name=payload.contact_name,
        contact_phone=payload.contact_phone,
        viewing_time=payload.viewing_time,
        remark=payload.remark,
        status="pending",
    )
    session.add(appointment)
    session.commit()
    session.refresh(appointment)
    return {"ok": True, "appointment": appointment.to_dict()}


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
