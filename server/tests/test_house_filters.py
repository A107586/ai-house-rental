"""高级筛选（K-05）。

守的是一句话：**任意组合、结果必然正确**。

这条测试存在的原因是筛选曾经是错的，不是缺的——「电梯」挤在逗号分隔的
`tags` 里，用 `LIKE '%电梯%'` 去筛，「无电梯」会被「电梯」命中。
所以这里每一条用例都要真的去核对返回的每一套房是否满足条件，
只断言「返回了几套」是测不出那个 bug 的。
"""

from datetime import date, timedelta

from tests.base import ApiTestCase, auth_header
import app.db as db
from app.models import House


def make(session, house_no, landlord_id, **fields):
    base = dict(title="房源" + house_no, district="中山区", community="青泥洼桥",
                rent_price=2800, area=70, layout="两室一厅", rent_type="整租",
                status="available")
    base.update(fields)
    house = House(house_no=house_no, landlord_id=landlord_id, **base)
    session.add(house)
    return house


class FilterCase(ApiTestCase):
    """自建一批房源，每套的维度都是已知的，好逐条核对结果。"""

    def setUp(self):
        super().setUp()
        session = db.SessionLocal()
        try:
            landlord = session.query(House).filter_by(
                house_no="DL0001").one().landlord_id
            # 清掉 base fixture 的三套，避免它们的空字段混进断言
            session.query(House).delete()
            session.flush()

            today = date.today()
            make(session, "F001", landlord, elevator=True, orientation="南",
                 decoration="精装", heating="集中供暖", floor_no=2,
                 floor_total=18, min_months=1,
                 available_from=today)
            make(session, "F002", landlord, elevator=False, orientation="北",
                 decoration="简装", heating="自采暖", floor_no=5,
                 floor_total=6, min_months=12,
                 available_from=today + timedelta(days=60))
            make(session, "F003", landlord, elevator=True, orientation="南北",
                 decoration="毛坯", heating="集中供暖", floor_no=9,
                 floor_total=18, min_months=6,
                 available_from=today + timedelta(days=20))
            make(session, "F004", landlord, elevator=True, orientation="南",
                 decoration="精装", heating="集中供暖", floor_no=17,
                 floor_total=18, min_months=3,
                 available_from=None)
            session.commit()
        finally:
            session.close()

    def nos(self, **params):
        params.setdefault("limit", 100)
        body = self.client.get("/api/houses", params=params).json()
        return sorted(h["house_no"] for h in body["list"])

    def rows(self, **params):
        params.setdefault("limit", 100)
        return self.client.get("/api/houses", params=params).json()["list"]


class TestElevator(FilterCase):

    def test_with_elevator(self):
        self.assertEqual(self.nos(elevator="true"), ["F001", "F003", "F004"])

    def test_without_elevator(self):
        """这一条是整组测试的由来。

        原先「电梯」是 `tags` 里的一个词，筛「无电梯」的唯一写法是
        `tags NOT LIKE '%电梯%'`，而挂着「无电梯」标签的房源同样含有
        「电梯」二字——**筛出来的结果是反的**。
        """
        self.assertEqual(self.nos(elevator="false"), ["F002"])

    def test_two_sides_partition_the_set(self):
        """有 + 无 = 全部。少一套都说明有房源被筛丢了。"""
        total = len(self.nos())
        self.assertEqual(len(self.nos(elevator="true"))
                         + len(self.nos(elevator="false")), total)


class TestOrientation(FilterCase):

    def test_exact_match_not_substring(self):
        """「南」不能把「南北」也带出来。

        子串匹配在朝向上是错的：找朝南房的人要的是南向，
        南北通透是另一件事。
        """
        self.assertEqual(self.nos(orientation="南"), ["F001", "F004"])
        self.assertEqual(self.nos(orientation="南北"), ["F003"])


