# -*- coding: utf-8 -*-
"""流式输出：从没生成完的 JSON 里取回答、循环推的事件、/agent/chat/stream 的 SSE 格式。

不依赖网络、数据库与大模型。
"""

import asyncio
import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import main  # noqa: E402
from app.agents.assistant import loop  # noqa: E402
from app.agents.assistant.loop import AnswerStream  # noqa: E402
from app.agents.assistant.toolkit import Toolkit  # noqa: E402
from app.llm.base import LLMUnavailable  # noqa: E402
from app.main import ChatRequest  # noqa: E402
from tests.test_assistant_agent import ScriptedLLM, deps  # noqa: E402


def feed_all(pieces):
    stream = AnswerStream()
    return "".join(stream.feed(p) for p in pieces)


class TestAnswerStream(unittest.TestCase):

    def test_answer_comes_out_as_it_arrives(self):
        stream = AnswerStream()
        self.assertEqual(stream.feed('{"thought": "寒暄", "ans'), "")
        self.assertEqual(stream.feed('wer": "你'), "你")
        self.assertEqual(stream.feed('好，想找'), "好，想找")
        self.assertEqual(stream.feed('房吗？"}'), "房吗？")
        self.assertEqual(stream.feed(" 后面的不要"), "")

    def test_escapes_including_split_ones(self):
        raw = '{"answer": "第一行\\n第二行，他说\\"好\\"，\\u4e2d文"}'
        expected = '第一行\n第二行，他说"好"，中文'
        self.assertEqual(feed_all([raw]), expected)
        # 一个字一个字地喂，转义被切在两段之间
        self.assertEqual(feed_all(list(raw)), expected)

    def test_tool_call_yields_nothing(self):
        raw = '{"thought": "查账单", "tool": "my_bills", "params": {}}'
        self.assertEqual(feed_all(list(raw)), "")

    def test_plain_text_reply_streams_whole(self):
        self.assertEqual(feed_all(["押金", "七天内退。"]), "押金七天内退。")

    def test_answer_key_inside_thought_text_is_not_matched_early(self):
        # thought 里写了 answer 这个词但不是那个键
        raw = '{"thought": "直接 answer 就行", "answer": "好的"}'
        self.assertEqual(feed_all(list(raw)), "好的")


class StreamingLLM(ScriptedLLM):
    """把每条剧本拆成几段流式吐出来。"""

    def chat_stream(self, system, messages, max_tokens=None, deadline=None, temperature=0.0):
        raw = self.chat(system, messages, max_tokens=max_tokens, deadline=deadline)
        for i in range(0, len(raw), 5):
            yield raw[i:i + 5]


def run_with_events(llm, text="你好", **dep):
    events = []
    out = loop.run(text, [], llm, Toolkit(deps(**dep)), on_event=events.append)
    return out, events


class TestLoopEvents(unittest.TestCase):

    def test_step_then_streamed_answer(self):
        llm = StreamingLLM({"tool": "my_bills", "params": {}},
                           {"thought": "算一下", "answer": "最贵的是 3 月房租，3050 元。"})
        out, events = run_with_events(llm, "哪笔最贵")
        kinds = [e["type"] for e in events]
        self.assertEqual(kinds[0], "step")
        self.assertEqual(events[0]["label"], "在查你的账单")
        streamed = "".join(e["text"] for e in events if e["type"] == "delta")
        self.assertEqual(streamed, "最贵的是 3 月房租，3050 元。")
        self.assertEqual(out["message"], streamed)
        self.assertNotIn("reset", kinds)

    def test_reset_when_streamed_text_turns_out_not_final(self):
        # 模型先写了 answer，却又带着工具名——按约定这一步是调工具，推过的字作废
        llm = StreamingLLM({"answer": "我查一下", "tool": "my_bills", "params": {}},
                           {"answer": "查好了"})
        out, events = run_with_events(llm)
        kinds = [e["type"] for e in events]
        self.assertIn("reset", kinds)
        self.assertLess(kinds.index("reset"), kinds.index("step"))
        self.assertEqual(out["message"], "查好了")

    def test_falls_back_to_chat_when_stream_fails_before_any_text(self):
        class FlakyStream(ScriptedLLM):
            def chat_stream(self, *a, **k):
                raise LLMUnavailable("连不上")
                yield  # noqa: unreachable — 让它成为生成器

        out, events = run_with_events(FlakyStream({"answer": "你好"}))
        self.assertEqual(out["message"], "你好")
        self.assertEqual(events, [])

    def test_stream_breaking_midway_is_reported(self):
        class Breaks(ScriptedLLM):
            def chat_stream(self, *a, **k):
                yield '{"answer": "你'
                raise LLMUnavailable("断了")

        with self.assertRaises(LLMUnavailable):
            run_with_events(Breaks())

    def test_no_listener_means_no_streaming(self):
        llm = StreamingLLM({"answer": "你好"})
        with mock.patch.object(llm, "chat_stream", side_effect=AssertionError("不该流式")):
            out = loop.run("你好", [], llm, Toolkit(deps()))
        self.assertEqual(out["message"], "你好")


def collect_sse(response):
    async def drain():
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk if isinstance(chunk, str) else chunk.decode("utf-8"))
        return "".join(chunks)
    text = asyncio.run(drain())
    events = []
    for block in text.split("\n\n"):
        lines = [l for l in block.splitlines() if l and not l.startswith(":")]
        if not lines:
            continue
        name = lines[0].split(":", 1)[1].strip()
        data = json.loads(lines[1].split(":", 1)[1])
        events.append((name, data))
    return events


class TestStreamEndpoint(unittest.TestCase):

    def setUp(self):
        patches = [
            mock.patch.object(main.settings, "assistant_mode", "agent"),
            mock.patch.object(main.backend_client, "list_chat", return_value=[]),
            mock.patch.object(main.backend_client, "log_chat"),
            mock.patch.object(main, "_whoami", return_value={"logged_in": True, "role": "tenant"}),
            mock.patch.object(main, "_fetch_houses", return_value=[]),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_sse_events_end_with_the_same_result_as_chat(self):
        llm = StreamingLLM({"thought": "寒暄", "answer": "你好，想找什么样的房子？"})
        with mock.patch.object(main, "provider", llm):
            response = main.agent_chat_stream(ChatRequest(text="你好", session_id=""))
            self.assertEqual(response.media_type, "text/event-stream")
            events = collect_sse(response)
        names = [n for n, _ in events]
        self.assertEqual(names[-1], "done")
        self.assertIn("delta", names)
        self.assertEqual(events[-1][1]["result"]["message"], "你好，想找什么样的房子？")
        self.assertEqual(events[-1][1]["result"]["route_source"], "agent")

    def test_unavailable_model_still_ends_with_done(self):
        with mock.patch.object(main, "provider", SimpleNamespace(available=False)):
            events = collect_sse(main.agent_chat_stream(ChatRequest(text="你好", session_id="")))
        self.assertEqual([n for n, _ in events], ["done"])
        self.assertEqual(events[0][1]["result"]["status"], "unavailable")

    def test_crash_becomes_an_error_event(self):
        with mock.patch.object(main, "_agent_turn", side_effect=RuntimeError("boom")):
            events = collect_sse(main.agent_chat_stream(ChatRequest(text="你好", session_id="")))
        self.assertEqual(events, [("error", {"type": "error",
                                             "message": "这一轮处理出错了，稍后再试一次。"})])


if __name__ == "__main__":
    unittest.main()
