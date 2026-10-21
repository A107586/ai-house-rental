"""请求编号（app/tracing.py）：每个响应都带编号；智能体传来的编号原样沿用。"""
import json
import logging

from tests.base import ApiTestCase


class TracingTest(ApiTestCase):

    def test_every_response_carries_a_request_id(self):
        res = self.client.get("/api/houses")
        self.assertEqual(res.status_code, 200)
        self.assertRegex(res.headers.get("X-Request-ID", ""), r"^[0-9a-f]{16}$")

    def test_id_from_the_agent_is_reused(self):
        # 智能体调后端时把它那一轮的编号带过来，两边日志用同一个编号串起来
        res = self.client.get("/api/houses", headers={"X-Request-ID": "turn-abc12345"})
        self.assertEqual(res.headers.get("X-Request-ID"), "turn-abc12345")

    def test_error_responses_carry_it_too(self):
        res = self.client.get("/api/auth/me")
        self.assertEqual(res.status_code, 401)
        self.assertTrue(res.headers.get("X-Request-ID"))

    def test_access_log_is_one_json_line(self):
        records = []
        handler = logging.Handler()
        handler.emit = lambda r: records.append(json.loads(r.getMessage()))
        logger = logging.getLogger("rental.access")
        level = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        try:
            self.client.get("/api/houses", headers={"X-Request-ID": "turn-abc12345"})
        finally:
            logger.removeHandler(handler)
            logger.setLevel(level)
        line = next(r for r in records if r["path"] == "/api/houses")
        self.assertEqual((line["service"], line["request_id"], line["status"]),
                         ("server", "turn-abc12345", 200))
