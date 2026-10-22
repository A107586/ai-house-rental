"""业务后端 REST 接口。

职责边界：只做数据的增删改查与业务校验，不含任何大模型逻辑。
智能体服务通过这些接口取数，不直接连数据库
（对应非功能需求 N-06：禁止拼接执行任意 SQL）。
"""

import json
import os
import time
import uuid
import logging
import re
from datetime import datetime, time as dtime, timedelta
from typing import Literal

from fastapi import (Body, Depends, FastAPI, HTTPException, Query,
                     Request)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload, selectinload
from sqlalchemy.orm import Session

from app import gateway, house_query, leasing
from app.console import force_utf8
from app import prodcheck
from app import tracing
from app.auth import create_token, using_dev_secret
from app import db as db_module
from app.db import get_session, init_db
from app.deps import (current_user, require_house_manager,
                      require_login, require_staff)
from app.permissions import (can_manage_house,
                             can_report_repair,
                             can_see_tenant_contact,
                             require_can_manage_house,
                             responsible_user_id)
from app.models import (Appointment, Bill, BrowsingHistory, ChatMessage,
                        ContentFlag, Contract, Favorite, House,
                        AgentProfile, HouseDelegation, HouseImage, HouseReport,
                        Message, Notice, OperationLog, Payment, Refund, Repair,
                        RepairImage, RepairRecord, Review, User,
                        ViewingRecord,
                        mask_name)
from app import (config_store, idcard, moderation, oplog,
                 pricing,
                 ratelimit,
                 uploads)
from app.schemas import (AppointmentCreate, AppointmentUpdate, ChatLog,
                         ContractUpdate,
                         ContractCreate, ContractEnd, ContractRenew,
                         HouseCreate, HouseReview, HouseUpdate,
                         LoginRequest, NoticeCreate, PasswordChange,
                         PayRequest, ProfileUpdate, RegisterRequest,
                         RepairCreate, RepairUpdate, ReviewCreate,
                         UserUpdate)
from app.routers import (account, agent, conversation, password_reset,
                         repair_data, reports, saved_search, support)
from app.security import hash_password, verify_password

# 上线自检放在建 app 之前：配置不合格就别把服务起起来。
# 等到监听上了公网端口再报错已经晚了。见 app/prodcheck.py
prodcheck.enforce()

app = FastAPI(
    title="房屋租赁系统 · 业务后端",
    description="房源、预约、报修、收藏、用户的增删改查接口",
    version="0.2.0",
)

# 静态资源根目录。上传接口与文件末尾的静态挂载都要用。
STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "static")

# 跨域白名单。**默认只有本地地址**，上线时用 CORS_ORIGINS 覆盖
# （逗号分隔）。写死在代码里的白名单上线那天一定会被忘掉，
# 表现是前端一个接口都调不通，而浏览器控制台里只说一句 CORS 错误。
_DEV_ORIGINS = ["http://localhost:3000", "http://localhost:5173",
                "http://localhost:8000", "http://127.0.0.1:3000",
                "http://127.0.0.1:5173", "http://127.0.0.1:8000"]
CORS_ORIGINS = [o.strip() for o in (os.environ.get("CORS_ORIGINS") or "").split(",")
                if o.strip()] or _DEV_ORIGINS

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 慢请求的判定线。超过它就打一条日志。
#
# 500ms 是拍出来的，但有依据：需求 N-01 要求 100 并发下 1 秒以内，
# 单请求超过半秒说明离那条线已经不远了，值得看一眼。
SLOW_REQUEST_MS = int(os.environ.get("SLOW_REQUEST_MS") or 500)

# 慢请求日志是中文的。输出一旦被重定向到文件（部署时的常态），
# 本地编码写不出汉字，这行日志就会变成一段 logging 的报错。
# 见 app/console.py
force_utf8()

_slow_logger = logging.getLogger("rental.slow")


@app.middleware("http")
async def log_slow_requests(request, call_next):
    """记录慢请求。

    出问题时只能靠压测复现，是排障最耗时的一种情况——线上慢了，
    本地怎么都跑不出来。有这条日志至少知道**是哪个接口、慢到什么程度**。

    只打日志不做别的：既不告警也不熔断。那是运维系统的事，
    在这里做只会把一个诊断工具变成新的故障点。
    """
    started = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - started) * 1000

    if elapsed_ms >= SLOW_REQUEST_MS and request.url.path.startswith("/api/"):
        _slow_logger.warning(
            "慢请求 %.0fms %s %s -> %s",
            elapsed_ms, request.method, request.url.path,
            response.status_code)
    return response


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


# 请求编号 + 结构化访问日志，见 app/tracing.py。
# **注册在最后**：Starlette 里后注册的中间件在最外层，这样编号从请求一进来就有，
# 上面的慢请求日志、操作日志里如果要用也拿得到，耗时也包住了整条链。
tracing.setup()
app.middleware("http")(tracing.trace_requests)


# 按业务模块拆出去的路由，见 app/routers/__init__.py
app.include_router(repair_data.router)
app.include_router(support.router)
app.include_router(conversation.router)
app.include_router(reports.router)
app.include_router(saved_search.router)
app.include_router(password_reset.router)
app.include_router(account.router)
app.include_router(agent.router)


# 一次看房占用多久由系统配置决定（M-09），这里只是配置缺失时的兜底。
# 智能体端的默认值与此保持一致。
VIEWING_MINUTES = 60
# 可预约的看房时段，与智能体 booking/tools/parse_datetime.py 的 BUSINESS_START/END 一致
VIEWING_OPEN = dtime(9, 0)
VIEWING_CLOSE = dtime(18, 0)
VIEWING_AHEAD_DAYS = 30

# 看房预约的状态只能往前走：待确认的可以确认、拒绝、取消；已确认的只能取消；
# 拒绝与取消是终点——要再看就重新约一次，重新约会重新查时段冲突。
APPOINTMENT_NEXT = {
    "pending": ("confirmed", "rejected", "cancelled"),
    "confirmed": ("cancelled",),
}
APPOINTMENT_STATUS_TEXT = {"pending": "还在待确认", "confirmed": "已经确认",
                           "rejected": "已经被拒绝", "cancelled": "已经取消"}

# 允许匿名与普通用户查询的房源状态。pending 待审、offline 已下架不对外。
PUBLIC_HOUSE_STATUS = ("available", "rented")


@app.on_event("startup")
def _startup():
    init_db()
    # 补齐缺失的配置项。只插不覆盖，否则每次重启都会把管理员改过的
    # 配置打回默认值。
    session = db_module.SessionLocal()
    try:
        config_store.ensure_defaults(session)
    finally:
        session.close()


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
    house_count = session.scalar(select(func.count(House.id)))
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
    username = payload.username.strip()
    real_name = payload.real_name.strip()
    phone = payload.phone.strip()
    if not username:
        raise HTTPException(status_code=422, detail="请输入用户名")
    if not re.fullmatch(r"[A-Za-z0-9_]{3,20}", username):
        raise HTTPException(status_code=422,
                            detail="用户名需为 3–20 位字母、数字或下划线")
    if not payload.password:
        raise HTTPException(status_code=422, detail="请输入密码")
    if not 6 <= len(payload.password) <= 64:
        raise HTTPException(status_code=422, detail="密码长度需为 6–64 位")
    if any(char.isspace() for char in payload.password):
        raise HTTPException(status_code=422, detail="密码不能包含空格")
    if real_name and not re.fullmatch(r"[A-Za-z\u4e00-\u9fff· ]{2,20}", real_name):
        raise HTTPException(status_code=422,
                            detail="姓名需为 2–20 位中英文字符")
    if phone and not re.fullmatch(r"1[3-9]\d{9}", phone):
        raise HTTPException(status_code=422, detail="请输入正确的 11 位手机号")
    if payload.role not in ("tenant", "landlord", "agent"):
        raise HTTPException(status_code=400,
                            detail="只能注册租客、房东或经纪人账号")
    # 经纪人可以自助注册，但**这个角色本身不带任何权限**——
    # 接单要等资质审核通过（J-01）。所以这里不必额外设卡：
    # 注册一个 agent 账号在能力上等同于一个什么都做不了的账号。
    # 反过来把注册也卡死，只会逼着每个中介去找管理员开户。
    exists = session.scalar(select(User).where(User.username == username))
    if exists:
        raise HTTPException(status_code=409, detail="用户名已被占用")

    user = User(
        username=username,
        password_hash=hash_password(payload.password),
        real_name=real_name,
        phone=phone,
        role=payload.role,
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return {"ok": True, "token": create_token(user), "user": user.to_dict()}


@app.post("/api/auth/login")
def login(payload: LoginRequest, request: Request,
          session: Session = Depends(get_session)):
    """登录。带失败限流（B-1）。

    限流按 IP 而不按用户名：按用户名看着更精准，实际上是**给攻击者提供
    了锁死他人账号的手段**——拿一个已知用户名连错八次，本人就登不上了。
    """
    ip = ratelimit.client_ip(request)
    allowed, wait = ratelimit.check(ip)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail="登录尝试过于频繁，请 %d 秒后再试" % wait)

    username = payload.username.strip()
    # 参数没填不算失败尝试——那是手滑，不是在猜密码
    if not username:
        raise HTTPException(status_code=422, detail="请输入用户名")
    if not payload.password:
        raise HTTPException(status_code=422, detail="请输入密码")

    user = session.scalar(select(User).where(User.username == username))
    # 用户不存在与密码错误返回同一句提示，避免暴露账号是否存在
    if not user or not verify_password(payload.password, user.password_hash):
        ratelimit.record_failure(ip)
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    if user.status != "active":
        raise HTTPException(status_code=403, detail="账号已被禁用")

    # 本人已经证明身份，之前的手误不该继续记账
    ratelimit.record_success(ip)
    return {"ok": True, "token": create_token(user), "user": user.to_dict()}


@app.post("/api/auth/logout-all")
def logout_all(user: User = Depends(require_login),
               session: Session = Depends(get_session)):
    """退出所有设备（B-2）。

    手机丢了、在网吧登录过忘了退——这时用户需要的是一个「把所有地方都
    踢下线」的按钮，而不是逐台设备去点退出。改密码也能达到同样效果，
    但不该强迫一个只想下线的人换掉自己记得住的密码。
    """
    user.token_valid_after = datetime.now()
    session.commit()
    return {"ok": True, "relogin": True}


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
    # id_card 必须从这个循环里排除掉。User 上没有同名字段，setattr 会
    # 直接给对象挂一个属性，把**明文号码**带进 ORM 会话——正是这里
    # 最不该发生的事。它只走下面那段专门的处理。
    fields.pop("id_card", None)
    for key, value in fields.items():
        setattr(user, key, value)

    if payload.id_card is not None and payload.id_card.strip():
        ok, result = idcard.prepare(payload.id_card)
        if not ok:
            raise HTTPException(status_code=400, detail=result)
        # 同一个身份证号只能绑一个账号，否则实名就形同虚设
        taken = session.scalar(select(User).where(
            User.id_card_hash == result["hash"], User.id != user.id))
        if taken:
            raise HTTPException(status_code=409,
                                detail="该身份证号已被其他账号绑定")
        user.id_card_hash = result["hash"]
        user.id_card_masked = result["masked"]
        # 到此为止，payload.id_card 里的原文再没有被使用，
        # 请求结束就随对象一起回收——从不写入任何持久化存储

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
    # 改密码即吊销此前签发的全部令牌（B-2）。
    #
    # 「改密码」在用户心里等于「把别人踢下去」——如果旧令牌还能用满
    # 七天，那这个动作实际上什么都没做。密码泄露后改密码是第一反应，
    # 也正是这一步必须真的生效的时候。
    user.token_valid_after = datetime.now()
    session.commit()
    # 当前这条令牌也一并作废了，所以要求前端重新登录
    return {"ok": True, "relogin": True}


# ------------------------------------------------------------------ 房源相册

@app.get("/api/houses/{house_no}/images")
def list_house_images(house_no: str, session: Session = Depends(get_session)):
    """房源相册的完整信息。

    房源详情里的 images 只有 URL 字符串，够列表页展示；房东要管理相册
    就得知道每张的 id 与是不是封面，所以单开一个接口。
    """
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
    # ---- K-05 高级筛选。字段在 K-04 里已经落库，这里只是接上 ----
    orientation: str = "",
    floor_level: str = "",
    elevator: bool | None = None,
    decoration: str = "",
    heating: str = "",
    lease_term: str = "",
    available_days: int | None = None,
    features: str = "",
    # 首页分栏：最近 N 天上架 / 均分不低于 N。都是筛选，不是排序。
    published_days: int | None = None,
    min_rating: float | None = None,
    deposit_months: int | None = Query(None, ge=0, le=12),
    pay_months: int | None = Query(None, ge=1, le=12),
    sort: str = "published_at",
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    """房源列表。所有筛选条件都是可选的，未传即不过滤。

    K-05 之前这里只有六个维度，而 `orientation`、`floor` 明明已经在库里
    ——**数据在，只是没人能筛**。「电梯」「精装」则更糟：它们挤在逗号
    分隔的 `tags` 字符串里，用 `LIKE '%电梯%'` 去筛，「无电梯」会被
    「电梯」命中，筛出来的结果是错的。K-04 把这些拆成了独立列，
    这里才谈得上「任意组合、结果必然正确」。
    """
    stmt = select(House)

    # 待审核与已下架的房源不对外可见。若不拦，房东一发布就能被匿名用户
    # 通过 ?status=pending 查到，审核机制就形同虚设了。
    if not user or user.role != "admin":
        if status not in PUBLIC_HOUSE_STATUS:
            status = "available"

    if status:
        stmt = stmt.where(House.status == status)

    # 条件构造统一在 app/house_query.py。**这里不再手写一遍**——
    # 保存搜索要判断「新上架的房源符不符合用户存下的条件」，
    # 两份实现一旦分叉，表现是最难查的那种：列表页筛得到这套房、
    # 上新提醒却不发，或者反过来（K-09）。
    stmt = house_query.apply_filters(stmt, {
        "district": district, "layout": layout, "rent_type": rent_type,
        "rent_price_min": rent_price_min, "rent_price_max": rent_price_max,
        "area_min": area_min, "area_max": area_max, "keyword": keyword,
        "orientation": orientation, "floor_level": floor_level,
        "elevator": elevator, "decoration": decoration, "heating": heating,
        "lease_term": lease_term, "available_days": available_days,
        "features": features,
        "published_days": published_days, "min_rating": min_rating,
        "deposit_months": deposit_months, "pay_months": pay_months,
    })

    # 用 count(*) 而不是把匹配的行全捞出来再 len()。
    # 后者会把每一行都实例化成 ORM 对象，只为得到一个数字——
    # 100 并发下这一处就把列表接口拖过了 1 秒。
    total = session.scalar(
        select(func.count()).select_from(stmt.subquery()))

    order = {
        "rent_price": House.rent_price.asc(),
        "-rent_price": House.rent_price.desc(),
        "area": House.area.asc(),
        "-area": House.area.desc(),
        "view_count": House.view_count.desc(),
    }.get(sort, House.published_at.desc())
    # 统一成元组，下面一律 order_by(*order)——
    # 有的分支给一个排序键，有的给三个，不归一化就得在调用处分叉
    order = (order,)

    # 按评分排序要单独处理：avg_rating 是 Python 属性，不是列。
    # 用一个左连接的聚合子查询把均分带进来再排，没有评价的排在最后
    # （NULL 视作 0），否则「优选好房」第一页会全是没人评过的房子。
    if sort == "rating":
        scores = (select(Review.house_id.label("hid"),
                         func.avg(Review.rating).label("avg_score"),
                         func.count(Review.id).label("cnt"))
                  .where(Review.deleted_at.is_(None))
                  .group_by(Review.house_id)
                  .subquery())
        stmt = stmt.outerjoin(scores, scores.c.hid == House.id)
        order = (func.coalesce(scores.c.avg_score, 0).desc(),
                 func.coalesce(scores.c.cnt, 0).desc(),
                 House.published_at.desc())

    # 预加载 to_dict() 会用到的三个关联：相册、房东、评价。
    # 不预加载的话每条房源都要再发三次查询（images / landlord /
    # reviews，后者被 avg_rating 与 review_count 各读一次）——
    # 一页 12 条就是 36 次额外往返，典型的 N+1。
    rows = session.scalars(
        stmt.options(selectinload(House.images),
                     selectinload(House.reviews),
                     joinedload(House.landlord))
        .order_by(*order).offset((page - 1) * limit).limit(limit)).all()

    # 列表走精简载荷。完整字典是详情页的，列表用它等于每个请求都白白
    # 序列化一遍房源描述、整个相册数组和费用明细（见 House.to_card_dict）
    return {"total": total, "page": page, "limit": limit,
            "list": [h.to_card_dict() for h in rows]}


@app.get("/api/houses/map")
def map_houses(
    district: str = "",
    layout: str = "",
    rent_type: str = "",
    rent_price_min: float | None = None,
    rent_price_max: float | None = None,
    area_min: float | None = None,
    area_max: float | None = None,
    keyword: str = "",
    orientation: str = "",
    floor_level: str = "",
    elevator: bool | None = None,
    decoration: str = "",
    heating: str = "",
    lease_term: str = "",
    available_days: int | None = None,
    features: str = "",
    west: float | None = None,
    south: float | None = None,
    east: float | None = None,
    north: float | None = None,
    limit: int = Query(500, ge=1, le=1000),
    session: Session = Depends(get_session),
):
    """当前筛选条件 + 地图视口内的可租房源点位。

    **必须声明在 `/api/houses/{house_no}` 之前**，否则 FastAPI 会把
    `map` 当成一个房源编号去匹配那条路由，这个接口永远走不到。

    筛选条件走 `house_query.apply_filters`，和列表接口同一套。
    这一点不能省：地图和列表是同一次检索的两种看法，各写一份筛选的话，
    用户勾了「有电梯」，列表里那套没电梯的房子消失了、地图上却还亮着——
    而他会以为是地图坏了。同事那版只覆盖了 7 个维度，
    K-04/K-05 补的朝向、楼层、电梯、装修、租期、可入住日全都漏在外面。

    只返回有坐标的房源。没定过位的房子在地图上无处可画，
    硬塞一个默认坐标会让它们全堆在市中心某一点上。
    """
    stmt = select(House).where(
        House.status == "available",
        House.lng.isnot(None),
        House.lat.isnot(None),
    )
    stmt = house_query.apply_filters(stmt, {
        "district": district, "layout": layout, "rent_type": rent_type,
        "rent_price_min": rent_price_min, "rent_price_max": rent_price_max,
        "area_min": area_min, "area_max": area_max, "keyword": keyword,
        "orientation": orientation, "floor_level": floor_level,
        "elevator": elevator, "decoration": decoration, "heating": heating,
        "lease_term": lease_term, "available_days": available_days,
        "features": features,
    })

    # 视野四个角要么齐全、要么当没传。只给两个角的半截参数
    # 拿去过滤，结果是一片说不清对不对的点
    if None not in (west, south, east, north):
        stmt = stmt.where(House.lng >= west, House.lng <= east,
                          House.lat >= south, House.lat <= north)

    rows = session.scalars(
        stmt.order_by(House.published_at.desc()).limit(limit)).all()
    return {"total": len(rows), "list": [h.to_map_dict() for h in rows]}


