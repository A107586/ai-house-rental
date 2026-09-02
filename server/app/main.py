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
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app import leasing
from app.auth import create_token, decode_token, using_dev_secret
from app import db as db_module
from app.db import get_session, init_db
from app.models import (Appointment, Bill, BrowsingHistory, ChatMessage,
                        Contract, Favorite, House, HouseImage, Message,
                        Notice, OperationLog, Payment, Repair, Review, User)
from app import oplog
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

@app.middleware("http")
async def record_operation(request, call_next):
    """M-07 操作日志：统一采集所有改变状态的业务请求。

    放在中间件而不是各接口里手写，是因为手写**会漏**——新增接口时忘了
    加日志，事后从日志里根本看不出来少了什么。这里是「默认全记，显式排除」。

    失败的请求也记：一串 403 正是需要被看见的信号。
    但写日志本身不能拖垮业务——落库异常一律吞掉。
    """
    response = await call_next(request)

    method, path = request.method, request.url.path
    if not oplog.should_log(method, path):
        return response

    user_id, username, role = oplog.identify(request.headers.get("authorization"))
    # 按模块引用取会话工厂，不用 from ... import 绑死名字：
    # 测试会把 db.SessionLocal 换成内存库，绑死了就写到真库里去了
    session = db_module.SessionLocal()
    try:
        session.add(OperationLog(
            user_id=user_id, username=username, role=role,
            action=oplog.action_of(method, path),
            method=method, path=path[:255],
            status_code=response.status_code,
            ip=oplog.client_ip(request)))
        session.commit()
    except Exception:
        session.rollback()
    finally:
        session.close()
    return response


# 一次看房占用一小时，与智能体端的约定保持一致
VIEWING_MINUTES = 60

# 允许匿名与普通用户查询的房源状态。pending 待审、offline 已下架不对外。
PUBLIC_HOUSE_STATUS = ("available", "rented")


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


@app.put("/api/auth/profile")
def update_profile(payload: ProfileUpdate, user: User = Depends(require_login),
                   session: Session = Depends(get_session)):
    """维护个人资料。对应需求 F-03 此前缺失的后半段。

    用户名与角色不在这里改——用户名是登录凭据，角色调整属于管理员权限。
    """
    fields = payload.model_dump(exclude_unset=True, exclude_none=True)
    for key, value in fields.items():
        setattr(user, key, value)
    session.commit()
    session.refresh(user)
    return {"ok": True, "user": user.to_dict()}


@app.post("/api/auth/password")
def change_password(payload: PasswordChange, user: User = Depends(require_login),
                    session: Session = Depends(get_session)):
    """修改密码。必须验证原密码，否则拿到令牌就能直接改密夺号。"""
    if not verify_password(payload.old_password, user.password_hash):
        raise HTTPException(status_code=403, detail="原密码不正确")
    if payload.old_password == payload.new_password:
        raise HTTPException(status_code=400, detail="新密码不能与原密码相同")

    user.password_hash = hash_password(payload.new_password)
    session.commit()
    # 令牌是无状态的，改密后旧令牌仍然有效。课程项目不做令牌吊销，
    # 但前端会主动要求重新登录，避免用户误以为旧会话已失效。
    return {"ok": True, "relogin": True}


# ------------------------------------------------------------------ 房源相册

@app.get("/api/houses/{house_no}/images")
def list_house_images(house_no: str, session: Session = Depends(get_session)):
    house = session.scalar(select(House).where(House.house_no == house_no))
    if not house:
        raise HTTPException(status_code=404, detail="房源不存在")
    rows = session.scalars(
        select(HouseImage).where(HouseImage.house_id == house.id)
        .order_by(HouseImage.is_cover.desc(), HouseImage.sort_order)).all()
    return {"total": len(rows), "list": [i.to_dict() for i in rows]}


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
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    """房源列表。所有筛选条件都是可选的，未传即不过滤。"""
    stmt = select(House)

    # 待审核与已下架的房源不对外可见。若不拦，房东一发布就能被匿名用户
    # 通过 ?status=pending 查到，审核机制就形同虚设了。
    if not user or user.role != "admin":
        if status not in PUBLIC_HOUSE_STATUS:
            status = "available"

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
def get_house(house_no: str, user: User = Depends(current_user),
              session: Session = Depends(get_session)):
    house = session.scalar(select(House).where(House.house_no == house_no))
    if not house:
        raise HTTPException(status_code=404, detail="房源不存在")
    house.view_count += 1

    # 登录用户顺手记一条浏览足迹。同一房源只留一条，重复浏览更新时间与次数，
    # 否则刷十次详情页就把足迹列表刷满了。
    if user:
        record = session.scalar(select(BrowsingHistory).where(
            BrowsingHistory.user_id == user.id,
            BrowsingHistory.house_id == house.id))
        if record:
            record.view_count += 1
            record.viewed_at = datetime.now()
        else:
            session.add(BrowsingHistory(user_id=user.id, house_id=house.id))

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


