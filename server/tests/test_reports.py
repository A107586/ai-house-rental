"""房源举报（K-08）。

原先平台只有机器审核（`app/moderation.py`）——发布房源、发评价时自动
拦一道，写得相当好，但**全站没有一个「我要举报」按钮**。
虚假房源、盗图、价格异常这些，机器的词表拦不住，只有看房的人知道。

这份测试盯三件事：
1. 举报进得来（登录、类型、防刷）；
2. 管理员处理得了，且**举报人拿得到结论**——石沉大海的话，
   下次就没人愿意举报了，而举报量是这套机制唯一的输入；
3. 权限不越界。
"""

from tests.base import ApiTestCase, auth_header, make_admin
import app.db as db
from app.models import House, HouseReport, Message, User


class ReportCase(ApiTestCase):

    def setUp(self):
        super().setUp()
        self.tenant = auth_header(self.client, "tenant1", "test123")
        self.landlord = auth_header(self.client, "landlord1", "test123")
        self.admin = make_admin(self.client)

    def report(self, headers=None, **overrides):
        body = {"house_no": "DL0001", "kind": "fake",
                "description": "打电话过去说这套早就租出去了"}
        body.update(overrides)
        return self.client.post("/api/reports",
                                headers=headers or self.tenant, json=body)


class TestSubmit(ReportCase):

    def test_tenant_can_report(self):
        response = self.report()
        self.assertEqual(response.status_code, 200, response.text)
        report = response.json()["report"]
        self.assertEqual(report["kind_label"], "虚假房源")
        self.assertEqual(report["status"], "pending")
        self.assertTrue(report["report_no"].startswith("JB"))

    def test_login_required(self):
        """未登录先要求登录。

        匿名举报既没法给回执，也挡不住刷单。
        """
        response = self.client.post("/api/reports",
                                    json={"house_no": "DL0001", "kind": "fake"})
        self.assertEqual(response.status_code, 401)

    def test_unknown_kind_rejected(self):
        self.assertEqual(self.report(kind="随便写的").status_code, 422)

    def test_unknown_house_rejected(self):
        self.assertEqual(self.report(house_no="DL9999").status_code, 404)

    def test_landlord_cannot_report_own_house(self):
        """房东举报自己的房源没有意义，只会污染待办列表。"""
        response = self.report(headers=self.landlord)
        self.assertEqual(response.status_code, 400)

    def test_duplicate_pending_report_rejected(self):
        """同一个人对同一套房只留一条待处理的举报。

        不拦的话，连点五次就把管理员的待办刷满了，
        真正需要看的举报反而被埋掉。
        """
        self.assertEqual(self.report().status_code, 200)
        again = self.report()
        self.assertEqual(again.status_code, 409)
        self.assertIn("正在处理中", again.json()["detail"])

    def test_can_report_again_after_handled(self):
        """上一条处理完了，就该允许再报——房源可能又变了。"""
        no = self.report().json()["report"]["report_no"]
        self.client.patch("/api/admin/reports/%s" % no, headers=self.admin,
                          json={"status": "rejected", "conclusion": "已核实无误"})
        self.assertEqual(self.report().status_code, 200)

    def test_evidence_is_capped(self):
        """证据图片有上限，不能靠塞几百个 URL 把字段撑爆。

        地址换成了本系统上传的样子——证据图现在要过
        `uploads.is_managed`（见 TestEvidenceMustBeOurs），
        `/static/0.jpg` 这种一进门就被拒了，量不到上限那一步。
        """
        urls = ["/static/uploads/202609/%032d.png" % i for i in range(20)]
        response = self.report(evidence=urls)
        self.assertEqual(len(response.json()["report"]["evidence"]), 6)


class TestHandling(ReportCase):

    def test_admin_sees_pending(self):
        self.report()
        body = self.client.get("/api/admin/reports", headers=self.admin).json()
        self.assertEqual(body["pending"], 1)
        self.assertEqual(body["list"][0]["house_no"], "DL0001")

    def test_handle_records_verdict_and_handler(self):
        no = self.report().json()["report"]["report_no"]
        response = self.client.patch(
            "/api/admin/reports/%s" % no, headers=self.admin,
            json={"status": "accepted", "conclusion": "已联系房东下架"})
        self.assertEqual(response.status_code, 200)
        report = response.json()["report"]
        self.assertEqual(report["status"], "accepted")
        self.assertEqual(report["conclusion"], "已联系房东下架")
        self.assertTrue(report["handled_at"])

    def test_reporter_is_notified(self):
        """**举报人必须收到结论。** 这是整条链路的闭环所在。"""
        no = self.report().json()["report"]["report_no"]
        self.client.patch("/api/admin/reports/%s" % no, headers=self.admin,
                          json={"status": "accepted", "conclusion": "已核实属实"})

        messages = self.client.get("/api/messages", headers=self.tenant).json()
        titles = [m["title"] for m in messages["list"]]
        self.assertTrue(any("举报处理结果" in t for t in titles), titles)
        body = next(m for m in messages["list"] if "举报处理结果" in m["title"])
        self.assertIn("已核实属实", body["content"])

    def test_cannot_handle_twice(self):
        no = self.report().json()["report"]["report_no"]
        self.client.patch("/api/admin/reports/%s" % no, headers=self.admin,
                          json={"status": "accepted", "conclusion": "已核实"})
        again = self.client.patch("/api/admin/reports/%s" % no,
                                  headers=self.admin,
                                  json={"status": "rejected", "conclusion": "改判"})
        self.assertEqual(again.status_code, 409)

    def test_conclusion_is_required(self):
        """K-08：处理时必须填结论且原样通知举报人。原先空着也能交，举报人收到一句「感谢你的反馈」。"""
        no = self.report().json()["report"]["report_no"]
        r = self.client.patch("/api/admin/reports/%s" % no, headers=self.admin,
                              json={"status": "rejected", "conclusion": "  "})
        self.assertEqual(r.status_code, 422)

    def test_upheld_report_tells_the_landlord(self):
        """属实的要让房东知道，他才知道哪里出了问题；不属实的不去打扰他。"""
        no = self.report().json()["report"]["report_no"]
        self.client.patch("/api/admin/reports/%s" % no, headers=self.admin,
                          json={"status": "accepted", "conclusion": "实拍图与房源不符"})
        landlord = auth_header(self.client, "landlord1", "test123")
        titles = [m["title"] for m in self.client.get("/api/messages", headers=landlord)
                  .json()["list"]]
        self.assertIn("你的房源被举报，经核实属实", titles)

    def test_unknown_status_rejected(self):
        no = self.report().json()["report"]["report_no"]
        response = self.client.patch("/api/admin/reports/%s" % no,
                                     headers=self.admin,
                                     json={"status": "deleted"})
        self.assertEqual(response.status_code, 422)

    def test_accepting_does_not_silently_remove_the_house(self):
        """「属实」不等于自动下架。

        判定和处置要分开：绑死的话，管理员为了下架一套信息过期的房源，
        就得把它判成「虚假房源」——房东的记录上从此背着一条假房源。
        """
        no = self.report().json()["report"]["report_no"]
        self.client.patch("/api/admin/reports/%s" % no, headers=self.admin,
                          json={"status": "accepted"})
        house = self.client.get("/api/houses/DL0001").json()
        self.assertEqual(house["status"], "available")