@app.get("/api/houses/{house_no}")
def get_house(house_no: str, user: User = Depends(current_user),
              session: Session = Depends(get_session)):
    house = session.scalar(select(House).where(House.house_no == house_no))
    if not house:
        raise HTTPException(status_code=404, detail="房源不存在")
    # **加一这件事交给数据库做，不要读出来加完再写回去。**
    #
    # `house.view_count += 1` 是「读-改-写」：两个请求同时读到 563，
    # 各自算出 564，各自写回，两次浏览只记了一次。实测 200 次并发浏览
    # 只涨了 6——**丢了 97%**。浏览量在房东那边是要看的（「浏览 346」），
    # 也参与热度与推荐，不是可有可无的数字。
    #
    # 写成 SQL 的 `view_count = view_count + 1`，加法在库里做，
    # 并发事务自然串起来，一次不丢。
    session.execute(update(House).where(House.id == house.id)
                    .values(view_count=House.view_count + 1))

    # 登录用户顺手记一条浏览足迹。同一房源只留一条，重复浏览更新时间与次数，
    # 否则刷十次详情页就把足迹列表刷满了。
    if user:
        record = session.scalar(select(BrowsingHistory).where(
            BrowsingHistory.user_id == user.id,
            BrowsingHistory.house_id == house.id))
        if record:
            # 同上：同一个人开两个标签页点同一套房也会撞
            session.execute(
                update(BrowsingHistory)
                .where(BrowsingHistory.id == record.id)
                .values(view_count=BrowsingHistory.view_count + 1,
                        viewed_at=datetime.now()))
        else:
            session.add(BrowsingHistory(user_id=user.id, house_id=house.id))

    session.commit()
    data = house.to_dict()
    # 谁带看：有生效中的委托就是中介，否则房东自己带。租客常问「这套有中介带看吗」，
    # 助手原先只能去翻平台规则，答「没查到」——这是这套房自己的事实，直接给出来。
    delegation = session.scalar(select(HouseDelegation).where(
        HouseDelegation.house_id == house.id, HouseDelegation.status == "active"))
    data["viewing_by"] = "agent" if delegation else "landlord"
    data["viewing_agent"] = mask_name(delegation.agent) if delegation and delegation.agent else ""
    # 页面靠它决定给不给「报修」——判断与 POST /api/repairs 同一处，界面和接口不会各说各的
    data["can_report_repair"] = can_report_repair(session, user, house)
    return data


@app.get("/api/houses/{house_no}/landlord")
def house_landlord(house_no: str, session: Session = Depends(get_session)):
    """房源的房东概要（A-5）。

    租客决定要不要约看房时，很在意「这个房东靠不靠谱」——在租几套、
    平均多久回应、租客怎么评价。这些信息系统里都有，只是从没露出来过。

    **刻意不返回联系方式。** 平台的价值就在于居间，把手机号直接摆出来
    等于鼓励双方绕过平台私下成交。姓名也只留姓氏。
    """
    house = session.scalar(select(House).where(House.house_no == house_no))
    if not house or not house.landlord:
        raise HTTPException(status_code=404, detail="房源不存在")

    landlord = house.landlord
    raw = landlord.real_name or landlord.username
    masked = raw[0] + "*" * max(len(raw) - 1, 1)

    listed = session.scalar(select(func.count(House.id)).where(
        House.landlord_id == landlord.id,
        House.status.in_(("available", "rented")))) or 0
    rented = session.scalar(select(func.count(House.id)).where(
        House.landlord_id == landlord.id, House.status == "rented")) or 0

    # 评价按房东名下所有房源汇总，单套房的评价太少，说明不了什么。
    # **取整条记录而不是只取分数**：分数说明不了「他哪里好」，
    # 而租客决定要不要跟这个房东打交道时，看的正是那几句话
    landlord_reviews = session.scalars(select(Review).join(
        House, Review.house_id == House.id).where(
        House.landlord_id == landlord.id,
        Review.rating_landlord > 0,
        Review.deleted_at.is_(None))
        .order_by(Review.created_at.desc())).all()
    ratings = [r.rating_landlord for r in landlord_reviews]

    # 处理速度用「已结案工单的平均耗时」衡量。这是租客最关心的一项——
    # 房子出问题时，房东多久会管
    records = session.scalars(select(RepairRecord).join(
        Repair, RepairRecord.repair_id == Repair.id).join(
        House, Repair.house_id == House.id).where(
        House.landlord_id == landlord.id)).all()
    # **负数要丢掉，不能取绝对值也不能照样平均。**
    #
    # 实测这里算出过「报修平均 -24 小时处理完」——完成时间早于报修时间。
    # 那是数据本身有问题（种子数据里两个时间独立随机），
    # 不是「处理得特别快」。照样算进去会把均值往下拽，
    # 一个 -24 能抵掉一条真实的 24 小时记录；取绝对值更糟，
    # 等于把一条坏数据当成一条好数据用。
    #
    # 全是坏数据时 spans 为空，下面返回 None，前端那一栏直接不显示——
    # **不显示比显示一个假数字强**。
    spans = [span for span in (
        (r.finished_at - r.repair.created_at).total_seconds() / 3600
        for r in records if r.repair and r.finished_at)
        if span >= 0]

    return {
        "name": masked,
        "verified": bool(landlord.id_card_hash),
        "listed_count": listed,
        "rented_count": rented,
        "review_count": len(ratings),
        "rating": round(sum(ratings) / len(ratings), 1) if ratings else 0,
        # 最近三条写给房东的话。**只取写了字的**——
        # 一串「（未填写）」比没有这一栏更让人怀疑
        "comments": [
            {"name": mask_name(r.tenant) if r.tenant else "租客",
             "rating": r.rating_landlord,
             "content": r.content_landlord.strip(),
             "created_at": r.created_at.strftime("%Y-%m-%d")}
            for r in landlord_reviews if (r.content_landlord or "").strip()
        ][:3],
        # 小时数取整到 0.5，精确到分钟既没意义也显得可疑
        "avg_repair_hours": (round(sum(spans) / len(spans) * 2) / 2
                             if spans else None),
        "joined_at": landlord.created_at.strftime("%Y-%m"),
    }


@app.get("/api/houses/{house_no}/similar")
def similar_houses(house_no: str, limit: int = Query(4, ge=1, le=12),
                   session: Session = Depends(get_session)):
    """相似房源（A-5）。

    用户看完一套不满意就得退回列表重搜，这是最没必要的一次往返。

    相似的判据：同区域 + 租金上下 20% + 同户型。三条都放宽到「或」会
    推出一堆不相干的，所以区域是硬条件，租金与户型用来排序——
    **宁可少推几套，也不推一套明显不像的**。
    """
    house = session.scalar(select(House).where(House.house_no == house_no))
    if not house:
        raise HTTPException(status_code=404, detail="房源不存在")

    price = float(house.rent_price)
    candidates = session.scalars(select(House).where(
        House.id != house.id,
        House.status == "available",
        House.district == house.district,
        House.rent_price >= price * 0.8,
        House.rent_price <= price * 1.2,
    )).all()

    def closeness(other):
        # 户型相同的排前面，其次按租金接近程度
        return (0 if other.layout == house.layout else 1,
                abs(float(other.rent_price) - price))

    candidates.sort(key=closeness)
    return {"total": len(candidates),
            "list": [h.to_dict() for h in candidates[:limit]]}


# K-04 新增的筛选维度。发布与编辑走同一份清单，两处不一致就会出现
# 「发布时填得了、编辑时改不了」这种半截功能。
FACET_FIELDS = ("elevator", "decoration", "floor_no", "floor_total",
                "heating", "available_from", "min_months", "deposit_months",
                "pay_months", "service_fee", "business_area", "lng", "lat")


def _next_house_no(session):
    """生成房源编号。

    原先是 `"DL%04d" % (max(id) + 1)`——两个房东同时发布会算出同一个号，
    撞上 `house_no` 的唯一约束，后提交的那个直接 500（K-12）。
    编号本身没有连续性要求（它只是给人念的），所以撞了就往后顺延，
    顺延几次仍然撞就退回带随机后缀的形式。
    """
    last = session.scalar(select(House).order_by(House.id.desc()))
    base = (last.id if last else 0) + 1
    for offset in range(10):
        candidate = "DL%04d" % (base + offset)
        if not session.scalar(select(House.id).where(
                House.house_no == candidate)):
            return candidate
    return "DL%s" % uuid.uuid4().hex[:8].upper()


def _facet_fields(payload):
    """把用户真正填了的维度取出来。

    `None` 表示「这一项没填」，不是「填了空」——原样 `setattr` 下去会把
    列上的默认值（比如 `elevator=False`、`min_months=1`）覆盖成 NULL，
    之后按这些列筛选就再也筛不到这套房了。
    """
    values = payload.model_dump(include=set(FACET_FIELDS), exclude_none=True)
    return values


def _resolve_listing_deposit(rent, deposit=0, deposit_months=None):
    """房源上的押金。**押金只有 `houses.deposit` 一个真相**（K-02）。

    返回 ``(押金金额, 押金月数)``——月数也要返回，理由见下面第 3 条。

    房东可以用两种方式表达押金：直接填金额，或者选「押二付一」。
    后者填的是月数，这里当场折算成金额存进 `deposit`——
    如果把月数也当成第二个押金来源，详情页读金额、合同读月数，
    就又回到了「同一套房两个押金」的老问题上。

    优先级：

    1. **两个都没填** → 回落到一个月租金。房东把这一栏留空通常是
       「按惯例来」，不是「不收押金」；真要免押就明确选押零付一。
    2. **只填了一个** → 用填的那个。
    3. **两个都填了且对不上** → **以房东手填的金额为准，并把月数清空。**

       第 3 条是这次修的 bug。原来的写法是「只要选了押付方式就按月数算」，
       手填的金额直接被丢掉：房东填「押金 800」又顺手选了「押一付三」，
       存进去的是 `2800`。他在发布页填的是 800，合同上签出来是 2800——
       **而中间没有任何一处提示他这个数被改掉了。**

       为什么是金额赢：月数是一个下拉框的默认档位，金额是他一个字一个字
       敲进去的。两者冲突时，敲进去的那个才是他的意思。

       为什么要把月数清空：留着的话卡片上会印「押一付三」，而押金是 800、
       月租是 2800——**标签和数字当场自相矛盾**，比只给一个数字更糟。
    """
    rent = float(rent)
    typed = float(deposit or 0)

    if deposit_months is None:
        return (typed or rent), None

    by_months = rent * deposit_months
    if not typed or abs(typed - by_months) < 0.01:
        # 没手填，或者手填的正好等于按月数算出来的——两者一致，都留着
        return by_months, deposit_months
    return typed, None


@app.post("/api/houses")
def create_house(payload: HouseCreate, user: User = Depends(require_staff),
                 session: Session = Depends(get_session)):
    """房东发布房源。新房源默认待审核，管理员通过后才对外可见。"""
    house_fields = {"标题": payload.title, "描述": payload.description}
    verdict = moderate(house_fields, scene="house")

    next_no = _next_house_no(session)

    # 押金与押金月数一起定。**两个值必须同时算出来**——
    # 手填金额赢的时候要把月数清空，否则卡片上会印着「押一付三」
    # 而押金是另一个数（见 _resolve_listing_deposit 第 3 条）
    listing_deposit, listing_months = _resolve_listing_deposit(
        payload.rent_price, payload.deposit, payload.deposit_months)

    house = House(
        house_no=next_no, title=payload.title, landlord_id=user.id,
        district=payload.district, community=payload.community,
        address=payload.address, rent_price=payload.rent_price,
        deposit=listing_deposit,
        area=payload.area,
        layout=payload.layout, rent_type=payload.rent_type,
        floor=payload.floor, orientation=payload.orientation,
        building=payload.building.strip(), unit=payload.unit.strip(),
        description=payload.description, tags=",".join(payload.tags),
        maintained_at=datetime.now(),
        **_facet_fields(payload),
        # 管理员发布的房源无需自审；房东发布的是否免审由系统配置决定（M-09）
        status="available" if (
            user.role == "admin"
            or config_store.get_bool(session, "house_auto_approve")
        ) else "pending",
    )
    if house.status == "available":
        house.approved_at = datetime.now()
    # 放在 _facet_fields 之后：那一摊会把 payload 里的 deposit_months
    # 原样塞进来，而这里要的是校对过的那个值
    house.deposit_months = listing_months
    session.add(house)
    session.flush()
    record_flag(session, verdict, house_fields, "house", user,
                house.id, house.house_no)
    # 开了免审的站点，房源发布即上架，这一刻就是「上新」。
    # 要审核的走 review_house，那边另有一处触发（K-09）
    if house.status == "available":
        saved_search.notify_new_house(session, house)
    session.commit()
    session.refresh(house)
    return {"ok": True, "house": house.to_dict()}


@app.patch("/api/houses/{house_no}/status")
def update_house_status(house_no: str, status: str,
                        user: User = Depends(require_house_manager),
                        session: Session = Depends(get_session)):
    """上下架或审核房源。房东只能改自己的房源。"""
    if status not in ("available", "rented", "offline", "pending"):
        raise HTTPException(status_code=400, detail="状态值不合法")

    house = session.scalar(select(House).where(House.house_no == house_no))
    if not house:
        raise HTTPException(status_code=404, detail="房源不存在")
    # 受托中介也管得了（J-02）。判断只写在 app/permissions 一处，
    # 散着写就是这次改造要收拾的那种局面
    require_can_manage_house(session, user, house)

    # **房东和中介只能上架、下架；其余状态由流程推进，不能手动改。**
    #
    # 原先四个状态随便设：待审核的直接改 available、被驳回的点一下上架，
    # 审核形同虚设；「已出租」也能手动标，和合同对不上。
    # 出租由签约生效推进，待审核由发布和编辑推进，只有管理员可以直接改。
    message = ""
    if user.role != "admin":
        if status not in ("available", "offline"):
            raise HTTPException(
                status_code=403,
                detail="房源只能上架或下架；出租状态随合同变化，待审核由编辑后自动进入")
        if house.status == "rented":
            raise HTTPException(status_code=409,
                                detail="这套房源正在出租中，租约结束后才能上下架")
        if status == "available" and house.status == "pending":
            raise HTTPException(status_code=409, detail="这套房源还在审核中，通过后会自动上架")
        if status == "available" and not house.approved_at:
            # 被驳回的、或改过内容待重审的：上架等于提交审核
            status = "pending"
            message = "这套房源的当前内容还没通过审核，已提交审核，通过后会自动上架。"
    if status in ("available", "pending"):
        # 房主停用了或已不是房东，上架了也签不了约。受托中介原先能把这种房子重新挂出去
        problem = leasing.owner_can_list(session.get(User, house.landlord_id))
        if problem:
            raise HTTPException(status_code=409, detail=problem)

    was = house.status
    house.status = status
    if status == "offline":
        # 下架了，还没到时间的看房也就没意义了——同签约生效时的处理
        leasing.close_open_viewings(session, house, "房东已下架这套房")
    if status == "available" and was != "available":
        # 重新上架也是「有新房」。原先只有首次审核通过才提醒，下架过的房子回来没人知道；
        # 同一条搜索对同一套房只提醒一次，反复上下架不会刷屏
        saved_search.notify_new_house(session, house)
    session.commit()
    out = {"ok": True, "house": house.to_dict()}
    if message:
        out["message"] = message
    return out


