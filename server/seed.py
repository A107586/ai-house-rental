"""生成演示数据。

原项目自带的种子数据是纯占位内容（房屋编号 1、面积 1、租价 1 元），
既没法验证筛选逻辑，也没法用于原型评审演示，因此重新生成一批
贴合大连实际的房源。

    python seed.py          # 建表并填充数据（已有数据会先清空）
"""

import io
import os
import random
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from app.db import SessionLocal, engine, init_db  # noqa: E402
from app import leasing  # noqa: E402
from app.models import (Appointment, Base, ContentFlag, House,  # noqa: E402
                        HouseImage, Notice, Repair, RepairItem,
                        RepairItemUsage, RepairRecord, RepairWorker, User)
from app.security import hash_password  # noqa: E402

# 大连各区及其代表性小区
DISTRICTS = {
    "中山区": ["青泥洼桥", "人民路", "桂林街", "五惠路", "海军广场"],
    "西岗区": ["八一路", "香炉礁", "五四广场", "北京街"],
    "沙河口区": ["星海广场", "黑石礁", "西安路", "马栏广场", "李家街"],
    "甘井子区": ["华南广场", "泡崖", "红旗", "革镇堡", "椒金山"],
    "旅顺口区": ["水师营", "龙王塘", "铁山"],
    "金州区": ["拥政街道", "光明街道", "friendship 友谊"],
}

LAYOUTS = [
    ("一室", 1, (28, 45), (1300, 2200)),
    ("一室一厅", 1, (40, 60), (1600, 2600)),
    ("两室一厅", 2, (55, 85), (2000, 3800)),
    ("两室两厅", 2, (75, 100), (2800, 4500)),
    ("三室一厅", 3, (85, 110), (3200, 5200)),
    ("三室两厅", 3, (100, 140), (4000, 7000)),
]

RENT_TYPES = ["整租", "整租", "整租", "合租", "公寓"]
ORIENTATIONS = ["朝南", "朝南", "南北通透", "朝东", "朝西"]
ALL_TAGS = ["电梯", "地铁", "近学校", "近医院", "有阳台", "可做饭",
            "有家具", "可养宠物", "采光好"]

DESC_TEMPLATES = [
    "{community}成熟社区，{layout}，{orientation}，{area}平米。{tag_text}房源干净整洁，随时可看房。",
    "位于{district}{community}，{layout}户型方正，{area}平米，{orientation}。{tag_text}周边生活配套齐全。",
    "{community}优质房源，{area}平米{layout}，{orientation}。{tag_text}业主诚心出租，价格可小谈。",
]

# 区域租金系数：市中心贵，远郊便宜
DISTRICT_PRICE_FACTOR = {
    "中山区": 1.15, "西岗区": 1.05, "沙河口区": 1.10,
    "甘井子区": 0.90, "旅顺口区": 0.72, "金州区": 0.80,
}

LANDLORD_NAMES = ["王建国", "李淑芬", "张伟", "刘敏", "陈海涛", "赵丽娟",
                  "孙志强", "周雅静"]
TENANT_NAMES = ["林小雨", "郭子豪", "何静怡", "马文轩"]


def _phone():
    return "1" + random.choice("3578") + "".join(random.choice("0123456789") for _ in range(9))


