"""数据库连接。

本地开发默认用 SQLite——零安装、无密码、开箱即用，组员拉下代码就能跑。
部署到阿里云时改 `DATABASE_URL` 环境变量即可切到 MySQL，业务代码不用动：

    DATABASE_URL=mysql+pymysql://user:password@host:3306/rental?charset=utf8mb4
"""

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base

DEFAULT_SQLITE = "sqlite:///" + os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "rental.db")

DATABASE_URL = os.environ.get("DATABASE_URL") or DEFAULT_SQLITE

# SQLite 默认禁止跨线程共享连接，而 FastAPI 的同步接口跑在线程池里
_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=_connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, future=True)


def init_db():
    """建表。已存在的表不会被改动。"""
    Base.metadata.create_all(engine)


def get_session():
    """FastAPI 依赖：每个请求一个会话，用完即关。"""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
