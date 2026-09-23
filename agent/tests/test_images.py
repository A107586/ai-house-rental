# -*- coding: utf-8 -*-
"""交给模型的图片地址，收敛在哪里。

这一组盯的是**安全**，不是业务：用户给一个地址，去取图的是服务器。
放任意地址进去，内网地址、云厂商的元数据接口、一个会记录每次访问的
外部图床，都在射程之内。

跟着 `app/images.py` 从报修那边搬过来——「哪些地址允许交给模型」
和「拿图片干什么」是两件事，报修与房源图片说明共用同一套规矩。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import images as vision                        # noqa: E402

JPG = "data:image/jpeg;base64,QUJDREVG"


class TestSafeImages(unittest.TestCase):
    """只放行两种来源：请求里自带的图，和本系统自己的静态路径。"""

    def setUp(self):
        self.fetched = []

        def fake_fetch(url):
            self.fetched.append(url)
            return "data:image/jpeg;base64,RkVUQ0hFRA=="

        self.fetch = fake_fetch

    def keep(self, url):
        return vision.safe_images([url], "http://127.0.0.1:8080",
                                  fetch=self.fetch)

    def test_data_uri_passes_through(self):
        self.assertEqual(self.keep(JPG), [JPG])
        self.assertEqual(self.fetched, [])       # 自带的图不需要去取

    def test_managed_path_is_fetched_from_the_fixed_backend(self):
        self.assertTrue(self.keep("/static/uploads/202609/a.jpg"))
        self.assertEqual(self.fetched,
                         ["http://127.0.0.1:8080/static/uploads/202609/a.jpg"])

    def test_external_addresses_are_dropped(self):
        """这一条是 SSRF 的正面用例：地址由用户给，取图的是服务器。"""
        for url in ("http://evil.example.com/x.jpg",
                    "https://evil.example.com/x.jpg",
                    "//evil.example.com/x.jpg",
                    "http://169.254.169.254/latest/meta-data/",
                    "file:///c:/windows/win.ini"):
            self.assertEqual(self.keep(url), [], url)
        self.assertEqual(self.fetched, [])

    def test_path_traversal_is_dropped(self):
        self.assertEqual(self.keep("/static/uploads/../../etc/passwd"), [])

    def test_other_backend_paths_are_dropped(self):
        """只认 /static/ 下那两个目录，不是「本机地址就放行」。"""
        for url in ("/api/admin/users", "/docs", "/static/../app/main.py"):
            self.assertEqual(self.keep(url), [], url)

    def test_non_image_data_uri_is_dropped(self):
        self.assertEqual(self.keep("data:text/html;base64,PHNjcmlwdD4="), [])

    def test_oversized_data_uri_is_dropped(self):
        big = "data:image/png;base64," + "A" * (vision.MAX_BYTES * 2)
        self.assertEqual(self.keep(big), [])

    def test_at_most_three_images(self):
        got = vision.safe_images([JPG] * 9, "http://x", fetch=self.fetch)
        self.assertEqual(len(got), vision.MAX_IMAGES)


class TestFetch(unittest.TestCase):
    """真正去取图的那个函数，不打桩。

    上面那组用例全部传了假的 `fetch`——**于是把真正会执行的那几行
    整个绕开了**。实际跑起来第一次就 500：`_PNG_MAGIC` 根本没定义，
    而所有测试都是绿的。桩打在哪里，哪里就没人看着。
    """

    def call(self, data):
        class _Resp(object):
            def __init__(self, blob):
                self.blob = blob

            def read(self, n=None):
                return self.blob[:n] if n else self.blob

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        real = vision.urllib.request.urlopen
        vision.urllib.request.urlopen = lambda *a, **k: _Resp(data)
        try:
            return vision._fetch("http://127.0.0.1:8080/static/houses/a.jpg")
        finally:
            vision.urllib.request.urlopen = real

    def test_jpeg_becomes_a_jpeg_data_uri(self):
        got = self.call(bytes([0xff, 0xd8, 0xff]) + b"body")
        self.assertTrue(got.startswith("data:image/jpeg;base64,"))

    def test_png_is_detected_by_its_magic_bytes(self):
        got = self.call(bytes([0x89]) + b"PNG" + bytes([13, 10, 26, 10]) + b"body")
        self.assertTrue(got.startswith("data:image/png;base64,"))

    def test_empty_response_yields_nothing(self):
        self.assertEqual(self.call(b""), "")

    def test_oversized_response_is_rejected(self):
        self.assertEqual(self.call(b"x" * (vision.MAX_BYTES + 1)), "")


if __name__ == "__main__":
    unittest.main()
