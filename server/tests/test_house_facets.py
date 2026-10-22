"""房源筛选维度、费用与核验字段（K-04）。

这一批字段是「高级筛选 / 费用明细 / 地图打点 / 房源核验」四件事共同的地基。
它们必须在数据库评审之前定稿——评审之后再改表要重走一遍评审流程，
所以这份测试守的不只是功能，还有**字段本身别被后来的重构悄悄改掉**。

重点测三件事：

1. 每个概念**有自己的列**，不再挤在 `tags` 字符串里
   （`tags LIKE '%电梯%'` 会把「无电梯」也命中，这是做了会错）；
2. 发布与编辑收的是**同一组字段**，不出现「发得了、改不了」；
3. 押金仍然只有 `houses.deposit` 一个真相（K-02 不被 K-04 带回去）。
"""

from datetime import date, datetime

from tests.base import ApiTestCase, auth_header, make_admin
import app.db as db
from app.models import House


FULL = {
    "title": "测试房源两室一厅",
    "district": "中山区",
    "community": "青泥洼桥",
    "address": "中山区青泥洼桥路 12 号楼 3 单元",
    "rent_price": 2800,
    "area": 70,
    "layout": "两室一厅",
    "rent_type": "整租",
    "orientation": "南",
    "elevator": True,
    "decoration": "精装",
    "floor_no": 5,
    "floor_total": 18,
    "heating": "集中供暖",
    "available_from": "2026-10-01",
    "min_months": 6,
    "deposit_months": 1,
    "pay_months": 3,
    "service_fee": 140,
    "business_area": "青泥洼桥",
    "lng": 121.6447,
    "lat": 38.9186,
}


class FacetCase(ApiTestCase):

    def setUp(self):
        super().setUp()
        self.landlord = auth_header(self.client, "landlord1", "test123")

    def publish(self, **overrides):
        body = dict(FULL)
        body.update(overrides)
        return self.client.post("/api/houses", headers=self.landlord, json=body)

    def fetch(self, house_no):
        # 新发布的房源还在待审，只有房东自己打得开详情
        return self.client.get("/api/houses/%s" % house_no, headers=self.landlord).json()


class TestPublishAcceptsFacets(FacetCase):

    def test_all_facets_are_stored(self):
        response = self.publish()
        self.assertEqual(response.status_code, 200, response.text)
        house = response.json()["house"]

        self.assertIs(house["elevator"], True)
        self.assertEqual(house["decoration"], "精装")
        self.assertEqual(house["floor_no"], 5)
        self.assertEqual(house["floor_total"], 18)
        self.assertEqual(house["heating"], "集中供暖")
        self.assertEqual(house["available_from"], "2026-10-01")
        self.assertEqual(house["min_months"], 6)
        self.assertEqual(house["service_fee"], 140.0)
        self.assertEqual(house["business_area"], "青泥洼桥")
        self.assertAlmostEqual(house["lng"], 121.6447, places=4)
        self.assertAlmostEqual(house["lat"], 38.9186, places=4)

    def test_omitted_facets_keep_column_defaults(self):
        """没填的维度要落在列默认值上，**不能变成 NULL**。

        变成 NULL 之后，按 `elevator=false` 或 `min_months<=3` 去筛
        就再也筛不到这套房了——用户看到的是「房源凭空消失」。
        """
        response = self.publish(elevator=None, min_months=None,
                                decoration=None)
        house = response.json()["house"]
        self.assertIs(house["elevator"], False)
        self.assertEqual(house["min_months"], 1)
        self.assertEqual(house["decoration"], "")

    def test_floor_level_is_derived_not_stored(self):
        """低/中/高楼层按三等分算，不写死层数阈值。

        6 层老楼的第 5 层是高楼层，33 层塔楼的第 5 层是低楼层——
        固定阈值对不上这两种情况。
        """
        low = self.publish(floor_no=2, floor_total=18).json()["house"]
        high = self.publish(floor_no=5, floor_total=6).json()["house"]
        self.assertEqual(low["floor_level"], "低楼层")
        self.assertEqual(high["floor_level"], "高楼层")

    def test_floor_level_is_blank_when_unknown(self):
        house = self.publish(floor_no=None, floor_total=None).json()["house"]
        self.assertEqual(house["floor_level"], "")

    def test_pay_plan_phrase(self):
        house = self.publish(deposit_months=1, pay_months=3).json()["house"]
        self.assertEqual(house["pay_plan"], "押一付三")

    def test_pay_plan_blank_without_both_parts(self):
        house = self.publish(deposit_months=None, pay_months=None).json()["house"]
        self.assertEqual(house["pay_plan"], "")


