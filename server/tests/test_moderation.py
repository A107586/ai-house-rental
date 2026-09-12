"""内容审核测试（M-08）。

两条主线：

* **绕过手法必须失效**——想躲关键词的人不会老老实实打「微信」，
  他会写「微 信」「微❤信」「Ｗｅｉｘｉｎ」。这批用例逐个试过去。
* **误伤必须为零**——「微信联系管家」「面积138平米」这类正常表述
  被拦下来，功能就没法上线了。这比漏判更容易毁掉一个审核系统。
"""

from sqlalchemy import select

import app.db as db
from app.models import House
from tests.base import ApiTestCase, auth_header, make_admin
from app import moderation as mod


def make_agent(client, username="agent_probe"):
    """注册一个中介账号并返回它的请求头。

    资质审核与这里无关——改简介不要求资质通过（真要求的话，
    新中介连自我介绍都写不了）。"""
    res = client.post("/api/auth/register", json={
        "username": username, "password": "test123", "real_name": "中介探测",
        "phone": "13700001111", "role": "agent"})
    assert res.status_code == 200, res.text
    return {"Authorization": "Bearer " + res.json()["token"]}


class TestNormalize(ApiTestCase):

    def test_full_width_folded(self):
        self.assertEqual(mod.normalize("ＷｅＩｘｉｎ"), "weixin")

    def test_separators_removed(self):
        self.assertEqual(mod.normalize("微 信"), "微信")
        self.assertEqual(mod.normalize("微·信"), "微信")
        self.assertEqual(mod.normalize("微❤信"), "微信")

    def test_case_folded(self):
        self.assertEqual(mod.normalize("QQ"), "qq")


class TestContactDetection(ApiTestCase):

    def test_plain_phone(self):
        self.assertTrue(mod.find_contacts("电话13812345678"))

    def test_phone_with_separators(self):
        """138-1234-5678 和 138 1234 5678 都要认出来。"""
        self.assertTrue(mod.find_contacts("电话 138-1234-5678"))
        self.assertTrue(mod.find_contacts("电话 138 1234 5678"))

    def test_wechat_with_account(self):
        hits = mod.find_contacts("加微信 zhangsan888")
        self.assertEqual(hits[0][0], "微信号")

    def test_obfuscated_wechat(self):
        self.assertTrue(mod.find_contacts("加微 信：lisi2026"))
        self.assertTrue(mod.find_contacts("加ｖ信 wang_2026"))

    def test_qq(self):
        self.assertTrue(mod.find_contacts("加ＱＱ：123456789"))

    def test_bare_keyword_is_not_a_contact(self):
        """「微信联系管家」是平台内的正常表述，不该被当成留联系方式。"""
        self.assertEqual(mod.find_contacts("可以微信联系管家"), [])

    def test_numbers_in_description_are_not_phones(self):
        """房源描述里全是数字，误伤一次就没人敢用这个功能了。"""
        self.assertEqual(
            mod.find_contacts("建筑面积138平米，楼层15层，租金3800元"), [])
        self.assertEqual(mod.find_contacts("5号楼3单元1201室"), [])