def build_house(index, landlord, district, layout_spec):
    """生成一套房源。

    区域与户型由调用方指定而非随机抽取——若全靠随机，60 套房撒在
    6 个区 × 6 种户型上必然出现空组合，演示时一句「中山区两室一厅」
    就会查不到任何结果。
    """
    community = random.choice(DISTRICTS[district])
    layout, rooms, area_range, price_range = layout_spec

    area = round(random.uniform(*area_range), 1)
    # 面积越大租价越高，避免生成「140 平米 1300 元」这种不可信数据
    ratio = (area - area_range[0]) / max(area_range[1] - area_range[0], 1)
    price = price_range[0] + (price_range[1] - price_range[0]) * ratio
    price = round(price * random.uniform(0.9, 1.1) / 50) * 50

    # 市中心贵、远郊便宜，让数据经得起追问
    price = round(price * DISTRICT_PRICE_FACTOR[district] / 50) * 50

    rent_type = random.choice(RENT_TYPES)
    if rent_type == "合租":
        price = round(price * 0.45 / 50) * 50

    tags = random.sample(ALL_TAGS, random.randint(2, 5))
    tag_text = "配备" + "、".join(tags) + "。" if tags else ""
    orientation = random.choice(ORIENTATIONS)

    description = random.choice(DESC_TEMPLATES).format(
        district=district, community=community, layout=layout,
        area=area, orientation=orientation, tag_text=tag_text)

    return House(
        house_no="DL%04d" % index,
        title="%s%s %s %s平" % (district, community, layout, int(area)),
        landlord_id=landlord.id,
        district=district,
        community=community,
        address="%s%s%s%d号楼%d单元" % (
            district, community, random.choice(["路", "街", "巷"]),
            random.randint(1, 88), random.randint(1, 6)),
        rent_price=price,
        deposit=price,
        area=area,
        layout=layout,
        rent_type=rent_type,
        floor="%d/%d层" % (random.randint(1, 18), random.randint(18, 33)),
        orientation=orientation,
        description=description,
        tags=",".join(tags),
        status=random.choice(["available"] * 9 + ["rented"]),
        view_count=random.randint(0, 500),
        published_at=datetime.now() - timedelta(days=random.randint(0, 90)),
    )