@app.get("/api/landlord/houses/{house_no}/candidates")
def contract_candidates(house_no: str,
                        user: User = Depends(require_login),
                        session: Session = Depends(get_session)):
    """这套房可以签给谁——**在这套房上有过往来的租客**。

    ## 为什么需要它

    签约表单原先要房东**手打租客用户名**。打错一个字符的结果是
    「租客不存在」，而他手上多半只有一个微信昵称或者一句「小李」——
    平台用户名他压根不知道。这就是「乙方姓名不自动匹配」那条反馈。

    ## 候选人从哪来

    只列**在这套房上约过看房或被带看过**的人。理由和「主动联系」那条
    限制一样（见 conversation.start_conversation）：把全站用户列出来，
    这个接口就成了一个通讯录。而真要签约的那个人，一定在这两张表里
    留下过痕迹——没看过房就签约的情况不存在。

    ## 为什么给真名而不是掩码

    别处的姓名一律只露姓氏，这里例外：**房东正要和这个人签一份合同**，
    合同上写的就是真名，他必须能核对「我要签的是不是这个人」。
    掩码在这里不保护任何人，只会让他签错。
    """
    house = _house_or_404(session, house_no)
    _require_house_owner(session, user, house)

    seen = {}

    def add(uid, source, when):
        if not uid:
            return
        row = seen.get(uid)
        # 同一个人两条记录时留更近的那条，并把来源都记上
        if row:
            row["sources"].add(source)
            if when and (not row["last_at"] or when > row["last_at"]):
                row["last_at"] = when
            return
        seen[uid] = {"sources": {source}, "last_at": when}

    for appt in session.scalars(select(Appointment).where(
            Appointment.house_id == house.id)).all():
        add(appt.tenant_id, "约过看房", appt.viewing_time)
    for record in session.scalars(select(ViewingRecord).where(
            ViewingRecord.house_id == house.id)).all():
        add(record.tenant_id, "带看过", getattr(record, "viewed_at", None))

    users = {u.id: u for u in session.scalars(
        select(User).where(User.id.in_(seen or {-1}))).all()}

    rows = []
    for uid, meta in seen.items():
        person = users.get(uid)
        if not person or person.status != "active":
            continue
        rows.append({
            "username": person.username,
            "real_name": person.real_name or "",
            # 真名没填时要说清楚——合同上的乙方不能是空的
            "ready": bool(person.real_name and person.phone),
            "source": "、".join(sorted(meta["sources"])),
            "last_at": meta["last_at"].strftime("%Y-%m-%d")
                       if meta["last_at"] else "",
        })
    rows.sort(key=lambda r: r["last_at"], reverse=True)
    return {"total": len(rows), "list": rows}


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
                 user: User = Depends(require_house_manager),
                 session: Session = Depends(get_session)):
    """编辑房源内容。对应需求 F-01 此前缺失的「修改」。

    房东改完关键信息后房源要重新走审核，避免先发一套合规的、
    审核通过之后再改成违规内容。
    """
    house = session.scalar(select(House).where(House.house_no == house_no))
    if not house:
        raise HTTPException(status_code=404, detail="房源不存在")
    # 受托中介也管得了（J-02）。判断只写在 app/permissions 一处，
    # 散着写就是这次改造要收拾的那种局面
    require_can_manage_house(session, user, house)

    fields = payload.model_dump(exclude_unset=True, exclude_none=True)
    tags = fields.pop("tags", None)

    # **编辑也要过审核，不能只在发布时查一遍。**
    #
    # 上面那句「改完要重新走审核」说的是**人工**复核；自动拦截这一层
    # 原先只挂在 `POST /api/houses` 上，编辑接口一个字都不查。
    # 于是「先发一套干净的，审核过了再改成留微信」这条路是通的——
    # 实测六种写法（`加微信 wxid_abc123`、`微 信：`、`Ｗｅｉｘｉｎ：`、
    # `微❤信`、11 位手机号、QQ 号）从编辑接口全部原样入库。
    #
    # 只查这次真的改了的字段：没动标题就别拿库里的老标题去撞规则，
    # 那样一条历史遗留的违规文案会把整个编辑功能卡死，
    # 房东连改租金都改不了。
    checked = {name: fields[key] for key, name in
               (("title", "标题"), ("description", "描述")) if key in fields}
    verdict = moderate(checked, scene="house") if checked else None

    # 核验认的是「这个地址、这么大的一套房」的产权凭证。改了这几项还挂着「已核验」，
    # 公开的核验编号就在为一套没核验过的房子背书（2026-10-22 补）。管理员改的不算
    identity_changed = [
        key for key in ("address", "community", "building", "unit", "area")
        if key in fields and str(getattr(house, key) or "") != str(fields[key] or "")]

    for key, value in fields.items():
        setattr(house, key, value)
    if tags is not None:
        house.tags = ",".join(tags)

    if (identity_changed and user.role != "admin"
            and house.verify_status in ("verified", "pending")):
        house.verify_status = "unverified"
        house.verify_no = ""
        house.verified_at = None
        house.verify_remark = "改过房源地址或面积，需要重新提交核验"
        leasing.notify(session, house.landlord_id, "system", "房源需要重新核验",
                       "房源 %s 的地址或面积改过了，原来的核验已失效，请重新提交核验材料。"
                       % house.house_no, house.house_no)

    # 改了押付方式或租金，押金要跟着重算——否则「押一付一」的标签
    # 和 `deposit` 里那个数会各说各话（K-02）
    if (payload.deposit_months is not None or payload.deposit is not None
            or "rent_price" in fields):
        # **「库里存的金额」和「用户这次填的金额」不是一回事。**
        #
        # 第一版把 house.deposit 当成手填值传回去，于是只改租金时：
        # 库里 2800（是上次按押一算出来的）对上新的 3000×1，两者不等，
        # 「手填优先」规则生效 → 押金停在 2800、押付标签被清空。
        # 而用户只是改了个租金，押金本该跟着变成 3000。
        #
        # 手填只在**这一次请求真的带了金额**时才算数。
        months = (payload.deposit_months if payload.deposit_months is not None
                  else house.deposit_months)
        if payload.deposit is None and months is not None:
            # 没碰金额、有押付方式 → 按月数重算，标签和数字一起走
            house.deposit, house.deposit_months = _resolve_listing_deposit(
                house.rent_price, 0, months)
        else:
            house.deposit, house.deposit_months = _resolve_listing_deposit(
                house.rent_price,
                payload.deposit if payload.deposit is not None
                else house.deposit,
                payload.deposit_months)

    # 内容变更后回到待审状态，管理员改的不用重审
    if fields or tags is not None:
        # 房源信息动过就刷新维护时间。列表卡片上那句「3天前维护」
        # 靠它，不刷新的话一套天天在改的房源看起来像是半年没人管（K-04）
        house.maintained_at = datetime.now()
        if user.role != "admin":
            # 内容变了，之前那次审核就不再作数——哪怕现在是下架或出租中，
            # 否则「下架、改成违规内容、再点上架」就绕开了这条规矩
            house.approved_at = None
            if house.status == "available":
                house.status = "pending"
    if verdict is not None:
        # 「需人工复核」那一档原先只在发布时落待办，编辑时判出来就丢了——
        # 先发干净的、再改成擦边的，复核队列里永远看不到
        record_flag(session, verdict, checked, "house", user, house.id, house.house_no)

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
    # 已到期、还没办退租的单独一类：它们不是「快到期」，是**已经该办了**。
    # 原先混在 days_left <= 30 里显示「剩余 0 天」，看不出要做什么（2026-10-22）
    expired = [c for c in active if c.expired]
    expiring = [c for c in active if not c.expired and c.days_left <= 30]
    # 租客提的退租 / 续租等房东确认——不在待办里，房东只能靠翻消息想起来
    lease_requests = [c for c in active if c.request_type and c.request_by == "tenant"]

    contract_ids = [c.id for c in contracts] or [-1]
    bills = session.scalars(
        select(Bill).where(Bill.contract_id.in_(contract_ids))).all()
    paid = [b for b in bills if b.status == "paid"]
    unpaid = [b for b in bills if b.status == "unpaid"]

    this_month = datetime.now().strftime("%Y-%m")
    month_income = sum(b.net_amount for b in paid
                       if b.paid_at and b.paid_at.strftime("%Y-%m") == this_month)

    pending_appointments = session.scalars(
        select(Appointment).where(Appointment.house_id.in_(house_ids),
                                  Appointment.status == "pending")).all()
    open_repairs = session.scalars(
        select(Repair).where(Repair.house_id.in_(house_ids),
                             Repair.status != "done")).all()
    urgent_repairs = [r for r in open_repairs if r.urgency == "紧急"]

    # 近半年的月度收租，用于工作台的收入曲线。
    # 只统计本房东自己的账单——房东看到全平台的收入既没意义也不该看到。
    monthly = {}
    for bill in paid:
        if not bill.paid_at:
            continue
        monthly.setdefault(bill.paid_at.strftime("%Y-%m"), 0.0)
        monthly[bill.paid_at.strftime("%Y-%m")] += bill.net_amount

    # 中间没有收入的月份要补 0，否则曲线会把 1 月和 5 月画成相邻两根柱子，
    # 看上去像是「一直有稳定收入」
    months = []
    if monthly:
        cursor = datetime.strptime(min(monthly), "%Y-%m")
        last = datetime.strptime(max(monthly), "%Y-%m")
        while cursor <= last:
            key = cursor.strftime("%Y-%m")
            months.append({"month": key, "amount": round(monthly.get(key, 0), 2)})
            cursor = leasing.add_months(cursor, 1)
    months = months[-6:]

    return {
        "house_total": len(houses),
        "house_rented": len(rented),
        "house_vacant": len(vacant),
        "monthly_income": months,
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
            "expired_leases": len(expired),
            "lease_requests": len(lease_requests),
            "total": (len(pending_appointments) + len(open_repairs)
                      + len(expiring) + len(expired) + len(lease_requests)),
        },
        "expiring_list": [c.to_dict() for c in expiring],
        "expired_leases": [c.to_dict() for c in expired],
        "lease_requests": [c.to_dict() for c in lease_requests],
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
        if house.status == "offline":
            # 下架的房子只有「被驳回过」的那种还能直接通过。房东自己下架的点通过，
            # 等于管理员替房东把房子重新挂了出去
            if house.approved_at:
                raise HTTPException(status_code=409,
                                    detail="这套房是房东自己下架的，不在审核范围内")
        problem = leasing.owner_can_list(session.get(User, house.landlord_id))
        if problem:
            raise HTTPException(status_code=409, detail=problem)
        house.status = "available"
        house.approved_at = datetime.now()
        title, content = "房源审核通过", "房源 %s 已通过审核并上架。" % house.house_no
        # 上架这一刻才是「上新」。发布时房源还是 pending，对外不可见，
        # 那时通知等于让用户去看一套他点不开的房（K-09）
        saved_search.notify_new_house(session, house)
    else:
        house.status = "offline"
        house.approved_at = None
        title = "房源审核未通过"
        content = "房源 %s 未通过审核。%s" % (house.house_no, payload.reason or "")

    leasing.notify(session, house.landlord_id, "system", title, content,
                   house.house_no)
    session.commit()
    session.refresh(house)
    return {"ok": True, "house": house.to_dict()}


# ------------------------------------------------------------------ 房源核验

# 核验状态机（K-07）。
#
# 《待完善清单》D-4 的判断是对的：不动产登记数据接口我们拿不到，
# 所以「核验」不可能做成真的产权核验。但**状态机和公开的核验编号
# 是我们自己能做的**——把核验方从「登记接口」换成「管理员人工看证件
# 照片」，流程一模一样，将来接上接口只是换掉判定那一步。
#
#     unverified 未核验 ──房东提交──► pending 待核验
#                                        │
#                          管理员通过 ───┴─── 管理员驳回
#                                │              │
#                          verified 已核验   rejected 核验失败
#                                                │
#                                          房东改完再提交 ─► pending
VERIFY_SUBMITTABLE = ("unverified", "rejected")


def _make_verify_no(house):
    """生成公开的核验编号。

    格式 `DL` + 两位年 + 八位。它要能被租客拿去和客服核对，
    所以必须**稳定且唯一**——用房源自增 id 做种子，同一套房重复核验
    也是同一个编号，不会出现「上次给我的号查不到」。
    """
    return "DL%s%08d" % (datetime.now().strftime("%y"), house.id * 7919 % 10**8)


@app.post("/api/houses/{house_no}/verify-request")
def request_verify(house_no: str, user: User = Depends(require_house_manager),
                   session: Session = Depends(get_session)):
    """房东提交核验申请。

    证件照片走已有的相册上传接口，这里只推进状态——把「上传」和
    「提交」分开，房东可以先传齐再提交，不会传一半就进了待审队列。
    """
    house = session.scalar(select(House).where(House.house_no == house_no))
    if not house:
        raise HTTPException(status_code=404, detail="房源不存在")
    # 房东可以把「提交核验材料」这件事一并委托出去（J-02）
    require_can_manage_house(session, user, house)
    if house.verify_status not in VERIFY_SUBMITTABLE:
        raise HTTPException(
            status_code=409,
            detail="该房源已在核验中或已核验通过")

    house.verify_status = "pending"
    house.verify_remark = ""
    session.commit()
    session.refresh(house)
    return {"ok": True, "house": house.to_dict()}


@app.patch("/api/houses/{house_no}/verify")
def decide_verify(house_no: str, payload: HouseReview,
                  user: User = Depends(require_login),
                  session: Session = Depends(get_session)):
    """管理员裁定核验结果。

    **房东不能自己盖这个章。** 核验是平台对房源做的背书，
    房东能自己标成「已核验」，这个标记就一文不值了——
    所以这里只认管理员，且房东侧的编辑接口收不到 `verify_status`。
    """
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    house = session.scalar(select(House).where(House.house_no == house_no))
    if not house:
        raise HTTPException(status_code=404, detail="房源不存在")
    if house.verify_status != "pending":
        raise HTTPException(status_code=409, detail="该房源当前没有待处理的核验申请")

    if payload.approved:
        house.verify_status = "verified"
        house.verify_no = _make_verify_no(house)
        house.verified_at = datetime.now()
        house.verify_remark = ""
        title = "房源核验通过"
        content = "房源 %s 已通过核验，核验编号 %s。" % (house.house_no,
                                                       house.verify_no)
    else:
        # 驳回要写清原因。只说「未通过」，房东不知道该改什么，
        # 下一次提交上来还是同样的问题
        if not payload.reason.strip():
            raise HTTPException(status_code=422, detail="驳回核验必须填写原因")
        house.verify_status = "rejected"
        house.verify_no = ""
        house.verified_at = None
        house.verify_remark = payload.reason.strip()
        title = "房源核验未通过"
        content = "房源 %s 核验未通过：%s" % (house.house_no, house.verify_remark)

    leasing.notify(session, house.landlord_id, "system", title, content,
                   house.house_no)
    session.commit()
    session.refresh(house)
    return {"ok": True, "house": house.to_dict()}


@app.get("/api/admin/verify-queue")
def verify_queue(user: User = Depends(require_login),
                 session: Session = Depends(get_session)):
    """待核验队列。和房源审核队列分开——两者判的不是一件事：
    审核看的是内容合规，核验看的是产权凭证。"""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    rows = session.scalars(
        select(House).where(House.verify_status == "pending")
        .order_by(House.id.desc())).all()
    return {"total": len(rows), "list": [h.to_dict() for h in rows]}


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


@app.get("/api/admin/users/{user_id}/overview")
def admin_user_overview(user_id: int, user: User = Depends(require_login),
                        session: Session = Depends(get_session)):
    """管理员看一个人的全貌：作为租客、房东、中介各做过什么，最近的操作。

    用户管理、户主档案、中介档案三处原先都只有一行摘要，点不开。
    一个账号可以同时是租客和房东（K-13），所以三块都查，有数据才给。
    """
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    target = session.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="用户不存在")
    from app.models import AgentCommission, AgentProfile

    def contract_row(c):
        return {"contract_no": c.contract_no,
                "house_no": c.house.house_no if c.house else "",
                "house_title": c.house.title if c.house else "",
                "start_date": c.start_date.strftime("%Y-%m-%d"),
                "end_date": c.end_date.strftime("%Y-%m-%d"),
                "rent": float(c.rent), "status": c.status}

    out = {"user": dict(target.to_dict(),
                        email=target.email,
                        created_at=target.created_at.strftime("%Y-%m-%d")
                        if target.created_at else "")}

    rented = session.scalars(select(Contract).where(Contract.tenant_id == user_id)
                             .order_by(Contract.start_date.desc())).all()
    if rented:
        unpaid = [b for c in rented for b in c.bills if b.status == "unpaid"]
        out["as_tenant"] = {
            "contracts": [contract_row(c) for c in rented[:10]],
            "active": sum(1 for c in rented if c.status == "active"),
            "overdue_count": sum(1 for b in unpaid if b.overdue),
            "overdue_total": round(sum(float(b.amount) for b in unpaid if b.overdue), 2),
            "repairs": session.scalar(select(func.count(Repair.id))
                                      .where(Repair.tenant_id == user_id)) or 0,
        }

    houses = session.scalars(select(House).where(House.landlord_id == user_id)).all()
    if houses:
        let = session.scalars(select(Contract).where(Contract.landlord_id == user_id)).all()
        by_status = {}
        for h in houses:
            by_status[h.status] = by_status.get(h.status, 0) + 1
        open_repairs = session.scalar(select(func.count(Repair.id)).where(
            Repair.house_id.in_([h.id for h in houses]), Repair.status != "done")) or 0
        out["as_landlord"] = {
            "houses": [{"house_no": h.house_no, "title": h.title, "status": h.status,
                        "rent_price": float(h.rent_price)} for h in houses[:12]],
            "house_count": len(houses),
            "by_status": by_status,
            "active_contracts": sum(1 for c in let if c.status == "active"),
            "open_repairs": open_repairs,
        }

    profile = session.scalar(select(AgentProfile).where(AgentProfile.user_id == user_id))
    if profile or target.role == "agent":
        commissions = session.scalars(select(AgentCommission)
                                      .where(AgentCommission.agent_id == user_id)).all()
        out["as_agent"] = {
            "license_status": profile.license_status if profile else "unsubmitted",
            "license_no": profile.license_no if profile else "",
            "service_districts": profile.service_districts if profile else "",
            "accepting": bool(profile.accepting) if profile else False,
            "delegations": session.scalar(select(func.count(HouseDelegation.id)).where(
                HouseDelegation.agent_id == user_id,
                HouseDelegation.status == "active")) or 0,
            "viewings": session.scalar(select(func.count(ViewingRecord.id))
                                       .where(ViewingRecord.agent_id == user_id)) or 0,
            "deals": len(commissions),
            "commission_total": round(sum(float(c.amount) for c in commissions
                                          if c.status != "cancelled"), 2),
        }

    logs = session.scalars(select(OperationLog).where(OperationLog.user_id == user_id)
                           .order_by(OperationLog.created_at.desc()).limit(10)).all()
    out["recent_ops"] = [{"action": l.action, "status_code": l.status_code,
                          "created_at": l.created_at.strftime("%Y-%m-%d %H:%M")
                          if l.created_at else ""} for l in logs]
    return out


