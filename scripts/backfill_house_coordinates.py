"""为已有演示数据库补齐地图坐标。"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.map_service import locate_house  # noqa: E402
from app.models import House  # noqa: E402


def main():
    init_db()
    with SessionLocal() as session:
        houses = session.scalars(select(House)).all()
        updated = 0
        for house in houses:
            if house.lng is not None and house.lat is not None:
                continue
            if locate_house(house, prefer_geocode=False):
                updated += 1
        session.commit()
    print("已补齐 %d 套房源坐标。" % updated)


if __name__ == "__main__":
    main()