class TestCheck(ApiTestCase):

    def test_clean_text_passes(self):
        r = mod.check("中山区两室一厅，采光好，近地铁", scene="house")
        self.assertEqual(r["level"], mod.LEVEL_PASS)

    def test_contact_blocks(self):
        r = mod.check("看房加微信 zhangsan888", scene="house")
        self.assertEqual(r["level"], mod.LEVEL_BLOCK)
        self.assertIn("私下联系方式", r["categories"])

    def test_reason_does_not_repeat_the_number(self):
        """报错信息里不该把号码原样再抄一遍。"""
        r = mod.check("电话13812345678", scene="house")
        self.assertNotIn("13812345678", " ".join(r["reasons"]))

    def test_contact_allowed_in_repair(self):
        """报修本来就有独立的联系人字段，描述里写电话既正常也无害。"""
        r = mod.check("水管漏水，我电话13900001111", scene="repair")
        self.assertEqual(r["level"], mod.LEVEL_PASS)

    def test_exaggeration_flags_not_blocks(self):
        r = mod.check("本房源全网最低价", scene="house")
        self.assertEqual(r["level"], mod.LEVEL_FLAG)

    def test_discrimination_flags(self):
        r = mod.check("不租给外地人", scene="house")
        self.assertEqual(r["level"], mod.LEVEL_FLAG)
        self.assertIn("歧视性表述", r["categories"])

    def test_block_beats_flag(self):
        r = mod.check("全网最低价，加微信 zhangsan888", scene="house")
        self.assertEqual(r["level"], mod.LEVEL_BLOCK)

    def test_empty_passes(self):
        self.assertEqual(mod.check("", scene="house")["level"], mod.LEVEL_PASS)

    def test_check_all_blocks_if_any_field_blocks(self):
        """标题干净、描述里留了微信号，显然不能算通过。"""
        r = mod.check_all({"标题": "中山区两室一厅",
                           "描述": "详情加微信 zhangsan888"}, scene="house")
        self.assertEqual(r["level"], mod.LEVEL_BLOCK)

    def test_check_all_reports_field_names(self):
        r = mod.check_all({"标题": "全网最低价", "描述": "正常描述"},
                          scene="house")
        self.assertTrue(any("标题" in reason for reason in r["reasons"]))


class ModerationApiCase(ApiTestCase):

    def setUp(self):
        super().setUp()
        self.admin = make_admin(self.client)
        self.landlord = auth_header(self.client, "landlord1", "test123")

    def publish(self, title="中山区两室一厅", description="采光好，近地铁"):
        return self.client.post("/api/houses", headers=self.landlord, json={
            "title": title, "district": "中山区", "rent_price": 3000,
            "area": 80, "layout": "两室一厅", "description": description})

    def flags(self, status="pending"):
        return self.client.get("/api/admin/flags", headers=self.admin,
                               params={"status": status}).json()


class TestModerationInHouses(ModerationApiCase):

    def test_clean_house_publishes(self):
        self.assertEqual(self.publish().status_code, 200)
        self.assertEqual(self.flags()["total"], 0)

    def test_contact_in_description_blocked(self):
        r = self.publish(description="看房直接加微信 zhangsan888")
        self.assertEqual(r.status_code, 400)
        self.assertIn("未通过审核", r.json()["detail"])

    def test_blocked_house_is_not_created(self):
        """先存后删的话，中间那段时间违规内容对所有人可见。"""
        before = self.client.get("/api/houses").json()["total"]
        self.publish(description="电话13812345678")
        after = self.client.get("/api/houses").json()["total"]
        self.assertEqual(before, after)

    def test_flagged_house_is_created_and_queued(self):
        r = self.publish(title="全网最低价 中山区两室一厅")
        self.assertEqual(r.status_code, 200, "存疑内容应放行，交人工判断")
        body = self.flags()
        self.assertEqual(body["total"], 1)
        flag = body["list"][0]
        self.assertEqual(flag["scene"], "house")
        self.assertEqual(flag["target_key"], r.json()["house"]["house_no"])
        self.assertIn("夸大宣传", flag["categories"])

    def test_flag_points_at_a_real_target(self):
        """target_id 为空的标记等于废纸，管理员点「下架」无处可下。"""
        self.publish(title="全网最低价 两室一厅")
        self.assertGreater(self.flags()["list"][0]["target_id"], 0)