@app.get("/api/admin/landlords")
def admin_landlords(keyword: str = "", user: User = Depends(require_login),
                    session: Session = Depends(get_session)):
    """户主（房东）档案（F-02）。

    「用户管理」看的是账号本身——谁在什么时候注册、是不是被禁用。
    户主档案要回答的是另一类问题：**这个房东手上有多少房、租出去几套、
    这个月收了多少、有没有积压的工单**。同一批数据换个视角看，
    单开一个页面比在用户列表里塞一堆列要清楚。

    电话按 R-02 掩码。管理员要联系房东走站内消息，不需要看到完整号码。
    """
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")

    stmt = select(User).where(User.role == "landlord")
    if keyword:
        like = "%" + keyword + "%"
        stmt = stmt.where(User.username.like(like) | User.real_name.like(like))
    landlords = session.scalars(stmt.order_by(User.created_at)).all()
    if not landlords:
        return {"total": 0, "list": []}

    ids = [u.id for u in landlords]

    # 一次把房源、合同、账单、工单全取回来再在内存里归组，
    # 而不是对每个房东各查一遍——8 个房东就是 32 次查询（N+1）
    houses = session.scalars(
        select(House).where(House.landlord_id.in_(ids))).all()
    contracts = session.scalars(
        select(Contract).where(Contract.landlord_id.in_(ids))).all()
    contract_ids = [c.id for c in contracts] or [-1]
    bills = session.scalars(
        select(Bill).where(Bill.contract_id.in_(contract_ids))).all()
    bill_owner = {c.id: c.landlord_id for c in contracts}
    house_owner = {h.id: h.landlord_id for h in houses}
    repairs = session.scalars(
        select(Repair).where(Repair.house_id.in_(
            [h.id for h in houses] or [-1]))).all()

    this_month = datetime.now().strftime("%Y-%m")
    rows = []
    for u in landlords:
        mine = [h for h in houses if h.landlord_id == u.id]
        my_contracts = [c for c in contracts if c.landlord_id == u.id]
        my_bills = [b for b in bills if bill_owner.get(b.contract_id) == u.id]
        my_repairs = [r for r in repairs
                      if house_owner.get(r.house_id) == u.id]

        month_income = sum(
            b.net_amount for b in my_bills
            if b.status == "paid" and b.paid_at
            and b.paid_at.strftime("%Y-%m") == this_month)

        rows.append({
            **u.to_dict(),
            "house_total": len(mine),
            "house_rented": sum(1 for h in mine if h.status == "rented"),
            "house_pending": sum(1 for h in mine if h.status == "pending"),
            "contract_active": sum(1 for c in my_contracts
                                   if c.status == "active"),
            "month_income": round(month_income, 2),
            "unpaid": round(sum(float(b.amount) for b in my_bills
                                if b.status == "unpaid"), 2),
            "repair_open": sum(1 for r in my_repairs if r.status != "done"),
        })

    rows.sort(key=lambda r: -r["house_total"])
    return {"total": len(rows), "list": rows}


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

    # 禁用账号与调整角色都要吊销对方已签发的令牌（B-2）。
    #
    # 禁用：current_user 虽然会拦住 status != active，但那要等到下一次
    # 请求；把基线一并推上去，语义上更干净。
    # 降级：角色**写在令牌里**，不吊销的话一个刚被降为租客的人，
    # 拿着旧令牌还能以房东身份操作满七天——这才是更危险的那半。
    revoke = False

    offlined = 0
    if payload.status is not None:
        if payload.status not in ("active", "disabled"):
            raise HTTPException(status_code=400, detail="状态值不合法")
        if target.status != payload.status:
            revoke = True
        if payload.status == "disabled" and target.status != "disabled":
            # 停用房东时，他在架的房源一并下架、还没到时间的看房取消。
            # 原先只吊销了令牌：房源照常对外展示、照常能约看，约的人等一个
            # 已经登不上来的房东（2026-10-22）。重新启用不会自动上架，由房东自己决定
            for house in session.scalars(select(House).where(
                    House.landlord_id == target.id, House.status == "available")).all():
                house.status = "offline"
                leasing.close_open_viewings(session, house, "房东账号已停用")
                offlined += 1
            # 他作为租客约的看房、作为房东或中介挂着的委托，也都没人能接着办了
            leasing.cancel_tenant_viewings(session, target, "租客账号已停用")
            if target.role == "agent":
                leasing.wind_down_delegations(session, "中介账号已停用", agent_id=target.id)
            else:
                leasing.wind_down_delegations(session, "房东账号已停用", landlord_id=target.id)
        target.status = payload.status
    if payload.role is not None:
        if payload.role not in ("tenant", "landlord", "admin"):
            raise HTTPException(status_code=400, detail="角色值不合法")
        if target.role != payload.role:
            revoke = True
            if target.role == "landlord" and payload.role == "tenant":
                # 和房东自己点「退回租客」一样的前置，原先管理员这条路什么都不查
                blockers = leasing.landlord_blockers(session, target)
                if blockers:
                    raise HTTPException(
                        status_code=409,
                        detail="改成租客前需要先处理：" + "；".join(blockers))
                leasing.wind_down_delegations(session, "房东已改为租客身份",
                                              landlord_id=target.id)
            if target.role == "agent":
                # 中介的管房权来自 role == agent，角色一改委托就名存实亡，
                # 可预约和会话还在按委托派给他
                leasing.wind_down_delegations(session, "中介已不再是经纪人身份",
                                              agent_id=target.id)
        target.role = payload.role

    if revoke:
        target.token_valid_after = datetime.now()

    session.commit()
    session.refresh(target)
    return {"ok": True, "user": target.to_dict(), "houses_offlined": offlined}


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
        select(Review).where(Review.house_id == house.id,
                             Review.deleted_at.is_(None))
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

    # 评价有效期（M-09 可配）。放太久的评价参考价值很低，
    # 也容易变成事后翻旧账的工具。
    window_days = config_store.get_int(session, "review_window_days")
    if contract.ended_at and (
            datetime.now() - contract.ended_at).days > window_days:
        raise HTTPException(
            status_code=409,
            detail="退租超过 %d 天，已过评价有效期" % window_days)

    exists = session.scalar(
        select(Review).where(Review.contract_id == contract.id,
                             Review.deleted_at.is_(None)))
    if exists:
        raise HTTPException(status_code=409, detail="该租约已评价过")

    # C1：配图只收本系统上传出来的地址。
    #
    # 放开成任意 URL，评价区就成了一个人人可写的外链位：图片指向哪里，
    # 哪里就拿得到每个看房人的 IP 与访问时间；换掉远端那张图，
    # 页面上的内容也就跟着变了——评价本该是**发出去就定了**的东西。
    images = []
    for url in payload.images:
        url = (url or "").strip()
        if not url:
            continue
        if not uploads.is_managed(url):
            raise HTTPException(status_code=400, detail="只能使用本系统上传的图片")
        if url not in images:          # 同一张传两遍不算两张
            images.append(url)
    images = images[:6]

    # 三段文字一起送审。**漏掉任何一段，敏感词就从那一段流出去了**——
    # 「对中介说」和「对房源说」在审核面前没有区别
    review_fields = {"评价内容": payload.content,
                     "对房东的评价": payload.content_landlord,
                     "对中介的评价": payload.content_agent}
    verdict = moderate(review_fields, scene="review")

    # 中介评分只在这份合同确实有受托中介时才收。
    # 没有中介却给了分，那个分落不到任何人头上，还会让「多少人评过我」
    # 这类统计对不上账
    agent_id = contract.agent_id
    rating_agent = payload.rating_agent if agent_id else 0
    content_agent = payload.content_agent if agent_id else ""

    review = Review(
        contract_id=contract.id, house_id=contract.house_id,
        tenant_id=user.id, rating=payload.rating,
        rating_house=payload.rating_house or payload.rating,
        rating_landlord=payload.rating_landlord or payload.rating,
        rating_agent=rating_agent,
        agent_id=agent_id,
        content=payload.content,
        content_landlord=payload.content_landlord,
        content_agent=content_agent,
        images=json.dumps(images, ensure_ascii=False) if images else "")
    session.add(review)
    session.flush()
    record_flag(session, verdict, review_fields, "review", user,
                review.id, contract.contract_no)

    leasing.notify(session, contract.landlord_id, "system", "收到新评价",
                   "房源 %s 收到一条 %d 星评价。"
                   % (contract.house.house_no if contract.house else "", payload.rating))
    session.commit()
    session.refresh(review)
    return {"ok": True, "review": review.to_dict()}


@app.get("/api/reviews/pending")
def pending_reviews(user: User = Depends(require_login),
                    session: Session = Depends(get_session)):
    """待评价的租约：已结束、还在评价有效期内、还没评过的。

    **口径必须和 create_review 一模一样**（2026-10-22 补）。原先这里不看有效期：
    退租超过 30 天的租约一直挂在「待评价」里，角标永远消不掉，点进去提交又被
    「已过评价有效期」拒掉。被删掉的评价也同理——那份合同允许重评，这里却当它评过了。
    """
    contracts = session.scalars(
        select(Contract).where(Contract.tenant_id == user.id,
                               Contract.status == "ended")).all()
    reviewed = {row[0] for row in session.execute(
        select(Review.contract_id).where(Review.tenant_id == user.id,
                                         Review.deleted_at.is_(None))).all()}
    window_days = config_store.get_int(session, "review_window_days")
    now = datetime.now()
    rows = [c.to_dict() for c in contracts
            if c.id not in reviewed
            and not (c.ended_at and (now - c.ended_at).days > window_days)]
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
    """某房源**已被占用的看房时段**。用于时段冲突检测。

    这个接口不需要登录——房源详情页要在未登录时就告诉访客「哪些时段
    已经有人约了」，智能体推荐时段前也要先问一次。

    **正因为它不需要登录，返回的字段被压到最小：只有时刻和状态。**
    在此之前它返回的是完整的 `to_dict()`，里面带着 `contact_name` 和
    脱敏手机号——也就是说任何人不登录就能翻出全站「谁、什么时候、
    看哪套房」，还附带姓名和号码后四位。**姓名 + 号码后四位 + 具体地址
    + 具体时间，凑在一起已经足够定位到一个真人。** 掩码不是免死金牌，
    R-02 要求的是脱敏，不是「脱敏了就可以随便公开」。

    页面与智能体都只用到时刻和状态（房源详情页显示「已被预约的时段」、
    智能体做冲突检测），所以收窄没有任何调用方受影响。房东要看是谁约的，
    走 `/api/appointments/mine`，那里是要登录的。

    也不再允许不带 `house_no` 拉全表：那个用法只有「翻遍全平台」一种
    意义，没有正当场景。
    """
    if not house_no:
        raise HTTPException(status_code=400,
                            detail="请指定房源编号")
    house = session.scalar(select(House).where(House.house_no == house_no))
    if not house:
        return {"total": 0, "list": []}

    stmt = select(Appointment).where(Appointment.house_id == house.id)
    if status:
        stmt = stmt.where(Appointment.status == status)
    else:
        # 默认排除已取消与已拒绝，它们不占用时段
        stmt = stmt.where(Appointment.status.in_(["pending", "confirmed"]))

    rows = session.scalars(stmt.order_by(Appointment.viewing_time)).all()
    return {"total": len(rows),
            "list": [{"id": a.id,
                      "viewing_time": a.viewing_time.strftime(
                          "%Y-%m-%d %H:%M:%S"),
                      "status": a.status} for a in rows]}


@app.get("/api/appointments/mine")
def my_appointments(side: Literal["", "tenant", "landlord"] = "",
                    user: User = Depends(require_login),
                    session: Session = Depends(get_session)):
    """我的预约：我提交的，加上我名下房源收到的（K-13）。

    不再按角色二选一——房东升级前约看过的房，升级后仍然是他的预约。

    `side` 同 contracts/mine（D3）：个人中心问「我约了谁」，
    工作台问「谁约了我」。**这是两份清单**——前者我在等回话，
    后者等着我回话，能做的动作正好相反。
    """
    if side == "tenant":
        stmt = select(Appointment).where(Appointment.tenant_id == user.id)
        rows = session.scalars(
            stmt.order_by(Appointment.viewing_time.desc())).all()
        return _appointment_page(session, user, rows)

    if side == "landlord":
        house_ids = [row[0] for row in session.execute(
            select(House.id).where(House.landlord_id == user.id)).all()]
        delegated = select(HouseDelegation.house_id).where(
            HouseDelegation.agent_id == user.id,
            HouseDelegation.status == "active")
        stmt = select(Appointment).where(
            or_(Appointment.house_id.in_(house_ids or [-1]),
                Appointment.house_id.in_(delegated)))
        rows = session.scalars(
            stmt.order_by(Appointment.viewing_time.desc())).all()
        return _appointment_page(session, user, rows)

    if user.role == "admin":
        stmt = select(Appointment)
    else:
        house_ids = [row[0] for row in session.execute(
            select(House.id).where(House.landlord_id == user.id)).all()]
        # 受托中介也是当事一方：委托期间这套房的预约本来就归他处理（J-05）
        delegated = select(HouseDelegation.house_id).where(
            HouseDelegation.agent_id == user.id,
            HouseDelegation.status == "active")
        stmt = select(Appointment).where(
            or_(Appointment.tenant_id == user.id,
                Appointment.house_id.in_(house_ids or [-1]),
                Appointment.house_id.in_(delegated)))

    rows = session.scalars(stmt.order_by(Appointment.viewing_time.desc())).all()
    return _appointment_page(session, user, rows)


def _appointment_page(session, user, rows):
    """把预约列表转成响应体。

    联系方式按窗口给（J-07）。房源**一次性取回来**，
    不在循环里逐行查——五十条预约就是五十次往返。
    """
    houses = {h.id: h for h in session.scalars(select(House).where(
        House.id.in_({a.house_id for a in rows} or {-1}))).all()}
    return {"total": len(rows),
            "list": [a.to_dict(show_contact=can_see_tenant_contact(
                session, user, a, houses.get(a.house_id))) for a in rows]}