class TestFloorLevel(FilterCase):

    def test_ratio_not_fixed_threshold(self):
        """低/中/高按三等分算。

        F002 是 6 层楼的第 5 层——按固定阈值（比如「1-3 层算低」）
        它会被算成中低层，实际上它是这栋楼的高层。
        """
        self.assertEqual(self.nos(floor_level="低楼层"), ["F001"])
        self.assertEqual(self.nos(floor_level="中楼层"), ["F003"])
        self.assertEqual(self.nos(floor_level="高楼层"), ["F002", "F004"])

    def test_three_levels_partition_the_set(self):
        got = (self.nos(floor_level="低楼层") + self.nos(floor_level="中楼层")
               + self.nos(floor_level="高楼层"))
        self.assertEqual(sorted(got), self.nos())

    def test_unknown_floor_is_excluded(self):
        """楼层信息不全的房源不参与楼层筛选——猜不了，也不该猜。"""
        session = db.SessionLocal()
        try:
            make(session, "F900", session.query(House).first().landlord_id,
                 floor_no=None, floor_total=None)
            session.commit()
        finally:
            session.close()
        for level in ("低楼层", "中楼层", "高楼层"):
            self.assertNotIn("F900", self.nos(floor_level=level))
        self.assertIn("F900", self.nos())

    def test_unknown_level_is_ignored_not_error(self):
        """传了不认识的档位就当没传，不要 500。"""
        self.assertEqual(self.nos(floor_level="顶楼"), self.nos())


class TestDecorationAndHeating(FilterCase):

    def test_decoration(self):
        self.assertEqual(self.nos(decoration="精装"), ["F001", "F004"])

    def test_heating(self):
        self.assertEqual(self.nos(heating="自采暖"), ["F002"])


class TestLeaseTerm(FilterCase):

    def test_monthly_means_one_month_start(self):
        self.assertEqual(self.nos(lease_term="monthly"), ["F001"])

    def test_quarter_includes_shorter(self):
        """「三月内起租」要包含「一月起租」。

        用户点这个档位是在问「最短能租多久」，一个月起租的房源
        当然满足三个月的需求——把它排掉是把筛选做成了等值匹配。
        """
        self.assertEqual(self.nos(lease_term="quarter"), ["F001", "F004"])

    def test_yearly_is_the_other_direction(self):
        """年租问的是「房东要求至少租一年」，方向和前几档相反。"""
        self.assertEqual(self.nos(lease_term="yearly"), ["F002"])


class TestAvailableFrom(FilterCase):

    def test_available_now(self):
        """随时入住 = 可入住日已到，外加日期未填的。

        日期没填的房源状态已经是「可租」，缺的只是一个具体日子；
        把它藏起来对房东不公平，对找房的人也少了一条选择。
        """
        self.assertEqual(self.nos(available_days=0), ["F001", "F004"])

    def test_within_30_days(self):
        self.assertEqual(self.nos(available_days=30),
                         ["F001", "F003", "F004"])


class TestCombination(FilterCase):
    """组合筛选。这是「一个概念一个字段」真正要买到的东西。"""

    def test_all_conditions_hold_simultaneously(self):
        rows = self.rows(elevator="true", orientation="南",
                         decoration="精装", floor_level="高楼层")
        self.assertEqual([r["house_no"] for r in rows], ["F004"])
        # 不只看数量——逐条核对每个条件是否真的成立
        for row in rows:
            self.assertIs(row["elevator"], True)
            self.assertEqual(row["orientation"], "南")
            self.assertEqual(row["decoration"], "精装")
            self.assertEqual(row["floor_level"], "高楼层")

    def test_empty_result_is_not_an_error(self):
        body = self.client.get("/api/houses",
                               params={"elevator": "false",
                                       "decoration": "精装"}).json()
        self.assertEqual(body["total"], 0)
        self.assertEqual(body["list"], [])

    def test_combines_with_existing_filters(self):
        """新维度要能和原有的区域/租金/户型叠加，不是另一条检索路径。"""
        self.assertEqual(
            self.nos(district="中山区", layout="两室一厅", elevator="true",
                     decoration="精装"),
            ["F001", "F004"])


