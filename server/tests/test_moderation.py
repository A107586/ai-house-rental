"""内容审核测试（M-08）。

两条主线：

* **绕过手法必须失效**——想躲关键词的人不会老老实实打「微信」，
  他会写「微 信」「微❤信」「Ｗｅｉｘｉｎ」。这批用例逐个试过去。
* **误伤必须为零**——「微信联系管家」「面积138平米」这类正常表述
  被拦下来，功能就没法上线了。这比漏判更容易毁掉一个审核系统。
"""

from tests.base import ApiTestCase, auth_header, make_admin
from app import moderation as mod


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