@app.put("/api/houses/{house_no}")
def update_house(house_no: str, payload: HouseUpdate,
                 user: User = Depends(require_staff),
                 session: Session = Depends(get_session)):
    """编辑房源内容。对应需求 F-01 此前缺失的「修改」。

    房东改完关键信息后房源要重新走审核，避免先发一套合规的、
    审核通过之后再改成违规内容。
    """
    house = session.scalar(select(House).where(House.house_no == house_no))
    if not house:
        raise HTTPException(status_code=404, detail="房源不存在")
    if user.role != "admin" and house.landlord_id != user.id:
        raise HTTPException(status_code=403, detail="只能修改自己发布的房源")

    fields = payload.model_dump(exclude_unset=True, exclude_none=True)
    tags = fields.pop("tags", None)
    for key, value in fields.items():
        setattr(house, key, value)
    if tags is not None:
        house.tags = ",".join(tags)

    # 内容变更后回到待审状态，管理员改的不用重审
    if fields or tags is not None:
        if user.role != "admin" and house.status == "available":
            house.status = "pending"

    session.commit()
    session.refresh(house)
    return {"ok": True, "house": house.to_dict()}


# ------------------------------------------------------------------ 房东工作台

@app.get("/api/landlord/dashboard")
def landlord_dashboard(user: User = Depends(require_staff),
                       session: Session = Depends(get_session)):
    """房东工作台：数据卡片 + 待办聚合，登录后一屏看清该处理什么。"""
    houses = session.scalars(
        select(House).where(House.landlord_id == user.id)).all()
    house_ids = [h.id for h in houses] or [-1]

    rented = [h for h in houses if h.status == "rented"]
    vacant = [h for h in houses if h.status == "available"]

    contracts = session.scalars(
        select(Contract).where(Contract.landlord_id == user.id)).all()
    active = [c for c in contracts if c.status == "active"]
    expiring = [c for c in active if c.days_left <= 30]

    contract_ids = [c.id for c in contracts] or [-1]
    bills = session.scalars(
        select(Bill).where(Bill.contract_id.in_(contract_ids))).all()
    paid = [b for b in bills if b.status == "paid"]
    unpaid = [b for b in bills if b.status == "unpaid"]

    this_month = datetime.now().strftime("%Y-%m")
    month_income = sum(float(b.amount) for b in paid
                       if b.paid_at and b.paid_at.strftime("%Y-%m") == this_month)

    pending_appointments = session.scalars(
        select(Appointment).where(Appointment.house_id.in_(house_ids),
                                  Appointment.status == "pending")).all()
    open_repairs = session.scalars(
        select(Repair).where(Repair.house_id.in_(house_ids),
                             Repair.status != "done")).all()
    urgent_repairs = [r for r in open_repairs if r.urgency == "紧急"]

    return {
        "house_total": len(houses),
        "house_rented": len(rented),
        "house_vacant": len(vacant),
        "house_pending": sum(1 for h in houses if h.status == "pending"),
        "occupancy_rate": (round(len(rented) / len(houses) * 100, 1)
                           if houses else 0),
        "month_income": round(month_income, 2),
        "income_pending": round(sum(float(b.amount) for b in unpaid), 2),
        "overdue_count": sum(1 for b in unpaid if b.overdue),
        "contract_active": len(active),
        "todo": {
            "appointments": len(pending_appointments),
            "repairs": len(open_repairs),
            "urgent_repairs": len(urgent_repairs),
            "expiring_contracts": len(expiring),
            "total": (len(pending_appointments) + len(open_repairs)
                      + len(expiring)),
        },
        "expiring_list": [c.to_dict() for c in expiring],
    }