class TestPublishValidation(FacetCase):
    """K-12 的一部分：发布校验的上下限。

    没有上限就能发出「月租 9999999」的房源，而列表按价格排序时
    这一条会永远霸占榜首；标题没有长度限制则是在 SQLite 上能存、
    换到 MySQL（`String(120)`）直接报错。
    """

    def test_rejects_absurd_rent(self):
        self.assertEqual(self.publish(rent_price=9999999).status_code, 422)

    def test_rejects_absurd_area(self):
        self.assertEqual(self.publish(area=100000).status_code, 422)

    def test_rejects_negative_area(self):
        self.assertEqual(self.publish(area=-10).status_code, 422)

    def test_rejects_overlong_title(self):
        self.assertEqual(self.publish(title="房" * 200).status_code, 422)

    def test_rejects_floor_above_total(self):
        self.assertEqual(
            self.publish(floor_no=20, floor_total=6).status_code, 422)

    def test_rejects_unknown_decoration(self):
        self.assertEqual(self.publish(decoration="豪装").status_code, 422)

    def test_rejects_unknown_orientation(self):
        """**这条原先断言「朝南」该被拒，改了。**

        理由是它和系统其余部分对不上：前端发布表单的下拉框给的就是
        「朝南」「南北通透」，种子数据里 88 套房源用的也是这一套写法。
        照原先的校验，九个选项里有五个一提交就 422——房东在页面上
        选「朝南」根本发不出去，编辑已有房源时朝向原样回填也存不下。

        两条路：把库里的存量数据和前端统一成「南」，或者校验放宽成两种都收。
        选了后者——封版在即，改数据要写迁移、还要同步前端与展示，
        而这里只是入参校验，放宽不丢信息。真正无效的值照样拒。
        """
        self.assertEqual(self.publish(orientation="朝里").status_code, 422)
        self.assertEqual(self.publish(orientation="斜着").status_code, 422)

    def test_accepts_both_spellings(self):
        for value in ("南", "朝南", "东南", "南北通透", ""):
            with self.subTest(orientation=value):
                self.assertEqual(self.publish(orientation=value).status_code,
                                 200, value)

    def test_rejects_swapped_coordinates(self):
        """经纬度填反了要挡住。

        纬度填成 121 的话点位会掉到地图外面去，而页面上看不出哪里不对——
        这类错误不拦在入口，就得靠人一条条去核。
        """
        self.assertEqual(
            self.publish(lng=38.9186, lat=121.6447).status_code, 422)

    def test_blank_optional_fields_are_fine(self):
        """空串表示「没填」，是合法的。

        把「没填」当成非法值，会逼着房东为了发布而瞎选一个，
        那比留空更糟——瞎选的值会被当成事实拿去筛选。
        """
        response = self.publish(decoration="", heating="", orientation="")
        self.assertEqual(response.status_code, 200, response.text)


class TestEditAcceptsSameFacets(FacetCase):
    """发布能填的，编辑也要能改。半截功能比没有更让人恼火。"""

    def test_every_facet_is_editable(self):
        house_no = self.publish().json()["house"]["house_no"]
        response = self.client.put(
            "/api/houses/%s" % house_no, headers=self.landlord,
            json={"elevator": False, "decoration": "简装", "floor_no": 2,
                  "floor_total": 6, "heating": "自采暖", "min_months": 12,
                  "available_from": "2026-12-01", "service_fee": 0,
                  "business_area": "人民路"})
        self.assertEqual(response.status_code, 200, response.text)
        house = response.json()["house"]
        self.assertIs(house["elevator"], False)
        self.assertEqual(house["decoration"], "简装")
        self.assertEqual(house["heating"], "自采暖")
        self.assertEqual(house["min_months"], 12)
        self.assertEqual(house["available_from"], "2026-12-01")
        self.assertEqual(house["business_area"], "人民路")

    def test_edit_refreshes_maintained_at(self):
        """改过房源就刷新维护时间。

        列表卡片上那句「3天前维护」靠它。不刷新的话，
        一套天天在改的房源看起来像是半年没人管——而这正是
        租客用来判断「这条信息还值不值得信」的依据。
        """
        house_no = self.publish().json()["house"]["house_no"]
        session = db.SessionLocal()
        try:
            house = session.query(House).filter_by(house_no=house_no).one()
            house.maintained_at = datetime(2026, 1, 1)
            session.commit()
        finally:
            session.close()

        self.client.put("/api/houses/%s" % house_no, headers=self.landlord,
                        json={"description": "改一下描述"})
        self.assertNotEqual(self.fetch(house_no)["maintained_at"], "2026-01-01")


