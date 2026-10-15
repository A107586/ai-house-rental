"""业务后端客户端对错误响应的处理。

后端报错时智能体要把后端的说明转给用户，但只转能读的那种：
FastAPI 的 422 把 detail 写成校验错误列表，原样转出去就是一串 Python 字面量。
"""

import io
import os
import sys
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import backend  # noqa: E402
from app.backend import BackendClient, BackendError  # noqa: E402


def http_error(code, body):
    return urllib.error.HTTPError(
        "http://backend/x", code, "err", {}, io.BytesIO(body.encode("utf-8")))


class BackendErrorDetailTest(unittest.TestCase):

    def call_with(self, error):
        client = BackendClient(base_url="http://backend", timeout=1)
        with mock.patch.object(backend.settings, "under_test", False), \
                mock.patch.object(backend.urllib.request, "urlopen",
                                  side_effect=error):
            with self.assertRaises(BackendError) as caught:
                client.get("/api/x")
        return str(caught.exception)

    def test_string_detail_is_passed_through(self):
        error = http_error(409, '{"detail": "该时段已被预约"}')
        self.assertEqual(self.call_with(error), "该时段已被预约")
        self.assertTrue(error.fp is None or error.fp.closed)

    def test_validation_list_falls_back_to_status(self):
        body = '{"detail": [{"loc": ["body", "phone"], "msg": "field required"}]}'
        self.assertEqual(self.call_with(http_error(422, body)), "后端返回 HTTP 422")

    def test_unparsable_body_falls_back_to_status(self):
        self.assertEqual(self.call_with(http_error(502, "<html>bad gateway")),
                         "后端返回 HTTP 502")


if __name__ == "__main__":
    unittest.main()