class TestPermissions(ReportCase):

    def test_tenant_cannot_list_all_reports(self):
        self.assertEqual(
            self.client.get("/api/admin/reports", headers=self.tenant).status_code,
            403)

    def test_landlord_cannot_handle(self):
        no = self.report().json()["report"]["report_no"]
        response = self.client.patch("/api/admin/reports/%s" % no,
                                     headers=self.landlord,
                                     json={"status": "rejected"})
        self.assertEqual(response.status_code, 403)

    def test_reporter_only_sees_own(self):
        self.report()
        mine = self.client.get("/api/reports/mine", headers=self.tenant).json()
        self.assertEqual(mine["total"], 1)
        # 房东那边看不到别人提交的举报
        other = self.client.get("/api/reports/mine",
                                headers=self.landlord).json()
        self.assertEqual(other["total"], 0)


class TestSeparateFromMachineFlags(ReportCase):
    """用户举报和机器审核是两条线，不能互相污染。

    `content_flags` 记的是机器拦下的内容（有命中词、没有举报人）；
    `house_reports` 记的是人报上来的问题（有举报人、要回执）。
    """

    def test_report_does_not_land_in_content_flags(self):
        self.report()
        flags = self.client.get("/api/admin/flags", headers=self.admin).json()
        self.assertEqual(flags["total"], 0)

    def test_kinds_are_served_not_hardcoded(self):
        """举报类型由接口提供，前端不写死——加一类不用改两处。"""
        body = self.client.get("/api/report-kinds").json()
        keys = [item["key"] for item in body["list"]]
        self.assertIn("fake", keys)
        self.assertIn("stolen_photo", keys)
        self.assertIn("price", keys)


class TestOperationLog(ReportCase):
    """处理举报要留痕：事后有人问「谁把它判成属实的」，得答得上来。"""

    def test_handling_is_logged_with_a_readable_name(self):
        no = self.report().json()["report"]["report_no"]
        self.client.patch("/api/admin/reports/%s" % no, headers=self.admin,
                          json={"status": "accepted"})
        logs = self.client.get("/api/admin/logs", headers=self.admin).json()
        actions = [row["action"] for row in logs["list"]]
        self.assertIn("处理房源举报", actions)


class TestEvidenceMustBeOurs(ApiTestCase):
    """证据图只收本系统上传的地址。

    房源相册、评价配图、报修照片三处都有这道校验，举报这条路原先没有。
    **看这张图的是管理员**，而提交的人恰恰是平台要审查的对象——
    外链能拿到管理员的 IP 与访问时间，还能在管理员点开之前换掉图上的内容。

    实测：`https://evil.example.com/a.png` 直接入库，并显示在管理端。
    """

    def setUp(self):
        super().setUp()
        self.headers = auth_header(self.client)

    def _report(self, evidence):
        return self.client.post("/api/reports", json={
            "house_no": "DL0001", "kind": "fake",
            "description": "探测", "evidence": evidence}, headers=self.headers)

    def test_external_urls_are_refused(self):
        for url in ("https://evil.example.com/a.png",
                    "//evil.example.com/a.png",
                    "data:image/png;base64,iVBORw0KGgo=",
                    "javascript:alert(1)",
                    "/static/uploads/../../../etc/passwd"):
            res = self._report([url])
            self.assertEqual(res.status_code, 400, "%s 该被拒：%s" % (url, res.text))

    def test_our_own_upload_passes(self):
        url = "/static/uploads/202609/0123456789abcdef0123456789abcdef.png"
        res = self._report([url])
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["report"]["evidence"], [url])

    def test_no_evidence_is_still_allowed(self):
        """**没有证据也得能举报。**

        「打电话过去说早租出去了」这种就是拿不出图，
        强制上传等于把这类举报挡在门外。
        """
        res = self._report([])
        self.assertEqual(res.status_code, 200, res.text)