@app.post("/api/appointments")
def create_appointment(payload: AppointmentCreate,
                       user: User = Depends(require_login),
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
    # 下面三条原先只有智能体那一侧在守：它只给 9:00–18:00 的时段、不会替房东约自己的房。
    # 绕开智能体直接调接口，凌晨三点、三年以后、自己约自己都能落库（2026-10-22）
    if can_manage_house(session, user, house):
        raise HTTPException(status_code=400, detail="这是你管理的房源，不需要预约看房")
    clock = payload.viewing_time.time()
    if not (VIEWING_OPEN <= clock <= VIEWING_CLOSE):
        raise HTTPException(status_code=400, detail="看房时间需在 %s–%s 之间" % (
            VIEWING_OPEN.strftime("%H:%M"), VIEWING_CLOSE.strftime("%H:%M")))
    if payload.viewing_time > datetime.now() + timedelta(days=VIEWING_AHEAD_DAYS):
        raise HTTPException(status_code=400,
                            detail="只能预约 %d 天以内的看房" % VIEWING_AHEAD_DAYS)
    replaced = None
    if payload.replaces is not None:
        replaced = session.get(Appointment, payload.replaces)
        if (not replaced or replaced.tenant_id != user.id
                or replaced.house_id != house.id
                or replaced.status not in ("pending", "confirmed")):
            raise HTTPException(status_code=409, detail="要改期的那条预约不存在或已经结束")
    skip = [replaced.id] if replaced else [-1]
    # 同一套房同时只留一条待看的。原先能连占几个时段，别人约不进来，
    # 房东的待办里也是同一个人的好几条
    held = session.scalar(select(Appointment).where(
        Appointment.house_id == house.id, Appointment.tenant_id == user.id,
        Appointment.status.in_(["pending", "confirmed"]),
        Appointment.viewing_time > datetime.now(),
        Appointment.id.not_in(skip)))
    if held:
        raise HTTPException(
            status_code=409,
            detail="你已经约过这套房（%s），要换时间请先取消原来的预约"
                   % held.viewing_time.strftime("%m-%d %H:%M"))

    contact_phone = (payload.contact_phone or user.phone or "").strip()
    if not re.fullmatch(r"1[3-9]\d{9}", contact_phone):
        raise HTTPException(status_code=422, detail="请输入正确的 11 位手机号")

    # 每人每日预约上限，防止刷单占位（M-09 可配）
    daily_limit = config_store.get_int(session, "appointment_daily_limit")
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    used = len(session.scalars(select(Appointment).where(
        Appointment.tenant_id == user.id,
        Appointment.created_at >= today,
        Appointment.status.in_(["pending", "confirmed"]))).all())
    if used >= daily_limit and replaced is None:     # 改期是换一条，不是多约一条
        raise HTTPException(
            status_code=429,
            detail="今天的预约次数已达上限（%d 次），请明天再试" % daily_limit)

    # 并发下的时段冲突（B-3）。
    #
    # 「先查冲突再插入」在两个请求同时进来时会双双通过检查、双双插入。
    # 这里对该房源加一把行级锁：后来的请求会阻塞到前一个提交完，
    # 再查冲突时就能看到那条记录。
    #
    # 为什么不用 UNIQUE(house_id, viewing_time)：models.py 里有原注释——
    # 那个约束既太严（取消掉的预约会永久占住时段），又太松（管不了
    # 「一次看房占用一小时」的重叠判断）。这个判断至今成立。
    #
    # **SQLite 上这行是空操作**：它是库级写锁，反而天然不会出这个问题。
    # 真正生效是在 MySQL 上——这正是「本地 SQLite、部署 MySQL」这个
    # 架构选择需要付出的代价之一：两边的并发行为并不完全一样。
    if session.bind and session.bind.dialect.name != "sqlite":
        session.execute(
            select(House.id).where(House.id == house.id).with_for_update())

    window = timedelta(minutes=config_store.get_int(
        session, "viewing_minutes", VIEWING_MINUTES))
    clash = session.scalar(
        select(Appointment).where(
            Appointment.house_id == house.id,
            Appointment.status.in_(["pending", "confirmed"]),
            Appointment.viewing_time > payload.viewing_time - window,
            Appointment.viewing_time < payload.viewing_time + window,
            Appointment.id.not_in(skip),     # 改期挪半小时，不算和自己冲突
        ))
    if clash:
        raise HTTPException(status_code=409, detail="该时段已被预约")

    appointment = Appointment(
        house_id=house.id,
        tenant_id=user.id,
        contact_name=payload.contact_name or user.real_name,
        contact_phone=contact_phone,
        viewing_time=payload.viewing_time,
        remark=payload.remark,
        status="pending",
    )
    session.add(appointment)
    session.flush()
    if replaced is not None:
        replaced.status = "cancelled"
        replaced.reply = "租客改约到 %s" % payload.viewing_time.strftime("%m-%d %H:%M")

    # 通知当前负责这套房的人（J-05）。
    #
    # 委托出去了就发给受托中介，否则发给房东。**房东始终会收到一份抄送**
    # ——他委托出去的是带看这件事，不是知情权；自己的房子被谁约了、
    # 约在什么时候，他有权知道。
    #
    # 这条通知原本根本不存在：预约提交后没有任何人被告知，房东只能自己
    # 去工作台里翻。加中介时才发现这个洞——「活转给谁」这个问题
    # 逼着人先回答「活到底有没有人接到」。
    responsible = responsible_user_id(session, house)
    when = payload.viewing_time.strftime("%m-%d %H:%M")
    moved = ("（由 %s 改约）" % replaced.viewing_time.strftime("%m-%d %H:%M")
             if replaced is not None else "")
    leasing.notify(
        session, responsible, "system", "新的看房预约",
        "%s 有一条 %s 的看房预约待确认%s。" % (house.house_no, when, moved),
        house.house_no)
    if responsible != house.landlord_id:
        leasing.notify(
            session, house.landlord_id, "system", "新的看房预约（已转交中介）",
            "%s 有一条 %s 的看房预约，已通知受托中介处理。"
            % (house.house_no, when),
            house.house_no)

    session.commit()
    session.refresh(appointment)
    return {"ok": True, "appointment": appointment.to_dict()}


@app.patch("/api/appointments/{appointment_id}")
def update_appointment(appointment_id: int, payload: AppointmentUpdate,
                       user: User = Depends(require_login),
                       session: Session = Depends(get_session)):
    """确认 / 拒绝 / 取消预约。

    房东、受托中介与管理员可确认或拒绝；租客只能取消自己的预约。
    **处理结果一律通知租客**——他要据此安排出门。
    """
    if payload.status not in ("confirmed", "rejected", "cancelled"):
        raise HTTPException(status_code=400, detail="状态值不合法")

    appointment = session.get(Appointment, appointment_id)
    if not appointment:
        raise HTTPException(status_code=404, detail="预约不存在")

    house = session.get(House, appointment.house_id)
    # 受托中介也算「能处理这套房预约的人」（J-05）。
    #
    # 改这一行之前，委托是**只转了权、没转活**：中介能改房源、能回咨询，
    # 唯独确认不了预约——而带看正是委托他去做的那件事。
    # 房东仍然处理得了，`can_manage_house` 里房东的权限不因委托而丧失。
    is_staff = bool(house) and can_manage_house(session, user, house)

    if payload.status == "cancelled":
        if not is_staff and appointment.tenant_id != user.id:
            raise HTTPException(status_code=403, detail="只能取消自己的预约")
    elif not is_staff:
        raise HTTPException(status_code=403,
                            detail="只有房东、受托中介或管理员可以处理预约")

    # 状态只能往前走（2026-10-22 补）。原先任意状态都能改成任意状态：
    # 租客取消之后、别人约了同一时段并被确认，房东再把取消的那条「确认」一下，
    # 同一时段就有两条已确认的看房——取消时让出的时段没有再查冲突。
    if payload.status == appointment.status:
        # 重复点（网络慢时点了两下）：什么都不改，也不再发一遍通知
        return {"ok": True,
                "appointment": appointment.to_dict(
                    show_contact=can_see_tenant_contact(session, user, appointment, house))}
    allowed = APPOINTMENT_NEXT.get(appointment.status, ())
    if payload.status not in allowed:
        raise HTTPException(
            status_code=409,
            detail="这条预约%s，不能再%s" % (
                APPOINTMENT_STATUS_TEXT.get(appointment.status, "已处理"),
                {"confirmed": "确认", "rejected": "拒绝",
                 "cancelled": "取消"}[payload.status]))
    if payload.status == "confirmed" and appointment.viewing_time <= datetime.now():
        raise HTTPException(status_code=409, detail="看房时间已经过了，不能再确认")
    if (payload.status == "cancelled" and appointment.status == "confirmed"
            and appointment.viewing_time <= datetime.now()):
        # 已经确认、时间也过了的，是一次发生过（或爽约）的看房。原先还能「取消」，
        # 带看记录挂在一条已取消的预约上，看房历史就对不上了
        raise HTTPException(status_code=409, detail="看房时间已经过了，这条预约不能再取消")

    appointment.status = payload.status
    appointment.reply = payload.reply

    # 处理结果必须回给租客。
    #
    # 在此之前这里**一条通知都不发**：租客约了看房，之后是确认了、
    # 被拒了、还是没人管，他只能自己回页面一遍遍刷。约看房的人是要
    # 据此安排出门的，把结果压在数据库里不告诉他，等于让他白等。
    #
    # 取消不通知发起人自己——租客自己点的取消，再给他发一条
    # 「你的预约已取消」是噪音。房东取消才需要通知。
    when = appointment.viewing_time.strftime("%m-%d %H:%M")
    house_no = house.house_no if house else ""
    if payload.status == "confirmed":
        leasing.notify(
            session, appointment.tenant_id, "system", "看房预约已确认",
            "%s %s 的看房已确认。%s" % (house_no, when, payload.reply or ""),
            house_no)
    elif payload.status == "rejected":
        # 理由原样带上（同 K-08）：只说「未通过」，租客不知道是时间不合适
        # 还是房子已经租出去了，只能再约一次碰运气
        leasing.notify(
            session, appointment.tenant_id, "system", "看房预约未通过",
            "%s %s 的看房未能安排。%s" % (house_no, when,
                                   payload.reply or "房东未说明原因"),
            house_no)
    elif payload.status == "cancelled" and user.id != appointment.tenant_id:
        leasing.notify(
            session, appointment.tenant_id, "system", "看房预约已取消",
            "%s %s 的看房已被取消。%s" % (house_no, when, payload.reply or ""),
            house_no)
    elif payload.status == "cancelled" and house:
        # 租客自己取消：要告诉负责带看的人。原先这一支不存在，
        # 已确认的看房被租客取消后房东 / 中介毫不知情，照样跑一趟（2026-10-22 补）
        responsible = responsible_user_id(session, house)
        for user_id in {responsible, house.landlord_id}:
            leasing.notify(
                session, user_id, "system", "看房预约已被租客取消",
                "%s %s 的看房，租客已取消。%s" % (house_no, when, payload.reply or ""),
                house_no)

    session.commit()
    return {"ok": True,
            "appointment": appointment.to_dict(
                show_contact=can_see_tenant_contact(
                    session, user, appointment, house))}


# ------------------------------------------------------------------ 报修

@app.get("/api/repairs")
def list_repairs(house_no: str = "", status: str = "", urgency: str = "",
                 side: Literal["", "tenant", "landlord"] = "",
                 user: User = Depends(current_user),
                 session: Session = Depends(get_session)):
    """报修列表。

    指定房源号时任何人都能看（房源详情页要展示该房源的维修历史，
    这是对租客透明的信息）；不指定时会列出全平台工单，
    含联系人与故障描述，必须登录且按角色限定范围。
    """
    stmt = select(Repair)

    # 公开的房源维修历史里**不带报修人**。
    #
    # 「这套房修过什么、修好没有」是对看房人透明的信息；
    # 「谁报的、他的号码后四位」不是。原来两者一起返回，于是任何人
    # 打开房源页，就能看到上一任租客的名字连同「他家卧室门锁坏过」。
    # 姓名 + 号码后四位 + 具体地址，凑在一起足以定位到一个真人。
    with_contact = True

    if house_no:
        house = session.scalar(select(House).where(House.house_no == house_no))
        if not house:
            return {"total": 0, "list": []}
        stmt = stmt.where(Repair.house_id == house.id)
        # 房东本人、受托中介、管理员看自己那套房时仍然要看得到联系人——
        # 他们得联系报修人。判据是「管不管得了这套房」，不是角色
        with_contact = can_manage_house(session, user, house)
    else:
        if not user:
            raise HTTPException(status_code=401, detail="请先登录")
        if user.role != "admin":
            # **按当事方取，不按角色分支**（K-13）。
            #
            # 原来写的是「role 是房东就只看名下房源的工单，否则只看自己
            # 报的」。房东自己也可能是别处的租客——他报的那张工单，
            # 在他升级成房东的那一刻就从这个列表里消失了；反过来，
            # 一个被改成中介角色的账号，名下房源的工单也全看不到了。
            # **数据一直在库里，是查询把它挡住的。**
            #
            # K-13 那次修了 contracts/bills/appointments/refunds 四个
            # `/mine`，漏了这一个。三个条件并列，谁占哪个位置就看得到哪份。
            house_ids = [row[0] for row in session.execute(
                select(House.id).where(House.landlord_id == user.id)).all()]
            stmt = stmt.where(or_(Repair.tenant_id == user.id,
                                  Repair.house_id.in_(house_ids or [-1])))

        # D3：同 contracts/mine。个人中心问「我报的修」，
        # 工作台问「我要修的」——**这是两份清单，不是一份的两种排序**：
        # 前者我在等人来修，后者等着我去修，能做的动作完全不同。
        # 放在 admin 判断之外：管理员看自己那套房时同样需要收窄。
        if side == "tenant":
            stmt = stmt.where(Repair.tenant_id == user.id)
        elif side == "landlord":
            my_houses = [row[0] for row in session.execute(
                select(House.id).where(House.landlord_id == user.id)).all()]
            stmt = stmt.where(Repair.house_id.in_(my_houses or [-1]))

    if status:
        stmt = stmt.where(Repair.status == status)
    if urgency:
        stmt = stmt.where(Repair.urgency == urgency)

    rows = session.scalars(stmt.order_by(Repair.created_at.desc())).all()

    # 工单 SLA（C-4）：超时未处理的要能被看见。
    #
    # 判定为「紧急」之后就没有下文了——没人处理也不会催，这是原先最大的
    # 问题。时限做成可配置（M-09），因为不同规模的团队标准不一样。
    #
    # 只标记不自动升级：自动改优先级会让「紧急」这一档失去意义——
    # 挂久了都变紧急，等于没有紧急。**该被催的是人，不是数据**。
    urgent_hours = config_store.get_int(session, "repair_sla_urgent_hours")
    normal_hours = config_store.get_int(session, "repair_sla_normal_hours")
    now = datetime.now()

    result = []
    for repair in rows:
        # 自己报的那张，报修人就是自己，没有隐去的必要
        show = with_contact or (user is not None
                                and repair.tenant_id == user.id)
        item = repair.to_dict(with_contact=show)
        if repair.status == "done" or not repair.created_at:
            item["overdue"] = False
            item["waited_hours"] = 0
        else:
            waited = (now - repair.created_at).total_seconds() / 3600
            limit = (urgent_hours if repair.urgency == "紧急"
                     else normal_hours)
            item["overdue"] = waited > limit
            item["waited_hours"] = round(waited, 1)
            item["sla_hours"] = limit
        result.append(item)

    return {"total": len(result),
            "overdue_count": sum(1 for r in result if r.get("overdue")),
            "list": result}


@app.post("/api/repairs")
def create_repair(payload: RepairCreate, user: User = Depends(require_login),
                  session: Session = Depends(get_session)):
    """提交报修工单。

    `category` / `urgency` / `assignee` 由调用方（智能体）分析后填入；
    直接调用本接口而不填这几项也允许，此时工单进入人工分派流程。

    **只有在住的租客和管得了这套房的人能报**（见 permissions.can_report_repair）。
    原先连登录都不用，任何人都能往任何一套房的公开维修历史里写一条。
    """
    house = session.scalar(select(House).where(House.house_no == payload.house_no))
    if not house:
        raise HTTPException(status_code=404, detail="房源不存在")
    if not can_report_repair(session, user, house):
        raise HTTPException(status_code=403,
                            detail="只有正住在这套房的租客、房东或受托中介可以报修")

    repair = Repair(
        house_id=house.id,
        tenant_id=user.id,
        contact_name=payload.contact_name or user.real_name,
        contact_phone=payload.contact_phone or user.phone or "",
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
            "overdue_amount": 0.0,
        })
        entry["contracts"].append(contract)
        for bill in contract.bills:
            if bill.status == "paid":
                entry["paid"] += bill.net_amount
            elif bill.status == "unpaid":
                entry["unpaid"] += float(bill.amount)
                if bill.overdue:
                    entry["overdue"] += 1
                    entry["overdue_amount"] += float(bill.amount)

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
            # unpaid_total 含合同里排好、还没到期的账单；真正拖欠的是 overdue_total。
            # 只给前者时，助手把「待收 39650」说成了「欠了 39650」——实际逾期 2 笔
            "unpaid_total": round(entry["unpaid"], 2),
            "overdue_count": entry["overdue"],
            "overdue_total": round(entry["overdue_amount"], 2),
        })
    # 有逾期的排前面，房东最需要先看到这些人
    rows.sort(key=lambda r: (-r["overdue_count"], -r["unpaid_total"]))
    return {"total": len(rows), "list": rows}


@app.get("/api/landlord/tenants/{tenant_id}")
def landlord_tenant_detail(tenant_id: int, user: User = Depends(require_staff),
                           session: Session = Depends(get_session)):
    """一位租客的详情：**只看他和我之间的事**——在我名下的合同、账单、报修。

    原先租客列表点进去什么都没有，房东想知道「他哪笔没交」「上次报修修好没」
    得去合同、工单两个栏目各翻一遍再自己对。别的房东和他之间的事不给看。
    """
    stmt = select(Contract).where(Contract.tenant_id == tenant_id)
    if user.role != "admin":
        stmt = stmt.where(Contract.landlord_id == user.id)
    contracts = sorted(session.scalars(stmt).all(), key=lambda c: c.start_date, reverse=True)
    if not contracts:
        raise HTTPException(status_code=404, detail="这位租客没有在你名下签过合同")
    tenant = session.get(User, tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="租客不存在")

    bills = sorted((b for c in contracts for b in c.bills if b.status != "cancelled"),
                   key=lambda b: b.due_date)
    house_ids = {c.house_id for c in contracts}
    repairs = session.scalars(select(Repair).where(
        Repair.tenant_id == tenant_id, Repair.house_id.in_(house_ids))
        .order_by(Repair.created_at.desc()).limit(20)).all()

    def bill_row(b):
        return {"bill_no": b.bill_no, "type": b.type, "period": b.period,
                "amount": float(b.amount), "status": b.status, "overdue": b.overdue,
                "due_date": b.due_date.strftime("%Y-%m-%d"),
                "house_no": b.contract.house.house_no if b.contract and b.contract.house else ""}

    unpaid = [b for b in bills if b.status == "unpaid"]
    profile = tenant.to_dict()
    return {
        "tenant": {k: profile.get(k) for k in ("id", "username", "real_name", "phone")},
        "summary": {
            "paid_total": round(sum(b.net_amount for b in bills if b.status == "paid"), 2),
            "unpaid_total": round(sum(float(b.amount) for b in unpaid), 2),
            "overdue_count": sum(1 for b in unpaid if b.overdue),
            "overdue_total": round(sum(float(b.amount) for b in unpaid if b.overdue), 2),
        },
        "contracts": [{k: c.to_dict()[k] for k in (
            "contract_no", "house_no", "house_title", "start_date", "end_date", "rent",
            "deposit", "status", "days_left", "request")} for c in contracts],
        # 逾期的、最近要交的排前面；已交的只留最近几笔，够看出缴费习惯就行
        "bills": ([bill_row(b) for b in unpaid][:12]
                  + [bill_row(b) for b in bills if b.status == "paid"][-6:]),
        "repairs": [{"id": r.id, "house_no": r.house.house_no if r.house else "",
                     "category": r.category, "urgency": r.urgency, "status": r.status,
                     "content": r.content[:60],
                     "created_at": r.created_at.strftime("%Y-%m-%d")} for r in repairs],
    }


# 同一位租客一天只催一次。房东在对话框里连说几遍「提醒一下」，
# 租客收到的不该是一串一模一样的消息。
REMIND_TITLE = "房东提醒你缴纳逾期账单"


