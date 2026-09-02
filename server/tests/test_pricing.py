"""定价建议测试（L-10）。

重点在两处「不硬给」：样本不足时如实说明，以及比较基准里不能混进
已下架的房源——那些往往正是因为定价不合理才下架的。
"""

from tests.base import ApiTestCase, auth_header, make_admin
from app import pricing


def house(district="中山区", layout="两室一厅", price=2800, area=70):
    return {"district": district, "layout": layout,
            "rent_price": price, "area": area}


class TestSuggest(ApiTestCase):

    def test_needs_area(self):
        r = pricing.suggest([house()] * 5, "中山区", "两室一厅", 0)
        self.assertFalse(r["ok"])
        self.assertIn("面积", r["message"])

    def test_basic_band(self):
        rows = [house(price=2800, area=70)] * 5
        r = pricing.suggest(rows, "中山区", "两室一厅", 70)
        self.assertTrue(r["ok"])
        self.assertLess(r["low"], r["median"])
        self.assertGreater(r["high"], r["median"])
        self.assertAlmostEqual(r["median"], 2800, delta=100)

    def test_scales_by_area(self):
        """同样是「中山区两室一厅」，60 平和 110 平不是一回事。"""
        rows = [house(price=2800, area=70)] * 5
        small = pricing.suggest(rows, "中山区", "两室一厅", 50)
        large = pricing.suggest(rows, "中山区", "两室一厅", 100)
        self.assertLess(small["median"], large["median"])

    def test_median_resists_one_outlier(self):
        """一套定价离谱的房源就能把平均值拉歪，中位数不会。"""
        rows = [house(price=2800, area=70) for _ in range(5)]
        rows.append(house(price=99000, area=70))
        r = pricing.suggest(rows, "中山区", "两室一厅", 70)
        self.assertLess(r["median"], 3500, "中位数被离群值带跑了")

    def test_falls_back_to_district_when_layout_sample_thin(self):
        rows = [house(layout="一室") for _ in range(6)]
        rows.append(house(layout="五室两厅", price=9000, area=200))
        r = pricing.suggest(rows, "中山区", "五室两厅", 200)
        self.assertTrue(r["ok"])
        self.assertEqual(r["basis"], "中山区", "应退回按区域比较")

    def test_refuses_when_even_district_is_thin(self):
        """一个看起来精确、实则瞎编的数字比不给更糟。"""
        r = pricing.suggest([house(district="旅顺口区")], "中山区",
                            "两室一厅", 70)
        self.assertFalse(r["ok"])
        self.assertIn("太少", r["message"])

    def test_ignores_broken_rows(self):
        rows = [house() for _ in range(4)]
        rows.append({"district": "中山区", "layout": "两室一厅",
                     "rent_price": None, "area": "不是数字"})
        r = pricing.suggest(rows, "中山区", "两室一厅", 70)
        self.assertTrue(r["ok"])
        self.assertEqual(r["sample"], 4)

    def test_message_shows_its_basis(self):
        """建议必须说清是拿什么比出来的，否则房东没法判断可信度。"""
        r = pricing.suggest([house() for _ in range(5)],
                            "中山区", "两室一厅", 70)
        self.assertIn("中山区", r["message"])
        self.assertIn("5 套", r["message"])

    def test_prices_are_rounded(self):
        r = pricing.suggest([house(price=2837, area=71) for _ in range(5)],
                            "中山区", "两室一厅", 71)
        for key in ("low", "median", "high"):
            self.assertEqual(r[key] % 50, 0, key)


class TestPricingApi(ApiTestCase):

    def setUp(self):
        super().setUp()
        self.landlord = auth_header(self.client, "landlord1", "test123")
        self.tenant = auth_header(self.client, "tenant1", "test123")
        self.admin = make_admin(self.client)

    def ask(self, headers=None, **params):
        return self.client.get("/api/pricing/suggest",
                               headers=headers or self.landlord,
                               params={"district": "中山区",
                                       "layout": "两室一厅",
                                       "area": 70, **params})

    def test_landlord_can_ask(self):
        self.assertEqual(self.ask().status_code, 200)

    def test_tenant_denied(self):
        """这是发布房源时的辅助，对租客没有意义，还会暴露定价口径。"""
        self.assertEqual(self.ask(headers=self.tenant).status_code, 403)

    def test_anonymous_denied(self):
        r = self.client.get("/api/pricing/suggest",
                            params={"district": "中山区", "area": 70})
        self.assertEqual(r.status_code, 401)

    def test_offline_houses_are_not_used_as_reference(self):
        """已下架的房源往往正是因为定价不合理，
        拿它们当参照就是拿反面教材做标准。"""
        import app.db as db
        from app.models import House
        session = db.SessionLocal()
        try:
            for i in range(6):
                session.add(House(
                    house_no="OFF%03d" % i, title="下架房源",
                    landlord_id=1, district="西岗区", community="测试",
                    rent_price=99000, area=70, layout="两室一厅",
                    rent_type="整租", status="offline"))
            session.commit()
        finally:
            session.close()
        body = self.ask(district="西岗区").json()
        self.assertFalse(body["ok"], "下架房源不该被当成比较基准")