@app.get("/api/landlord/houses")
def landlord_houses(user: User = Depends(require_staff),
                    session: Session = Depends(get_session)):
    """我的房源。房东只看自己的，管理员看全部。"""
    stmt = select(House)
    if user.role != "admin":
        stmt = stmt.where(House.landlord_id == user.id)
    rows = session.scalars(stmt.order_by(House.published_at.desc())).all()
    return {"total": len(rows), "list": [h.to_dict() for h in rows]}


# ------------------------------------------------------------------ 管理员

@app.get("/api/admin/houses")
def admin_houses(status: str = "", keyword: str = "",
                 user: User = Depends(require_login),
                 session: Session = Depends(get_session)):
    """管理员房源总览，可按状态筛出待审核的。"""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    stmt = select(House)
    if status:
        stmt = stmt.where(House.status == status)
    if keyword:
        like = "%" + keyword + "%"
        stmt = stmt.where(House.title.like(like) | House.house_no.like(like))
    rows = session.scalars(stmt.order_by(House.published_at.desc())).all()
    return {"total": len(rows), "list": [h.to_dict() for h in rows]}


@app.patch("/api/houses/{house_no}/review")
def review_house(house_no: str, payload: HouseReview,
                 user: User = Depends(require_login),
                 session: Session = Depends(get_session)):
    """房源审核。通过则上架，驳回则退回待审并通知房东。

    此前房东发布的房源会落成 pending 却没有任何地方能审，
    导致它永远不会出现在前台——这个接口补上了这个断点。
    """
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    house = session.scalar(select(House).where(House.house_no == house_no))
    if not house:
        raise HTTPException(status_code=404, detail="房源不存在")
    if house.status not in ("pending", "offline"):
        raise HTTPException(status_code=409, detail="该房源当前无需审核")

    if payload.approved:
        house.status = "available"
        title, content = "房源审核通过", "房源 %s 已通过审核并上架。" % house.house_no
    else:
        house.status = "offline"
        title = "房源审核未通过"
        content = "房源 %s 未通过审核。%s" % (house.house_no, payload.reason or "")

    leasing.notify(session, house.landlord_id, "system", title, content,
                   house.house_no)
    session.commit()
    session.refresh(house)
    return {"ok": True, "house": house.to_dict()}


@app.get("/api/admin/users")
def admin_users(keyword: str = "", role: str = "",
                user: User = Depends(require_login),
                session: Session = Depends(get_session)):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    stmt = select(User)
    if role:
        stmt = stmt.where(User.role == role)
    if keyword:
        like = "%" + keyword + "%"
        stmt = stmt.where(User.username.like(like) | User.real_name.like(like))
    rows = session.scalars(stmt.order_by(User.created_at.desc())).all()
    return {"total": len(rows), "list": [u.to_dict() for u in rows]}


@app.patch("/api/admin/users/{user_id}")
def admin_update_user(user_id: int, payload: UserUpdate,
                      user: User = Depends(require_login),
                      session: Session = Depends(get_session)):
    """禁用／启用账号，或调整角色。"""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    target = session.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="用户不存在")
    if target.id == user.id:
        # 管理员把自己禁用或降级会直接锁死后台，必须挡住
        raise HTTPException(status_code=409, detail="不能修改自己的账号状态")

    if payload.status is not None:
        if payload.status not in ("active", "disabled"):
            raise HTTPException(status_code=400, detail="状态值不合法")
        target.status = payload.status
    if payload.role is not None:
        if payload.role not in ("tenant", "landlord", "admin"):
            raise HTTPException(status_code=400, detail="角色值不合法")
        target.role = payload.role

    session.commit()
    session.refresh(target)
    return {"ok": True, "user": target.to_dict()}


@app.get("/api/history")
def browsing_history(limit: int = Query(20, ge=1, le=100),
                     user: User = Depends(require_login),
                     session: Session = Depends(get_session)):
    """我的浏览足迹。同时是 AI-07 房源推荐的输入。"""
    rows = session.scalars(
        select(BrowsingHistory).where(BrowsingHistory.user_id == user.id)
        .order_by(BrowsingHistory.viewed_at.desc()).limit(limit)).all()
    result = []
    for record in rows:
        if not record.house:
            continue
        result.append({
            **record.house.to_dict(),
            "viewed_at": record.viewed_at.strftime("%Y-%m-%d %H:%M"),
            "my_view_count": record.view_count,
        })
    return {"total": len(result), "list": result}


