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

    def test_timeout_is_not_retried(self):
        """**超时不重试**，第一次挂住就收手。

        重试是给瞬时故障用的（限流、网关抖动，等半秒就好）。而这里的超时
        不是抖动，是中转站挂住不回——实测同一句话有时 4.6 秒返回、
        有时挂满超时，再发一次多半还是挂。

        重试它的代价是实打实的：一次挂死烧掉 7 秒，重试再烧 7 秒，
        预算见底，这一轮只能降级到规则——**而规则本来就能答对**，
        用户白等了两倍的时间拿到同一个答案。
        """
        responses = [socket.timeout("timed out"), FakeResponse(OK_BODY)]
        with mock.patch("urllib.request.urlopen",
                        side_effect=responses) as call:
            with self.assertRaises(LLMUnavailable):
                self.provider.complete("s", "u")
            self.assertEqual(call.call_count, 1, "超时之后不该再发第二次")

    def test_circuit_opens_after_consecutive_timeouts(self):
        """连挂两次就熔断，之后直接快速失败，不再发请求。

        挂的时候每个请求都要先等满超时才降级，而规则本来就能答——
        于是所有人都为同一个坑各付一次 7 秒。
        熔断就是「有人已经踩过了，后面的人别再踩」。
        """
        with mock.patch("urllib.request.urlopen",
                        side_effect=socket.timeout("timed out")) as call:
            for _ in range(providers.OpenAICompatibleProvider.CIRCUIT_AFTER):
                with self.assertRaises(LLMUnavailable):
                    self.provider.complete("s", "u")
            sent = call.call_count
            # 熔断已开：这一次应当**一个请求都不发**
            with self.assertRaises(LLMUnavailable):
                self.provider.complete("s", "u")
            self.assertEqual(call.call_count, sent, "熔断期间不该再发请求")

    def test_success_closes_the_circuit(self):
        """**恢复不用等满那一分钟**——上游好了就该立刻用起来。"""
        self.provider._consecutive_timeouts = 1
        with mock.patch("urllib.request.urlopen",
                        return_value=FakeResponse(OK_BODY)):
            self.assertEqual(self.provider.complete("s", "u"), "结果")
        self.assertEqual(self.provider._consecutive_timeouts, 0)
        self.assertEqual(self.provider._circuit_until, 0.0)

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


class TestTotalBudget(unittest.TestCase):
    """**总时限**：重试不能把一次调用拖到配置时长的三倍。

    `llm_timeout` 管的是单次连接。三次重试加退避，最坏是
    8 + 0.6 + 8 + 1.2 + 8 ≈ 26 秒——实测「水电费谁交」等了 25.97 秒，
    而配置里写的是 8 秒。K-10 把超时从 30 秒压到 8 秒，图的正是
    「宁可拿规则结果也别干等」，这条被重试悄悄绕过去了。

    这一组不打桩 `time.sleep` 之外的东西，靠**真的推进时钟**来验：
    每次 urlopen 都当成耗满一次超时。
    """

    def make(self, timeout=1.0, total_budget=None):
        cls = providers.OpenAICompatibleProvider
        return cls(api_key="k", base_url="https://example.com/v1",
                   model="test", timeout=timeout, max_tokens=64,
                   total_budget=total_budget)

    def test_default_budget_is_twice_the_timeout(self):
        self.assertEqual(self.make(timeout=8.0)._total_budget, 16.0)

    def test_explicit_budget_wins(self):
        self.assertEqual(self.make(timeout=8.0, total_budget=10)._total_budget,
                         10)

    def test_slow_timeouts_stop_early(self):
        """每次都耗满超时，第二次之后预算就没了，不该再试第三次。"""
        provider = self.make(timeout=1.0, total_budget=1.5)
        clock = [0.0]

        def slow(*args, **kwargs):
            clock[0] += 1.0           # 这次连接耗满一整个超时
            raise socket.timeout()

        with mock.patch.object(providers.time, "time", lambda: clock[0]),                 mock.patch.object(providers.time, "sleep",
                                  lambda n: clock.__setitem__(0, clock[0] + n)),                 mock.patch("urllib.request.urlopen", side_effect=slow) as call:
            with self.assertRaises(LLMUnavailable):
                provider.complete("s", "u")
        self.assertLess(call.call_count,
                        providers.OpenAICompatibleProvider.MAX_ATTEMPTS,
                        "预算已经用光，不该还发第三次请求")

    def test_fast_failures_still_get_all_attempts(self):
        """**限流那种失败得快的仍要重试满三次。**

        429 是立刻返回的，几乎不花时间——为了封顶总时长而少试一次，
        等于把「等半秒就好了」的情况也一起放弃了。
        """
        provider = self.make(timeout=1.0, total_budget=1.5)
        error = urllib.error.HTTPError("u", 429, "Too Many", {}, None)
        with mock.patch.object(providers.time, "sleep"),                 mock.patch("urllib.request.urlopen",
                           side_effect=error) as call:
            with self.assertRaises(LLMUnavailable):
                provider.complete("s", "u")
        self.assertEqual(call.call_count,
                         providers.OpenAICompatibleProvider.MAX_ATTEMPTS)


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
