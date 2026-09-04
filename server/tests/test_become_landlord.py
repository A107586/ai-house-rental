"""租客升级为房东（K-13）。

`奇思妙想.docx` 第 2 条。改造前要演示完整链路得开两个账号来回切换。

这份测试的重心是**「升级不能丢数据」**——那是用户真正担心的事，
也是这个功能最容易出问题的地方。数据其实全挂在 `user_id` 上、
与 role 无关，但「本来就不会丢」和「测过了不会丢」是两回事：
将来有人为了「清理身份残留」加一段删除逻辑，这组用例会立刻拦住他。
"""

from datetime import datetime, timedelta

from tests.base import ApiTestCase, auth_header, make_admin
import app.db as db
from app.models import BrowsingHistory, Favorite, House, User


class UpgradeCase(ApiTestCase):

    def setUp(self):
        super().setUp()
        self.tenant = auth_header(self.client, "tenant1", "test123")

    def upgrade(self, headers=None):
        return self.client.post("/api/auth/become-landlord",
                                headers=headers or self.tenant)

    def eligibility(self, headers=None):
        return self.client.get("/api/auth/landlord-eligibility",
                               headers=headers or self.tenant).json()

    def set_profile(self, **fields):
        session = db.SessionLocal()
        try:
            user = session.query(User).filter_by(username="tenant1").one()
            for key, value in fields.items():
                setattr(user, key, value)
            session.commit()
        finally:
            session.close()


class TestUpgrade(UpgradeCase):

    def test_tenant_becomes_landlord(self):
        response = self.upgrade()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["user"]["role"], "landlord")

    def test_can_publish_after_upgrading(self):
        """升级的意义就在这里：之前发不了房源，现在发得了。"""
        before = self.client.post("/api/houses", headers=self.tenant, json={
            "title": "升级前的房源", "district": "中山区",
            "rent_price": 2600, "area": 70})
        self.assertEqual(before.status_code, 403)

        self.upgrade()

        after = self.client.post("/api/houses", headers=self.tenant, json={
            "title": "升级后的房源", "district": "中山区",
            "rent_price": 2600, "area": 70})
        self.assertEqual(after.status_code, 200, after.text)

    def test_token_still_works(self):
        """**不吊销令牌。** 角色变了但还是同一个人。

        踢下线之后他得重新登录才能看到「发布房源」，白白多一步，
        而且用户会以为出错了。
        """
        self.upgrade()
        self.assertEqual(
            self.client.get("/api/auth/me", headers=self.tenant).status_code,
            200)

    def test_already_landlord_is_rejected(self):
        landlord = auth_header(self.client, "landlord1", "test123")
        response = self.upgrade(landlord)
        self.assertEqual(response.status_code, 409)

    def test_admin_is_rejected(self):
        admin = make_admin(self.client)
        self.assertEqual(self.upgrade(admin).status_code, 400)

    def test_login_required(self):
        self.assertEqual(
            self.client.post("/api/auth/become-landlord").status_code, 401)


class TestPrerequisites(UpgradeCase):
    """先实名再当房东。

    合同上的甲方必须是真实姓名（K-03 在签约那一步已经拦了一道）。
    在这里再拦一次不是重复：等到发起签约才发现签不了，
    房东已经把房源发布、约看房都做完了。
    """

    def test_needs_real_name(self):
        self.set_profile(real_name="")
        response = self.upgrade()
        self.assertEqual(response.status_code, 400)
        self.assertIn("实名", response.json()["detail"])

    def test_needs_phone(self):
        self.set_profile(phone="")
        response = self.upgrade()
        self.assertEqual(response.status_code, 400)
        self.assertIn("手机号", response.json()["detail"])

    def test_eligibility_says_what_is_missing(self):
        """让用户点下去才被告知缺什么，是多余的一次往返。"""
        self.set_profile(real_name="", phone="")
        body = self.eligibility()
        self.assertFalse(body["can_upgrade"])
        self.assertIn("真实姓名", body["missing"])
        self.assertIn("手机号", body["missing"])

    def test_eligibility_when_ready(self):
        body = self.eligibility()
        self.assertTrue(body["can_upgrade"])
        self.assertEqual(body["missing"], [])

    def test_eligibility_for_existing_landlord(self):
        landlord = auth_header(self.client, "landlord1", "test123")
        body = self.eligibility(landlord)
        self.assertTrue(body["already_landlord"])
        self.assertFalse(body["can_upgrade"])


class TestNothingIsLost(UpgradeCase):
    """**升级不能丢数据。**

    收藏、足迹、租约、账单全挂在 `user_id` 上，与 role 无关，
    所以本来就不会丢。但「本来不会」和「测过不会」是两回事——
    将来有人为了「清理身份残留」加一段删除逻辑，这组用例会拦住他。
    """

    def seed_activity(self):
        self.client.post("/api/favorites/DL0001", headers=self.tenant)
        # 看一眼详情页会记一条足迹
        self.client.get("/api/houses/DL0002", headers=self.tenant)

        session = db.SessionLocal()
        try:
            tenant = session.query(User).filter_by(username="tenant1").one()
            house = session.query(House).filter_by(house_no="DL0003").one()
            session.add(BrowsingHistory(user_id=tenant.id, house_id=house.id))
            session.commit()
        finally:
            session.close()

    def counts(self):
        session = db.SessionLocal()
        try:
            tenant = session.query(User).filter_by(username="tenant1").one()
            return (
                session.query(Favorite).filter_by(user_id=tenant.id).count(),
                session.query(BrowsingHistory)
                .filter_by(user_id=tenant.id).count(),
            )
        finally:
            session.close()

    def test_favorites_and_history_survive(self):
        self.seed_activity()
        before = self.counts()
        self.assertGreater(before[0], 0)
        self.assertGreater(before[1], 0)

        self.upgrade()

        self.assertEqual(self.counts(), before)

    def test_favorites_still_readable_after_upgrade(self):
        """不只是行还在，接口也要照常读得到。"""
        self.client.post("/api/favorites/DL0001", headers=self.tenant)
        self.upgrade()
        body = self.client.get("/api/favorites", headers=self.tenant).json()
        self.assertEqual([h["house_no"] for h in body["list"]], ["DL0001"])

    def test_existing_lease_survives(self):
        """升级前签的租约，升级后仍然是他的租约。"""
        landlord = auth_header(self.client, "landlord1", "test123")
        start = (datetime.now() + timedelta(days=7)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        signed = self.client.post("/api/contracts", headers=landlord, json={
            "house_no": "DL0001", "tenant_username": "tenant1",
            "start_date": start.isoformat(), "months": 12})
        self.assertEqual(signed.status_code, 200, signed.text)
        contract_no = signed.json()["contract"]["contract_no"]

        self.upgrade()

        mine = self.client.get("/api/contracts/mine", headers=self.tenant).json()
        self.assertIn(contract_no, [c["contract_no"] for c in mine["list"]])


class TestNoDowngrade(UpgradeCase):
    """不提供反向降级。

    名下还有在租房源、生效合同的房东降回租客，那些房源就成了
    没有主人的数据。真要退出出租业务，该走的是「下架全部房源 +
    结清合同」，那是另一件事，不是一个开关。
    """

    def test_there_is_no_downgrade_endpoint(self):
        # 404（没这条路由）和 405（路径被别的方法占了）都算「没有这个接口」
        for path in ("/api/auth/become-tenant", "/api/auth/downgrade"):
            self.assertIn(
                self.client.post(path, headers=self.tenant).status_code,
                (404, 405))
