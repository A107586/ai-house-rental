# -*- coding: utf-8 -*-
"""房源图片说明的落库与权限。

`house_images.caption` 这一列原先**324 条全是空的**：上传接口一直收
`caption`，前端却永远传空串，没有任何入口能填它。相册的 `<img alt>`
也因此一直是空的——读屏软件念到那一段只有一串「图片」。

现在有了两条路：智能体看图写草稿（在 agent 那边），房东改完存到这里。
这一组盯住存这一步的三件事：

1. **只有这套房的管理者能改。** 别人的房源图上写什么，不该由我说了算。
2. **走内容审核。** 图片说明是挂在房源上给租客看的公开文案，
   和标题、描述一个性质——不能因为它是「一句小字」就绕过去。
   私下联系方式塞在图片说明里，和塞在描述里是一模一样的事。
3. **先审再落库。** 命中拦截时那句话一个字都不许进库。
"""

from tests.base import ApiTestCase, auth_header, make_admin
import app.db as db
from app.models import HouseImage


class ImageCaptionTestCase(ApiTestCase):

    def setUp(self):
        super().setUp()
        self.owner = auth_header(self.client, "landlord1", "test123")
        got = self.client.post(
            "/api/houses/DL0001/images", headers=self.owner,
            json={"url": "/static/uploads/202609/"
                         "0123456789abcdef0123456789abcdef.png"})
        self.assertEqual(got.status_code, 200, got.text)
        self.image_id = got.json()["image"]["id"]

    def path(self, image_id=None):
        return ("/api/houses/DL0001/images/%d/caption"
                % (image_id if image_id is not None else self.image_id))

    def caption_in_db(self):
        session = db.SessionLocal()
        try:
            return session.get(HouseImage, self.image_id).caption
        finally:
            session.close()


class TestSetCaption(ImageCaptionTestCase):

    def test_owner_can_set_and_read_back(self):
        got = self.client.post(self.path(), headers=self.owner,
                               json={"caption": "采光充足的客厅，绿植环绕"})
        self.assertEqual(got.status_code, 200, got.text)
        self.assertEqual(got.json()["caption"], "采光充足的客厅，绿植环绕")
        self.assertEqual(self.caption_in_db(), "采光充足的客厅，绿植环绕")

    def test_caption_shows_up_in_the_album(self):
        """存完要能在相册接口里读到——否则 alt 还是空的。"""
        self.client.post(self.path(), headers=self.owner,
                         json={"caption": "明亮厨房，白色橱柜"})
        rows = self.client.get("/api/houses/DL0001/images").json()["list"]
        self.assertEqual([r["caption"] for r in rows], ["明亮厨房，白色橱柜"])

    def test_empty_caption_clears_it(self):
        """房东写了又后悔，得能清掉。空串不该被当成「没传」。"""
        self.client.post(self.path(), headers=self.owner,
                         json={"caption": "先写一句"})
        got = self.client.post(self.path(), headers=self.owner,
                               json={"caption": ""})
        self.assertEqual(got.status_code, 200)
        self.assertEqual(self.caption_in_db(), "")

    def test_over_long_is_truncated_not_refused(self):
        """超长截断而不是 400：这一列本来就只有 100 个字符。"""
        self.client.post(self.path(), headers=self.owner,
                         json={"caption": "客" * 200})
        self.assertEqual(len(self.caption_in_db()), 100)

    def test_unknown_image_is_404(self):
        self.assertEqual(
            self.client.post(self.path(999999), headers=self.owner,
                             json={"caption": "x"}).status_code, 404)


class TestOnlyTheOwnerCanWrite(ImageCaptionTestCase):

    def test_tenant_is_refused(self):
        tenant = auth_header(self.client, "tenant1", "test123")
        got = self.client.post(self.path(), headers=tenant,
                               json={"caption": "我来改一句"})
        self.assertIn(got.status_code, (401, 403), got.text)
        self.assertEqual(self.caption_in_db(), "")

    def test_anonymous_is_refused(self):
        got = self.client.post(self.path(), json={"caption": "我来改一句"})
        self.assertIn(got.status_code, (401, 403), got.text)
        self.assertEqual(self.caption_in_db(), "")


class TestModeration(ImageCaptionTestCase):
    """和标题、描述走同一道审核。"""

    def test_contact_details_are_blocked_and_nothing_is_stored(self):
        """**先审再落库**：拦下的那句话一个字都不许进库。"""
        got = self.client.post(
            self.path(), headers=self.owner,
            json={"caption": "看房加微信 13800138000"})
        self.assertEqual(got.status_code, 400, got.text)
        self.assertEqual(self.caption_in_db(), "")

    def test_flagged_content_is_stored_but_leaves_a_review_item(self):
        """模糊的放行、但记一条待人工复核——审核的价值在于让人能复查。

        用「全网最低」这种确实在 `FLAG_WORDS` 里的词，
        才谈得上断言「多出一条待办」；拿一句大概不会命中的话来测，
        `assertGreaterEqual` 永远成立，等于什么都没测。
        """
        admin = make_admin(self.client)
        before = len(self.client.get(
            "/api/admin/flags", headers=admin).json()["list"])

        got = self.client.post(self.path(), headers=self.owner,
                               json={"caption": "全网最低价的客厅"})
        self.assertEqual(got.status_code, 200, got.text)
        # 标记不拦，内容照存
        self.assertEqual(self.caption_in_db(), "全网最低价的客厅")

        rows = self.client.get("/api/admin/flags", headers=admin).json()["list"]
        self.assertEqual(len(rows), before + 1)
        self.assertIn("全网最低价的客厅", rows[0]["excerpt"])

    def test_clean_caption_leaves_no_review_item(self):
        """反过来也要盯：正常文案不该给管理员制造待办。"""
        admin = make_admin(self.client)
        before = len(self.client.get(
            "/api/admin/flags", headers=admin).json()["list"])
        self.client.post(self.path(), headers=self.owner,
                         json={"caption": "采光充足的客厅，绿植环绕"})
        rows = self.client.get("/api/admin/flags", headers=admin).json()["list"]
        self.assertEqual(len(rows), before)


if __name__ == "__main__":
    import unittest
    unittest.main()