@app.post("/api/landlord/tenants/{tenant_id}/remind")
def remind_tenant(tenant_id: int, user: User = Depends(require_staff),
                  session: Session = Depends(get_session)):
    """给欠租的租客发一条站内催缴提醒。

    原先平台没有这件事：房东问助手「帮我提醒一下欠租的那个人」，
    助手只能背一句「账单会标记为逾期并持续提醒」——可逾期状态只是
    一个标记，租客那边什么也收不到。

    **只催名下合同里真正逾期的账单**：没有逾期的不发（那是骚扰），
    别人合同上的不发（那是冒名）。消息走 `leasing.notify`，
    账单类会同步推外部通道，与到期提醒同一条路。
    """
    stmt = select(Bill).join(Contract, Bill.contract_id == Contract.id).where(
        Contract.tenant_id == tenant_id, Bill.status == "unpaid")
    if user.role != "admin":
        stmt = stmt.where(Contract.landlord_id == user.id)
    late = sorted((b for b in session.scalars(stmt).all() if b.overdue),
                  key=lambda b: b.due_date)
    if not late:
        raise HTTPException(status_code=400, detail="这位租客在你名下没有逾期账单")

    since = datetime.now() - timedelta(hours=24)
    already = session.scalar(select(Message).where(
        Message.user_id == tenant_id, Message.title == REMIND_TITLE,
        Message.created_at >= since))
    if already:
        raise HTTPException(status_code=429, detail="今天已经提醒过这位租客了，明天再催吧")

    total = round(sum(float(b.amount) for b in late), 2)
    first = late[0]
    content = ("你有 %d 笔逾期账单，共 %s 元，最早一笔 %s 到期。请尽快在「我的租约 → 账单」里缴纳，"
               "有困难可以先和房东沟通。" % (len(late), ("%g" % total),
                                            first.due_date.strftime("%Y-%m-%d")))
    leasing.notify(session, tenant_id, "bill", REMIND_TITLE, content,
                   link_no=first.bill_no)
    session.commit()
    return {"ok": True, "count": len(late), "amount": total}


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
def stats(user: User | None = Depends(current_user),
          session: Session = Depends(get_session)):
    """平台概况。供管理端仪表盘与 AI-05 经营分析取数。

    **分两档返回。** 这个接口原先完全不需要登录，却把
    `income_received`、`income_pending`、`monthly_income`、逾期笔数
    一并返回——**平台收了多少钱、还有多少没收上来，全网可见**。
    这不是个人隐私问题，是经营数据外泄：竞品照着这个接口就能算出
    我们的规模和现金流。

    公开的那一档只留房源侧的事实：总数、可租/已租、均价、出租率、
    分区分布。这些本来就写在每一个房源卡片上，聚合起来也推不出别的，
    而首页的地图找房（未登录也能用）正是靠 `by_district` 上色的。

    经营数据（钱、工单、合同、逾期）只给管理员。AI-05 的经营分析报告
    因此要带管理员令牌来取——那份报告本来就只在管理后台里看得到。

    **聚合下推到数据库，不把整表拉进内存。**

    原先这里是 `select(House)).all()` 之类连查五张表，再用 Python 循环
    数数、求和。单个请求看不出问题，可 100 并发就是 100 次全表扫描加
    100 份对象实例化——实测 p50 到了 1114ms，而需求 N-01 要求 1 秒以内。

    现在每个数字都是一条 COUNT / SUM / GROUP BY，数据库只回传结果行。
    表越大差距越明显：全量拉取是 O(行数)，聚合查询走索引近似 O(1)。
    """
    def scalar(stmt, default=0):
        """取单值。COUNT 不会给 None，但 SUM 在没有匹配行时会。"""
        return session.scalar(stmt) or default

    # ---------------- 房源 ----------------
    house_total = scalar(select(func.count(House.id)))
    house_available = scalar(
        select(func.count(House.id)).where(House.status == "available"))
    house_rented = scalar(
        select(func.count(House.id)).where(House.status == "rented"))
    avg_rent = session.scalar(
        select(func.avg(House.rent_price)).where(House.status == "available"))

    # 区域分布：一条 GROUP BY 顶原先的整表遍历
    district_rows = session.execute(
        select(House.district,
               func.count(House.id),
               func.avg(House.rent_price))
        .where(House.status == "available")
        .group_by(House.district)
        .order_by(func.count(House.id).desc())).all()

    # ---------------- 预约与报修 ----------------
    appointment_total = scalar(select(func.count(Appointment.id)))
    appointment_pending = scalar(select(func.count(Appointment.id))
                                 .where(Appointment.status == "pending"))
    repair_total = scalar(select(func.count(Repair.id)))
    repair_pending = scalar(select(func.count(Repair.id))
                            .where(Repair.status == "pending"))
    repair_urgent = scalar(select(func.count(Repair.id))
                           .where(Repair.urgency == "紧急"))

    # ---------------- 合同与账单 ----------------
    contract_total = scalar(select(func.count(Contract.id)))
    contract_active = scalar(select(func.count(Contract.id))
                             .where(Contract.status == "active"))

    # 实收 = 已缴 − 已退（押金结算、提前退租退回的预付租金）
    income_received = (float(scalar(select(func.sum(Bill.amount))
                                    .where(Bill.status == "paid"), 0.0))
                       - float(scalar(select(func.sum(Refund.refund_amount))
                                      .where(Refund.status == "succeeded"), 0.0)))
    income_pending = scalar(select(func.sum(Bill.amount))
                            .where(Bill.status == "unpaid"), 0.0)
    # 逾期的判定与 Bill.overdue 属性保持一致：未缴且到期日已过。
    # 属性是 Python 侧的，SQL 里得重写一遍——两处必须同时改，
    # 测试里有一条用例专门比对二者结果。
    overdue_count = scalar(select(func.count(Bill.id)).where(
        Bill.status == "unpaid",
        Bill.due_date < datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0)))

    # 按月汇总已收租金（F-07 收租统计）。
    # 月份用 strftime 分组：SQLite 与 MySQL 的日期函数名不同，
    # 这里取的是行数最少的那部分数据（只有已缴租金），
    # 在 Python 侧分组反而更省事也更可移植。
    paid_rents = session.scalars(
        select(Bill).where(
            Bill.status == "paid", Bill.type == "rent",
            Bill.paid_at.isnot(None))).all()
    monthly = {}
    for bill in paid_rents:
        key = bill.paid_at.strftime("%Y-%m")
        # 退回的预付租金要扣掉，否则这个月的「已收租金」比实际多
        monthly[key] = monthly.get(key, 0.0) + bill.net_amount

    # 公开的一档：房源侧的事实。这些数字本来就写在每一张房源卡片上，
    # 聚合起来也推不出别的；首页地图（未登录可用）靠 by_district 上色
    body = {
        "house_total": house_total,
        "house_available": house_available,
        "house_rented": house_rented,
        "avg_rent": round(float(avg_rent), 2) if avg_rent else 0,
        "occupancy_rate": (round(house_rented / house_total * 100, 1)
                           if house_total else 0),
        "by_district": [
            {"district": district, "count": count,
             "avg_rent": round(float(avg), 2)}
            for district, count, avg in district_rows
        ],
    }
    if not user or user.role != "admin":
        return body

    # 经营数据只给管理员。**钱和工单不是公开信息**——
    # 竞品照着 income_received / monthly_income 就能算出我们的规模和现金流
    body.update({
        "appointment_total": appointment_total,
        "appointment_pending": appointment_pending,
        "repair_total": repair_total,
        "repair_pending": repair_pending,
        "repair_urgent": repair_urgent,
        "contract_total": contract_total,
        "contract_active": contract_active,
        "income_received": round(float(income_received), 2),
        "income_pending": round(float(income_pending), 2),
        "overdue_count": overdue_count,
        "monthly_income": [{"month": m, "amount": round(v, 2)}
                           for m, v in sorted(monthly.items())],
    })
    return body


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
def my_contracts(status: str = "",
                 side: Literal["", "tenant", "landlord"] = "",
                 user: User = Depends(require_login),
                 session: Session = Depends(get_session)):
    """我的合同：**只要我是其中一方就算我的**，管理员看全部。

    原先是按角色二选一：role 是 landlord 就只查出租的，否则只查承租的。
    单账号双身份之后这条规则就错了（K-13）——租客升级成房东，
    他升级前签的那份租约当场从「我的租约」里消失，
    看起来就像升级把数据弄丢了。数据一直都在，是这个查询把它挡住了。

    按「我是不是其中一方」来判断，和 role 无关，这样也顺带覆盖了
    「房东自己也租房」这种本来就该支持的情况。
    """
    stmt = select(Contract)
    if user.role != "admin":
        stmt = stmt.where(or_(Contract.tenant_id == user.id,
                              Contract.landlord_id == user.id))

    # D3：`side` 指明「我想看我作为哪一方的合同」。
    #
    # 上面那段（K-13）解决的是**看不到**，这里解决的是**混在一起**：
    # 一个既出租又租房的账号，个人中心和工作台请求的是同一个接口，
    # 拿回同一份列表，于是「我租的房」和「我出租的房」并排躺着，
    # 每看一条都得先认自己是哪一方。同一个人的两种身份，
    # 得由**发起请求的那一屏**来说清楚，而不是让人在结果里自己分。
    #
    # 不传就还是老行为：外部脚本和老前端不会因为这次改动少拿到数据。
    # 管理员传了也照样收窄——他也可能是在看自己名下的那几套。
    if side == "tenant":
        stmt = stmt.where(Contract.tenant_id == user.id)
    elif side == "landlord":
        stmt = stmt.where(Contract.landlord_id == user.id)

    if status:
        stmt = stmt.where(Contract.status == status)

    rows = session.scalars(stmt.order_by(Contract.created_at.desc())).all()

    # **要处理的排前面，已经完结的排后面。**
    #
    # 原先只按创建时间倒序。实测房东那边 20 份合同里 17 份已结束，
    # 两份生效中的排在第 19、20 位——**退租、续租这些按钮只挂在生效中的
    # 合同上，而它们在列表最底下**，用户翻不到就以为功能不存在。
    # 「房东不能退租」这条反馈正是这么来的：按钮一直在，只是没人找得到。
    #
    # 不按状态字母排：那样得到的是 active/cancelled/ended 这种无意义的
    # 顺序。显式给每个状态一个优先级——待确认最急（有人在等我点头），
    # 其次生效中（还能操作），最后是已经没有下一步动作的。
    #
    # 同一档内部仍按创建时间倒序：sorted 是稳定排序，上一步的顺序留着。
    _ORDER = {"pending": 0, "active": 1, "cancelled": 2, "ended": 3}
    rows = sorted(rows, key=lambda c: _ORDER.get(c.status, 9))
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
        leasing.check_start_date(payload.start_date)
        contract = leasing.create_contract(
            session, house, tenant, payload.start_date,
            months=payload.months,
            # None 原样传下去，**不要在这里替换成配置值**。押金口径由
            # leasing.resolve_deposit 一处裁决：显式月数 > 房源上填的押金
            # > 系统配置。在这里就把 None 填掉，房源上的押金永远读不到（K-02）
            deposit_months=payload.deposit_months,
            pay_months=payload.pay_months,
            default_deposit_months=config_store.get_int(session,
                                                        "deposit_months"))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    leasing.notify(session, tenant.id, "contract", "有一份合同待确认",
                   "房源 %s 的租赁合同已生成，请确认。" % house.house_no,
                   contract.contract_no)
    session.commit()
    session.refresh(contract)
    return {"ok": True, "contract": contract.to_dict()}


@app.patch("/api/contracts/{contract_no}")
def update_contract(contract_no: str, payload: ContractUpdate,
                    user: User = Depends(require_login),
                    session: Session = Depends(get_session)):
    """改一份**还没生效**的合同（B4）。

    ## 为什么之前没有这条

    合同原先建出来就定死了：日期填错一天、押付方式选错一档，
    唯一的办法是让租客先拒签、房东再重开一份——而重开的那份又是
    新编号，之前发出去的链接全部作废。**一个填错日期的代价不该是这个。**

    ## 边界：只有待确认的能改

    租客一旦确认（active），这就是一份双方认可的协议了，
    单方面改它是另一回事，不是「修正笔误」。要改就走正式路径：
    退租重签，或者续租时调整。

    这条边界不是保守，是**账单**：整期账单在生效那一刻一次排出
    （见 leasing.activate_contract）。改一份已生效合同的租期或付款周期，
    意味着已经排出的账单要重排——而其中可能已经有付过款的。
    那是一整套对账逻辑，不是一个 PATCH 能承担的。

    ## 改完要重新渲染正文

    合同正文是按条款生成的一段文字。只改字段不重渲染，**页面上显示的
    还是旧条款**——用户看到日期变了、正文没变，会不知道该信哪个。
    """
    contract = _visible_contract(session, contract_no, user)

    if user.role != "admin" and user.id != contract.landlord_id:
        raise HTTPException(status_code=403, detail="只有出租方可以修改合同")
    if contract.status != "pending":
        raise HTTPException(
            status_code=409,
            detail="只有待确认的合同可以修改。这份已经%s，"
                   "要调整条款请走退租重签或续租。"
                   % {"active": "生效", "ended": "结束",
                      "cancelled": "取消"}.get(contract.status, "变更"))

    house = session.get(House, contract.house_id)
    tenant = session.get(User, contract.tenant_id)
    if not house or not tenant:
        raise HTTPException(status_code=409, detail="房源或租客已不存在")

    start = payload.start_date or contract.start_date
    if payload.start_date:
        try:
            leasing.check_start_date(start)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
    months = payload.months
    if months is None:
        # 没传月数就沿用原来的跨度，**按月算而不是按天**——
        # 按天算会把「一年」变成 365 天，遇到闰年或月末就对不齐
        months = leasing.months_between(contract.start_date, contract.end_date)
    pay_months = payload.pay_months or contract.pay_months

    contract.start_date = start
    contract.end_date = leasing.add_months(start, months)
    contract.pay_months = pay_months
    if payload.deposit_months is not None:
        contract.deposit = leasing.resolve_deposit(
            house, payload.deposit_months,
            config_store.get_int(session, "deposit_months"))
    # 租金始终跟着房源当前定价走，和创建时同一个口径——
    # 房东想改租金就去改房源，不该在合同里另开一个价
    contract.rent = house.rent_price
    contract.content = leasing.render_contract_text(
        house, tenant, contract.start_date, contract.end_date,
        contract.rent, contract.deposit, contract.pay_months)

    leasing.notify(session, contract.tenant_id, "system", "合同条款已修改",
                   "房东修改了合同 %s 的条款，确认前请再看一遍。"
                   % contract.contract_no, house.house_no)
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


def _contract_side(contract, user):
    """我在这份合同里是哪一方。管理员按出租方算（代房东处理）。"""
    if user.id == contract.tenant_id and user.role != "admin":
        return "tenant"
    return "landlord"


@app.post("/api/contracts/{contract_no}/end")
def end_contract(contract_no: str, payload: ContractEnd,
                 user: User = Depends(require_login),
                 session: Session = Depends(get_session)):
    """退租。**提前退租要对方同意；到期退租房东直接办。**

    原先租客一点就退：押金全额退、房源重新上架，房东事后才收到一条消息，
    想扣点损坏赔偿都没地方填——改成了租客提、房东确认时结算押金。
    而房东这一侧一直是**点了就退**：租约中途、不需要租客知情、押金扣多少自己定。
    系统生成的合同第六条写的却是「提前 30 日书面通知」（2026-10-22 补上这一半）。

    现在的规则：

    - 租客点：提申请；房东已经提了提前退租时，租客点就是**同意**，
      押金按房东申请时写明的扣款结算（租客同意的就是那个数）；
    - 房东点：租客提过申请 → 确认并结算；合同已到期 → 直接办（到期退租是合同
      本来的约定）；否则 → 提前退租申请，写明原因与押金拟扣，等租客同意；
    - 管理员点：直接办（平台介入的纠纷处理）。
    """
    contract = _visible_contract(session, contract_no, user)
    side = _contract_side(contract, user)

    def requested():
        session.commit()
        session.refresh(contract)
        return {"ok": True, "requested": True, "contract": contract.to_dict()}

    try:
        if side == "tenant":
            if payload.deduct:
                raise HTTPException(status_code=403, detail="只有出租方可以扣除押金")
            if contract.request_type == "end" and contract.request_by == "landlord":
                result = leasing.end_contract(
                    session, contract,
                    reason=contract.request_reason or "房东提前退租，租客同意",
                    deduct=contract.request_deduct)
            else:
                leasing.request_change(session, contract, "end", "tenant",
                                       reason=payload.reason)
                return requested()
        elif (user.role == "admin" or contract.expired
              or (contract.request_type == "end" and contract.request_by == "tenant")):
            result = leasing.end_contract(session, contract,
                                          reason=payload.reason, deduct=payload.deduct)
        else:
            leasing.request_change(session, contract, "end", "landlord",
                                   reason=payload.reason, deduct=payload.deduct)
            return requested()
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    session.commit()
    session.refresh(contract)
    return {"ok": True, "contract": contract.to_dict(), **result}


@app.post("/api/contracts/{contract_no}/renew")
def renew_contract(contract_no: str, payload: ContractRenew,
                   user: User = Depends(require_login),
                   session: Session = Depends(get_session)):
    """续租。一方提出，**另一方**再点一次就是确认，租期在本合同上延长。

    对方已经提了续租，这一下就是同意，按对方申请的月数办；
    否则记成我方的申请，等对方确认。
    """
    contract = _visible_contract(session, contract_no, user)
    side = _contract_side(contract, user)
    try:
        if contract.request_type == "renew" and contract.request_by != side:
            leasing.renew_contract(session, contract, months=contract.request_months)
            session.commit()
            session.refresh(contract)
            return {"ok": True, "renewed": True, "contract": contract.to_dict()}
        leasing.request_change(session, contract, "renew", side, months=payload.months)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    session.commit()
    session.refresh(contract)
    return {"ok": True, "requested": True, "contract": contract.to_dict()}


@app.delete("/api/contracts/{contract_no}/request")
def withdraw_contract_request(contract_no: str, user: User = Depends(require_login),
                              session: Session = Depends(get_session)):
    """撤回自己提的退租 / 续租申请，或拒绝对方提的。"""
    contract = _visible_contract(session, contract_no, user)
    try:
        leasing.withdraw_request(session, contract, _contract_side(contract, user))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    session.commit()
    session.refresh(contract)
    return {"ok": True, "contract": contract.to_dict()}


class ReviewResultIn(BaseModel):
    score: int = Field(ge=0, le=100)
    summary: str = Field(default="", max_length=500)
    risk_count: int = Field(default=0, ge=0)
    source: str = Field(default="", max_length=30)


