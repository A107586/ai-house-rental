"""保存搜索与上新提醒（K-09）。

**这份测试最要紧的一条是「匹配和列表页说的是同一件事」。**
条件构造只有 `app/house_query.py` 一处实现，两边共用；
如果哪天有人图省事在匹配里另写一份判断，`TestMatchingAgreesWithListing`
会立刻红——那种分叉造成的 bug（列表筛得到、提醒不发，或者反过来）
在页面上几乎查不出来。
"""

import json
from datetime import datetime
import re

from tests.base import ApiTestCase, auth_header, make_admin
import app.db as db
from app.models import House, SavedSearch, User


class SavedSearchCase(ApiTestCase):

    def setUp(self):
        super().setUp()
        self.tenant = auth_header(self.client, "tenant1", "test123")
        self.landlord = auth_header(self.client, "landlord1", "test123")
        self.admin = make_admin(self.client)

    # 哨兵：`{}` 是一个**有意义的取值**（「什么都不限」），
    # 用 `or` 兜默认值会把它悄悄换成默认条件，那条用例就白测了
    _DEFAULT = object()

    def save(self, conditions=_DEFAULT, **extra):
        if conditions is self._DEFAULT:
            conditions = {"district": "中山区", "elevator": "true"}
        body = {"conditions": conditions}
        body.update(extra)
        return self.client.post("/api/saved-searches",
                                headers=self.tenant, json=body)

    def publish(self, **fields):
        """房东发布一套房，管理员审核通过——走的是真实的上架路径。"""
        body = {"title": "新上房源两室", "district": "中山区",
                "community": "青泥洼桥", "rent_price": 2600, "area": 70,
                "layout": "两室一厅", "elevator": True}
        body.update(fields)
        house_no = self.client.post("/api/houses", headers=self.landlord,
                                    json=body).json()["house"]["house_no"]
        self.client.patch("/api/houses/%s/review" % house_no,
                          headers=self.admin, json={"approved": True})
        return house_no

    def notices(self):
        body = self.client.get("/api/messages", headers=self.tenant).json()
        return [m for m in body["list"] if "有新房源符合" in m["title"]]


class TestCrud(SavedSearchCase):

    def test_save_and_list(self):
        response = self.save()
        self.assertEqual(response.status_code, 200, response.text)
        rows = self.client.get("/api/saved-searches",
                               headers=self.tenant).json()
        self.assertEqual(rows["total"], 1)

    def test_name_defaults_to_a_readable_phrase(self):
        """不起名就用条件拼一句人话。

        「未命名搜索 3」对用户毫无意义——他需要一眼认出这是哪一组条件。
        """
        row = self.save({"district": "中山区", "rent_price_max": 3000,
                         "elevator": "true"}).json()["saved_search"]
        self.assertIn("中山区", row["name"])
        self.assertIn("3000 元以内", row["name"])
        self.assertIn("有电梯", row["name"])

    def test_empty_conditions_rejected(self):
        """一条什么都不限的搜索会匹配上每一套新房，等于给用户开了轰炸机。"""
        self.assertEqual(self.save({}).status_code, 400)

    def test_unknown_keys_are_dropped(self):
        """条件是会被原样喂回查询的 JSON，未知键一律丢掉。"""
        row = self.save({"district": "中山区", "drop_table": "houses"}).json()
        self.assertNotIn("drop_table", row["saved_search"]["conditions"])
        self.assertEqual(row["saved_search"]["conditions"]["district"], "中山区")

    def test_rename_and_toggle(self):
        row = self.save().json()["saved_search"]
        response = self.client.patch(
            "/api/saved-searches/%d" % row["id"], headers=self.tenant,
            json={"name": "上班近的两室", "notify_on": False})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["saved_search"]["name"], "上班近的两室")
        self.assertFalse(response.json()["saved_search"]["notify_on"])

    def test_delete(self):
        row = self.save().json()["saved_search"]
        self.assertEqual(
            self.client.delete("/api/saved-searches/%d" % row["id"],
                               headers=self.tenant).status_code, 200)
        self.assertEqual(
            self.client.get("/api/saved-searches",
                            headers=self.tenant).json()["total"], 0)

    def test_cannot_touch_other_users_search(self):
        row = self.save().json()["saved_search"]
        for call in (
            lambda: self.client.patch("/api/saved-searches/%d" % row["id"],
                                      headers=self.landlord,
                                      json={"name": "改掉"}),
            lambda: self.client.delete("/api/saved-searches/%d" % row["id"],
                                       headers=self.landlord),
        ):
            self.assertEqual(call().status_code, 404)

    def test_login_required(self):
        self.assertEqual(
            self.client.post("/api/saved-searches",
                             json={"conditions": {"district": "中山区"}}
                             ).status_code, 401)