class TestDepositStaysSingleSourced(FacetCase):
    """K-04 引入了 `deposit_months`，但押金仍然只有 `deposit` 一个真相。"""

    def test_deposit_months_derives_the_amount(self):
        house = self.publish(rent_price=2800, deposit_months=2,
                             deposit=0).json()["house"]
        self.assertEqual(house["deposit"], 5600.0)

    def test_custom_amount_survives_when_no_months_given(self):
        """填了不成整数倍的押金就按金额走，月数留空。"""
        house = self.publish(rent_price=2800, deposit=800,
                             deposit_months=None).json()["house"]
        self.assertEqual(house["deposit"], 800.0)
        self.assertIsNone(house["deposit_months"])

    def test_typed_amount_beats_the_months_preset(self):
        """**两个都填了且对不上时，以手填的金额为准。**

        这是验收意见「房源发布上的押金与合同不符合」的成因：原来只要
        选了押付方式就按月数算，手填的金额直接丢掉——房东填「押金 800」
        又顺手选了「押一付三」，存进去的是 2800，而中间没有任何提示。

        为什么是金额赢：月数是下拉框的一个默认档位，金额是他一个字一个字
        敲进去的。冲突时敲进去的那个才是他的意思。
        """
        house = self.publish(rent_price=2800, deposit=800,
                             deposit_months=1).json()["house"]
        self.assertEqual(house["deposit"], 800.0)

    def test_months_is_cleared_when_the_amount_wins(self):
        """月数要一起清掉，否则卡片上印着「押一付三」而押金是 800。

        **标签和数字当场自相矛盾，比只给一个数字更糟。**
        """
        house = self.publish(rent_price=2800, deposit=800,
                             deposit_months=1).json()["house"]
        self.assertIsNone(house["deposit_months"])

    def test_both_kept_when_they_agree(self):
        """填的金额正好等于按月数算出来的——不冲突，标签留着。"""
        house = self.publish(rent_price=2800, deposit=2800,
                             deposit_months=1).json()["house"]
        self.assertEqual(house["deposit"], 2800.0)
        self.assertEqual(house["deposit_months"], 1)

    def test_zero_months_is_not_treated_as_blank(self):
        """押零付一是「明确免押」，不是「这一栏留空了」。"""
        house = self.publish(rent_price=2800, deposit=0,
                             deposit_months=0).json()["house"]
        self.assertEqual(house["deposit"], 0.0)
        self.assertEqual(house["deposit_months"], 0)

    def test_changing_rent_recomputes_deposit(self):
        """改了租金，按押付方式算出来的押金要跟着变。

        不重算的话，「押一付一」的标签和 `deposit` 里那个数会各说各话。
        """
        house_no = self.publish(rent_price=2800,
                                deposit_months=1).json()["house"]["house_no"]
        self.client.put("/api/houses/%s" % house_no, headers=self.landlord,
                        json={"rent_price": 3000})
        self.assertEqual(self.fetch(house_no)["deposit"], 3000.0)

    def test_zero_deposit_months_means_really_no_deposit(self):
        """免押是明确的选择，不能被当成「这一栏没填」。

        两者的 `deposit` 都是 0，只有 `deposit_months` 分得开——
        分不开的话，签一份免押合同会收到一个月租金的押金。
        """
        from app import leasing

        house_no = self.publish(rent_price=2800,
                                deposit_months=0).json()["house"]["house_no"]
        session = db.SessionLocal()
        try:
            house = session.query(House).filter_by(house_no=house_no).one()
            self.assertEqual(float(house.deposit), 0.0)
            self.assertEqual(
                float(leasing.resolve_deposit(house,
                                              default_deposit_months=1)), 0.0)
        finally:
            session.close()