@app.post("/api/contracts/{contract_no}/review-result")
def save_review_result(contract_no: str, payload: ReviewResultIn,
                       user: User = Depends(require_login),
                       session: Session = Depends(get_session)):
    """保存 AI-02 合同审阅的结论快照。

    只存**结论摘要**（评分、一句话总结、风险条数、是否走了大模型），
    不存完整的风险明细——明细每次审阅都能重新算出来，存下来只会在
    规则升级后变成一份过期的旧结论。存摘要是为了让合同列表能直接显示
    「已审阅 · 92 分」，不必为了一个数字把每份合同都重跑一遍。

    只有合同的当事人（承租方或出租方）与管理员能写。
    """
    contract = _visible_contract(session, contract_no, user)
    contract.review_result = json.dumps({
        "score": payload.score,
        "summary": payload.summary,
        "risk_count": payload.risk_count,
        "source": payload.source,
        "reviewed_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }, ensure_ascii=False)
    session.commit()
    return {"ok": True, "review_result": json.loads(contract.review_result)}


# ------------------------------------------------------------------ 账单

@app.get("/api/contracts/{contract_no}/detail")
def contract_detail(contract_no: str, user: User = Depends(require_login),
                    session: Session = Depends(get_session)):
    """一份合同的完整档案：账单、报修、状态时间线。

    做成一个接口而不是让前端拉三次：这三块在界面上是同时出现的，
    分三次请求既慢，也会出现「账单加载好了、报修还在转」的割裂感。

    时间线不需要新表——signed_at / ended_at / created_at 拼得出来。
    """
    contract = session.scalar(
        select(Contract).where(Contract.contract_no == contract_no))
    if not contract:
        raise HTTPException(status_code=404, detail="合同不存在")
    if user.role != "admin" and user.id not in (contract.tenant_id,
                                                contract.landlord_id):
        raise HTTPException(status_code=403, detail="只能查看自己的合同")

    bills = sorted(contract.bills, key=lambda b: (b.due_date, b.id))
    paid = [b for b in bills if b.status == "paid"]
    unpaid = [b for b in bills if b.status == "unpaid"]

    # 报修按房源查，但只取这份合同存续期间的——这套房在别人租的时候报过
    # 的修，与本合同无关，摆进来只会让人以为是自己惹的。
    #
    # 起点取**合同创建时间**而不是起租日：签约与起租之间常有几天间隔，
    # 这期间租客已经拿到钥匙看房验房，报的修属于这份合同，
    # 按起租日算会把它们漏掉。
    repairs = session.scalars(select(Repair).where(
        Repair.house_id == contract.house_id,
        Repair.created_at >= contract.created_at,
    ).order_by(Repair.created_at.desc())).all()
    if contract.ended_at:
        repairs = [r for r in repairs if r.created_at <= contract.ended_at]

    timeline = [{"at": contract.created_at.strftime("%Y-%m-%d %H:%M"),
                 "title": "合同创建", "detail": "由房东发起，等待租客确认"}]
    if contract.signed_at:
        timeline.append({"at": contract.signed_at.strftime("%Y-%m-%d %H:%M"),
                         "title": "租客确认，合同生效",
                         "detail": "已排出整个租期的账单"})
    for bill in paid:
        if bill.paid_at:
            timeline.append({
                "at": bill.paid_at.strftime("%Y-%m-%d %H:%M"),
                "title": "缴纳%s" % ("押金" if bill.type == "deposit" else "租金"),
                "detail": "%s · %d 元" % (bill.bill_no, round(float(bill.amount)))})
    if contract.ended_at:
        # 原先这里写「退还押金 X 元」，押金一分没交过也照写——和退租通知里的
        # 「押金未曾缴纳，无需退款」对不上。退了什么，以退款流水为准，逐笔列出
        timeline.append({
            "at": contract.ended_at.strftime("%Y-%m-%d %H:%M"),
            "title": "合同结束",
            "detail": contract.end_reason or "退租"})
    for bill in bills:
        for refund in bill.refunds:
            timeline.append({
                "at": refund.refunded_at.strftime("%Y-%m-%d %H:%M"),
                "title": "押金退款" if refund.type == "deposit" else "退回预付租金",
                "detail": "%s · %s 元%s" % (
                    refund.refund_no, leasing._money(refund.refund_amount),
                    "（扣款 %s 元）" % leasing._money(refund.deduct_amount)
                    if refund.type == "deposit" and float(refund.deduct_amount or 0) else "")})
    timeline.sort(key=lambda item: item["at"])

    return {
        "contract": contract.to_dict(),
        "bills": [b.to_dict() for b in bills],
        "summary": {
            "total_periods": len(bills),
            "paid_count": len(paid),
            "unpaid_count": len(unpaid),
            "paid_amount": round(sum(float(b.amount) for b in paid), 2),
            "refunded_amount": round(sum(b.refunded_amount for b in bills), 2),
            "unpaid_amount": round(sum(float(b.amount) for b in unpaid), 2),
            "overdue_count": sum(1 for b in unpaid if b.overdue),
        },
        "repairs": [r.to_dict() for r in repairs],
        "timeline": timeline,
    }


@app.get("/api/bills/mine")
def my_bills(status: str = "", contract_no: str = "",
             side: Literal["", "tenant", "landlord"] = "",
             user: User = Depends(require_login),
             session: Session = Depends(get_session)):
    """我的账单。租客看自己要交的，房东看自己该收的。

    `contract_no` 用于合同详情页——只看这一份合同排了哪些账单。
    它**不放宽可见范围**：下面按角色算出来的 contract_ids 仍然生效，
    传别人的合同号只会得到空列表。
    """
    if user.role == "admin":
        stmt = select(Bill)
    else:
        # 和合同一样按「我是不是其中一方」取，不按角色二选一（K-13）
        contract_ids = [row[0] for row in session.execute(
            select(Contract.id).where(
                or_(Contract.tenant_id == user.id,
                    Contract.landlord_id == user.id))).all()]
        stmt = select(Bill).where(Bill.contract_id.in_(contract_ids or [-1]))
    # D3：同 contracts/mine。**我要交的和我该收的是两份清单。**
    # 不分开的后果实测过：房东问「我现在该处理哪些事」，助手说
    # 「你还有 13 笔待缴账单共 39650 元」——那是租客欠他的钱；
    # 他要是接着说「帮我交房租」，确认卡片上就是替租客付那一笔。
    if side:
        column = Contract.tenant_id if side == "tenant" else Contract.landlord_id
        mine = [row[0] for row in session.execute(
            select(Contract.id).where(column == user.id)).all()]
        stmt = stmt.where(Bill.contract_id.in_(mine or [-1]))
    if status:
        stmt = stmt.where(Bill.status == status)
    if contract_no:
        target = session.scalar(
            select(Contract).where(Contract.contract_no == contract_no))
        # 合同不存在时给一个不可能命中的条件，而不是忽略这个参数——
        # 忽略会把「查某份合同」变成「查全部」，是最坏的一种失败
        stmt = stmt.where(Bill.contract_id == (target.id if target else -1))

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
    """第一段：发起支付。只有承租方与管理员能付。

    **这个接口不再代表「已付款」。** 它创建一张 pending 的支付单并返回
    收银台地址，账单仍然是 unpaid——钱要等网关回调
    （`POST /api/payments/notify`）确认之后才算收到。

    改造前它是一步到位的：进来就把账单置 paid。那种写法在本地跑得通，
    但它假设「发起支付 == 收到钱」，而真实链路里用户可能跳到收银台就走了、
    可能付失败、也可能回调晚到几秒——这三种情况旧写法全都记成已付款。
    """
    bill = session.scalar(select(Bill).where(Bill.bill_no == bill_no))
    if not bill:
        raise HTTPException(status_code=404, detail="账单不存在")

    contract = bill.contract
    if user.role != "admin" and (not contract or user.id != contract.tenant_id):
        raise HTTPException(status_code=403, detail="只有承租方可以缴费")

    # 顺手清掉过期的挂单，否则用户会被自己十分钟前那张没付的单堵住
    leasing.sweep_expired_payments(session)

    if payload.idempotency_key:
        previous = session.scalar(select(Payment).where(
            Payment.idempotency_key == payload.idempotency_key))
        if previous:
            if previous.bill_id != bill.id:
                raise HTTPException(status_code=409, detail="支付请求号已被其它账单使用")
            return {"ok": True, "bill": previous.bill.to_dict(),
                    "payment": previous.to_dict(), "idempotent": True}

    # 线下缴费与管理员代收走一步到位：钱已经通过别的渠道收到了，
    # 系统只需要如实记一笔，不该再让它去走一遍网关。
    offline = payload.method in ("offline", "cash") or user.role == "admin"

    try:
        if offline:
            payment = leasing.pay_bill(
                session, bill, user, method=payload.method,
                idempotency_key=payload.idempotency_key or None)
        else:
            payment = leasing.start_payment(
                session, bill, user, method=payload.method,
                idempotency_key=payload.idempotency_key or None)
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(status_code=409, detail="该账单正在支付或已经支付，请勿重复提交")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    session.refresh(bill)
    session.refresh(payment)
    result = {"ok": True, "bill": bill.to_dict(), "payment": payment.to_dict()}
    if payment.status == "pending":
        result["pay_url"] = "/#/pay/%s" % payment.trade_no
        result["next"] = "waiting_notify"
    return result


@app.post("/api/payments/notify")
def payment_notify(payload: dict, session: Session = Depends(get_session)):
    """第二段：网关异步回调。**账单是在这里才真正变成已付的。**

    这个接口有三个和其它接口都不一样的地方，每一个都是真实网关的要求：

    1. **没有登录态。** 调用方是网关服务器，不是浏览器，带不了用户令牌。
       身份靠**签名**验证——这也是为什么验签失败必须直接拒绝，
       它是这个接口唯一的门。
    2. **必须幂等。** 支付宝对同一笔交易最多重复推送 8 次（间隔递增），
       直到平台返回成功为止。处理两次就多记一笔收入。
    3. **要回一个网关认识的响应。** 真实网关看不懂 HTTP 200 里的 JSON，
       支付宝认的是纯文本 `success`；这里保留 JSON，但把
       `code` 字段留成同样的语义位置。

    金额也要核对：**不能信回调报文里的钱数就直接入账**，
    要和平台自己的单子对上——回调是可以被伪造的，签名挡住了伪造，
    金额核对挡住的是网关侧的错单。
    """
    if not gateway.verify(payload):
        # 验签失败不解释原因。回调接口暴露在公网上，
        # 告诉对方「签名错了」还是「单号不存在」等于帮着试探。
        raise HTTPException(status_code=400, detail="签名校验失败")

    trade_no = payload.get("trade_no", "")
    payment = session.scalar(select(Payment).where(Payment.trade_no == trade_no))
    if not payment:
        raise HTTPException(status_code=404, detail="支付单不存在")

    try:
        if abs(float(payload.get("amount", 0)) - float(payment.amount)) > 0.01:
            raise HTTPException(status_code=409, detail="回调金额与支付单不符")
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="回调金额格式不正确")

    try:
        payment, changed = leasing.confirm_payment(
            session, payment, payload, payload.get("result", "success"))
        session.commit()
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc))

    # changed=False 表示这是一次重复推送，账没有再动过。
    # 仍然返回成功——**返回失败会让网关继续重推**。
    return {"code": "success", "trade_no": payment.trade_no,
            "status": payment.status, "duplicated": not changed}


@app.post("/api/payments/{trade_no}/mock-pay")
def mock_pay(trade_no: str, result: str = "success",
             user: User = Depends(require_login),
             session: Session = Depends(get_session)):
    """假网关的「我付好了」按钮——**替代真实收银台的那一步**。

    真实链路里用户是跳到支付宝页面完成支付，然后支付宝回调平台。
    我们拿不到支付宝，所以由这个接口代替用户点下「确认付款」，
    再由它去构造一份**带正确签名的回调报文**打给上面那个 notify 接口。

    重点是：**回调链路本身是真的**，只有触发回调的人换成了我们自己。
    将来接上真网关，删掉这个接口即可，notify 一行不用改。
    """
    payment = session.scalar(select(Payment).where(Payment.trade_no == trade_no))
    if not payment:
        raise HTTPException(status_code=404, detail="支付单不存在")
    contract = payment.bill.contract
    if user.role != "admin" and (not contract or user.id != contract.tenant_id):
        raise HTTPException(status_code=403, detail="无权操作该支付单")
    if payment.status != "pending":
        raise HTTPException(status_code=409, detail="该支付单不在等待付款状态")

    notice = gateway.get_gateway().build_notify(
        payment.trade_no, payment.gateway_trade_no, payment.amount, result)
    return payment_notify(notice, session)


@app.get("/api/payments/{trade_no}/status")
def payment_status(trade_no: str, user: User = Depends(require_login),
                   session: Session = Depends(get_session)):
    """第三段：前端轮询确认到账。

    还带一条**主动查询**的兜底：回调是会丢的（网络抖动、平台正好在重启），
    只等回调的系统一定会有付了钱但账单没变的用户。所以单子还挂着 pending
    时，反过来问一次网关。
    """
    payment = session.scalar(select(Payment).where(Payment.trade_no == trade_no))
    if not payment:
        raise HTTPException(status_code=404, detail="支付单不存在")
    contract = payment.bill.contract
    allowed = (user.role == "admin" or
               (contract and user.id in (contract.tenant_id, contract.landlord_id)))
    if not allowed:
        raise HTTPException(status_code=403, detail="无权查看该支付单")

    if payment.status == "pending":
        if payment.expire_at and payment.expire_at < datetime.now():
            leasing.close_payment(session, payment, "超时未支付，自动关单")
            session.commit()
        else:
            remote = gateway.get_gateway().query(payment.gateway_trade_no or "")
            if remote.get("status") == "succeeded":
                # 网关说付了，我们这边还是 pending —— 回调丢了，就地补上
                notice = gateway.get_gateway().build_notify(
                    payment.trade_no, payment.gateway_trade_no, payment.amount)
                leasing.confirm_payment(session, payment, notice, "success")
                session.commit()

    session.refresh(payment)
    return {"ok": True, "payment": payment.to_dict(),
            "bill": payment.bill.to_dict()}


@app.get("/api/payments/{trade_no}")
def payment_receipt(trade_no: str, user: User = Depends(require_login),
                    session: Session = Depends(get_session)):
    payment = session.scalar(select(Payment).where(Payment.trade_no == trade_no))
    if not payment:
        raise HTTPException(status_code=404, detail="缴费流水不存在")
    contract = payment.bill.contract
    allowed = (user.role == "admin" or
               (contract and user.id in (contract.tenant_id, contract.landlord_id)))
    if not allowed:
        raise HTTPException(status_code=403, detail="无权查看该缴费凭证")
    return {"payment": payment.to_dict(), "bill": payment.bill.to_dict()}


@app.get("/api/refunds/mine")
def my_refunds(user: User = Depends(require_login),
               session: Session = Depends(get_session)):
    stmt = select(Refund).join(Contract, Refund.contract_id == Contract.id)
    if user.role != "admin":
        # 同上（K-13）：按「我是不是其中一方」，不按角色二选一
        stmt = stmt.where(or_(Contract.tenant_id == user.id,
                              Contract.landlord_id == user.id))
    rows = session.scalars(stmt.order_by(Refund.refunded_at.desc())).all()
    return {"total": len(rows), "list": [row.to_dict() for row in rows]}


@app.get("/api/contracts/{contract_no}/settlement")
def contract_settlement(contract_no: str, deduct: float = 0,
                        user: User = Depends(require_login),
                        session: Session = Depends(get_session)):
    """退租结算预览：**如果今天退租**，押金退多少、预付租金退多少、哪些账单作废、还欠多少。

    和真正退租用的是同一份计算（leasing.plan_settlement），只是不落库——
    弹窗里显示的数和点了确认之后的数不会对不上。
    租客在看房东的提前退租申请时，扣款按申请里写明的那个数算。
    """
    contract = _visible_contract(session, contract_no, user)
    if contract.status != "active":
        raise HTTPException(status_code=409, detail="只有生效中的合同可以结算")
    if (_contract_side(contract, user) == "tenant" and contract.request_type == "end"
            and contract.request_by == "landlord"):
        deduct = float(contract.request_deduct or 0)
    try:
        plan = leasing.plan_settlement(contract, deduct)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"ok": True, "settlement": leasing.settlement_summary(plan)}


@app.get("/api/money/records")
def money_records(side: Literal["", "tenant", "landlord"] = "",
                  user: User = Depends(require_login),
                  session: Session = Depends(get_session)):
    """资金流水：**真正发生过的每一笔钱**——缴费成功的，和退回去的。

    原先缴费凭证只能一张张点开账单看，退款记录有接口（/api/refunds/mine）
    却没有任何页面用它，退了多少钱、哪天退的，租客房东都看不到。
    这里按时间合成一张流水；`direction` 从当前这一方的角度写：
    租客交钱是 out、收到退款是 in；房东正好相反。管理员看全平台，不分方向。

    失败、超时关单的支付尝试不在这里（没有发生资金流动），挂在各自的账单上（attempts）。
    """
    contracts = select(Contract.id)
    if user.role != "admin" or side:
        if side == "tenant":
            contracts = contracts.where(Contract.tenant_id == user.id)
        elif side == "landlord":
            contracts = contracts.where(Contract.landlord_id == user.id)
        else:
            contracts = contracts.where(or_(Contract.tenant_id == user.id,
                                            Contract.landlord_id == user.id))
    contract_ids = [row[0] for row in session.execute(contracts).all()]
    bills = session.scalars(select(Bill).where(
        Bill.contract_id.in_(contract_ids or [-1]))).all()
    by_id = {c.id: c for c in session.scalars(select(Contract).where(
        Contract.id.in_(contract_ids or [-1]))).all()}

    def viewer_is_tenant(contract):
        if side:
            return side == "tenant"
        return user.role != "admin" and contract.tenant_id == user.id

    def direction(contract, money_to_landlord):
        if user.role == "admin" and not side:
            return ""
        return ("out" if money_to_landlord else "in") if viewer_is_tenant(contract) \
            else ("in" if money_to_landlord else "out")

    rows = []
    for bill in bills:
        contract = by_id.get(bill.contract_id)
        house_no = contract.house.house_no if contract and contract.house else ""
        what = "押金" if bill.type == "deposit" else "租金"
        for payment in bill.payments:
            if payment.status != "succeeded" or not payment.paid_at:
                continue
            rows.append({
                "kind": "payment", "no": payment.trade_no, "title": "缴纳%s" % what,
                "detail": bill.period or bill.remark, "amount": float(payment.amount),
                "direction": direction(contract, True), "method": payment.method,
                "at": payment.paid_at.strftime("%Y-%m-%d %H:%M"),
                "bill_no": bill.bill_no, "house_no": house_no,
                "contract_no": contract.contract_no if contract else ""})
        for refund in bill.refunds:
            if refund.status != "succeeded":
                continue
            rows.append({
                "kind": "refund", "no": refund.refund_no,
                "title": "押金退款" if refund.type == "deposit" else "退回预付租金",
                "detail": refund.reason, "amount": float(refund.refund_amount),
                "direction": direction(contract, False), "method": "",
                "at": refund.refunded_at.strftime("%Y-%m-%d %H:%M"),
                "bill_no": bill.bill_no, "house_no": house_no,
                "contract_no": contract.contract_no if contract else ""})
    rows.sort(key=lambda r: r["at"], reverse=True)
    total_in = sum(r["amount"] for r in rows if r["direction"] == "in")
    total_out = sum(r["amount"] for r in rows if r["direction"] == "out")
    return {"total": len(rows), "list": rows[:300],
            "total_in": round(total_in, 2), "total_out": round(total_out, 2)}


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
    notice_fields = {"标题": payload.title, "正文": payload.content}
    verdict = moderate(notice_fields, scene="notice")

    notice = Notice(title=payload.title, content=payload.content,
                    summary=payload.summary or payload.content[:80],
                    category=payload.category, publisher_id=user.id)
    session.add(notice)
    session.flush()
    record_flag(session, verdict, notice_fields, "notice", user,
                notice.id, notice.title[:60])
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


@app.get("/api/pricing/suggest")
def pricing_suggest(district: str, layout: str = "", area: float = 0,
                    user: User = Depends(require_house_manager),
                    session: Session = Depends(get_session)):
    """定价建议（L-10）。

    只对房东与管理员开放：这是发布房源时的辅助，对租客没有意义，
    反而会暴露平台的定价口径。

    比较基准只取**在租与可租**的房源。待审核的还没被认可，已下架的
    往往正是因为定价不合理——拿它们当参照就是在拿反面教材做标准。
    """
    rows = session.scalars(select(House).where(
        House.status.in_(["available", "rented"]))).all()
    houses = [{"district": h.district, "layout": h.layout,
               "rent_price": float(h.rent_price or 0),
               "area": float(h.area or 0)} for h in rows]
    return pricing.suggest(houses, district, layout, area)


# ------------------------------------------------------------------ 内容审核