@app.delete("/api/history")
def clear_history(user: User = Depends(require_login),
                  session: Session = Depends(get_session)):
    rows = session.scalars(select(BrowsingHistory).where(
        BrowsingHistory.user_id == user.id)).all()
    for record in rows:
        session.delete(record)
    session.commit()
    return {"ok": True, "deleted": len(rows)}


# ------------------------------------------------------------------ 评价

@app.get("/api/houses/{house_no}/reviews")
def list_reviews(house_no: str, session: Session = Depends(get_session)):
    house = session.scalar(select(House).where(House.house_no == house_no))
    if not house:
        raise HTTPException(status_code=404, detail="房源不存在")
    rows = session.scalars(
        select(Review).where(Review.house_id == house.id)
        .order_by(Review.created_at.desc())).all()
    return {
        "total": len(rows),
        "average": house.avg_rating,
        "list": [r.to_dict() for r in rows],
    }


@app.post("/api/reviews")
def create_review(payload: ReviewCreate, user: User = Depends(require_login),
                  session: Session = Depends(get_session)):
    """租后评价。

    只有租约正常结束的承租方才能评，且一份合同只能评一次——
    不然任何人都能刷分，评分就失去参考价值了。
    """
    contract = session.scalar(
        select(Contract).where(Contract.contract_no == payload.contract_no))
    if not contract:
        raise HTTPException(status_code=404, detail="合同不存在")
    if contract.tenant_id != user.id:
        raise HTTPException(status_code=403, detail="只有承租方可以评价")
    if contract.status != "ended":
        raise HTTPException(status_code=409, detail="租约结束后才能评价")

    exists = session.scalar(
        select(Review).where(Review.contract_id == contract.id))
    if exists:
        raise HTTPException(status_code=409, detail="该租约已评价过")

    review = Review(
        contract_id=contract.id, house_id=contract.house_id,
        tenant_id=user.id, rating=payload.rating,
        rating_house=payload.rating_house or payload.rating,
        rating_landlord=payload.rating_landlord or payload.rating,
        content=payload.content)
    session.add(review)

    leasing.notify(session, contract.landlord_id, "system", "收到新评价",
                   "房源 %s 收到一条 %d 星评价。"
                   % (contract.house.house_no if contract.house else "", payload.rating))
    session.commit()
    session.refresh(review)
    return {"ok": True, "review": review.to_dict()}


@app.get("/api/reviews/pending")
def pending_reviews(user: User = Depends(require_login),
                    session: Session = Depends(get_session)):
    """待评价的租约：已结束但还没评过的。"""
    contracts = session.scalars(
        select(Contract).where(Contract.tenant_id == user.id,
                               Contract.status == "ended")).all()
    reviewed = {row[0] for row in session.execute(
        select(Review.contract_id).where(Review.tenant_id == user.id)).all()}
    rows = [c.to_dict() for c in contracts if c.id not in reviewed]
    return {"total": len(rows), "list": rows}


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
                 user: User = Depends(current_user),
                 session: Session = Depends(get_session)):
    """报修列表。

    指定房源号时任何人都能看（房源详情页要展示该房源的维修历史，
    这是对租客透明的信息）；不指定时会列出全平台工单，
    含联系人与故障描述，必须登录且按角色限定范围。
    """
    stmt = select(Repair)

    if house_no:
        house = session.scalar(select(House).where(House.house_no == house_no))
        if not house:
            return {"total": 0, "list": []}
        stmt = stmt.where(Repair.house_id == house.id)
    else:
        if not user:
            raise HTTPException(status_code=401, detail="请先登录")
        if user.role == "landlord":
            house_ids = [row[0] for row in session.execute(
                select(House.id).where(House.landlord_id == user.id)).all()]
            stmt = stmt.where(Repair.house_id.in_(house_ids or [-1]))
        elif user.role != "admin":
            stmt = stmt.where(Repair.tenant_id == user.id)

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

    # 房东只能处理自己房源上的工单。缺了这一层，任何房东都能改
    # 别人房源工单的状态，甚至把没修的标成已完成。
    if user.role != "admin":
        house = session.get(House, repair.house_id)
        if not house or house.landlord_id != user.id:
            raise HTTPException(status_code=403, detail="只能处理自己房源的工单")

    repair.status = payload.status
    if payload.status == "done" and repair.tenant_id:
        leasing.notify(session, repair.tenant_id, "repair", "报修已处理完成",
                       "你提交的报修（%s）已处理完成。" % repair.content[:20])
    session.commit()
    return {"ok": True, "repair": repair.to_dict()}