class TestKeywordReachesAddress(FilterCase):
    """关键词搜索要能命中地址和商圈，不只是标题和小区。

    找房的人打的是「桂林街」「青泥洼桥」这种地名，
    而它们往往只出现在地址里。
    """

    def setUp(self):
        super().setUp()
        session = db.SessionLocal()
        try:
            landlord = session.query(House).first().landlord_id
            make(session, "F800", landlord, address="中山区桂林街 12 号",
                 business_area="桂林街", community="别的小区")
            session.commit()
        finally:
            session.close()

    def test_matches_address(self):
        self.assertIn("F800", self.nos(keyword="桂林街"))

    def test_search_works_without_the_agent(self):
        """这条路径完全不经过智能体。

        找房入口只剩自然语言框的时候，智能体一挂整站就搜不了房；
        关键词检索是那条保底路径（K-06）。
        """
        body = self.client.get("/api/houses", params={"keyword": "桂林街"})
        self.assertEqual(body.status_code, 200)
        self.assertGreaterEqual(body.json()["total"], 1)


class TestFilteringRespectsVisibility(FilterCase):
    """筛选不能成为绕过审核的后门。"""

    def test_pending_houses_stay_hidden(self):
        session = db.SessionLocal()
        try:
            make(session, "F700", session.query(House).first().landlord_id,
                 elevator=True, status="pending")
            session.commit()
        finally:
            session.close()
        self.assertNotIn("F700", self.nos(elevator="true"))
        self.assertNotIn("F700", self.nos(status="pending"))


class TestFeatureFilter(FilterCase):
    """设施偏好筛选（K-06）。

    这一组是把智能体的「设施条件」从前端子串匹配挪到后端整词匹配之后
    补的。原先的做法有两个毛病：为了拿到正确的 total 得把候选集整个翻完
    （一次找房打好几趟后端），而且靠子串匹配——**「无电梯」里含「电梯」**，
    要电梯的人会被推一套没电梯的房。
    """

    def setUp(self):
        super().setUp()
        session = db.SessionLocal()
        try:
            landlord = session.query(House).first().landlord_id
            make(session, "G001", landlord, tags="地铁,可养宠物", elevator=True)
            make(session, "G002", landlord, tags="可养宠物", elevator=False)
            make(session, "G003", landlord, tags="地铁", elevator=False)
            # 名字里含另一个标签的整词，用来验证边界
            make(session, "G004", landlord, tags="不可养宠物", elevator=False)
            session.commit()
        finally:
            session.close()

    def test_single_feature(self):
        got = self.nos(features="地铁")
        self.assertEqual(got, ["G001", "G003"])

    def test_features_are_and_not_or(self):
        """用户提了两个条件就是两个都要，不是满足一个就行。"""
        self.assertEqual(self.nos(features="地铁,可养宠物"), ["G001"])

    def test_whole_word_not_substring(self):
        """「可养宠物」不能命中「不可养宠物」。

        标签之间以逗号分隔，匹配时两端补上逗号做整词比对；
        少了这一步，一个否定标签会被当成肯定的。
        """
        got = self.nos(features="可养宠物")
        self.assertIn("G001", got)
        self.assertIn("G002", got)
        self.assertNotIn("G004", got)

    def test_column_backed_feature_uses_the_column(self):
        """已经升格成独立列的设施走列，不走标签。

        列比标签可靠：标签是「有没有写」，列是「是不是真的有」。
        """
        self.assertEqual(self.nos(features="电梯"), self.nos(elevator="true"))

    def test_feature_combines_with_structured_filters(self):
        self.assertEqual(
            self.nos(district="中山区", features="地铁", elevator="true"),
            ["G001"])

    def test_unknown_feature_returns_nothing_not_everything(self):
        """认不出的设施要筛成空，**不能当作没传**。

        当没传的话，用户要「带游泳池的」会拿到全部房源，
        还以为每一套都带游泳池。
        """
        self.assertEqual(self.nos(features="游泳池"), [])

    def test_blank_feature_entries_are_ignored(self):
        """`features=,,电梯,` 这种脏参数不该让查询出错或跑偏。"""
        self.assertEqual(self.nos(features=",,电梯,"), self.nos(features="电梯"))
