# -*- coding: utf-8 -*-
"""助手对话里发照片（`/agent/chat` 的 `images`），以及报修「要拍张照片吗」接照片。

`_agent_chat`（路由本身）和看图都换成假的：这里验的是照片怎么进这一轮——
路由只拿用户原话、办成了事就不覆盖、没办成才看图作答、助手正等照片时照片
就是答案、报修刚问要不要拍照时自动填上、外链一律不认。
不依赖网络、数据库与大模型。
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import main  # noqa: E402
from app.agents.support import agent as support  # noqa: E402
from app.agents.support.tools import photo  # noqa: E402
from app.main import ChatRequest, agent_chat  # noqa: E402

# 这个文件测的是 legacy 链路里的 `_with_photos`（ASSISTANT_MODE=legacy）。
# agent 链路里照片直接交给模型看，见 test_assistant_agent。
_legacy = mock.patch.object(main.settings, "assistant_mode", "legacy")


def setUpModule():
    _legacy.start()


def tearDownModule():
    _legacy.stop()


URL = "/static/uploads/202609/a.jpg"
UPLOAD_ASK = {"action": "submit_repair", "slots": {},
              "ask": {"slot": "images", "question": "要拍张照片吗？",
                      "options": [{"value": "__skip__", "label": "跳过，直接提交"}], "kind": "upload"}}


class FakeChat:
    """记下每次交给路由的请求，按顺序返回预设的结果。"""

    def __init__(self, *results):
        self.calls = []
        self.results = list(results)

    def __call__(self, payload):
        self.calls.append(payload)
        return dict(self.results.pop(0)) if self.results else {"message": "路由的回答"}


def run(fake, seen="照片里是卫生间天花板，有一片水渍。可以直接说「帮我报修」。", **kwargs):
    with mock.patch.object(main, "_agent_chat", fake), \
         mock.patch.object(main.photo, "answer", return_value=seen) as answer, \
         mock.patch.object(main.repair_vision, "safe_images", side_effect=lambda urls, base: urls), \
         mock.patch.object(main.backend_client, "log_chat") as log:
        result = agent_chat(ChatRequest(session_id="s", token="t", **kwargs))
    return result, answer, log


class TestChatPhotos(unittest.TestCase):

    def test_no_images_is_untouched(self):
        fake = FakeChat()
        result, answer, _ = run(fake, text="押金怎么退")
        self.assertEqual(fake.calls[0].text, "押金怎么退")
        self.assertEqual(fake.calls[0].session_id, "s")     # 原路由自己写记录
        answer.assert_not_called()

    def test_router_sees_only_what_the_user_said(self):
        fake = FakeChat({"message": "关于押金……"})
        run(fake, text="这里漏水了", images=[URL])
        self.assertEqual(fake.calls[0].text, "这里漏水了")
        self.assertEqual(fake.calls[0].session_id, "")      # 记录由看图这层写

    def test_unacted_answer_is_replaced_and_reference_passed(self):
        fake = FakeChat({"message": "我这边还没有可以介绍的房源。"})
        result, answer, log = run(fake, text="这个厨房怎么样", images=[URL])
        self.assertTrue(result["photo_answer"])
        self.assertTrue(result["message"].startswith("照片里是"))
        self.assertEqual(answer.call_args.kwargs["reference"], "我这边还没有可以介绍的房源。")
        logged = [c.args[2] for c in log.call_args_list]
        self.assertEqual(logged, ["这个厨房怎么样（附 1 张照片）", result["message"]])

    def test_acted_result_is_kept(self):
        fake = FakeChat({"message": "找到 3 套", "items": [{"house_no": "DL1"}]})
        result, answer, _ = run(fake, text="找类似的房子", images=[URL])
        self.assertEqual(result["message"], "找到 3 套")
        answer.assert_not_called()

    def test_photo_only_skips_the_router(self):
        fake = FakeChat()
        result, answer, _ = run(fake, text="", images=[URL])
        self.assertEqual(fake.calls, [])
        self.assertEqual(answer.call_args.kwargs["said"], "")
        self.assertEqual(result["status"], "answered")

    def test_photo_only_keeps_a_pending_form(self):
        pending = {"action": "publish_house", "slots": {"title": "x"}, "ask": {"slot": "rent"}}
        result, _, _ = run(FakeChat(), text="", images=[URL], pending=pending)
        self.assertEqual(result["pending"], pending)

    def test_unreadable_photo_keeps_router_answer(self):
        fake = FakeChat({"message": "路由的回答"})
        result, _, _ = run(fake, seen="", text="看看", images=[URL])
        self.assertEqual(result["message"], "路由的回答")
        self.assertNotIn("photo_answer", result)

    def test_pending_upload_takes_images_as_answer(self):
        fake = FakeChat({"message": "确认提交报修？", "action": {"name": "submit_repair"}})
        _, answer, _ = run(fake, text="", pending=UPLOAD_ASK, images=[URL, "/static/uploads/b.jpg"])
        self.assertEqual(fake.calls[0].text, URL + ",/static/uploads/b.jpg")
        answer.assert_not_called()

    def test_repair_photo_question_is_answered_automatically(self):
        fake = FakeChat({"message": "要拍张照片吗？", "pending": UPLOAD_ASK},
                        {"message": "确认提交报修？", "action": {"name": "submit_repair"}})
        result, answer, _ = run(fake, text="卫生间漏水了，帮我报修", images=[URL])
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(fake.calls[1].text, URL)
        self.assertEqual(fake.calls[1].pending, UPLOAD_ASK)
        self.assertEqual(result["action"]["name"], "submit_repair")
        answer.assert_not_called()

    def test_foreign_urls_are_ignored(self):
        fake = FakeChat()
        _, answer, _ = run(fake, text="看看", images=["http://evil.example/x.jpg", "/etc/passwd"])
        self.assertEqual(fake.calls[0].text, "看看")
        answer.assert_not_called()


class TestUploadAnswersSlot(unittest.TestCase):
    """「选照片」按钮把地址当答案发回来：得认成在回答，而不是插了句别的话。"""

    def test_managed_urls_answer_the_upload_slot(self):
        self.assertTrue(support._answers_slot("submit_repair", UPLOAD_ASK["ask"], URL, UPLOAD_ASK))
        self.assertTrue(support._answers_slot("submit_repair", UPLOAD_ASK["ask"],
                                              URL + ",/static/uploads/b.jpg", UPLOAD_ASK))
        self.assertTrue(support.claims_pending(URL, UPLOAD_ASK))

    def test_foreign_or_mixed_do_not(self):
        ask = UPLOAD_ASK["ask"]
        self.assertFalse(support._answers_slot("submit_repair", ask, "http://evil.example/x.jpg", UPLOAD_ASK))
        self.assertFalse(support._answers_slot("submit_repair", ask, URL + ",押金怎么退", UPLOAD_ASK))

    def test_only_for_upload_asks(self):
        ask = {"slot": "content", "question": "哪里坏了？", "options": []}
        self.assertFalse(support._answers_slot("submit_repair", ask, URL, {"action": "submit_repair"}))


class TestPhotoClean(unittest.TestCase):

    def test_strips_markdown_keeps_lines(self):
        self.assertEqual(photo.clean("- **卫生间**天花板\n\n- 有一片水渍"), "卫生间天花板\n有一片水渍")

    def test_empty(self):
        self.assertEqual(photo.clean(""), "")
        self.assertEqual(photo.answer([], object()), "")


if __name__ == "__main__":
    unittest.main()
