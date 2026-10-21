"""系统配置测试（M-09）。

这批用例的重点不是「能不能存进去」，而是**改了以后业务行为真的变了**。
存了值却没人读的配置页是假功能，所以每一项配置都有一条对应的行为用例。
"""

from datetime import datetime, timedelta

from tests.base import ApiTestCase, auth_header, make_admin


def future(days=3, hour=10):
    return (datetime.now() + timedelta(days=days)).replace(
        hour=hour, minute=0, second=0, microsecond=0)


class ConfigCase(ApiTestCase):

    def setUp(self):
        super().setUp()
        self.admin = make_admin(self.client)
        self.tenant = auth_header(self.client, "tenant1", "test123")
        self.landlord = auth_header(self.client, "landlord1", "test123")

    def set_config(self, **items):
        return self.client.patch("/api/admin/config", headers=self.admin,
                                 json={"items": {k: str(v)
                                                 for k, v in items.items()}})


class TestConfigCrud(ConfigCase):

    def test_defaults_exist(self):
        body = self.client.get("/api/admin/config", headers=self.admin).json()
        keys = {row["key"] for row in body["list"]}
        self.assertIn("viewing_minutes", keys)
        self.assertIn("house_auto_approve", keys)

    def test_update_and_read_back(self):
        self.assertEqual(self.set_config(viewing_minutes=30).status_code, 200)
        body = self.client.get("/api/admin/config", headers=self.admin).json()
        row = next(r for r in body["list"] if r["key"] == "viewing_minutes")
        self.assertEqual(row["value"], "30")

    def test_bool_accepts_common_spellings(self):
        for text in ("true", "1", "on", "yes"):
            self.assertEqual(self.set_config(house_auto_approve=text).status_code,
                             200, text)

    def test_reject_non_integer(self):
        r = self.set_config(viewing_minutes="一小时")
        self.assertEqual(r.status_code, 400)

    def test_reject_zero_and_negative(self):
        """看房时段填 0 会让冲突窗口塌成一个点，预约上限填 0 等于把功能关掉。"""
        self.assertEqual(self.set_config(viewing_minutes=0).status_code, 400)
        self.assertEqual(
            self.set_config(appointment_daily_limit=-1).status_code, 400)

    def test_reject_values_that_are_typos(self):
        """少打一个小数点：看房时段 600 分钟、押金 99 个月——都会原样生效。"""
        self.assertEqual(self.set_config(viewing_minutes=600).status_code, 400)
        self.assertEqual(self.set_config(deposit_months=99).status_code, 400)
        self.assertEqual(self.set_config(viewing_minutes=240).status_code, 200)

    def test_reject_unknown_key(self):
        r = self.set_config(**{"drop_all_tables": "1"})
        self.assertEqual(r.status_code, 400)

    def test_partial_failure_writes_nothing(self):
        """半套配置比旧配置更危险：管理员多半以为两项都生效了。"""
        self.set_config(viewing_minutes=45)
        r = self.client.patch("/api/admin/config", headers=self.admin, json={
            "items": {"viewing_minutes": "90", "appointment_daily_limit": "abc"}})
        self.assertEqual(r.status_code, 400)
        body = self.client.get("/api/admin/config", headers=self.admin).json()
        row = next(r for r in body["list"] if r["key"] == "viewing_minutes")
        self.assertEqual(row["value"], "45", "整批都不该写入")

    def test_defaults_do_not_overwrite_changes(self):
        """补默认值只能插不能覆盖，否则重启一次就把管理员的修改打回去了。"""
        self.set_config(viewing_minutes=15)
        import app.db as db
        from app.config_store import ensure_defaults
        session = db.SessionLocal()
        try:
            ensure_defaults(session)
        finally:
            session.close()
        body = self.client.get("/api/admin/config", headers=self.admin).json()
        row = next(r for r in body["list"] if r["key"] == "viewing_minutes")
        self.assertEqual(row["value"], "15")


