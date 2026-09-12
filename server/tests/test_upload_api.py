"""图片上传测试。

文件上传是最容易被做成漏洞的一类接口，这批用例逐条对应一种真实攻击：

* 改扩展名绕过类型限制
* 用 `../../` 的文件名覆盖服务器上的源码
* 内容是图片、扩展名是 `.html`，靠浏览器渲染成存储型 XSS
* 传超大文件把磁盘写满
* 匿名上传，把服务器磁盘开放给全网
"""

import os

from tests.base import ApiTestCase, auth_header, make_admin
from app import uploads

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
GIF = b"GIF89a" + b"\x00" * 64
WEBP = b"RIFF\x24\x00\x00\x00WEBP" + b"\x00" * 64
HTML = b"<html><script>alert(1)</script></html>"
PHP = b"<?php system($_GET['c']); ?>"


class TestSniff(ApiTestCase):
    """类型判断只认文件头。扩展名是客户端说了算的。"""

    def test_known_formats(self):
        self.assertEqual(uploads.sniff(JPEG), "jpg")
        self.assertEqual(uploads.sniff(PNG), "png")
        self.assertEqual(uploads.sniff(GIF), "gif")
        self.assertEqual(uploads.sniff(WEBP), "webp")

    def test_webp_length_bytes_are_skipped(self):
        """RIFF 后面四字节是文件长度，必须跳过再比 WEBP。"""
        self.assertEqual(
            uploads.sniff(b"RIFF\xff\xff\xff\xffWEBP" + b"\x00" * 10), "webp")

    def test_script_content_is_rejected(self):
        self.assertIsNone(uploads.sniff(HTML))
        self.assertIsNone(uploads.sniff(PHP))
        self.assertIsNone(uploads.sniff(b""))

    def test_riff_without_webp_is_rejected(self):
        """RIFF 也可能是 wav，不能只看前四字节就放行。"""
        self.assertIsNone(uploads.sniff(b"RIFF\x00\x00\x00\x00WAVEfmt "))


class TestValidate(ApiTestCase):

    def test_ok(self):
        self.assertEqual(uploads.validate(JPEG), (True, "jpg"))

    def test_empty(self):
        ok, message = uploads.validate(b"")
        self.assertFalse(ok)

    def test_too_large(self):
        ok, message = uploads.validate(JPEG + b"\x00" * uploads.MAX_BYTES)
        self.assertFalse(ok)
        self.assertIn("MB", message)

    def test_wrong_type(self):
        ok, message = uploads.validate(HTML)
        self.assertFalse(ok)
        self.assertIn("只支持", message)


class TestBuildName(ApiTestCase):

    def test_name_is_generated_not_reused(self):
        """客户端的文件名可能是 ../../app/main.py，拼进路径就覆盖源码了。"""
        name = uploads.build_name("jpg")
        self.assertTrue(name.endswith(".jpg"))
        self.assertNotIn("/", name)
        self.assertNotIn("\\\\", name)
        self.assertNotIn("..", name)

    def test_names_are_unique(self):
        names = {uploads.build_name("png") for _ in range(50)}
        self.assertEqual(len(names), 50)


class TestIsManaged(ApiTestCase):

    def test_only_our_uploads(self):
        self.assertTrue(uploads.is_managed("/static/uploads/202609/a.jpg"))
        self.assertFalse(uploads.is_managed("/static/houses/demo1.jpg"))
        self.assertFalse(uploads.is_managed("https://evil.example.com/a.jpg"))
        self.assertFalse(uploads.is_managed(""))


class UploadCase(ApiTestCase):

    def setUp(self):
        super().setUp()
        self.admin = make_admin(self.client)
        self.landlord = auth_header(self.client, "landlord1", "test123")
        self.tenant = auth_header(self.client, "tenant1", "test123")

    def upload(self, data=JPEG, name="photo.jpg", headers=None,
               content_type="image/jpeg"):
        return self.client.post(
            "/api/upload", headers=headers or self.landlord,
            files={"file": (name, data, content_type)})


