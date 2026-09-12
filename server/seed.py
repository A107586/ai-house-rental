"""生成演示数据。

原项目自带的种子数据是纯占位内容（房屋编号 1、面积 1、租价 1 元），
既没法验证筛选逻辑，也没法用于原型评审演示，因此重新生成一批
贴合大连实际的房源。

    python seed.py          # 建表并填充数据（已有数据会先清空）
"""

import io
import os
import random
import re
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal, engine, init_db  # noqa: E402
from app import leasing  # noqa: E402
from app.map_service import approximate_coordinates  # noqa: E402
from app.models import (AgentProfile, Appointment, Base,  # noqa: E402
                        BrowsingHistory, Contract, Faq,
                        ContentFlag, Favorite, House, HouseImage, Notice,
                        Repair, RepairItem, RepairItemUsage, RepairRecord,
                        RepairWorker, Review, User)
from app.security import hash_password  # noqa: E402

# 大连各区及其代表性小区
DISTRICTS = {
    "中山区": ["青泥洼桥", "人民路", "桂林街", "五惠路", "海军广场"],
    "西岗区": ["八一路", "香炉礁", "五四广场", "北京街"],
    "沙河口区": ["星海广场", "黑石礁", "西安路", "马栏广场", "李家街"],
    "甘井子区": ["华南广场", "泡崖", "红旗", "革镇堡", "椒金山"],
    "旅顺口区": ["水师营", "龙王塘", "铁山"],
    "金州区": ["拥政街道", "光明街道", "友谊街道"],
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

# 朝向：(入库的规范值, 描述里的说法)。
# **两者必须分开。** 库里存「南」才筛得动——筛选按钮上写的就是「南」；
# 而描述里写「朝南」「南北通透」才像人话。原先只有一个「朝南」，
# 结果是筛选按钮点了什么都查不到（K-04）。
ORIENTATIONS = [("南", "朝南"), ("南", "朝南"), ("南北", "南北通透"),
                ("东", "朝东"), ("西", "朝西"), ("北", "朝北")]

# tags 里只留「说不清、也不拿来筛」的描述性标签。
# 电梯、装修、供暖都已经拆成独立列了，再留在 tags 里只会两边打架——
# 一套 elevator=False 的房子挂着「电梯」标签，用户会当成欺骗（K-04）。
ALL_TAGS = ["地铁", "近学校", "近医院", "有阳台", "可做饭",
            "有家具", "可养宠物", "采光好"]

DECORATIONS = ["精装", "精装", "简装", "简装", "毛坯"]
# 大连冬天长，供暖方式是本地租客必问的一条
HEATINGS = ["集中供暖", "集中供暖", "集中供暖", "自采暖"]
# 押付方式。押一付一最常见，押二付一多见于高价房
PAY_PLANS = [(1, 1), (1, 1), (1, 1), (1, 3), (2, 1), (0, 1)]

# 各区中心坐标，用于地图打点（K-04）。
# 房源坐标由 app/map_service.approximate_coordinates 生成：
# 商圈中心 + 按房号哈希出来的稳定偏移。
#
# 原先这里是「区中心 + random.uniform 抖动」，换掉的理由有两个：
# 商圈级比区级准一个数量级（青泥洼桥和海军广场同属中山区，
# 实际差着好几公里）；而且哈希是**确定的**——重新 seed 一次，
# 同一套房还在同一个点上，随机抖动则每次都飘，演示截图对不上。

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
    orientation, orientation_text = random.choice(ORIENTATIONS)

    description = random.choice(DESC_TEMPLATES).format(
        district=district, community=community, layout=layout,
        area=area, orientation=orientation_text, tag_text=tag_text)

    # ---- K-04 新增维度。全部给真值，否则高级筛选没有数据可筛 ----
    # 先定楼型再定层数。**不能直接随机层高**——那样撒出来的房源
    # 九成都是高层带电梯，「无电梯」这个筛选按钮点下去只剩两套，
    # 演示时看着就像没做。大连老城区多层住宅本来就不少。
    if random.random() < 0.35:
        # 多层：老式住宅，六七层顶天，绝大多数没电梯
        floor_total = random.randint(5, 7)
        elevator = random.random() < 0.15
    else:
        floor_total = random.randint(11, 33)
        elevator = True
    floor_no = random.randint(1, floor_total)

    deposit_months, pay_months = random.choice(PAY_PLANS)
    # 押金按押付方式折算，和 houses.deposit 始终是同一个数（K-02）
    deposit = price * deposit_months

    # 坐标：商圈中心 + 按房号哈希的稳定偏移（见 map_service）
    coordinates = approximate_coordinates(district, community,
                                          "DL%04d" % index)
    lng, lat = coordinates if coordinates else (None, None)

    # 核验状态：大部分已核验，留一部分待核验和未核验，
    # 好让详情页上的核验条三种样子都演示得出来
    verify_status = random.choice(
        ["verified"] * 7 + ["pending"] * 2 + ["unverified"])
    verified_at = (datetime.now() - timedelta(days=random.randint(1, 60))
                   if verify_status == "verified" else None)
    verify_no = ("DL%s%08d" % (datetime.now().strftime("%y"), index * 7919 % 10**8)
                 if verify_status == "verified" else "")

    published_at = datetime.now() - timedelta(days=random.randint(0, 90))

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
        deposit=deposit,
        area=area,
        layout=layout,
        rent_type=rent_type,
        floor="%d/%d层" % (floor_no, floor_total),
        orientation=orientation,
        description=description,
        tags=",".join(tags),
        status=random.choice(["available"] * 9 + ["rented"]),
        view_count=random.randint(0, 500),
        published_at=published_at,

        # ---- K-04 ----
        elevator=elevator,
        decoration=random.choice(DECORATIONS),
        floor_no=floor_no,
        floor_total=floor_total,
        heating=random.choice(HEATINGS),
        # 可入住日：多数房源随时可住，少部分要等现租客到期
        available_from=(date.today()
                        + timedelta(days=random.choice([0, 0, 0, 7, 15, 30, 60]))),
        min_months=random.choice([1, 3, 6, 12, 12]),
        deposit_months=deposit_months,
        pay_months=pay_months,
        # 服务费：只有一部分房源收，金额是月租的一个零头
        service_fee=(round(price * 0.05 / 10) * 10
                     if random.random() < 0.35 else 0),
        business_area=community,
        lng=lng,
        lat=lat,
        # 演示库的门牌号是随机生成的，坐标只能是「大致位置」。
        # 如实记下来源，界面上才知道该不该标注这一点
        location_source="approximate" if coordinates else "",
        geocoded_at=datetime.now() if coordinates else None,
        verify_status=verify_status,
        verify_no=verify_no,
        verified_at=verified_at,
        # 维护时间总在发布之后、今天之前。房东改一次房源信息就刷新一次，
        # 卡片上那句「3天前维护」显示的就是它
        maintained_at=published_at + timedelta(
            days=random.randint(0, max((datetime.now() - published_at).days, 0))),
    )