class TestConfigAccess(ConfigCase):

    def test_public_config_needs_no_login(self):
        body = self.client.get("/api/config").json()
        self.assertIn("site_name", body)

    def test_public_config_hides_internal_items(self):
        """公开范围由数据标记决定，前端换个 key 也读不走内部配置。"""
        body = self.client.get("/api/config").json()
        self.assertNotIn("house_auto_approve", body)
        self.assertNotIn("appointment_daily_limit", body)

    def test_admin_list_denied_for_landlord(self):
        self.assertEqual(
            self.client.get("/api/admin/config", headers=self.landlord).status_code,
            403)

    def test_update_denied_for_tenant(self):
        r = self.client.patch("/api/admin/config", headers=self.tenant,
                              json={"items": {"viewing_minutes": "10"}})
        self.assertEqual(r.status_code, 403)

    def test_update_denied_for_anonymous(self):
        r = self.client.patch("/api/admin/config",
                              json={"items": {"viewing_minutes": "10"}})
        self.assertEqual(r.status_code, 401)


class TestConfigTakesEffect(ConfigCase):
    """配置必须真的生效——这才是这个功能的价值所在。"""

    def book(self, moment, headers=None):
        return self.client.post("/api/appointments",
                                headers=headers or self.tenant,
                                json={"house_no": "DL0001",
                                      "viewing_time": moment.isoformat(),
                                      "contact_name": "租客甲",
                                      "contact_phone": "13900005678"})

    def test_viewing_window_widens(self):
        """把时段调长后，原本不冲突的两个时间点会开始互相冲突。"""
        self.set_config(viewing_minutes=30, appointment_daily_limit=20)
        base = future(3, 10)
        self.assertEqual(self.book(base).status_code, 200)
        # 相隔 45 分钟，30 分钟窗口下不冲突
        self.assertEqual(
            self.book(base + timedelta(minutes=45)).status_code, 200)

        self.set_config(viewing_minutes=120)
        # 同样相隔 45 分钟，两小时窗口下就冲突了
        self.assertEqual(
            self.book(base + timedelta(days=1)).status_code, 200)
        self.assertEqual(
            self.book(base + timedelta(days=1, minutes=45)).status_code, 409)

    def test_daily_limit_blocks_further_bookings(self):
        self.set_config(appointment_daily_limit=2, viewing_minutes=30)
        base = future(4, 9)
        self.assertEqual(self.book(base).status_code, 200)
        self.assertEqual(self.book(base + timedelta(hours=2)).status_code, 200)
        third = self.book(base + timedelta(hours=4))
        self.assertEqual(third.status_code, 429)
        self.assertIn("上限", third.json()["detail"])

    def test_auto_approve_publishes_directly(self):
        payload = {"title": "免审房源", "district": "中山区", "rent_price": 3000,
                   "area": 80, "layout": "两室一厅"}

        r = self.client.post("/api/houses", headers=self.landlord, json=payload)
        self.assertEqual(r.json()["house"]["status"], "pending")

        self.set_config(house_auto_approve="true")
        r = self.client.post("/api/houses", headers=self.landlord, json=payload)
        self.assertEqual(r.json()["house"]["status"], "available")

    def test_default_deposit_months_applies(self):
        self.set_config(deposit_months=3)
        r = self.client.post("/api/contracts", headers=self.landlord, json={
            "house_no": "DL0001", "tenant_username": "tenant1",
            "start_date": future(1).isoformat(), "months": 12})
        self.assertEqual(r.status_code, 200)
        contract = r.json()["contract"]
        self.assertEqual(float(contract["deposit"]), 2800 * 3)

    def test_explicit_deposit_months_wins(self):
        """0 是合法取值（免押），不能被当成「没填」而套上默认值。"""
        self.set_config(deposit_months=3)
        r = self.client.post("/api/contracts", headers=self.landlord, json={
            "house_no": "DL0001", "tenant_username": "tenant1",
            "start_date": future(1).isoformat(), "months": 12,
            "deposit_months": 0})
        self.assertEqual(float(r.json()["contract"]["deposit"]), 0)


class TestBadValueDoesNotBreakApi(ConfigCase):
    """配置值来自管理员手输，一个笔误不能把接口打成 500。"""

    def test_corrupted_value_falls_back(self):
        import app.db as db
        from app.config_store import get_int, set_value
        session = db.SessionLocal()
        try:
            set_value(session, "viewing_minutes", "六十")
            session.commit()
            self.assertEqual(get_int(session, "viewing_minutes"), 60)
        finally:
            session.close()

        r = self.client.post("/api/appointments", headers=self.tenant, json={
            "house_no": "DL0001", "viewing_time": future(5).isoformat(),
            "contact_name": "租客甲", "contact_phone": "13900005678"})
        self.assertEqual(r.status_code, 200)
