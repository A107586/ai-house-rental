"""容器启动时：库是空的才生成演示数据。

`server/seed.py` 每次运行都会**先清空再重建**——直接写进启动命令的话，
每重启一次容器，用户注册的账号、提交的订单就全没了。所以先数一下房源：
有数据就什么都不做，没有才调 seed。
"""
import os
import sys

# 镜像里后端在 /app/server；本机想先试一遍时用 SERVER_DIR 指过去
SERVER_DIR = os.environ.get("SERVER_DIR", "/app/server")
sys.path.insert(0, SERVER_DIR)
os.chdir(SERVER_DIR)

from sqlalchemy import func, select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.models import House  # noqa: E402

init_db()
with SessionLocal() as session:
    count = session.scalar(select(func.count()).select_from(House)) or 0

if count:
    print("数据库里已有 %d 套房源，跳过演示数据。" % count, flush=True)
else:
    print("空数据库，正在生成演示数据……", flush=True)
    import seed  # noqa: E402
    seed.main()
