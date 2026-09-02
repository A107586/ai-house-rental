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

from app.main import app  # noqa: E402
from app.models import Base, House, User  # noqa: E402
from app.security import hash_password  # noqa: E402


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
              description="有电梯近地铁", status="available"),
        House(house_no="DL0002", title="甘井子三室", landlord_id=landlord.id,
              district="甘井子区", community="泡崖", rent_price=4200, area=100,
              layout="三室一厅", rent_type="整租", tags="有阳台",
              status="available"),
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


def auth_header(client, username="tenant1", password="test123"):
    """登录并返回可直接用于请求的 Authorization 头。"""
    data = client.post("/api/auth/login",
                       json={"username": username, "password": password}).json()
    return {"Authorization": "Bearer " + data["token"]}