def moderate(fields, scene):
    """审核一段用户内容，命中拦截规则直接抛 400。

    先审再落库：先存后删的话，中间那段时间违规内容对所有人可见。

    只做判断、不写库——写标记必须等实体建出来拿到主键之后，
    否则标记指向的 target_id 是空的，管理员点「下架」就无处可下。
    """
    verdict = moderation.check_all(fields, scene=scene)
    if verdict["level"] == moderation.LEVEL_BLOCK:
        raise HTTPException(status_code=400,
                            detail="内容未通过审核。" + " ".join(verdict["reasons"]))
    return verdict


def record_flag(session, verdict, fields, scene, user, target_id, target_key=""):
    """把「需人工复核」的判定落成一条待办。实体建好之后再调。"""
    if verdict["level"] != moderation.LEVEL_FLAG:
        return None
    excerpt = " / ".join(str(v) for v in fields.values() if v)
    flag = ContentFlag(
        scene=scene, target_id=target_id, target_key=target_key,
        author_id=user.id if user else None,
        author_name=(user.real_name or user.username) if user else "",
        excerpt=excerpt[:300],
        categories=",".join(verdict["categories"]),
        reasons="\n".join(verdict["reasons"]))
    session.add(flag)
    return flag


@app.get("/api/admin/flags")
def list_flags(status: str = "pending", limit: int = Query(50, ge=1, le=200),
               user: User = Depends(require_login),
               session: Session = Depends(get_session)):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="只有管理员可以查看内容审核")
    query = select(ContentFlag)
    if status:
        query = query.where(ContentFlag.status == status)
    rows = session.scalars(
        query.order_by(ContentFlag.created_at.desc()).limit(limit)).all()
    pending = len(session.scalars(select(ContentFlag).where(
        ContentFlag.status == "pending")).all())
    return {"total": len(rows), "pending": pending,
            "list": [row.to_dict() for row in rows]}


@app.patch("/api/admin/flags/{flag_id}")
def handle_flag(flag_id: int, action: str,
                user: User = Depends(require_login),
                session: Session = Depends(get_session)):
    """处理一条待复核内容：approve 保留，remove 下架。

    下架的动作按场景不同：房源转为已下架、公告转为未发布、评价直接删除。
    评价没有状态字段，删掉是最直接的做法；flag 里留着摘要与处理人，
    事后仍查得到发生过什么。
    """
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="只有管理员可以处理内容审核")
    if action not in ("approve", "remove"):
        raise HTTPException(status_code=400, detail="action 只能是 approve 或 remove")

    flag = session.get(ContentFlag, flag_id)
    if not flag:
        raise HTTPException(status_code=404, detail="记录不存在")
    if flag.status != "pending":
        raise HTTPException(status_code=409, detail="该记录已处理过")

    if action == "remove":
        if flag.scene == "house":
            house = session.get(House, flag.target_id)
            if house:
                # 审核记录一并作废：不然房东点一下「上架」就原样回来了。
                # 出租中的不动状态——合同还在走，租约结束时会因为没有审核记录回到待审
                house.approved_at = None
                if house.status == "rented":
                    tail = "租约结束后需要修改内容、重新审核才能再上架。"
                else:
                    house.status = "offline"
                    leasing.close_open_viewings(session, house, "房源内容不合规，已被平台下架")
                    tail = "修改相关内容后可以重新提交审核。"
                # 原先房子一声不响就不见了，房东不知道是哪句话出的问题
                leasing.notify(session, house.landlord_id, "system", "房源内容未通过复核",
                               "房源 %s 的「%s」未通过平台复核，已下架。%s"
                               % (house.house_no, flag.excerpt[:40], tail), house.house_no)
        elif flag.scene == "notice":
            notice = session.get(Notice, flag.target_id)
            if notice:
                # 原先写的是 notice.published——模型上没有这一列，赋值不报错也不落库，
                # 「下架」点完公告照常挂在首页（2026-10-22）
                notice.is_published = False
        elif flag.scene == "review":
            review = session.get(Review, flag.target_id)
            if review:
                # 软删除而非真删（C-5）。审核会误判，误判之后要能恢复；
                # 用户申诉时也得拿得出原文。前台查询已经过滤掉了。
                review.deleted_at = datetime.now()
                reviewed = session.get(House, review.house_id)
                house_no = reviewed.house_no if reviewed else ""
                leasing.notify(session, review.tenant_id, "system", "评价未通过复核",
                               "你对房源 %s 的评价未通过平台复核，已不再公开展示。" % house_no,
                               house_no)

    flag.status = "approved" if action == "approve" else "removed"
    flag.handled_by = user.real_name or user.username
    flag.handled_at = datetime.now()
    session.commit()
    return {"ok": True, "flag": flag.to_dict()}


# ------------------------------------------------------------------ 图片上传
#
# python-multipart 不是本项目原有的依赖，组员拉下新代码却没重装依赖时，
# 带 File(...) 的路由会在**注册阶段**就抛异常，整个后端都起不来。
# 所以这里做成条件注册：缺依赖时退化成一个说明清楚的 503，
# 其余功能照常可用（对应非功能需求 N-07 的降级要求）。

try:
    from fastapi import File, UploadFile
    import multipart  # noqa: F401  仅用于探测依赖是否就位

    HAS_UPLOAD = True
except ImportError:
    HAS_UPLOAD = False


def _house_or_404(session, house_no):
    house = session.scalar(select(House).where(House.house_no == house_no))
    if not house:
        raise HTTPException(status_code=404, detail="房源不存在")
    return house


def _require_house_owner(session, user, house):
    """保留这个名字是为了少改调用点，实际判断在 app/permissions。

    改造前它写死了「admin 或 landlord_id 相等」，中介一律 403。
    现在受托中介也能过——但**只对他受托的那几套**（J-02）。
    """
    require_can_manage_house(session, user, house)


if HAS_UPLOAD:

    @app.post("/api/upload")
    async def upload_image(file: UploadFile = File(...),
                           user: User = Depends(require_login)):
        """上传一张图片，返回可直接引用的 URL。

        必须登录才能传——匿名上传等于把服务器磁盘开放给全网。
        类型判断只看文件头，不看扩展名，理由见 app/uploads.py。
        """
        data = await file.read()
        ok, result = uploads.validate(data)
        if not ok:
            raise HTTPException(status_code=400, detail=result)
        # 尺寸、体积与内容哈希在这里算一次就存下来（K-12）。
        # 每次要判断「这张图能不能当封面」都去读一遍文件是不必要的，
        # 图多了会把列表接口拖垮；哈希则是查盗图与重复图的唯一抓手
        meta = uploads.measure(data)
        return {"ok": True, "url": uploads.save(STATIC_DIR, data, result),
                **meta}

else:

    @app.post("/api/upload")
    def upload_unavailable():
        raise HTTPException(
            status_code=503,
            detail="图片上传需要 python-multipart，请执行 "
                   "pip install -r requirements.txt 后重启后端")


@app.post("/api/houses/{house_no}/images")
def add_house_image(house_no: str, url: str = Body(embed=True),
                    caption: str = Body(default="", embed=True),
                    width: int = Body(default=0, embed=True),
                    height: int = Body(default=0, embed=True),
                    file_size: int = Body(default=0, embed=True),
                    hash: str = Body(default="", embed=True),
                    user: User = Depends(require_house_manager),
                    session: Session = Depends(get_session)):
    """把已上传的图片挂到房源上。

    只接受本系统上传出来的地址。放开成任意 URL 的话，房东可以把图片指向
    外部站点，那个站点就能拿到每个访客的 IP 与访问时间。
    """
    house = _house_or_404(session, house_no)
    _require_house_owner(session, user, house)
    if not uploads.is_managed(url):
        raise HTTPException(status_code=400, detail="只能使用本系统上传的图片")
    if session.scalar(select(HouseImage.id).where(HouseImage.url == url).limit(1)):
        # 别的房源页上的图片地址是公开的。原先照样能挂到自己房源上，再从自己这边
        # 删掉——磁盘上那一份就没了，人家房源上的图跟着裂（2026-10-22）
        raise HTTPException(status_code=409, detail="这张图已经用在房源相册里了，请重新上传")

    has_cover = session.scalar(select(HouseImage).where(
        HouseImage.house_id == house.id, HouseImage.is_cover.is_(True)))
    image = HouseImage(house_id=house.id, url=url, caption=caption[:100],
                       sort_order=len(house.images or []),
                       # 第一张自动做封面，免得房东传完发现列表页还是空图
                       is_cover=not has_cover,
                       # 元数据由 /api/upload 算好后原样带过来（K-12）。
                       # 带不过来也不拦——老前端不传这几个字段，
                       # 拦下来等于让一次前端没升级把发布流程整个卡死
                       width=max(int(width or 0), 0),
                       height=max(int(height or 0), 0),
                       file_size=max(int(file_size or 0), 0),
                       hash=(hash or "")[:64])
    session.add(image)
    session.commit()
    session.refresh(image)
    return {"ok": True, "image": {"id": image.id, "url": image.url,
                                  "caption": image.caption,
                                  "is_cover": image.is_cover}}


@app.delete("/api/houses/{house_no}/images/{image_id}")
def delete_house_image(house_no: str, image_id: int,
                       user: User = Depends(require_house_manager),
                       session: Session = Depends(get_session)):
    house = _house_or_404(session, house_no)
    _require_house_owner(session, user, house)
    image = session.get(HouseImage, image_id)
    if not image or image.house_id != house.id:
        raise HTTPException(status_code=404, detail="图片不存在")

    was_cover = image.is_cover
    url = image.url
    session.delete(image)
    session.flush()

    if was_cover:
        # 封面被删掉就顺位提一张，否则列表页会留一个空图位
        nxt = session.scalar(select(HouseImage)
                             .where(HouseImage.house_id == house.id)
                             .order_by(HouseImage.sort_order))
        if nxt:
            nxt.is_cover = True
    session.commit()
    if not _upload_still_used(session, url):
        uploads.remove(STATIC_DIR, url)
    return {"ok": True}


def _upload_still_used(session, url):
    """这张上传的图还有没有别处在用：相册、报修照片、评价配图、举报证据、中介资质。

    删一条相册记录不等于能删磁盘上的文件——同一个地址可能是从别处挂过来的。
    """
    if session.scalar(select(HouseImage.id).where(HouseImage.url == url).limit(1)):
        return True
    if session.scalar(select(RepairImage.id).where(RepairImage.url == url).limit(1)):
        return True
    for model, column in ((Review, Review.images), (HouseReport, HouseReport.evidence),
                          (AgentProfile, AgentProfile.license_images)):
        if session.scalar(select(model.id).where(column.contains(url)).limit(1)):
            return True
    return False


@app.post("/api/houses/{house_no}/images/{image_id}/cover")
def set_cover(house_no: str, image_id: int,
              user: User = Depends(require_house_manager),
              session: Session = Depends(get_session)):
    house = _house_or_404(session, house_no)
    _require_house_owner(session, user, house)
    image = session.get(HouseImage, image_id)
    if not image or image.house_id != house.id:
        raise HTTPException(status_code=404, detail="图片不存在")
    for other in session.scalars(select(HouseImage).where(
            HouseImage.house_id == house.id)).all():
        other.is_cover = other.id == image.id
    session.commit()
    return {"ok": True}


@app.post("/api/houses/{house_no}/images/{image_id}/caption")
def set_image_caption(house_no: str, image_id: int,
                      caption: str = Body(default="", embed=True),
                      user: User = Depends(require_house_manager),
                      session: Session = Depends(get_session)):
    """改一张房源图片的说明文字。

    这一列原先**324 条全是空的**：上传接口一直收 `caption`，
    前端却永远传空串，没有任何入口能填它。少了它，相册的
    `<img alt>` 也一直是空的——读屏软件念到那一段只有一串「图片」。

    智能体那边可以看图写一句草稿（`POST /agent/image-caption`），
    但**草稿必须由房东看过再落到这里**。图片说明是挂在房源上给
    租客看的公开文案，和标题、描述一个性质。

    所以这条接口和标题、描述走**同一道内容审核**——
    不能因为它是「一句小字」就绕过去：私下联系方式塞在图片说明里，
    和塞在描述里是一模一样的事。
    """
    house = _house_or_404(session, house_no)
    _require_house_owner(session, user, house)
    image = session.get(HouseImage, image_id)
    if not image or image.house_id != house.id:
        raise HTTPException(status_code=404, detail="图片不存在")

    text = (caption or "").strip()[:100]
    fields = {"图片说明": text}
    # 先审再落库。命中拦截规则时 `moderate` 直接抛 400，
    # 下面这两行根本不会执行——顺序不能反
    verdict = moderate(fields, scene="house") if text else None
    image.caption = text
    if verdict is not None:
        record_flag(session, verdict, fields, "house", user,
                    house.id, house.house_no)
    session.commit()
    return {"ok": True, "caption": image.caption}


@app.post("/api/repairs/{repair_id}/images")
def add_repair_image(repair_id: int, url: str = Body(embed=True),
                     user: User = Depends(require_login),
                     session: Session = Depends(get_session)):
    """给报修单补一张照片。

    「哪里坏了」用文字往往说不清，一张照片能省掉一轮来回沟通。
    只有报修人本人、该房源的房东或管理员能加。
    """
    repair = session.get(Repair, repair_id)
    if not repair:
        raise HTTPException(status_code=404, detail="工单不存在")
    allowed = (user.role == "admin"
               or repair.tenant_id == user.id
               or (repair.house and repair.house.landlord_id == user.id))
    if not allowed:
        raise HTTPException(status_code=403, detail="无权操作该工单")
    if not uploads.is_managed(url):
        raise HTTPException(status_code=400, detail="只能使用本系统上传的图片")

    image = RepairImage(repair_id=repair.id, url=url)
    session.add(image)
    session.commit()
    return {"ok": True, "id": image.id, "url": image.url}


@app.get("/api/repairs/{repair_id}/images")
def list_repair_images(repair_id: int, user: User = Depends(require_login),
                       session: Session = Depends(get_session)):
    repair = session.get(Repair, repair_id)
    if not repair:
        raise HTTPException(status_code=404, detail="工单不存在")
    allowed = (user.role == "admin"
               or repair.tenant_id == user.id
               or (repair.house and repair.house.landlord_id == user.id))
    if not allowed:
        raise HTTPException(status_code=403, detail="无权查看该工单")
    rows = session.scalars(select(RepairImage)
                           .where(RepairImage.repair_id == repair.id)
                           .order_by(RepairImage.id)).all()
    return {"total": len(rows),
            "list": [{"id": r.id, "url": r.url} for r in rows]}


# ------------------------------------------------------------------ 系统配置

class ConfigUpdate(BaseModel):
    items: dict[str, str]


@app.get("/api/config")
def public_config(session: Session = Depends(get_session)):
    """公开配置。只返回标了 is_public 的项，未登录也能读。

    公开范围必须由数据本身标记，不能靠调用方传 key 来决定——
    否则前端换个 key 就能把预约上限、免审开关这些内部配置读走。
    """
    rows = config_store.list_all(session, public_only=True)
    return {row.key: row.value for row in rows}


@app.get("/api/admin/config")
def list_config(user: User = Depends(require_login),
                session: Session = Depends(get_session)):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="只有管理员可以查看系统配置")
    return {"list": [row.to_dict() for row in config_store.list_all(session)]}


@app.patch("/api/admin/config")
def update_config(payload: ConfigUpdate, user: User = Depends(require_login),
                  session: Session = Depends(get_session)):
    """批量更新配置。

    全部校验通过才写入。半套配置比旧配置更危险——比如看房时长改成功了、
    预约上限却因为写错被拒，管理员多半以为两项都生效了。
    """
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="只有管理员可以修改系统配置")

    cleaned = {}
    for key, value in payload.items.items():
        ok, result = config_store.validate(key, value)
        if not ok:
            raise HTTPException(status_code=400,
                                detail="%s：%s" % (key, result))
        cleaned[key] = result

    for key, value in cleaned.items():
        config_store.set_value(session, key, value)
    session.commit()
    return {"ok": True, "updated": len(cleaned),
            "list": [row.to_dict() for row in config_store.list_all(session)]}


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

    total = session.scalar(select(func.count()).select_from(query.subquery()))
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
# STATIC_DIR 的定义在文件靠前处——上传接口要用它，不能等到这里才定义。
os.makedirs(os.path.join(STATIC_DIR, uploads.UPLOAD_DIRNAME), exist_ok=True)
if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

class SpaFiles(StaticFiles):
    """带缓存策略的前端托管。

    **不加这一层会出一种很难查的问题：改了前端，用户看不到。**

    `StaticFiles` 只发 ETag 和 Last-Modified，不发 Cache-Control。
    浏览器对这种响应会启用「启发式缓存」——按 Last-Modified 距今时间的
    十分之一估一个新鲜期，于是 `index.html` 在几小时内根本不回源问一句。
    而 Vite 打出来的 JS/CSS 文件名带哈希，**新文件名只写在 index.html 里**，
    index.html 不更新，新代码就永远到不了浏览器。

    实测就撞上了：重新构建之后，页面加载的仍是上一版的
    `index-b7560a6a.js`，界面上的新功能一个都看不见，
    看上去像是「代码没生效」，其实是浏览器压根没去拿新的。

    所以分成两类：

    - `index.html`：`no-cache`。不是不缓存，是**每次都回来问一句**，
      没变就回 304，代价只有一个来回的头。
    - `/assets/*`：文件名带内容哈希，内容一变文件名就变，
      可以放心地长期缓存。
    """

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)

        # **别按 path 的字符串前缀去判。** StaticFiles 交进来的 path 已经过
        # `os.path.normpath`：Windows 上是 `assets\index-xxx.js` 这种反斜杠形式，
        # 而根路径「/」会被规范成一个孤零零的「.」。第一版就是这么写的，
        # 结果两条分支全落空，头一个都没发出去，看起来像没生效。
        #
        # 响应自己的 content-type 跨平台一致，用它判 HTML 最稳；
        # 资源目录改成按路径段匹配，正反斜杠都认。
        kind = response.headers.get("content-type", "")
        parts = (path or "").replace("\\", "/").split("/")

        if kind.startswith("text/html"):
            response.headers["Cache-Control"] = "no-cache"
        elif "assets" in parts:
            response.headers["Cache-Control"] = (
                "public, max-age=31536000, immutable")
        return response


if HAS_WEB:
    app.mount("/", SpaFiles(directory=WEB_DIST, html=True), name="web")
