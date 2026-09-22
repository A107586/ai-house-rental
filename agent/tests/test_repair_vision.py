# -*- coding: utf-8 -*-
"""报修照片识别。

两件事分开测，因为它们坏起来的样子完全不同：

* `safe_images` 坏了是**安全问题**——用户指定一个地址，模型服务替他去访问。
* `parse` 坏了是**业务问题**——把「没有损坏」读成「有损坏」，
  一张干净厨房的照片就能派出一个水暖工。

两者都不需要真的调模型，所以全部是离线用例，跑在没有大模型的环境里。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agents.repair import agent as repair            # noqa: E402
from app.agents.repair.tools import vision               # noqa: E402
from app.llm.base import LLMProvider, LLMUnavailable     # noqa: E402

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


class TestParse(unittest.TestCase):
    """模型那两行怎么写歪的都得兜住。"""

    def test_reads_the_verdict_and_the_detail(self):
        self.assertEqual(vision.parse("有\n厨房水槽下方管接口渗水"),
                         (True, "厨房水槽下方管接口渗水"))

    def test_no_damage_is_not_read_as_damage(self):
        """「没有」里面也有个「有」字。用 in 判断就会在这里翻车。"""
        ok, _ = vision.parse("没有\n这是一间干净的厨房")
        self.assertFalse(ok)

    def test_markdown_wrapping_is_tolerated(self):
        self.assertEqual(vision.parse("**有**\n\n阳台墙面发霉"),
                         (True, "阳台墙面发霉"))

    def test_single_line_with_colon(self):
        self.assertEqual(vision.parse("有：卫生间地面积水"),
                         (True, "卫生间地面积水"))

    def test_unparsable_output_is_treated_as_no_information(self):
        for text in ("", None, "我无法判断这张图片", "1. 这是厨房"):
            self.assertEqual(vision.parse(text), (False, ""), repr(text))


class _SaysDamage(LLMProvider):
    def complete_vision(self, system_prompt, user_prompt, images,
                        max_tokens=None, deadline=None):
        return "有\n卫生间洗手池下方管道接口渗水，地面有积水"


class _SaysClean(LLMProvider):
    def complete_vision(self, system_prompt, user_prompt, images,
                        max_tokens=None, deadline=None):
        return "没有\n这是一间厨房，橱柜和灶台都完好"


class _NoVision(LLMProvider):
    """只实现了 `complete` 的 provider——完全合法，识图该优雅降级。"""

    def complete(self, system_prompt, user_prompt, max_tokens=None,
                 deadline=None):
        return "{}"


class TestRepairWithPhoto(unittest.TestCase):
    """照片进来之后，分派仍然走原来那套规则。"""

    def test_photo_alone_produces_a_work_order(self):
        out = repair.run("", provider=_SaysDamage(), submit=False,
                         images=[JPG])
        self.assertTrue(out["ok"])
        self.assertEqual(out["category"], "水暖")
        self.assertEqual(out["urgency"], "紧急")     # 渗水是安全底线问题
        self.assertIn("渗水", out["vision"])

    def test_photo_detail_is_visible_to_the_user(self):
        """派单依据必须摆在用户眼前，不能拿他没见过的句子去派单。"""
        out = repair.run("", provider=_SaysDamage(), submit=False,
                         images=[JPG])
        self.assertTrue(out["vision"])

    def test_clean_photo_does_not_invent_a_fault(self):
        """拍一张干净的厨房，不能派个水暖工上门。"""
        out = repair.run("", provider=_SaysClean(), submit=False, images=[JPG])
        self.assertFalse(out["ok"])
        self.assertEqual(out["status"], "need_content")

    def test_text_and_photo_are_combined(self):
        out = repair.run("厨房有点问题", provider=_SaysDamage(), submit=False,
                         images=[JPG])
        self.assertEqual(out["category"], "水暖")   # 光靠原话只能落到「其他」

    def test_text_only_is_unchanged_when_a_photo_is_useless(self):
        out = repair.run("卫生间水管漏水", provider=_SaysClean(),
                         submit=False, images=[JPG])
        self.assertTrue(out["ok"])
        self.assertEqual(out["category"], "水暖")

    def test_provider_without_vision_degrades(self):
        """识图不可用时，文字报修照常能用。"""
        out = repair.run("卫生间水管漏水", provider=_NoVision(),
                         submit=False, images=[JPG])
        self.assertTrue(out["ok"])
        self.assertEqual(out["vision"], "")

    def test_base_provider_raises_unavailable_not_notimplemented(self):
        """默认实现要抛 LLMUnavailable——NotImplementedError 会冒到用户面前。"""
        with self.assertRaises(LLMUnavailable):
            LLMProvider().complete_vision("s", "u", [JPG])


if __name__ == "__main__":
    unittest.main()