@app.get("/api/landlord/tenants")
def landlord_tenants(user: User = Depends(require_staff),
                     session: Session = Depends(get_session)):
    """租客档案：名下所有签过约的租客，附租约与缴费概况。"""
    stmt = select(Contract)
    if user.role != "admin":
        stmt = stmt.where(Contract.landlord_id == user.id)
    contracts = session.scalars(stmt).all()

    profiles = {}
    for contract in contracts:
        entry = profiles.setdefault(contract.tenant_id, {
            "contracts": [], "paid": 0.0, "unpaid": 0.0, "overdue": 0,
        })
        entry["contracts"].append(contract)
        for bill in contract.bills:
            if bill.status == "paid":
                entry["paid"] += float(bill.amount)
            elif bill.status == "unpaid":
                entry["unpaid"] += float(bill.amount)
                if bill.overdue:
                    entry["overdue"] += 1

    rows = []
    for tenant_id, entry in profiles.items():
        tenant = session.get(User, tenant_id)
        if not tenant:
            continue
        active = [c for c in entry["contracts"] if c.status == "active"]
        rows.append({
            **tenant.to_dict(),
            "contract_count": len(entry["contracts"]),
            "active_count": len(active),
            "houses": [c.house.house_no for c in active if c.house],
            "paid_total": round(entry["paid"], 2),
            "unpaid_total": round(entry["unpaid"], 2),
            "overdue_count": entry["overdue"],
        })
    # 有逾期的排前面，房东最需要先看到这些人
    rows.sort(key=lambda r: (-r["overdue_count"], -r["unpaid_total"]))
    return {"total": len(rows), "list": rows}


@app.get("/api/admin/contracts")
def admin_contracts(status: str = "", user: User = Depends(require_login),
                    session: Session = Depends(get_session)):
    """全平台租约总览。"""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    stmt = select(Contract)
    if status:
        stmt = stmt.where(Contract.status == status)
    rows = session.scalars(stmt.order_by(Contract.created_at.desc())).all()
    return {"total": len(rows), "list": [c.to_dict() for c in rows]}


@app.get("/api/admin/appointments")
def admin_appointments(status: str = "", user: User = Depends(require_login),
                       session: Session = Depends(get_session)):
    """全平台看房预约总览。"""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    stmt = select(Appointment)
    if status:
        stmt = stmt.where(Appointment.status == status)
    rows = session.scalars(
        stmt.order_by(Appointment.viewing_time.desc()).limit(200)).all()
    return {"total": len(rows), "list": [a.to_dict() for a in rows]}


@app.patch("/api/notices/{notice_id}")
def toggle_notice(notice_id: int, published: bool,
                  user: User = Depends(require_login),
                  session: Session = Depends(get_session)):
    """上下架公告。下架后前台不再展示，但记录仍保留。"""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    notice = session.get(Notice, notice_id)
    if not notice:
        raise HTTPException(status_code=404, detail="公告不存在")
    notice.is_published = published
    session.commit()
    return {"ok": True, "notice": notice.to_dict()}


@app.get("/api/admin/notices")
def admin_notices(user: User = Depends(require_login),
                  session: Session = Depends(get_session)):
    """管理员看全部公告，含已下架的。"""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    rows = session.scalars(
        select(Notice).order_by(Notice.published_at.desc())).all()
    return {"total": len(rows),
            "list": [{**n.to_dict(), "is_published": n.is_published}
                     for n in rows]}


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

ALLOWED_CHAT_ROLES = ("user", "assistant")