class TestFlagHandling(ModerationApiCase):

    def _flagged_house(self):
        published = self.publish(title="全网最低价 中山区两室一厅")
        house_no = published.json()["house"]["house_no"]
        return house_no, self.flags()["list"][0]["id"]

    def test_approve_keeps_content(self):
        house_no, flag_id = self._flagged_house()
        r = self.client.patch("/api/admin/flags/%d" % flag_id,
                              headers=self.admin, params={"action": "approve"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["flag"]["status"], "approved")
        detail = self.client.get("/api/houses/%s" % house_no)
        self.assertEqual(detail.status_code, 200)

    def test_remove_takes_house_offline(self):
        house_no, flag_id = self._flagged_house()
        self.client.patch("/api/admin/flags/%d" % flag_id,
                          headers=self.admin, params={"action": "remove"})
        detail = self.client.get("/api/houses/%s" % house_no,
                                 headers=self.admin).json()
        self.assertEqual(detail["status"], "offline")

    def test_handled_flag_cannot_be_handled_again(self):
        _, flag_id = self._flagged_house()
        self.client.patch("/api/admin/flags/%d" % flag_id,
                          headers=self.admin, params={"action": "approve"})
        again = self.client.patch("/api/admin/flags/%d" % flag_id,
                                  headers=self.admin,
                                  params={"action": "remove"})
        self.assertEqual(again.status_code, 409)

    def test_records_who_handled_it(self):
        _, flag_id = self._flagged_house()
        body = self.client.patch("/api/admin/flags/%d" % flag_id,
                                 headers=self.admin,
                                 params={"action": "approve"}).json()
        self.assertTrue(body["flag"]["handled_by"])
        self.assertTrue(body["flag"]["handled_at"])

    def test_bad_action_rejected(self):
        _, flag_id = self._flagged_house()
        r = self.client.patch("/api/admin/flags/%d" % flag_id,
                              headers=self.admin, params={"action": "delete"})
        self.assertEqual(r.status_code, 400)

    def test_landlord_cannot_view_queue(self):
        self.assertEqual(
            self.client.get("/api/admin/flags", headers=self.landlord).status_code,
            403)

    def test_landlord_cannot_handle(self):
        _, flag_id = self._flagged_house()
        r = self.client.patch("/api/admin/flags/%d" % flag_id,
                              headers=self.landlord,
                              params={"action": "approve"})
        self.assertEqual(r.status_code, 403)


class TestModerationInNotices(ModerationApiCase):

    def test_contact_in_notice_blocked(self):
        r = self.client.post("/api/notices", headers=self.admin, json={
            "title": "系统维护通知", "content": "有问题加微信 admin_2026"})
        self.assertEqual(r.status_code, 400)

    def test_clean_notice_ok(self):
        r = self.client.post("/api/notices", headers=self.admin, json={
            "title": "系统维护通知", "content": "本周六凌晨维护两小时。"})
        self.assertEqual(r.status_code, 200)


class TestReviewSoftDelete(ModerationApiCase):
    """审核下架评价必须是**软删除**（C-5）。

    审核会误判——「微信联系管家」这种正常表述就可能被拦。误判之后要能
    恢复，用户申诉时也得拿得出原文。真删了就什么都没有了。
    """

    def test_removed_review_is_hidden_but_kept(self):
        import app.db as db
        from app.models import Review
        session = db.SessionLocal()
        try:
            review = Review(contract_id=1, house_id=1, tenant_id=1,
                            rating=5, content="很好的房子")
            session.add(review)
            session.commit()
            review_id = review.id
        finally:
            session.close()

        # 前台看得到
        before = self.client.get("/api/houses/DL0001/reviews").json()
        self.assertTrue(any(r["id"] == review_id for r in before["list"]))

        # 软删除
        session = db.SessionLocal()
        try:
            from datetime import datetime
            session.get(Review, review_id).deleted_at = datetime.now()
            session.commit()
        finally:
            session.close()

        # 前台看不到了……
        after = self.client.get("/api/houses/DL0001/reviews").json()
        self.assertFalse(any(r["id"] == review_id for r in after["list"]))

        # ……但记录还在，能恢复
        session = db.SessionLocal()
        try:
            self.assertIsNotNone(session.get(Review, review_id))
            self.assertEqual(session.get(Review, review_id).content, "很好的房子")
        finally:
            session.close()

    def test_deleted_review_excluded_from_rating(self):
        """被下架的评价不能再影响房源评分——否则下架了还在拉低分数。"""
        import app.db as db
        from datetime import datetime
        from app.models import Review
        session = db.SessionLocal()
        try:
            session.add(Review(contract_id=1, house_id=1, tenant_id=1,
                               rating=5, content="好"))
            session.add(Review(contract_id=2, house_id=1, tenant_id=1,
                               rating=1, content="差", deleted_at=datetime.now()))
            session.commit()
        finally:
            session.close()

        house = self.client.get("/api/houses/DL0001").json()
        self.assertEqual(house["rating"], 5.0)
        self.assertEqual(house["review_count"], 1)


class TestEditPathIsGuardedToo(ApiTestCase):
    """**只在发布时查一遍是不够的。**

    编辑接口原先一个字都不查，于是「先发一套干净的、审核过了再改成
    留微信」这条路是通的。实测六种写法（`加微信 wxid_abc123`、
    `微 信：`、`Ｗｅｉｘｉｎ：`、`微❤信`、11 位手机号、QQ 号）
    从 `PUT /api/houses/{no}` 全部原样入库。
    """

    def setUp(self):
        super().setUp()
        self.headers = auth_header(self.client, "landlord1")

    def _put(self, **fields):
        return self.client.put("/api/houses/DL0001", json=fields,
                               headers=self.headers)

    def test_contact_in_description_is_blocked_on_edit(self):
        for text in ("加微信 wxid_abc123 详谈",
                     "电话 13812345678 随时看房",
                     "微 信：fangdong2026",
                     "Ｗｅｉｘｉｎ：abc888",
                     "微❤信 abc888",
                     "QQ 1234567 联系我"):
            res = self._put(description=text)
            self.assertEqual(res.status_code, 400, "「%s」应当被拦：%s"
                             % (text, res.text))

    def test_contact_in_title_is_blocked_on_edit(self):
        res = self._put(title="两室一厅 微信 wxid_x9abc")
        self.assertEqual(res.status_code, 400, res.text)

    def test_normal_wording_still_passes(self):
        for text in ("可以微信联系管家，具体以平台为准",
                     "楼下就是 7-11，24 小时营业",
                     "月租 3500，押一付三"):
            res = self._put(description=text)
            self.assertEqual(res.status_code, 200, "「%s」不该被拦：%s"
                             % (text, res.text))

    def test_editing_only_the_rent_does_not_recheck_old_text(self):
        """**只查这次真的改了的字段。**

        拿库里的老文案去撞规则的话，一条历史遗留的违规描述会把整个
        编辑功能卡死——房东连改个租金都改不了，而他根本没碰那段文字。
        """
        session = db.SessionLocal()
        try:
            house = session.scalar(
                select(House).where(House.house_no == "DL0001"))
            house.description = "老数据：加微信 wxid_legacy01"
            session.commit()
        finally:
            session.close()
        res = self._put(rent_price=3333)
        self.assertEqual(res.status_code, 200, res.text)


class TestAgentIntroIsPublicCopy(ApiTestCase):
    """中介简介会出现在公开名录里，和房源描述一个性质。"""

    def test_intro_with_contact_is_blocked(self):
        headers = make_agent(self.client)
        res = self.client.put("/api/agent/profile",
                              json={"intro": "有事加微信 wxid_abc123 详谈"},
                              headers=headers)
        self.assertEqual(res.status_code, 400, res.text)

    def test_normal_intro_passes(self):
        headers = make_agent(self.client)
        res = self.client.put(
            "/api/agent/profile",
            json={"intro": "熟悉大连各区两室房源，可代房东带看与答复咨询。"},
            headers=headers)
        self.assertEqual(res.status_code, 200, res.text)
