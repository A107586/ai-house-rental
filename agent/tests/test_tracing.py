# -*- coding: utf-8 -*-
"""可观测性：请求编号、编号跨服务传递、结构化日志、agent 分步计时。

不依赖网络、数据库与大模型。
"""

import asyncio
import json
import logging
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import backend, tracing  # noqa: E402
from app.agents.assistant import loop  # noqa: E402
from app.agents.assistant.toolkit import Toolkit  # noqa: E402
from tests.test_assistant_agent import ScriptedLLM, deps  # noqa: E402


def run_middleware(headers=None, status=200):
    """用一个假请求走一遍中间件，返回（响应头里的编号，处理请求时看到的编号）。"""
    seen = {}

    async def call_next(request):
        seen["inside"] = tracing.current()
        return SimpleNamespace(status_code=status, headers={})

    request = SimpleNamespace(headers=headers or {}, method="GET",
                              url=SimpleNamespace(path="/agent/chat"))
    response = asyncio.run(tracing.trace_requests(request, call_next))
    return response.headers[tracing.HEADER], seen["inside"]


class CaptureLog:
    """把 rental.access 的日志收进列表。"""

    def __enter__(self):
        self.records = []
        self.handler = logging.Handler()
        self.handler.emit = lambda r: self.records.append(json.loads(r.getMessage()))
        logger = logging.getLogger("rental.access")
        self._level, self._propagate = logger.level, logger.propagate
        logger.addHandler(self.handler)
        logger.setLevel(logging.INFO)
        return self.records

    def __exit__(self, *exc):
        logger = logging.getLogger("rental.access")
        logger.removeHandler(self.handler)
        logger.setLevel(self._level)


class TestRequestId(unittest.TestCase):

    def test_generated_when_missing_and_visible_inside_and_in_response(self):
        returned, inside = run_middleware()
        self.assertRegex(returned, r"^[0-9a-f]{16}$")
        self.assertEqual(inside, returned)
        self.assertEqual(tracing.current(), "")              # 请求结束就清掉

    def test_incoming_id_is_kept(self):
        returned, inside = run_middleware({tracing.HEADER: "abc12345-turn"})
        self.assertEqual(returned, "abc12345-turn")
        self.assertEqual(inside, "abc12345-turn")

    def test_suspicious_incoming_id_is_replaced(self):
        returned, _ = run_middleware({tracing.HEADER: 'x"\n{"event":"fake"}'})
        self.assertRegex(returned, r"^[0-9a-f]{16}$")

    def test_one_json_line_per_request(self):
        with CaptureLog() as records:
            returned, _ = run_middleware(status=404)
        self.assertEqual(len(records), 1)
        line = records[0]
        self.assertEqual((line["event"], line["service"], line["status"], line["request_id"]),
                         ("request", "agent", 404, returned))
        self.assertIn("ms", line)


class TestPropagation(unittest.TestCase):

    def test_backend_calls_carry_the_request_id(self):
        sent = {}

        def fake_urlopen(request, timeout=None):
            sent.update({k.lower(): v for k, v in request.header_items()})
            raise backend.urllib.error.URLError("不真的发")

        token = tracing.REQUEST_ID.set("turn-12345678")
        try:
            with mock.patch.object(backend.settings, "under_test", False), \
                 mock.patch.object(backend.urllib.request, "urlopen", fake_urlopen):
                with self.assertRaises(Exception):
                    backend.BackendClient().get("/api/houses")
        finally:
            tracing.REQUEST_ID.reset(token)
        self.assertEqual(sent.get(tracing.HEADER.lower()), "turn-12345678")


class TestAgentTimings(unittest.TestCase):

    def test_each_model_call_and_tool_is_timed(self):
        llm = ScriptedLLM({"tool": "my_bills", "params": {}}, {"answer": "你有 2 笔"})
        out = loop.run("我的账单", [], llm, Toolkit(deps()))
        steps = out["timings"]["steps"]
        self.assertEqual([s["kind"] for s in steps], ["model", "tool", "model"])
        self.assertEqual(steps[1]["tool"], "my_bills")
        self.assertGreaterEqual(out["timings"]["total_ms"], 0)


if __name__ == "__main__":
    unittest.main()