@app.post("/api/chat-messages")
def log_chat(payload: ChatLog, user: User = Depends(require_login),
             session: Session = Depends(get_session)):
    """保存智能体对话记录，供 AI-09 多轮上下文使用。

    身份一律取自令牌，**不接受请求体里的 user_id**——否则任何人都能
    伪造成他人的对话记录，既污染别人的上下文，也污染分析数据。

    未登录用户不落库：服务端无从判断一个匿名 session_id 属于谁，
    存了就等于把对话摆在一个只靠猜编号保护的地方。匿名用户的多轮
    上下文由前端自己带着走，换设备记不住是可接受的代价。
    """
    if payload.role not in ALLOWED_CHAT_ROLES:
        raise HTTPException(status_code=400, detail="role 只能是 user 或 assistant")
    message = ChatMessage(
        session_id=payload.session_id, user_id=user.id,
        role=payload.role, content=payload.content, intent=payload.intent)
    session.add(message)
    session.commit()
    return {"ok": True, "id": message.id}


@app.get("/api/chat-messages")
def list_chat(session_id: str, limit: int = Query(50, ge=1, le=200),
              user: User = Depends(require_login),
              session: Session = Depends(get_session)):
    """读取自己的对话记录。

    必须同时按 user_id 过滤：只按 session_id 查的话，猜到或撞到别人的
    会话编号就能读走整段对话——里面有预算、找房意图，报修场景还带电话。
    """
    rows = session.scalars(
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id,
               ChatMessage.user_id == user.id)
        .order_by(ChatMessage.created_at.desc()).limit(limit)).all()
    return {"total": len(rows), "list": [
        {"role": m.role, "content": m.content, "intent": m.intent,
         "created_at": m.created_at.strftime("%Y-%m-%d %H:%M:%S")}
        for m in reversed(rows)]}


@app.delete("/api/chat-messages")
def clear_chat(session_id: str, user: User = Depends(require_login),
               session: Session = Depends(get_session)):
    """清空自己某个会话的记录，对应界面上的「重新开始」。"""
    rows = session.scalars(select(ChatMessage).where(
        ChatMessage.session_id == session_id,
        ChatMessage.user_id == user.id)).all()
    for record in rows:
        session.delete(record)
    session.commit()
    return {"ok": True, "deleted": len(rows)}


# ------------------------------------------------------------------ 操作日志

@app.get("/api/admin/logs")
def list_logs(keyword: str = "", action: str = "", role: str = "",
              failed_only: bool = False,
              limit: int = Query(50, ge=1, le=200), page: int = Query(1, ge=1),
              user: User = Depends(require_login),
              session: Session = Depends(get_session)):
    """操作日志查询（M-07）。

    只对管理员开放。日志里有全站所有人的操作轨迹，房东能翻到就等于
    能看别的房东在做什么。
    """
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="只有管理员可以查看操作日志")

    query = select(OperationLog)
    if keyword:
        like = "%" + keyword + "%"
        query = query.where(or_(OperationLog.username.like(like),
                                OperationLog.action.like(like),
                                OperationLog.path.like(like)))
    if action:
        query = query.where(OperationLog.action == action)
    if role:
        query = query.where(OperationLog.role == role)
    if failed_only:
        # 一串 403 正是需要被看见的信号
        query = query.where(OperationLog.status_code >= 400)

    total = len(session.scalars(query).all())
    rows = session.scalars(
        query.order_by(OperationLog.created_at.desc(), OperationLog.id.desc())
        .offset((page - 1) * limit).limit(limit)).all()
    return {"total": total, "page": page,
            "list": [row.to_dict() for row in rows]}


@app.get("/api/admin/logs/actions")
def log_actions(user: User = Depends(require_login),
                session: Session = Depends(get_session)):
    """日志里出现过的动作清单，供前端做下拉筛选。"""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="只有管理员可以查看操作日志")
    rows = session.scalars(select(OperationLog.action).distinct()).all()
    return {"list": sorted(name for name in rows if name)}


# ------------------------------------------------------------------ 前端托管
#
# 必须放在文件最末尾：挂载到 "/" 会接管所有未匹配的路径，
# 若放在 API 路由之前，/api/* 请求会被静态文件处理器吞掉。
#
# 有构建产物时，访问 http://localhost:8080 就是完整网站，
# 不需要额外启动前端开发服务器。

# 房源图片等静态资源。必须挂在 web 之前，否则会被 "/" 的挂载吞掉。
STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "static")
if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

if HAS_WEB:
    app.mount("/", StaticFiles(directory=WEB_DIST, html=True), name="web")
