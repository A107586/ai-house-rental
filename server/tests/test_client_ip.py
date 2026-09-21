# -*- coding: utf-8 -*-
"""客户端地址的取法。

这一组全部来自一个实测出来的洞：**登录限流可以用一个请求头绕过**。

    同一个伪造 IP 连打 10 次: [401×8, 429, 429]   ← 限住了
    换成 8.8.8.8: 401                              ← 又放行了
    换成 7.7.7.7: 401

原因是限流按 IP 计数，而 IP 取的是 `X-Forwarded-For` 的第一段——
那一段是**请求方自己填的**。同一份错误的代码在 `oplog.py` 里还有一份，
于是操作日志里「谁在哪个 IP 上做了什么」同样可以被伪造。

现在的规则：默认不信任转发头；部署在反向代理后面时设
`TRUSTED_PROXY_HOPS`，并且**从右往左数**。
"""

import unittest

from app import ratelimit
from app.netaddr import client_ip
from tests.base import ApiTestCase


class FakeClient(object):
    def __init__(self, host):
        self.host = host


class FakeRequest(object):
    """只带 headers 与 client 的假请求，够 client_ip 用。"""

    def __init__(self, peer, forwarded=None):
        self.client = FakeClient(peer) if peer else None
        self.headers = {}
        if forwarded is not None:
            self.headers["x-forwarded-for"] = forwarded


class TestClientIp(unittest.TestCase):

    def test_forwarded_header_is_ignored_without_a_proxy(self):
        """没有反向代理时，转发头一律不信。

        服务直接对外时，那个头百分之百是请求方自己写的。
        """
        request = FakeRequest("203.0.113.9", forwarded="1.2.3.4")
        self.assertEqual(client_ip(request, hops=0), "203.0.113.9")

    def test_one_proxy_takes_the_rightmost_entry(self):
        """一层代理时取最右边那一段——那一段是代理亲眼看见的地址。

        nginx 的 `$proxy_add_x_forwarded_for` 展开成
        「客户端发来的内容, 代理看到的地址」，所以真话在右边。
        """
        request = FakeRequest("10.0.0.2",
                              forwarded="1.2.3.4, 198.51.100.7")
        self.assertEqual(client_ip(request, hops=1), "198.51.100.7")

    def test_forged_prefix_cannot_move_the_trusted_entry(self):
        """**客户端塞多少段进来都没用**，只会把自己挤得更左。"""
        request = FakeRequest("10.0.0.2",
                              forwarded="9.9.9.9, 8.8.8.8, 7.7.7.7, 198.51.100.7")
        self.assertEqual(client_ip(request, hops=1), "198.51.100.7")

    def test_two_proxies_count_from_the_right(self):
        """两层代理就从右边数第二个：最右边那段是内层代理的地址。"""
        request = FakeRequest("10.0.0.2",
                              forwarded="1.2.3.4, 198.51.100.7, 10.0.0.9")
        self.assertEqual(client_ip(request, hops=2), "198.51.100.7")

    def test_short_chain_falls_back_to_the_leftmost(self):
        """链比代理层数短时取最左边，不越界。"""
        request = FakeRequest("10.0.0.2", forwarded="198.51.100.7")
        self.assertEqual(client_ip(request, hops=3), "198.51.100.7")

    def test_no_peer_and_no_header_returns_empty(self):
        """取不到就返回空串，**不要编一个默认值**——
        编出来的 IP 会被写进操作日志，当成证据。"""
        self.assertEqual(client_ip(FakeRequest(None), hops=0), "")


class TestRateLimitCannotBeBypassedByHeader(ApiTestCase):
    """端到端：换一个转发头，限流不该重新开始。"""

    def setUp(self):
        super(TestRateLimitCannotBeBypassedByHeader, self).setUp()
        ratelimit.reset()

    def tearDown(self):
        ratelimit.reset()
        super(TestRateLimitCannotBeBypassedByHeader, self).tearDown()

    def attempt(self, forwarded):
        return self.client.post(
            "/api/auth/login",
            json={"username": "nobody", "password": "wrong"},
            headers={"X-Forwarded-For": forwarded}).status_code

    def test_changing_the_forwarded_header_does_not_reset_the_counter(self):
        codes = [self.attempt("9.9.9.9") for _ in range(10)]
        self.assertIn(429, codes, "连打十次都没被限流，限流本身失效了")
        self.assertEqual(self.attempt("8.8.8.8"), 429,
                         "换一个 X-Forwarded-For 就又放行——限流等于不存在")
        self.assertEqual(self.attempt("1.1.1.1"), 429)


if __name__ == "__main__":
    unittest.main()