class TestNewHouseNotice(SavedSearchCase):

    def test_matching_house_triggers_notice(self):
        self.save({"district": "中山区", "elevator": "true"})
        house_no = self.publish()
        notices = self.notices()
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["link_no"], house_no)

    def test_non_matching_house_is_silent(self):
        self.save({"district": "中山区", "elevator": "true"})
        self.publish(elevator=False)
        self.assertEqual(self.notices(), [])

    def test_pending_house_does_not_notify(self):
        """上架那一刻才是「上新」。

        发布时房源还是 pending、对外不可见，那时通知等于让用户
        去看一套他点不开的房。
        """
        self.save({"district": "中山区", "elevator": "true"})
        self.client.post("/api/houses", headers=self.landlord, json={
            "title": "待审房源两室", "district": "中山区", "rent_price": 2600,
            "area": 70, "layout": "两室一厅", "elevator": True})
        self.assertEqual(self.notices(), [])

    def test_same_house_notifies_only_once(self):
        """房源下架再上架不该重发。

        不去重的话，用户第二天就把提醒关了——而提醒一旦关掉，
        这个功能对他就等于不存在。
        """
        self.save({"district": "中山区", "elevator": "true"})
        house_no = self.publish()
        self.client.patch("/api/houses/%s/status?status=offline" % house_no,
                          headers=self.landlord)
        back = self.client.patch("/api/houses/%s/status?status=available" % house_no,
                                 headers=self.landlord)
        self.assertEqual(back.json()["house"]["status"], "available")
        self.assertEqual(len(self.notices()), 1)

    def test_relisting_notifies_searches_saved_meanwhile(self):
        """重新上架也是「有新房」。原先只有首次审核通过才提醒，
        下架那几天才存下条件的人，房子回来了也不知道。"""
        house_no = self.publish()
        self.client.patch("/api/houses/%s/status?status=offline" % house_no,
                          headers=self.landlord)
        self.save({"district": "中山区", "elevator": "true"})
        self.client.patch("/api/houses/%s/status?status=available" % house_no,
                          headers=self.landlord)
        self.assertEqual([n["link_no"] for n in self.notices()], [house_no])

    def test_house_freed_by_a_lease_end_notifies(self):
        """退租后房子空出来，对按条件等房的人来说就是一套新房。"""
        from tests.base import end_lease, make_resident
        from app.models import Contract
        self.client.post("/api/auth/register", json={
            "username": "mover", "password": "test123", "real_name": "搬家的"})
        session = db.SessionLocal()
        try:
            session.query(House).filter_by(house_no="DL0003").one().approved_at =                 datetime.now()
            session.commit()
        finally:
            session.close()
        make_resident("mover", "DL0003")
        session = db.SessionLocal()
        try:
            contract_no = session.query(Contract).filter_by(status="active").one().contract_no
        finally:
            session.close()

        self.save({"district": "中山区"})
        ended = end_lease(self.client, contract_no, self.landlord,
                          auth_header(self.client, "mover", "test123"))
        self.assertEqual(ended.status_code, 200, ended.text)
        self.assertEqual([n["link_no"] for n in self.notices()], ["DL0003"])

    def test_notify_off_stays_silent_but_keeps_conditions(self):
        row = self.save().json()["saved_search"]
        self.client.patch("/api/saved-searches/%d" % row["id"],
                          headers=self.tenant, json={"notify_on": False})
        self.publish()
        self.assertEqual(self.notices(), [])
        # 条件还在，随时可以重新打开
        rows = self.client.get("/api/saved-searches",
                               headers=self.tenant).json()
        self.assertEqual(rows["total"], 1)

    def test_landlord_is_not_notified_about_own_house(self):
        """房东刚发布完，再收到一条「有新房源符合你的条件」很荒唐。"""
        self.client.post("/api/saved-searches", headers=self.landlord,
                         json={"conditions": {"district": "中山区"}})
        self.publish()
        body = self.client.get("/api/messages", headers=self.landlord).json()
        self.assertEqual(
            [m for m in body["list"] if "有新房源符合" in m["title"]], [])

    def test_notice_links_back_to_the_house(self):
        """点提醒要能直接打开那套房，否则用户还得自己去列表里找。"""
        self.save({"district": "中山区"})
        house_no = self.publish()
        notice = self.notices()[0]
        self.assertEqual(notice["link_no"], house_no)
        self.assertEqual(
            self.client.get("/api/houses/%s" % house_no).status_code, 200)