class TestUploadEndpoint(UploadCase):

    def test_upload_returns_url(self):
        r = self.upload()
        self.assertEqual(r.status_code, 200)
        url = r.json()["url"]
        self.assertTrue(url.startswith("/static/uploads/"))
        self.assertTrue(url.endswith(".jpg"))

    def test_file_actually_lands_on_disk(self):
        url = self.upload().json()["url"]
        from app.main import STATIC_DIR
        path = os.path.join(STATIC_DIR, url[len("/static/"):].replace("/", os.sep))
        self.assertTrue(os.path.isfile(path))
        os.remove(path)

    def test_anonymous_denied(self):
        """匿名上传等于把服务器磁盘开放给全网。"""
        r = self.client.post("/api/upload",
                             files={"file": ("a.jpg", JPEG, "image/jpeg")})
        self.assertEqual(r.status_code, 401)

    def test_renamed_script_is_rejected(self):
        """把 shell.php 改名成 a.jpg 是一秒钟的事。"""
        r = self.upload(data=PHP, name="a.jpg")
        self.assertEqual(r.status_code, 400)

    def test_lying_content_type_is_rejected(self):
        r = self.upload(data=HTML, name="a.png", content_type="image/png")
        self.assertEqual(r.status_code, 400)

    def test_traversal_filename_cannot_escape(self):
        """客户端文件名一律不采用，路径穿越无从谈起。"""
        url = self.upload(name="../../../app/main.py").json()["url"]
        self.assertNotIn("..", url)
        self.assertTrue(url.startswith("/static/uploads/"))

    def test_extension_comes_from_content_not_client(self):
        """内容是 GIF、扩展名写成 .html 的文件会被浏览器当页面渲染。"""
        url = self.upload(data=GIF, name="x.html").json()["url"]
        self.assertTrue(url.endswith(".gif"))

    def test_oversized_rejected(self):
        r = self.upload(data=JPEG + b"\x00" * uploads.MAX_BYTES)
        self.assertEqual(r.status_code, 400)


class TestHouseImages(UploadCase):

    def attach(self, house_no="DL0001", headers=None, url=None):
        if url is None:
            url = self.upload(headers=headers or self.landlord).json()["url"]
        return self.client.post("/api/houses/%s/images" % house_no,
                                headers=headers or self.landlord,
                                json={"url": url})

    def test_attach_and_first_becomes_cover(self):
        """第一张自动做封面，免得房东传完发现列表页还是空图。"""
        r = self.attach()
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["image"]["is_cover"])

    def test_second_is_not_cover(self):
        self.attach()
        self.assertFalse(self.attach().json()["image"]["is_cover"])

    def test_external_url_rejected(self):
        """放开成任意 URL，外部站点就能拿到每个访客的 IP 与访问时间。"""
        r = self.client.post("/api/houses/DL0001/images",
                             headers=self.landlord,
                             json={"url": "https://evil.example.com/track.gif"})
        self.assertEqual(r.status_code, 400)

    def test_seed_image_path_rejected(self):
        r = self.client.post("/api/houses/DL0001/images", headers=self.landlord,
                             json={"url": "/static/houses/demo1.jpg"})
        self.assertEqual(r.status_code, 400)

    def test_other_landlord_denied(self):
        self.client.post("/api/auth/register", json={
            "username": "landlord2", "password": "test123", "role": "landlord"})
        other = auth_header(self.client, "landlord2", "test123")
        url = self.upload(headers=other).json()["url"]
        r = self.client.post("/api/houses/DL0001/images", headers=other,
                             json={"url": url})
        self.assertEqual(r.status_code, 403)

    def test_tenant_denied(self):
        url = self.upload(headers=self.landlord).json()["url"]
        r = self.client.post("/api/houses/DL0001/images", headers=self.tenant,
                             json={"url": url})
        self.assertEqual(r.status_code, 403)

    def test_delete_promotes_next_cover(self):
        """封面被删掉要顺位提一张，否则列表页会留一个空图位。"""
        first = self.attach().json()["image"]
        second = self.attach().json()["image"]
        self.client.delete("/api/houses/DL0001/images/%d" % first["id"],
                           headers=self.landlord)
        body = self.client.get("/api/houses/DL0001/images").json()
        cover = [img for img in body["list"] if img["is_cover"]]
        self.assertEqual(len(cover), 1)
        self.assertEqual(cover[0]["id"], second["id"])

    def test_set_cover(self):
        self.attach()
        second = self.attach().json()["image"]
        self.client.post("/api/houses/DL0001/images/%d/cover" % second["id"],
                         headers=self.landlord)
        body = self.client.get("/api/houses/DL0001/images").json()
        cover = [img for img in body["list"] if img["is_cover"]]
        self.assertEqual(len(cover), 1, "封面只能有一张")
        self.assertEqual(cover[0]["id"], second["id"])

    def test_delete_removes_file(self):
        from app.main import STATIC_DIR
        image = self.attach().json()["image"]
        path = os.path.join(STATIC_DIR,
                            image["url"][len("/static/"):].replace("/", os.sep))
        self.assertTrue(os.path.isfile(path))
        self.client.delete("/api/houses/DL0001/images/%d" % image["id"],
                           headers=self.landlord)
        self.assertFalse(os.path.isfile(path))

    def test_delete_never_touches_seed_images(self):
        """种子演示图不在 uploads 目录下，不能被误删。"""
        from app.main import STATIC_DIR
        self.assertFalse(uploads.remove(STATIC_DIR, "/static/houses/demo1.jpg"))


