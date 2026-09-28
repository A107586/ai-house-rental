# -*- coding: utf-8 -*-
"""报修照片识别。

`parse` 坏了是业务问题：把「没有损坏」读成「有损坏」，
一张干净厨房的照片就能派出一个水暖工。

图片地址的收敛（`safe_images` / `_fetch`）搬去了 `tests/test_images.py`——
那一套是安全问题，且不止报修在用。

全部是离线用例，跑在没有大模型的环境里。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agents.repair import agent as repair            # noqa: E402
from app.agents.repair.tools import vision               # noqa: E402
from app.llm.base import LLMProvider, LLMUnavailable     # noqa: E402

JPG = "data:image/jpeg;base64,QUJDREVG"


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