class TestVerificationFields(FacetCase):
    """房源核验（贝壳的「房源验真编号」对应物）。

    《待完善清单》D-4 判断得对：不动产登记数据接口拿不到。
    但核验的**状态机和公开编号是自己能做的**——核验方从登记接口
    换成管理员人工看证件照片，状态机一模一样。
    """

    def test_new_house_starts_unverified(self):
        house = self.publish().json()["house"]
        self.assertEqual(house["verify_status"], "unverified")
        self.assertEqual(house["verify_no"], "")

    def test_verification_fields_are_public(self):
        """核验信息必须对未登录用户可见——它存在的意义就是给人看。
        （用已上架的那套：刚发布的还在待审，本来就不对外）"""
        body = self.client.get("/api/houses/DL0001").json()
        for key in ("verify_status", "verify_no", "verified_at",
                    "maintained_at"):
            self.assertIn(key, body)

    def _verified(self):
        from tests.base import make_admin
        house_no = self.publish().json()["house"]["house_no"]
        admin = make_admin(self.client)
        self.client.post("/api/houses/%s/verify-request" % house_no, headers=self.landlord)
        decided = self.client.patch("/api/houses/%s/verify" % house_no, headers=admin,
                                    json={"approved": True})
        assert decided.status_code == 200, decided.text
        return house_no

    def test_changing_the_address_voids_the_verification(self):
        """核验认的是这个地址、这么大的房子。改了地址还挂着「已核验」，
        公开的核验编号就在替一套没核验过的房子背书。"""
        house_no = self._verified()
        self.assertEqual(self.fetch(house_no)["verify_status"], "verified")
        self.client.put("/api/houses/%s" % house_no, headers=self.landlord,
                        json={"address": "中山区另一条街 8 号"})
        house = self.fetch(house_no)
        self.assertEqual(house["verify_status"], "unverified")
        self.assertEqual(house["verify_no"], "")

    def test_editing_the_description_keeps_the_verification(self):
        house_no = self._verified()
        self.client.put("/api/houses/%s" % house_no, headers=self.landlord,
                        json={"description": "新换了窗帘，采光很好"})
        self.assertEqual(self.fetch(house_no)["verify_status"], "verified")

    def test_landlord_cannot_self_verify(self):
        """房东不能把自己的房源标成「已核验」。

        核验是平台对房源做的背书，房东能自己盖这个章，
        这个章就一文不值了。
        """
        house_no = self.publish().json()["house"]["house_no"]
        self.client.put("/api/houses/%s" % house_no, headers=self.landlord,
                        json={"verify_status": "verified",
                              "verify_no": "DL26000000"})
        house = self.fetch(house_no)
        self.assertEqual(house["verify_status"], "unverified")
        self.assertEqual(house["verify_no"], "")


class TestImageMetadata(ApiTestCase):
    """相册元数据。图片哈希是查「盗图 / 同一张图发十套房」的唯一抓手。"""

    def test_image_dict_carries_metadata(self):
        from app.models import HouseImage

        session = db.SessionLocal()
        try:
            house = session.query(House).filter_by(house_no="DL0001").one()
            image = HouseImage(house_id=house.id, url="/static/a.jpg",
                               width=1200, height=800, file_size=204800,
                               hash="abc123", is_cover=True)
            session.add(image)
            session.commit()
            data = image.to_dict()
        finally:
            session.close()

        self.assertEqual(data["width"], 1200)
        self.assertEqual(data["height"], 800)
        # 事后审核：先展示，被举报或被发现再下架。
        # 默认 pending 会让房东发完图看不到自己的房子
        self.assertEqual(data["audit_status"], "approved")


class TestNewTablesExist(ApiTestCase):
    """三张新表建得出来、字段可用。

    保存搜索、举报、找回密码的界面都还没写，但表要赶在评审前定稿。
    这条测试守的是「表结构没被后来的改动带跑」。
    """

    def test_saved_search_round_trip(self):
        from app.models import SavedSearch, User

        session = db.SessionLocal()
        try:
            user = session.query(User).filter_by(username="tenant1").one()
            row = SavedSearch(user_id=user.id, name="上班近的两室",
                              conditions='{"district": "中山区", "elevator": true}')
            session.add(row)
            session.commit()
            self.assertEqual(row.condition_dict()["district"], "中山区")
            self.assertEqual(row.notified(), set())
            self.assertTrue(row.to_dict()["notify_on"])
        finally:
            session.close()

    def test_saved_search_survives_broken_json(self):
        """条件字段是自由文本，坏数据不该让整个列表接口崩掉。"""
        from app.models import SavedSearch, User

        session = db.SessionLocal()
        try:
            user = session.query(User).filter_by(username="tenant1").one()
            row = SavedSearch(user_id=user.id, conditions="{不是 JSON")
            session.add(row)
            session.commit()
            self.assertEqual(row.condition_dict(), {})
        finally:
            session.close()

    def test_house_report_round_trip(self):
        from app.models import HouseReport, User

        session = db.SessionLocal()
        try:
            user = session.query(User).filter_by(username="tenant1").one()
            house = session.query(House).filter_by(house_no="DL0001").one()
            row = HouseReport(report_no="JB260903001", reporter_id=user.id,
                              house_id=house.id, kind="fake",
                              description="打电话过去说这套早租出去了")
            session.add(row)
            session.commit()
            data = row.to_dict()
        finally:
            session.close()

        self.assertEqual(data["kind_label"], "虚假房源")
        self.assertEqual(data["status"], "pending")
        self.assertEqual(data["house_no"], "DL0001")

    def test_password_reset_expiry(self):
        from datetime import timedelta
        from app.models import PasswordReset, User

        session = db.SessionLocal()
        try:
            user = session.query(User).filter_by(username="tenant1").one()
            live = PasswordReset(user_id=user.id, token_hash="a" * 64,
                                 expires_at=datetime.now() + timedelta(hours=1))
            dead = PasswordReset(user_id=user.id, token_hash="b" * 64,
                                 expires_at=datetime.now() - timedelta(hours=1))
            used = PasswordReset(user_id=user.id, token_hash="c" * 64,
                                 expires_at=datetime.now() + timedelta(hours=1),
                                 used_at=datetime.now())
            session.add_all([live, dead, used])
            session.commit()

            self.assertTrue(live.is_usable())
            self.assertFalse(dead.is_usable())
            # 用过的链接不能再用一次，而且要和「从来不存在」区分开
            self.assertFalse(used.is_usable())
        finally:
            session.close()