# ---------------------------------------------------------------- AI-03 知识库

# 语料的编写源是 `agent/knowledge/faq.md`——Markdown 好写、好 review，
# 改动在 git diff 里一目了然，比在管理后台一条条敲强得多。
# 种子把它导进 `faqs` 表，此后**数据库才是运行时的权威源**：
# 管理员在后台增删改，智能体读的是表，不是文件。
#
# 这里重写了一遍解析而没有复用 `agent/app/agents/support/corpus.py`：
# 业务后端不应该 import 智能体包，两个服务是独立部署的进程，
# 让 server 依赖 agent 的目录结构，等于把两者绑死。
# 格式很简单，二十行的重复换取零耦合，这笔账是划算的。

FAQ_SOURCE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "agent", "knowledge", "faq.md")

_FAQ_META = re.compile(r"^(id|category|alias)\s*:\s*(.+)$")


def parse_faq_markdown(text):
    """解析 faq.md，返回 [(code, question, answer, category, aliases)]。"""
    items = []
    for part in re.split(r"(?m)^##\s+", text)[1:]:
        lines = part.splitlines()
        question = lines[0].strip()
        meta = {}
        body_start = 1
        for index in range(1, len(lines)):
            line = lines[index].strip()
            if not line:
                if meta:
                    body_start = index + 1
                    break
                continue
            matched = _FAQ_META.match(line)
            if not matched:
                body_start = index
                break
            meta[matched.group(1)] = matched.group(2).strip()

        code = meta.get("id")
        if not code:
            continue
        answer = "\n".join(lines[body_start:]).strip()
        answer = re.sub(r"\n*-{3,}\s*$", "", answer).strip()
        items.append((code, question, answer,
                      meta.get("category", "其他"), meta.get("alias", "")))
    return items