def main():
    random.seed(20260901)  # 固定随机种子，保证每个人生成的数据一致

    print("建表 ...")
    Base.metadata.drop_all(engine)
    init_db()

    session = SessionLocal()
    try:
        print("创建用户 ...")
        admin = User(username="admin", password_hash=hash_password("admin123"),
                     real_name="系统管理员", phone=_phone(), role="admin")
        session.add(admin)

        landlords = []
        for i, name in enumerate(LANDLORD_NAMES, start=1):
            user = User(username="landlord%d" % i,
                        password_hash=hash_password("test123"),
                        real_name=name, phone=_phone(), role="landlord")
            session.add(user)
            landlords.append(user)

        tenants = []
        for i, name in enumerate(TENANT_NAMES, start=1):
            user = User(username="tenant%d" % i,
                        password_hash=hash_password("test123"),
                        real_name=name, phone=_phone(), role="tenant")
            session.add(user)
            tenants.append(user)
        session.flush()

        print("生成房源 ...")
        # 每个区 × 每种户型至少 3 套，保证任意组合都能查到结果
        houses = []
        index = 1
        for district in DISTRICTS:
            for layout_spec in LAYOUTS:
                for _ in range(3):
                    houses.append(build_house(
                        index, random.choice(landlords), district, layout_spec))
                    index += 1
        session.add_all(houses)
        session.flush()

        print("绑定房源图片 ...")
        # static/houses/ 下的实拍图，按房源轮转分配。
        # 每套 3 张，首张为封面。用固定规则而不是随机，保证每个人跑出来一致。
        photos = sorted(
            f for f in os.listdir(os.path.join(os.path.dirname(
                os.path.abspath(__file__)), "static", "houses"))
            if f.endswith(".jpg"))
        if photos:
            for offset, house in enumerate(houses):
                for slot in range(3):
                    name = photos[(offset * 3 + slot) % len(photos)]
                    session.add(HouseImage(
                        house_id=house.id,
                        url="/static/houses/" + name,
                        sort_order=slot,
                        is_cover=(slot == 0)))
                house.cover_image = "/static/houses/" + photos[
                    (offset * 3) % len(photos)]
        session.flush()

        print("生成看房预约 ...")
        base = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
        used = set()
        for _ in range(15):
            house = random.choice(houses)
            moment = base + timedelta(days=random.randint(1, 6),
                                      hours=random.choice([0, 2, 4, 6]))
            if (house.id, moment) in used:
                continue
            used.add((house.id, moment))
            tenant = random.choice(tenants)
            session.add(Appointment(
                house_id=house.id, tenant_id=tenant.id,
                contact_name=tenant.real_name, contact_phone=tenant.phone,
                viewing_time=moment,
                status=random.choice(["pending", "confirmed"])))

        print("生成报修工单 ...")
        samples = [
            ("卫生间水管漏水，地上都是水", "水暖", "紧急", "水暖工"),
            ("空调不制冷了", "家电", "一般", "家电维修师傅"),
            ("客厅灯泡坏了，不急，有空再换", "电路", "可延后", "电工"),
            ("卧室门锁打不开", "门窗", "紧急", "门窗维修师傅"),
        ]
        made = []
        for content, category, urgency, assignee in samples:
            house = random.choice(houses)
            tenant = random.choice(tenants)
            repair = Repair(
                house_id=house.id, tenant_id=tenant.id,
                contact_name=tenant.real_name, contact_phone=tenant.phone,
                content=content, category=category, urgency=urgency,
                assignee=assignee,
                analysis="由报修智能体自动分类",
                status="pending")
            session.add(repair)
            made.append(repair)

        # 让同一套房出现三次报修，「报修最多的房源」那张表才有东西可看——
        # 这正是这份统计要暴露的问题：反复报修往往说明上次没修好。
        trouble = houses[0]
        for content, category in [("厨房下水道又堵了", "水暖"),
                                  ("下水道还是不通", "水暖")]:
            tenant = random.choice(tenants)
            repair = Repair(
                house_id=trouble.id, tenant_id=tenant.id,
                contact_name=tenant.real_name, contact_phone=tenant.phone,
                content=content, category=category, urgency="一般",
                assignee="水暖工", analysis="由报修智能体自动分类",
                status="pending")
            session.add(repair)
            made.append(repair)

        print("生成维修人员与物品 ...")
        workers = [
            RepairWorker(name="张建国", phone="13701010001", skill="水暖"),
            RepairWorker(name="李维民", phone="13701010002", skill="电路"),
            RepairWorker(name="王海涛", phone="13701010003", skill="家电"),
            RepairWorker(name="赵文斌", phone="13701010004", skill="门窗"),
        ]
        items = [
            RepairItem(name="水龙头", unit="个", unit_price=45, stock=30),
            RepairItem(name="角阀", unit="个", unit_price=18, stock=50),
            RepairItem(name="LED 灯泡", unit="个", unit_price=12, stock=80),
            RepairItem(name="门锁芯", unit="套", unit_price=95, stock=15),
            RepairItem(name="下水软管", unit="米", unit_price=9, stock=60),
            RepairItem(name="空调滤网", unit="片", unit_price=35, stock=20),
        ]
        session.add_all(workers + items)
        session.flush()

        print("结案部分工单，生成维修记录 ...")
        # 结掉前三张，留下几张待处理，看板上「待处理」才不是 0
        plans = [
            (0, 0, 1.5, 80, [(0, 1), (1, 2)]),
            (1, 2, 2.0, 120, [(5, 1)]),
            (2, 1, 0.5, 40, [(2, 2)]),
        ]
        for repair_index, worker_index, hours, labor, uses in plans:
            repair = made[repair_index]
            record = RepairRecord(
                repair_id=repair.id, worker_id=workers[worker_index].id,
                hours=hours, labor_cost=labor,
                finished_at=datetime.now() - timedelta(days=repair_index + 1),
                note="已完成并回访确认")
            session.add(record)
            session.flush()

            cost = 0.0
            for item_index, quantity in uses:
                item = items[item_index]
                price = float(item.unit_price)
                session.add(RepairItemUsage(
                    record_id=record.id, item_id=item.id,
                    item_name=item.name, quantity=quantity,
                    unit_price=price))
                item.stock -= quantity
                cost += price * quantity
            record.item_cost = round(cost, 2)
            repair.status = "done"

        print("生成租约与账单 ...")
        # 挑几套房签成在租状态，让演示时能看到完整的交易链
        signed = 0
        for house in houses:
            if signed >= 6 or house.status != "available":
                continue
            tenant = tenants[signed % len(tenants)]
            start = datetime.now().replace(day=1, hour=0, minute=0,
                                           second=0, microsecond=0)
            start = leasing.add_months(start, -(signed % 4))
            contract = leasing.create_contract(
                session, house, tenant, start_date=start, months=12,
                pay_months=(3 if signed % 2 else 1))
            leasing.activate_contract(session, contract)

            # 已到期的账单标成已缴，制造出真实的收租流水
            for bill in sorted(contract.bills, key=lambda b: b.due_date):
                if bill.due_date <= datetime.now():
                    leasing.pay_bill(session, bill, tenant)
            signed += 1

        print("生成一份待确认的合同（供答辩演示签约流程）...")
        # 答辩演示要从「租客确认签约」这一步开始讲，所以必须留一份
        # pending 的合同。全都 activate 掉的话，演示时只能现场调接口
        # 建一份，台上敲命令行既慢又容易出错。
        demo_house = next((h for h in houses
                           if h.status == "available"
                           and h.landlord_id == landlords[0].id), None)
        if demo_house:
            demo_contract = leasing.create_contract(
                session, demo_house, tenants[0],
                start_date=datetime.now().replace(
                    hour=0, minute=0, second=0, microsecond=0),
                months=12, pay_months=1)
            # 刻意不调 activate_contract：留在待确认状态
            print("  演示合同 %s（房源 %s，租客 %s）"
                  % (demo_contract.contract_no, demo_house.house_no,
                     tenants[0].username))

        print("生成一条待人工复核的内容 ...")
        # 让「内容审核」队列打开就有东西可看。这条是「机器拿不准」的典型：
        # 夸大宣传不违法，但也不该放任，正好需要人判断。
        # 确定违规的（留微信号、留手机号）在提交时就被拦了，进不到队列里。
        flagged = houses[3]
        flagged.title = "全网最低价！" + flagged.title
        session.flush()
        session.add(ContentFlag(
            scene="house", target_id=flagged.id, target_key=flagged.house_no,
            author_id=flagged.landlord_id,
            author_name=landlords[0].real_name,
            excerpt="标题：%s / 描述：%s" % (flagged.title, flagged.description or ""),
            categories="夸大宣传",
            reasons="标题：疑似夸大宣传：全网最低",
            status="pending"))

        print("生成平台公告 ...")
        notices = [
            ("押金退还说明",
             "租期届满且无违约情形的，平台将在租客腾房并结清费用后 7 个工作日内"
             "原路退还押金。若房屋存在人为损坏，由双方确认扣款金额后退还剩余部分。",
             "费用说明"),
            ("水电费如何缴纳",
             "水电费由租客按实际用量自行向供水供电单位缴纳，不通过平台代收。"
             "合租房源的水电费由同住租客按人数均摊，具体分摊方式以租赁合同约定为准。",
             "费用说明"),
            ("看房预约规则",
             "平台可预约时段为每天 9:00 至 18:00，每次看房占用一小时。"
             "预约提交后需等待房东确认，确认结果会通过站内消息通知。"
             "如需取消，请在约定时间前自行取消预约。",
             "服务规则"),
            ("报修处理时限",
             "涉及漏水、漏电、燃气泄漏等安全隐患的报修，平台将标记为紧急并优先派单，"
             "通常 24 小时内响应；一般报修 48 小时内响应；"
             "可延后的报修在 7 个工作日内安排。",
             "服务规则"),
            ("提前退租怎么办",
             "任一方提前解除合同的，应提前 30 日书面通知对方，"
             "并向对方支付相当于一个月租金的违约金。退租后未到期的租金账单将自动作废。",
             "合同条款"),
        ]
        for title, content, category in notices:
            session.add(Notice(title=title, content=content,
                               summary=content[:60], category=category,
                               publisher_id=admin.id))

        session.commit()

        available = sum(1 for h in houses if h.status == "available")
        print("\n完成：")
        print("  用户 %d 个（管理员 1、房东 %d、租客 %d）"
              % (1 + len(landlords) + len(tenants), len(landlords), len(tenants)))
        print("  房源 %d 套，其中可租 %d 套" % (len(houses), available))
        print("  看房预约 %d 条，报修工单 %d 条（已结案 %d 条）"
              % (len(used), len(made), len(plans)))
        print("  租约 %d 份，公告 %d 条" % (signed, len(notices)))
        print("  维修人员 %d 人，维修物品 %d 种" % (len(workers), len(items)))
        print("  待复核内容 1 条")
        print("  房源图片 %d 张，每套 3 张" % len(photos))
        print("\n  管理员账号 admin / admin123")
        print("  房东账号   landlord1 / test123")
        print("  租客账号   tenant1 / test123")
    finally:
        session.close()


if __name__ == "__main__":
    main()
