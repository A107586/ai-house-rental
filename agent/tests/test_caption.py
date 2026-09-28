# -*- coding: utf-8 -*-
"""房源图片说明的草稿生成。

这一层最危险的不是「写得不好」，是**写了照片上看不出来的东西**。
一句「两室一厅，近地铁三号线」挂在房源图下面，是要约内容的一部分，
而模型最擅长的恰恰是补这类听着自然、却没有依据的细节。

所以 `clean` 的判据是**宁可不出**：句子里出现了几室几厅、平米、租金、
地铁、学校这些词，整句作废，房东看到的是「这次没写出来」，
而不是一句需要他逐字核对的文案。

全部离线，不调模型。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agents.describe.tools import caption            # noqa: E402
from app.llm.base import LLMProvider, LLMUnavailable     # noqa: E402

JPG = "data:image/jpeg;base64,QUJDREVG"


class TestClean(unittest.TestCase):
    """模型那一句怎么写歪的都得兜住。"""

    def test_plain_sentence_passes(self):
        self.assertEqual(caption.clean("采光充足，绿植环绕的温馨客厅"),
                         "采光充足，绿植环绕的温馨客厅")

    def test_trailing_punctuation_and_quotes_are_stripped(self):
        """这句要贴在缩略图下面，那个位置上的句号只会显得多余。"""
        self.assertEqual(caption.clean("“明亮厨房，白色橱柜。”"),
                         "明亮厨房，白色橱柜")

    def test_only_the_first_line_is_used(self):
        self.assertEqual(caption.clean("简约北欧风卧室\n（仅供参考）"),
                         "简约北欧风卧室")

    def test_facts_that_cannot_be_seen_are_rejected(self):
        """照片上看不出来的东西，一律整句作废。"""
        for line in ("两室一厅采光充足",
                     "南向客厅，66平米",
                     "近地铁三号线，采光好",
                     "步行十分钟到学校",
                     "带电梯的明亮客厅",
                     "月租 2950 元的客厅"):
            self.assertEqual(caption.clean(line), "", line)

    def test_too_long_is_dropped_not_truncated(self):
        """截断会把半句话发出去，比没有更难看。"""
        long = "客" * (caption.MAX_CHARS + 1)
        self.assertEqual(caption.clean(long), "")

    def test_garbage_yields_nothing(self):
        for text in ("", None, "   ", "\n\n", "“”"):
            self.assertEqual(caption.clean(text), "", repr(text))


class _Writes(LLMProvider):
    def __init__(self, line):
        self.line = line
        self.seen = None

    def complete_vision(self, system_prompt, user_prompt, images,
                        max_tokens=None, deadline=None):
        self.seen = list(images)
        return self.line


class _NoVision(LLMProvider):
    """只实现了 `complete` 的 provider——识图该优雅降级，不该冒异常。"""

    def complete(self, system_prompt, user_prompt, max_tokens=None,
                 deadline=None):
        return "{}"


class _Explodes(LLMProvider):
    def complete_vision(self, system_prompt, user_prompt, images,
                        max_tokens=None, deadline=None):
        raise RuntimeError("网络炸了")


class TestDraft(unittest.TestCase):

    def test_draft_returns_the_cleaned_line(self):
        self.assertEqual(caption.draft([JPG], _Writes("明亮的客厅，落地窗采光")),
                         "明亮的客厅，落地窗采光")

    def test_only_one_image_is_sent(self):
        """一次一张。传一叠再让房东挨个核对，和让他自己写没什么区别。"""
        provider = _Writes("明亮的客厅")
        caption.draft([JPG, JPG, JPG], provider)
        self.assertEqual(len(provider.seen), 1)

    def test_no_images_no_call(self):
        provider = _Writes("不该被调到")
        self.assertEqual(caption.draft([], provider), "")
        self.assertIsNone(provider.seen)

    def test_provider_without_vision_degrades_quietly(self):
        self.assertEqual(caption.draft([JPG], _NoVision()), "")

    def test_unexpected_error_degrades_quietly(self):
        """写图片说明是纯增强，任何错都不该冒到房东面前。"""
        self.assertEqual(caption.draft([JPG], _Explodes()), "")

    def test_no_provider_at_all(self):
        self.assertEqual(caption.draft([JPG], None), "")

    def test_a_hallucinated_sentence_is_dropped_end_to_end(self):
        self.assertEqual(caption.draft([JPG], _Writes("两室一厅，近地铁")), "")


class TestBaseProviderStillRaises(unittest.TestCase):

    def test_default_complete_vision_is_unavailable(self):
        with self.assertRaises(LLMUnavailable):
            LLMProvider().complete_vision("s", "u", [JPG])


if __name__ == "__main__":
    unittest.main()