class TestCardPayloadStaysInSync(FacetCase):
    """列表载荷必须是详情载荷的**子集**，而且同名字段取值一致。

    `to_card_dict()` 存在的理由是性能：K-04/K-07 往 `to_dict()` 里加了
    二十来个字段之后，单套房的 JSON 长到 1.4KB，一页 12 套 15KB，
    而列表卡片一个都用不到房源描述、整个相册数组和费用明细拆解。
    实测精简后列表接口在 100 并发下 p50 从 1486ms 降到 1024ms。

    但「为了快另写一份序列化」是会出事的：加了字段只改一处，
    列表和详情从此对同一套房给出不一样的信息，而这种不一致
    在页面上极难发现——两处本来就长得不一样。

    所以约定是「挑字段」而不是「另写一份」，这组用例守的就是这个约定。
    """

    def test_card_is_a_subset_of_detail(self):
        session = db.SessionLocal()
        try:
            house = session.query(House).filter_by(house_no="DL0001").one()
            card = house.to_card_dict()
            full = house.to_dict()
        finally:
            session.close()

        extra = set(card) - set(full)
        self.assertEqual(extra, set(),
                         "列表返回了详情没有的字段：%s" % extra)

    def test_same_keys_carry_same_values(self):
        session = db.SessionLocal()
        try:
            house = session.query(House).filter_by(house_no="DL0001").one()
            card = house.to_card_dict()
            full = house.to_dict()
        finally:
            session.close()

        for key, value in card.items():
            self.assertEqual(value, full[key],
                             "字段 %s 在列表和详情里不一致" % key)

    def test_card_keeps_what_the_card_renders(self):
        """HouseCard 与 CompareBar 真正读到的键，一个都不能少。

        少一个的表现是卡片上某一项静悄悄变成空白——不报错，
        所以只能靠测试兜住。
        """
        session = db.SessionLocal()
        try:
            card = session.query(House).filter_by(
                house_no="DL0001").one().to_card_dict()
        finally:
            session.close()

        for key in ("house_no", "title", "district", "community", "floor",
                    "rent_price", "deposit", "area", "layout", "rent_type",
                    "orientation", "tags", "cover_image", "rating",
                    "elevator", "floor_level", "pay_plan",
                    "verify_status", "maintained_at"):
            self.assertIn(key, card)

    def test_card_drops_the_expensive_detail_only_fields(self):
        """详情页才用得上的大字段不该出现在列表里。

        这条会在有人「顺手」把字段加回 CARD_FIELDS 时变红，
        提醒他这里是全站最热的一条路径。
        """
        session = db.SessionLocal()
        try:
            card = session.query(House).filter_by(
                house_no="DL0001").one().to_card_dict()
        finally:
            session.close()

        for key in ("description", "images", "fee_detail"):
            self.assertNotIn(key, card)

    def test_list_endpoint_uses_the_card_payload(self):
        body = self.client.get("/api/houses", params={"limit": 1}).json()
        self.assertTrue(body["list"])
        row = body["list"][0]
        self.assertNotIn("description", row)
        self.assertNotIn("fee_detail", row)
        self.assertIn("floor_level", row)

    def test_detail_endpoint_still_returns_everything(self):
        """详情页不受影响——精简的只有列表。"""
        row = self.client.get("/api/houses/DL0001").json()
        for key in ("description", "images", "fee_detail", "verify_no",
                    "available_from", "min_months", "service_fee"):
            self.assertIn(key, row)
