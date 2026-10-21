"""测试公共基础设施。

**所有测试模块都必须从这里取数据库与基类。**
早先让两个测试文件各自在模块顶层建引擎，结果第二个模块把
`db.engine` 覆盖成了另一个内存库，先导入的那批用例全部报表不存在。
共享状态只能有一处初始化。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 必须在导入 app.db 之前设置，否则会连到默认的 SQLite 文件
os.environ.setdefault("DATABASE_URL", "sqlite://")
# 地理编码要打高德的接口。测试**一律不许联网**——联网的测试会在
# 断网、限流、Key 过期时无缘无故变红，而它们本来测的不是地图服务
os.environ.setdefault("MAP_GEOCODE_ENABLED", "false")

import unittest  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import app.db as db  # noqa: E402

# 内存库必须用 StaticPool，否则每个连接都是一个全新的空库
engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                       poolclass=StaticPool, future=True)
db.engine = engine
db.SessionLocal = sessionmaker(bind=engine, autoflush=False, future=True)

from app import security  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base, House, User  # noqa: E402
from app.security import hash_password  # noqa: E402

# **测试里把密码哈希调轻。**
#
# 26 万轮 PBKDF2 是给线上防撞库的，一次哈希、一次校验各 0.09 秒。
# 而每个用例 setUp 要哈希两个种子账号，用例里还要登录、注册，
# 九百多个用例算下来全花在这上面：调轻前整套 410 秒，调轻后 55 秒，
# 跑一次测试要等的几乎全是它。
#
# 只改轮数，算法与存储格式不变：轮数写在哈希串里，校验按串里的轮数算，
# 所以「哈希—校验」这条链路在测试里走的仍是线上那一套代码。
security._PBKDF2_ROUNDS = 1000


def seed(session):
    """三个用户、三套房源，覆盖可租与已租两种状态。"""
    landlord = User(username="landlord1", password_hash=hash_password("test123"),
                    real_name="房东甲", phone="13800001234", role="landlord")
    session.add(landlord)
    session.add(User(username="tenant1", password_hash=hash_password("test123"),
                     real_name="租客甲", phone="13900005678", role="tenant"))
    session.flush()

    session.add_all([
        House(house_no="DL0001", title="中山区两室", landlord_id=landlord.id,
              district="中山区", community="青泥洼桥", rent_price=2800, area=70,
              layout="两室一厅", rent_type="整租", tags="电梯,地铁",
              description="有电梯近地铁", status="available",
              # 标签里写着「电梯」，elevator 列就必须是 True。
              # 两者不一致正是 K-04 要消灭的那种矛盾，装置里也不该留
              elevator=True,
              lng=121.6346, lat=38.9186, location_source="approximate"),
        House(house_no="DL0002", title="甘井子三室", landlord_id=landlord.id,
              district="甘井子区", community="泡崖", rent_price=4200, area=100,
              layout="三室一厅", rent_type="整租", tags="有阳台",
              status="available", lng=121.5550, lat=38.9840,
              location_source="approximate"),
        House(house_no="DL0003", title="已租房源", landlord_id=landlord.id,
              district="中山区", community="人民路", rent_price=3000, area=80,
              layout="两室一厅", rent_type="整租", status="rented"),
    ])
    session.commit()


class ApiTestCase(unittest.TestCase):
    """每个用例都从干净的库开始，互不影响。"""

    def setUp(self):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        session = db.SessionLocal()
        try:
            seed(session)
        finally:
            session.close()
        self.client = TestClient(app)


def make_admin(client, username="root", password="admin123"):
    """注册一个账号并直接改库提权，返回可用的鉴权头。

    注册接口不允许自封管理员，这是刻意的；测试要用管理员只能绕过接口。
    """
    client.post("/api/auth/register", json={
        "username": username, "password": password, "real_name": "管理员"})
    session = db.SessionLocal()
    try:
        from app.models import User as _User
        user = session.query(_User).filter_by(username=username).one()
        user.role = "admin"
        session.commit()
    finally:
        session.close()
    return auth_header(client, username, password)


def make_resident(username="tenant1", house_no="DL0001"):
    """让这个人成为这套房的**在住租客**：直接写一份生效中的合同。

    2026-10-22 起报修只收在住租客与管得了这套房的人（permissions.can_report_repair），
    原先大量用例拿匿名或随便一个租客去报 DL0001。只想「有一张工单」的用例
    先调这一行。**不排账单、不改房源状态**——要测签约链路的用例走真接口，
    这里只是让报修的前提成立，别的用例数到的合同和账单不受影响。
    """
    from datetime import datetime, timedelta
    from app.models import Contract, House, User
    session = db.SessionLocal()
    try:
        user = session.query(User).filter_by(username=username).one()
        house = session.query(House).filter_by(house_no=house_no).one()
        exists = session.query(Contract).filter_by(
            tenant_id=user.id, house_id=house.id, status="active").first()
        if not exists:
            now = datetime.now()
            session.add(Contract(
                contract_no="HTRES%d%d" % (user.id, house.id), house_id=house.id,
                tenant_id=user.id, landlord_id=house.landlord_id,
                start_date=now - timedelta(days=30), end_date=now + timedelta(days=335),
                rent=house.rent_price, deposit=0, pay_months=1, status="active",
                content="（测试用在住租约）"))
            session.commit()
    finally:
        session.close()


def end_lease(client, contract_no, landlord, tenant, reason="到期", deduct=0):
    """把一份生效中的合同**正常地**退掉：租客申请，房东确认并结算押金。

    2026-10-22 起房东不能再一点就退（提前退租要租客同意，见 main.end_contract），
    只想「有一份已结束的合同」来测结算、评价、时间线的用例走这一个函数。
    返回房东确认那一步的响应。
    """
    client.post("/api/contracts/%s/end" % contract_no, headers=tenant,
                json={"reason": reason})
    return client.post("/api/contracts/%s/end" % contract_no, headers=landlord,
                       json={"reason": reason, "deduct": deduct})


def auth_header(client, username="tenant1", password="test123"):
    """登录并返回可直接用于请求的 Authorization 头。"""
    data = client.post("/api/auth/login",
                       json={"username": username, "password": password}).json()
    return {"Authorization": "Bearer " + data["token"]}


def settle(client, bill_no, headers, method="alipay"):
    """把一张账单**真正缴清**：发起支付 + 模拟网关回调（D-1 三段式）。

    支付改造之后，`POST /api/bills/{no}/pay` 只创建一张 pending 的支付单，
    **账单仍然是 unpaid**——钱要等 `POST /api/payments/notify` 回调
    确认才算收到。这是对的：真实链路里用户可能跳到收银台就走了、
    可能付失败、也可能回调晚到几秒，旧的一步到位写法会把这三种情况
    全都记成已付款。

    但测试里大量用例只是想「让这张账单变成已付」，好接着测后面的
    退租结算、房东收入、合同时间线。让每个用例都自己拼一遍带签名的
    回调报文，既啰嗦又会把「支付怎么走」这个细节复制到十几处——
    将来回调报文改一个字段就要改十几个地方。

    所以在这里收口：**要测支付链路本身的用例直接调那两个接口，
    其余只想「把钱缴了」的用例调这个函数。**

    返回回调接口的响应。
    """
    from app import gateway

    started = client.post("/api/bills/%s/pay" % bill_no, headers=headers,
                          json={"method": method})
    body = started.json()
    payment = body.get("payment") or {}
    trade_no = payment.get("trade_no")
    if not trade_no:
        # 线下缴费与管理员代收是一步到位的，没有回调这一段
        return started

    payload = {"trade_no": trade_no,
               "amount": float(payment["amount"]),
               "result": "success"}
    payload["sign"] = gateway.sign(payload)
    return client.post("/api/payments/notify", json=payload)