def seed_faqs(session):
    """导入知识条目。已存在的按编码更新，不重复插入。"""
    if not os.path.exists(FAQ_SOURCE):
        print("  未找到 %s，跳过知识库导入" % FAQ_SOURCE)
        return 0

    with io.open(FAQ_SOURCE, encoding="utf-8") as handle:
        items = parse_faq_markdown(handle.read())

    created = 0
    for order, (code, question, answer, category, aliases) in enumerate(items):
        row = session.scalar(select(Faq).where(Faq.code == code))
        if row is None:
            row = Faq(code=code)
            session.add(row)
            created += 1
        row.question = question
        row.answer = answer
        row.category = category
        row.aliases = aliases
        row.sort_order = order
        row.is_published = True
    session.commit()
    return created


def main():
    random.seed(20260901)  # 固定随机种子，保证每个人生成的数据一致

    print("建表 ...")
    Base.metadata.drop_all(engine)
    init_db()

    session = SessionLocal()
    try:
        print("创建用户 ...")
        # 每个演示账号都绑一个邮箱。
        #
        # **不绑的话「忘记密码」整条路是断的**：接口会正常返回
        # 「如果该账号存在……」，然后什么也不发生——功能在代码里是
        # 完整的，在演示里却一次也跑不通，而这两件事很容易被混为一谈。
        # 域名用 example.com：RFC 2606 专门留给文档与示例，
        # 不会真的投递到某个人的信箱里。
        def _mail(name):
            return "%s@example.com" % name

        admin = User(username="admin", password_hash=hash_password("admin123"),
                     real_name="系统管理员", phone=_phone(), role="admin",
                     email=_mail("admin"))
        session.add(admin)

        landlords = []
        for i, name in enumerate(LANDLORD_NAMES, start=1):
            user = User(username="landlord%d" % i,
                        password_hash=hash_password("test123"),
                        real_name=name, phone=_phone(), role="landlord",
                        email=_mail("landlord%d" % i))
            session.add(user)
            landlords.append(user)

        tenants = []
        for i, name in enumerate(TENANT_NAMES, start=1):
            user = User(username="tenant%d" % i,
                        password_hash=hash_password("test123"),
                        real_name=name, phone=_phone(), role="tenant",
                        email=_mail("tenant%d" % i))
            session.add(user)
            tenants.append(user)

        # 一个**已过审**的中介（J-01）。
        #
        # 不留给测试的人自己注册：注册出来的中介资质是「未提交」，
        # 工作台上除了「提交资质」什么都没有，得先切到管理员审一遍
        # 才能开始测中介功能——那是三步跟中介本身无关的准备动作。
        # 演示数据的职责就是把这类准备动作省掉。
        #
        # 主营区域**留空**（不限区域），否则「接单大厅」和「智能派单」
        # 在别的区都是空的，测的人会以为功能坏了。
        agent = User(username="agent1", password_hash=hash_password("test123"),
                     real_name="李经纪", phone=_phone(), role="agent",
                     email=_mail("agent1"))
        session.add(agent)
        session.flush()
        session.add(AgentProfile(
            user_id=agent.id, license_no="DL-JJ-20260001",
            license_status="approved", service_districts="",
            years=5, accepting=True,
            intro="熟悉大连各区两室房源，可代房东带看与答复咨询。"))
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
        # 结掉前三张，留下几张待处理，看板上「待处理」才不是 0。
        #
        # 每条多给一个「报修后多少小时修完」。**原来写的是
        # `datetime.now() - timedelta(days=index+1)`，而工单的
        # created_at 默认是 now——完成时间比报修时间还早**，
        # 房东概要里于是算出「报修平均 -24 小时处理完」。
        # 造数据时把两个时间各自随手取值，是最容易埋这种反常识记录的写法：
        # 有先后关系的两个时间，后一个必须从前一个推出来。
        plans = [
            (0, 0, 1.5, 80, 6, [(0, 1), (1, 2)]),
            (1, 2, 2.0, 120, 28, [(5, 1)]),
            (2, 1, 0.5, 40, 3, [(2, 2)]),
        ]
        for repair_index, worker_index, hours, labor, after_hours, uses in plans:
            repair = made[repair_index]
            record = RepairRecord(
                repair_id=repair.id, worker_id=workers[worker_index].id,
                hours=hours, labor_cost=labor,
                finished_at=repair.created_at + timedelta(hours=after_hours),
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

        print("生成浏览足迹与收藏 ...")
        # AI-07 房源推荐依赖浏览足迹。没有足迹的话，演示时首页的
        # 「为你推荐」是空的——得先手动点开六七套房才出得来，
        # 而答辩脚本里并没有这一步。
        watcher = tenants[0]
        # 刻意集中在中山区的两室一厅：偏好画像才有明显倾向，
        # 推荐理由也才说得出「在你常看的中山区」
        liked = [h for h in houses
                 if h.district == "中山区" and h.status == "available"][:5]
        others = [h for h in houses
                  if h.district == "西岗区" and h.status == "available"][:2]
        for index, house in enumerate(liked + others):
            session.add(BrowsingHistory(
                user_id=watcher.id, house_id=house.id,
                view_count=random.randint(1, 3),
                viewed_at=datetime.now() - timedelta(hours=index * 5)))
        for house in liked[:2]:
            session.add(Favorite(user_id=watcher.id, house_id=house.id))
        session.flush()

        print("生成一份已结束的租约与一条评价 ...")
        # 「待评价」与房源评分都需要已结束的合同。全是生效中的话，
        # 租后评价（T-09）和房源卡片上的星级都演示不了。
        done_house = next((h for h in houses
                           if h.status == "available"
                           and h.landlord_id == landlords[1].id), None)
        if done_house:
            past = leasing.add_months(
                datetime.now().replace(day=1, hour=0, minute=0,
                                       second=0, microsecond=0), -14)
            old_contract = leasing.create_contract(
                session, done_house, tenants[1], start_date=past,
                months=12, pay_months=1)
            leasing.activate_contract(session, old_contract)
            leasing.end_contract(session, old_contract, deduct=0,
                                 reason="租期届满，正常退租")
            session.flush()

            # 只评一份，另留一份「待评价」——两种状态在界面上都看得到
            second = next((h for h in houses
                           if h.status == "available"
                           and h.landlord_id == landlords[2].id), None)
            if second:
                # 待评价的那份给 tenants[0]——演示账号就是它，
                # 给别人的话「待评价」页打开还是空的
                pending_contract = leasing.create_contract(
                    session, second, tenants[0], start_date=past,
                    months=12, pay_months=1)
                leasing.activate_contract(session, pending_contract)
                leasing.end_contract(session, pending_contract, deduct=0,
                                     reason="租期届满，正常退租")

            session.add(Review(
                contract_id=old_contract.id, house_id=done_house.id,
                tenant_id=tenants[1].id, rating=5, rating_house=5,
                rating_landlord=4,
                content="房子采光很好，房东响应也快，水管坏了当天就来修了。"))
            session.flush()

        print("铺历史租住记录与评价 ...")
        _seed_history(session, houses, tenants, random)

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

        print("导入 AI-03 客服知识库 ...")
        faq_count = seed_faqs(session)

        available = sum(1 for h in houses if h.status == "available")
        print("\n完成：")
        print("  用户 %d 个（管理员 1、房东 %d、租客 %d）"
              % (1 + len(landlords) + len(tenants), len(landlords), len(tenants)))
        print("  房源 %d 套，其中可租 %d 套" % (len(houses), available))
        print("  客服知识条目 %d 条（AI-03 检索语料）" % faq_count)
        print("  看房预约 %d 条，报修工单 %d 条（已结案 %d 条）"
              % (len(used), len(made), len(plans)))
        print("  租约 %d 份，公告 %d 条" % (signed, len(notices)))
        print("  维修人员 %d 人，维修物品 %d 种" % (len(workers), len(items)))
        print("  待复核内容 1 条")
        print("  浏览足迹 7 条、收藏 2 条（供推荐功能演示）")
        print("  已结束租约 2 份，其中 1 份已评价、1 份待评价")
        print("  房源图片 %d 张，每套 3 张" % len(photos))
        print("\n  管理员账号 admin / admin123")
        print("  房东账号   landlord1 / test123")
        print("  租客账号   tenant1 / test123")
        print("  中介账号   agent1    / test123（资质已通过，可直接接单）")
    finally:
        session.close()



# ---------------------------------------------------------------- 历史与评价

# 评价正文。**写具体的事，不写形容词。**
#
# 「房子不错，房东人很好」这种话对看房的人毫无用处——他要判断的是
# 「这套房有没有我在意的那个毛病」。所以每条都落在一件具体的事上：
# 几点有太阳、水压够不够、报修多久上门、押金几天退。
#
# 正负都要有。清一色好评看着就是刷的，而**一条中评比十条好评更让人
# 相信这个评价区是真的**。
_GOOD = [
    "朝南的屋子下午三点还有太阳，晒被子很方便。房东报修当天就找人来了。",
    "楼下就是菜市场和地铁口，通勤四十分钟以内。隔音一般，但能接受。",
    "家具是新换的，冰箱洗衣机都能用。退租时押金一周内退到账，没扯皮。",
    "水压足，热水来得快。物业管得严，快递丢不了。",
    "房东话不多但办事利索，合同条款一条条讲清楚才让签。",
    "小区绿化好，晚上安静。楼层高，视野开阔。",
    "位置好找，第一次去也没绕路。房子和照片基本一致。",
    "厨房大，能开火。抽油烟机是新的。",
]
_FAIR = [
    "房子本身没问题，就是临街，晚上偶尔有车声。怕吵的要考虑。",
    "楼层不高采光一般，上午基本没直射。价格合适，将就住没问题。",
    "设施都齐但有点旧，冰箱声音大。房东说可以换，还没换。",
    "报修响应慢了两天，最后还是修好了。整体还行。",
    "电梯高峰期要等，其他都挺好。",
]
_POOR = [
    "墙皮有返潮，住了三个月才发现。房东后来刷了一遍，但当初没说。",
    "隔音比想象中差，隔壁说话听得见。其他方面还可以。",
]


def _seed_history(session, houses, tenants, random):
    """给一部分房源铺上「住过、评过」的历史。

    ## 为什么要铺

    原先只造了一条评价，于是 108 套房里 107 套的 `avg_rating` 是 0，
    列表页每一张卡片都写着「暂无评分」——**这个提示本来是用来区分
    「没人住过」和「分数没给你看」的，满屏都是的时候它一个意思也传达不出来。**
    房东口碑、优选好房、按评分排序这三处也跟着全空。

    ## 为什么直接插行，不走 leasing 的那套

    `create_contract → activate → end` 每份合同会连带排出整期账单。
    四十份历史合同就是四百多条早已付清的账单，把「我的账单」页面
    冲得没法看，而这些账单对演示没有任何价值——**它们是历史，
    不是待办**。所以这里直接写 contracts 与 reviews 两张表。

    留出三成房源不评价：`avg_rating` 为 0 的情况必须在演示数据里真实存在，
    否则「暂无评分」这条分支永远走不到，坏了也没人发现。
    """
    pool = [h for h in houses if h.landlord_id]
    random.shuffle(pool)
    rated = pool[:int(len(pool) * 0.7)]

    made = 0
    for index, house in enumerate(rated):
        # 每套一到三条，多数是一条——一套房一年换不了几个租客
        count = 1 if index % 4 else (2 if index % 8 else 3)
        for turn in range(count):
            tenant = tenants[(index + turn) % len(tenants)]
            if tenant.id == house.landlord_id:
                continue
            # 往前推 1–4 年，一年一租
            years = turn + 1
            start = datetime.now() - timedelta(days=365 * years + 30)
            end = start + timedelta(days=365)
            rent = Decimal(str(house.rent_price))
            contract = Contract(
                contract_no="HT%s%04d" % (start.strftime("%y%m"), made),
                house_id=house.id, tenant_id=tenant.id,
                landlord_id=house.landlord_id,
                start_date=start, end_date=end,
                rent=rent, deposit=rent, pay_months=1,
                status="ended", signed_at=start, ended_at=end,
                end_reason="租期届满，正常退租",
            )
            session.add(contract)
            session.flush()

            # 评分分布：七成好评、两成中评、一成差评。
            # **不做成清一色五星**——那种评价区没人信
            roll = (index * 7 + turn) % 10
            if roll < 7:
                rating, content = random.choice([5, 5, 4]), random.choice(_GOOD)
            elif roll < 9:
                rating, content = 3, random.choice(_FAIR)
            else:
                rating, content = 2, random.choice(_POOR)

            session.add(Review(
                contract_id=contract.id, house_id=house.id,
                tenant_id=tenant.id, rating=rating,
                rating_house=rating,
                # 房东分和房源分不总是一样：房子一般但房东靠谱是常见组合
                rating_landlord=min(5, max(1, rating + (1 if roll % 3 else 0))),
                content=content,
                created_at=end + timedelta(days=2),
            ))
            made += 1

    session.flush()
    print("  历史租约 %d 份，评价 %d 条，覆盖 %d / %d 套房源"
          % (made, made, len(rated), len(houses)))


if __name__ == "__main__":
    main()