class TestRepairImages(UploadCase):

    def new_repair(self):
        return self.client.post("/api/repairs", headers=self.tenant, json={
            "house_no": "DL0001", "content": "水管漏水",
            "contact_name": "租客甲", "contact_phone": "13900005678",
        }).json()["repair"]

    def test_tenant_can_attach_to_own_repair(self):
        repair = self.new_repair()
        url = self.upload(headers=self.tenant).json()["url"]
        r = self.client.post("/api/repairs/%d/images" % repair["id"],
                             headers=self.tenant, json={"url": url})
        self.assertEqual(r.status_code, 200)

    def test_landlord_can_view(self):
        repair = self.new_repair()
        url = self.upload(headers=self.tenant).json()["url"]
        self.client.post("/api/repairs/%d/images" % repair["id"],
                         headers=self.tenant, json={"url": url})
        body = self.client.get("/api/repairs/%d/images" % repair["id"],
                               headers=self.landlord).json()
        self.assertEqual(body["total"], 1)

    def test_unrelated_user_denied(self):
        repair = self.new_repair()
        self.client.post("/api/auth/register", json={
            "username": "nosy", "password": "test123", "role": "tenant"})
        nosy = auth_header(self.client, "nosy", "test123")
        r = self.client.get("/api/repairs/%d/images" % repair["id"],
                            headers=nosy)
        self.assertEqual(r.status_code, 403)

    def test_external_url_rejected(self):
        repair = self.new_repair()
        r = self.client.post("/api/repairs/%d/images" % repair["id"],
                             headers=self.tenant,
                             json={"url": "https://evil.example.com/a.gif"})
        self.assertEqual(r.status_code, 400)


class TestManagedUrlRejectsTraversal(ApiTestCase):
    """`is_managed` 不能只看前缀。

    `/static/uploads/../../../etc/passwd` 也以正确的前缀开头，
    原先照样能存进相册表。今天它顶多是一张裂图（浏览器先把 `..`
    规范化掉，StaticFiles 也只认自己那棵目录树，实测三种写法全是 404），
    但一条指向 uploads 之外的地址留在库里，等哪天图片改由别的东西
    来发（缩略图服务、CDN 回源、代理），它就是现成的入口。
    """

    def test_plain_upload_url_passes(self):
        self.assertTrue(uploads.is_managed(
            "/static/uploads/202609/0123456789abcdef0123456789abcdef.png"))

    def test_traversal_is_refused(self):
        for url in ("/static/uploads/../../../etc/passwd",
                    "/static/uploads/202609/../../seed.py",
                    "/static/uploads/%2e%2e/%2e%2e/seed.py",
                    "/static/uploads/..\..\seed.py",
                    "/static/uploads/202609/a.png\x00.txt",
                    "/static/uploads/202609/a\npng"):
            self.assertFalse(uploads.is_managed(url), url)

    def test_other_prefixes_still_refused(self):
        for url in ("https://evil.example.com/a.png",
                    "//evil.example.com/a.png",
                    "data:image/png;base64,iVBORw0KGgo=",
                    "javascript:alert(1)",
                    "/static/houses/h05.jpg",
                    "", None):
            self.assertFalse(uploads.is_managed(url), repr(url))