class TestMatchingAgreesWithListing(SavedSearchCase):
    """匹配和列表页必须说同一件事。

    两者共用 `app/house_query.py`；这组用例是那条约定的看门狗。
    分叉造成的 bug 在页面上几乎查不出来——用户只会觉得
    「这个提醒有时候来有时候不来」。
    """

    CASES = (
        {"district": "中山区", "elevator": "true"},
        {"district": "中山区", "elevator": "false"},
        {"orientation": "南"},
        {"floor_level": "低楼层"},
        {"decoration": "精装"},
        {"lease_term": "yearly"},
        {"features": "可养宠物"},
        {"rent_price_max": 2000},
        {"keyword": "青泥洼桥"},
    )

    def test_每条条件两边结论一致(self):
        house_no = self.publish(
            title="对照房源两室", orientation="南", decoration="精装",
            floor_no=2, floor_total=18, min_months=12, rent_price=2600,
            elevator=True, tags=["可养宠物"])

        for conditions in self.CASES:
            with self.subTest(conditions=conditions):
                # 列表页怎么说
                listed = self.client.get("/api/houses", params={
                    **conditions, "limit": 100}).json()
                in_listing = house_no in [h["house_no"] for h in listed["list"]]

                # 匹配怎么说：新建一条保存搜索，重新上架一次看发不发提醒
                session = db.SessionLocal()
                try:
                    tenant = session.query(User).filter_by(
                        username="tenant1").one()
                    session.query(SavedSearch).delete()
                    session.add(SavedSearch(
                        user_id=tenant.id, name="对照",
                        conditions=json.dumps(conditions, ensure_ascii=False)))
                    house = session.query(House).filter_by(
                        house_no=house_no).one()
                    session.commit()

                    from app.routers import saved_search
                    sent = saved_search.notify_new_house(session, house)
                    session.commit()
                finally:
                    session.close()

                self.assertEqual(
                    in_listing, sent == 1,
                    "条件 %s：列表页 %s，上新匹配 %s" % (
                        conditions, in_listing, sent == 1))


class TestNotifyIsNotQuadratic(SavedSearchCase):
    """一套房上架要跑遍所有人的所有条件，重复条件不该各查一次库。

    匹配必须走 `house_query.apply_filters`（否则就和列表页分叉），
    而那是一条 SQL——每条保存搜索一次往返。而「中山区 + 两室」这种
    条件会有很多人存下同一份，按条件做一次记忆能把往返砍到条件的种类数。
    """

    def test_identical_conditions_are_evaluated_once(self):
        from sqlalchemy import event

        session = db.SessionLocal()
        try:
            users = session.query(User).all()
            same = json.dumps({"district": "中山区"}, ensure_ascii=False)
            other = json.dumps({"district": "沙河口区"}, ensure_ascii=False)
            # 12 条搜索，只有两种条件
            for i in range(12):
                session.add(SavedSearch(
                    user_id=users[i % len(users)].id, name="第%d条" % i,
                    conditions=same if i % 2 == 0 else other))
            house = session.query(House).filter_by(house_no="DL0001").one()
            house.status = "available"
            session.commit()

            statements = []

            def before(conn, cursor, statement, params, context, many):
                # 只数「匹配查询」——它**只选 houses.id**。
                # ORM 提交后顺手重新加载整行房源那条和保存搜索的条数无关。
                # 原先靠「整行加载会给每列起别名 AS houses_xxx」来排除它，
                # SQLAlchemy 2.1 不再起这个别名，CI 装到新版后就把它也数进去了；
                # 改成直接认「SELECT 后面只有 houses.id」
                if re.match(r"\s*SELECT houses\.id\s+FROM houses\b", statement):
                    statements.append(statement)

            event.listen(session.get_bind(), "before_cursor_execute", before)
            try:
                from app.routers import saved_search
                saved_search.notify_new_house(session, house)
                session.commit()
            finally:
                event.remove(session.get_bind(), "before_cursor_execute", before)
        finally:
            session.close()

        # 两种条件 → 最多两条匹配查询。没有记忆的话这里会是 12 条
        self.assertLessEqual(len(statements), 2, statements)


