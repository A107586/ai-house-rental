# -*- coding: utf-8 -*-
"""补一批租后评价（T-09 / 首页「优选好房」的数据基础）。

**为什么要单独一个脚本。**
`Review.contract_id` 是非空外键——这是需求里那条规则的落点：
*只有已结束合同的租客才能评价，每份合同限一次*。
所以不能凭空往 reviews 表里塞几行，得连带补出对应的历史合同。
原来的 seed 只造了 1 条评价，108 套房里 107 套没有评分，
「优选好房」按评分筛出来是空的。

这段单独成文件、不并进 seed.py，理由是它**可以重复执行**：
已经有评价的房源直接跳过，不会越跑越多。seed.py 是「建一个干净的库」，
这个是「往已有的库里补数据」，两件事。

用法：
    cd server && python seed_reviews.py
"""
import os
import random
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import func, select                     # noqa: E402

from app import leasing                                 # noqa: E402
from app.db import SessionLocal                         # noqa: E402
from app.models import Contract, House, Review, User    # noqa: E402

# 覆盖多少套房。留一部分没有评价是**刻意的**：
# 真实平台上新上架的房源本来就没人评过，全都有评分反而假。
COVER_RATIO = 0.72

# 每套房补几条。同一套房的多条评价来自不同的历史租客，
# 每条挂在各自的合同上——这才符合「每份合同限一次」。
MIN_REVIEWS, MAX_REVIEWS = 1, 3

# 评语按「评分档 + 房源特征」组合出来，不用一句模板套所有房。
# 好评里也留了具体的不足，全是溢美之词看着就是刷的。
GOOD = [
    "住了一年多，{feature}这点是真的省心。房东话不多但事情办得快，"
    "上个月热水器坏了，报修当天下午师傅就上门了。",
    "位置比图片上看着还方便，{nearby}。屋子交房时打扫得很干净，"
    "退租时押金也是一周内退到账的，没扯皮。",
    "{feature}，采光比同小区其他户型好不少。唯一要说的是楼道隔音一般，"
    "晚上偶尔能听见邻居走动，但不影响睡觉。",
    "整体满意。签合同的时候条款一条条讲清楚了，水电燃气怎么算写得明白，"
    "住进来没有临时冒出来的费用。{feature}。",
]

FAIR = [
    "房子本身没问题，{feature}。扣一星是因为{issue}，"
    "跟房东提过，后来给解决了，但等了几天。",
    "价格在这个区段算合适的，{nearby}。{issue}，介意的话最好实地看一次。",
    "住得还行。{feature}，但{issue}。看在租金的份上可以接受。",
]

POOR = [
    "只能说一般。{issue}，住进去之后才发现。{feature}算是唯一的优点。",
    "不太推荐。{issue}，联系房东回复也慢，前后拖了两周才处理。",
]

ISSUES = [
    "厨房下水偶尔返味",
    "窗户密封条老化，冬天有点漏风",
    "小区停车位紧张，晚上回来经常没位置",
    "楼层高但电梯只有一部，早高峰要等",
    "墙面有几处返潮的痕迹",
    "热水要放很久才热",
]


def _feature_words(house):
    """从房源自己的标签里挑一句，让评语和这套房对得上。"""
    tags = [t for t in (house.tags or "").split(",") if t.strip()]
    mapping = {
        "电梯": "有电梯，搬东西和买菜回来都省力",
        "有阳台": "阳台够大，晒被子和放洗衣机都够用",
        "近学校": "送孩子上学走路十分钟",
        "可养宠物": "能养宠物这点很难得，附近遛狗的地方也多",
        "有家具": "家具家电都齐，拎包就能住",
        "采光好": "南向采光，冬天下午屋里很暖和",
        "地铁": "走到地铁口五六分钟",
        "可做饭": "厨房能开火，油烟机抽力也够",
        "近医院": "离医院近，家里有老人的话方便",
    }
    hits = [mapping[t] for t in tags if t in mapping]
    return random.choice(hits) if hits else "户型方正，没有浪费的过道"


def _nearby_words(house):
    return "楼下就有超市和菜市场，走几步是%s的公交站" % (house.community or house.district)


def main():
    random.seed(20260910)          # 固定种子，重跑得到同一批数据
    session = SessionLocal()
    try:
        rated = {row[0] for row in session.execute(
            select(Review.house_id).where(Review.deleted_at.is_(None)))}

        houses = session.scalars(select(House)).all()
        tenants = session.scalars(
            select(User).where(User.role == "tenant")).all()
        if not tenants:
            print("库里没有租客账号，先跑 seed.py")
            return

        # 只补「可租」的房源。在租、待审、已下架的都跳过——
        # create_contract 对非可租房源会直接拒绝，那是它该做的，
        # 不能为了造数据把这条校验绕开。
        targets = [h for h in houses
                   if h.id not in rated and h.status == "available"]
        random.shuffle(targets)
        targets = targets[:int(len(houses) * COVER_RATIO)]

        made_contracts = made_reviews = 0
        for house in targets:
            n = random.randint(MIN_REVIEWS, MAX_REVIEWS)
            # 起始时间从 30 个月前往后排，一份接一份，不重叠
            offset = -30
            for _ in range(n):
                tenant = random.choice(tenants)
                start = leasing.add_months(
                    datetime.now().replace(day=1, hour=0, minute=0,
                                           second=0, microsecond=0),
                    offset)
                offset += 12
                if offset > -6:      # 别排到快到今天，留出「已结束」的余地
                    break

                # 上一轮 end_contract 会把房源放回 available，
                # 但保险起见每轮都确认一次——状态不对就停下这套房，
                # 不是硬塞
                if house.status != "available":
                    break
                contract = leasing.create_contract(
                    session, house, tenant, start_date=start,
                    months=12, pay_months=1)
                leasing.activate_contract(session, contract)
                leasing.end_contract(session, contract, deduct=0,
                                     reason="租期届满，正常退租")
                session.flush()
                made_contracts += 1

                # 评分分布：多数 4–5 星，少量 3 星，个别 2 星。
                # 全五星的数据一眼假，也让「优选好房」失去区分度。
                roll = random.random()
                if roll < 0.62:
                    rating, pool = random.choice([4, 5, 5]), GOOD
                elif roll < 0.90:
                    rating, pool = 3, FAIR
                else:
                    rating, pool = 2, POOR

                content = random.choice(pool).format(
                    feature=_feature_words(house),
                    nearby=_nearby_words(house),
                    issue=random.choice(ISSUES))

                session.add(Review(
                    contract_id=contract.id,
                    house_id=house.id,
                    tenant_id=tenant.id,
                    rating=rating,
                    rating_house=max(1, min(5, rating + random.choice([0, 0, 1]))),
                    rating_landlord=max(1, min(5, rating + random.choice([-1, 0, 0, 1]))),
                    content=content))
                made_reviews += 1

            # 房源状态可能被 create/end 改动过，退租后应回到可租
            if house.status == "rented":
                house.status = "available"

        session.commit()

        total = session.scalar(select(func.count(Review.id)))
        covered = session.scalar(
            select(func.count(func.distinct(Review.house_id))))
        print("新建历史合同 %d 份，评价 %d 条" % (made_contracts, made_reviews))
        print("现在共有评价 %d 条，覆盖 %d / %d 套房源"
              % (total, covered, len(houses)))
    finally:
        session.close()


if __name__ == "__main__":
    main()
