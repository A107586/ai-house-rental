"""大模型调用的重试测试。

原先任何一次失败都直接降级到纯规则。但超时与限流是**可重试的瞬时故障**
——尤其限流，等半秒往往就好了，一次失败就放弃过于保守。

这批用例守的重点是**「什么时候不该重试」**：区分「暂时不行」和「根本
不行」是重试的全部意义所在，不分就成了固定把延迟放大三倍。
"""

import os
import socket
import sys
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.llm.base import LLMUnavailable  # noqa: E402
from app.llm import providers  # noqa: E402


def make_provider():
    cls = providers.OpenAICompatibleProvider
    return cls(api_key="k", base_url="https://example.com/v1",
               model="test", timeout=1, max_tokens=64)


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        import json
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


OK_BODY = {"choices": [{"message": {"content": "结果"}}]}


class RetryCase(unittest.TestCase):

    def setUp(self):
        self.provider = make_provider()
        # 真 sleep 会让这组用例跑好几秒，且测的不是等待本身
        patcher = mock.patch.object(providers.time, "sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)


class TestRetryable(RetryCase):
    """瞬时故障要重试。"""

    def test_succeeds_first_try(self):
        with mock.patch("urllib.request.urlopen",
                        return_value=FakeResponse(OK_BODY)) as call:
            self.assertEqual(self.provider.complete("s", "u"), "结果")
            self.assertEqual(call.call_count, 1, "成功时不该重试")
            self.sleep.assert_not_called()

    def test_recovers_after_rate_limit(self):
        """429 等半秒往往就好了——这正是重试最有价值的场景。"""
        responses = [
            urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None),
            FakeResponse(OK_BODY),
        ]
        with mock.patch("urllib.request.urlopen",
                        side_effect=responses) as call:
            self.assertEqual(self.provider.complete("s", "u"), "结果")
            self.assertEqual(call.call_count, 2)
            self.assertEqual(self.sleep.call_count, 1)

    def test_recovers_after_timeout(self):
        responses = [socket.timeout("timed out"), FakeResponse(OK_BODY)]
        with mock.patch("urllib.request.urlopen", side_effect=responses):
            self.assertEqual(self.provider.complete("s", "u"), "结果")

    def test_recovers_after_connection_error(self):
        responses = [urllib.error.URLError(socket.gaierror("dns")),
                     FakeResponse(OK_BODY)]
        with mock.patch("urllib.request.urlopen", side_effect=responses):
            self.assertEqual(self.provider.complete("s", "u"), "结果")

    def test_gives_up_after_max_attempts(self):
        """重试要有上限，不能让用户干等。"""
        error = urllib.error.HTTPError("u", 503, "Unavailable", {}, None)
        with mock.patch("urllib.request.urlopen",
                        side_effect=error) as call:
            with self.assertRaises(LLMUnavailable):
                self.provider.complete("s", "u")
            self.assertEqual(call.call_count, providers.
                             OpenAICompatibleProvider.MAX_ATTEMPTS)

    def test_backoff_grows(self):
        """指数退避：429 时服务端往往还在过载，立刻重试只会加剧。"""
        error = urllib.error.HTTPError("u", 429, "Too Many", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(LLMUnavailable):
                self.provider.complete("s", "u")
        waits = [c.args[0] for c in self.sleep.call_args_list]
        self.assertEqual(len(waits), 2)
        self.assertGreater(waits[1], waits[0], "第二次等待应当更久")


class TestNotRetryable(RetryCase):
    """**永久性错误不该重试**——重试多少次都一样，只会白白拖长响应。"""

    def test_bad_request_fails_fast(self):
        error = urllib.error.HTTPError("u", 400, "Bad Request", {}, None)
        with mock.patch("urllib.request.urlopen",
                        side_effect=error) as call:
            with self.assertRaises(LLMUnavailable):
                self.provider.complete("s", "u")
            self.assertEqual(call.call_count, 1)
            self.sleep.assert_not_called()

    def test_unauthorized_fails_fast(self):
        """密钥不对，重试三次还是不对。"""
        error = urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)
        with mock.patch("urllib.request.urlopen",
                        side_effect=error) as call:
            with self.assertRaises(LLMUnavailable):
                self.provider.complete("s", "u")
            self.assertEqual(call.call_count, 1)

    def test_forbidden_fails_fast(self):
        error = urllib.error.HTTPError("u", 403, "Forbidden", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=error) as call:
            with self.assertRaises(LLMUnavailable):
                self.provider.complete("s", "u")
            self.assertEqual(call.call_count, 1)

    def test_broken_json_fails_fast(self):
        """解析异常不是瞬时故障，重试没有意义。"""
        with mock.patch("urllib.request.urlopen",
                        side_effect=ValueError("bad json")) as call:
            with self.assertRaises(LLMUnavailable):
                self.provider.complete("s", "u")
            self.assertEqual(call.call_count, 1)


class TestErrorMessage(RetryCase):

    def test_message_never_leaks_the_key(self):
        """报错里绝不能带上密钥。响应体可能把它原样回显。"""
        error = urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(LLMUnavailable) as caught:
                self.provider.complete("s", "u")
        self.assertNotIn("k", str(caught.exception).replace("大模型", ""))