class TestConditionsAreSanitised(SavedSearchCase):
    """条件是**用户 POST 上来的 JSON**，之后会被当成可信数据用：
    拼成搜索名称、喂回查询、匹配新房源。所以入口处必须把类型定死。

    这一组是照着实际打出来的 500 补的——`{"rent_price_max": "abc"}`
    会让 `describe()` 里的 `int(float("abc"))` 抛异常，
    `{"features": ["电梯", 123]}` 会让 `123.strip()` 抛 AttributeError。
    两条都是「接口收下了，用的时候才炸」。
    """

    BAD = (
        {"rent_price_max": "abc"},
        {"rent_price_min": "不限"},
        {"area_max": []},
        {"available_days": "x"},
        {"features": ["电梯", 123]},
        {"features": 123},
        {"district": 123},
        {"elevator": "随便"},
        {"floor_level": {"a": 1}},
    )

    def test_bad_values_never_500(self):
        for conditions in self.BAD:
            with self.subTest(conditions=conditions):
                # 带上一个有效条件，避免因为「条件全空」被 400 挡掉，
                # 那样就测不到脏值这条路径了
                payload = dict(conditions)
                payload["district"] = payload.get("district", "中山区")
                response = self.save(payload)
                self.assertIn(response.status_code, (200, 400, 422),
                              "%s → %s" % (conditions, response.status_code))

    def test_non_numeric_number_is_dropped(self):
        row = self.save({"district": "中山区",
                         "rent_price_max": "abc"}).json()["saved_search"]
        self.assertNotIn("rent_price_max", row["conditions"])
        self.assertIn("中山区", row["name"])

    def test_numbers_are_stored_as_numbers(self):
        row = self.save({"district": "中山区",
                         "rent_price_max": "3000"}).json()["saved_search"]
        self.assertEqual(row["conditions"]["rent_price_max"], 3000.0)
        self.assertIn("3000 元以内", row["name"])

    def test_mixed_feature_list_is_stringified(self):
        row = self.save({"district": "中山区",
                         "features": ["电梯", 123]}).json()["saved_search"]
        self.assertEqual(row["conditions"]["features"], "电梯,123")

    def test_describe_survives_hand_edited_rows(self):
        """库里的条件可能是更早的版本写的，也可能被人手工改过。

        为了一句展示文案把列表接口打崩不值得。
        """
        from app import house_query
        for junk in ({"rent_price_max": "abc"}, {"rent_price_min": None},
                     {"features": 123}, {"elevator": object()}):
            self.assertIsInstance(house_query.describe(junk), str)

    def test_unknown_enum_values_are_dropped(self):
        """取值有限的键，认不出就丢掉。

        `apply_filters` 本来就会忽略它们（筛不出任何东西），
        但留在条件里会跟着跑进搜索名称——用户看到
        「中山区 · {'a': 1}」只会觉得系统坏了。
        """
        row = self.save({"district": "中山区", "floor_level": {"a": 1},
                         "lease_term": "随便", "elevator": "随便"}
                        ).json()["saved_search"]
        for key in ("floor_level", "lease_term", "elevator"):
            self.assertNotIn(key, row["conditions"])
        self.assertEqual(row["name"], "中山区")

    def test_valid_enum_values_survive(self):
        row = self.save({"district": "中山区", "floor_level": "中楼层",
                         "lease_term": "yearly", "elevator": "true"}
                        ).json()["saved_search"]
        self.assertEqual(row["conditions"]["floor_level"], "中楼层")
        self.assertEqual(row["conditions"]["lease_term"], "yearly")
        self.assertIs(row["conditions"]["elevator"], True)
        self.assertIn("中楼层", row["name"])
        self.assertIn("有电梯", row["name"])
