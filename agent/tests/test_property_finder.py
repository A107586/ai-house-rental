"""房源智能体端到端测试。

用假后端替换真实 HTTP 调用，因此不需要启动 SpringBoot、
也不需要导入数据库即可运行。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agents.property_finder import agent  # noqa: E402
from app.agents.property_finder.tools.search_properties import (  # noqa: E402
    build_params, score_by_features, summarize)
from app.backend import BackendError  # noqa: E402
from app.llm.base import NullProvider  # noqa: E402


class FakeBackend(object):
    """假后端：记录收到的参数，返回预设房源。"""

    def __init__(self, items=None, fail=False):
        self.items = items or []
        self.fail = fail
        self.last_params = None

    def list_properties(self, params):
        self.last_params = params
        if self.fail:
            raise BackendError("模拟后端不可用")
        return {"list": self.items, "total": len(self.items)}


def make_house(bianhao, zujia, huxing="两室一厅", diqu="中山区", xiangqing=""):
    return {
        "id": bianhao,
        "fangwubianhao": "F%03d" % bianhao,
        "zujia": zujia,
        "fangwuhuxing": huxing,
        "suozaidiqu": diqu,
        "mianji": "80",
        "xiangqing": xiangqing,
    }


class TestBuildParams(unittest.TestCase):
    """业务语义字段必须正确转成 MyBatis-Plus 的区间参数约定。"""

    def test_range_mapping(self):
        params = build_params({"zujia_min": 2000, "zujia_max": 3000})
        self.assertEqual(params["zujia_start"], 2000)
        self.assertEqual(params["zujia_end"], 3000)
        self.assertNotIn("zujia_min", params)

    def test_area_mapping(self):
        params = build_params({"mianji_min": 80})
        self.assertEqual(params["mianji_start"], 80)

    def test_pass_through(self):
        params = build_params({"suozaidiqu": "中山区", "fangwuhuxing": "两室一厅"})
        self.assertEqual(params["suozaidiqu"], "中山区")
        self.assertEqual(params["fangwuhuxing"], "两室一厅")

    def test_pagination_defaults(self):
        params = build_params({})
        self.assertEqual(params["page"], 1)
        self.assertEqual(params["limit"], 10)


class TestFeatureScoring(unittest.TestCase):

    def test_score_counts_matches(self):
        house = make_house(1, 2500, xiangqing="精装修，有电梯，紧邻地铁站")
        self.assertEqual(score_by_features(house, ["电梯", "地铁"]), 2)

    def test_no_features_scores_zero(self):
        self.assertEqual(score_by_features(make_house(1, 2500), []), 0)


class TestSummarize(unittest.TestCase):

    def test_no_result_suggests_relaxing_price(self):
        message = summarize([], 0, {"zujia_max": 3000}, [])
        self.assertIn("3000", message)
        self.assertIn("没有找到", message)

    def test_result_message(self):
        items = [make_house(1, 2200), make_house(2, 2800)]
        message = summarize(items, 2, {"suozaidiqu": "中山区"}, [])
        self.assertIn("2", message)
        self.assertIn("中山区", message)
        self.assertIn("2200", message)


class TestAgentRun(unittest.TestCase):

    def test_full_flow(self):
        backend = FakeBackend([make_house(1, 2200), make_house(2, 2800)])
        result = agent.run("中山区两室一厅，租金三千以内",
                           provider=NullProvider(), backend=backend)
        self.assertTrue(result["ok"])
        self.assertEqual(result["source"], "rules")
        self.assertEqual(result["total"], 2)
        self.assertEqual(backend.last_params["suozaidiqu"], "中山区")
        self.assertEqual(backend.last_params["zujia_end"], 3000)

    def test_features_reorder_results(self):
        """带电梯的房源应排在前面。"""
        backend = FakeBackend([
            make_house(1, 2200, xiangqing="普通装修"),
            make_house(2, 2800, xiangqing="有电梯，近地铁"),
        ])
        result = agent.run("中山区两室一厅三千以内，最好有电梯离地铁近",
                           provider=NullProvider(), backend=backend)
        self.assertEqual(result["items"][0]["id"], 2)

    def test_backend_down_is_graceful(self):
        """后端挂掉时给用户可读提示，而不是抛异常。"""
        backend = FakeBackend(fail=True)
        result = agent.run("中山区两室一厅三千以内",
                           provider=NullProvider(), backend=backend)
        self.assertFalse(result["ok"])
        self.assertIn("暂时不可用", result["message"])

    def test_unparseable_query_asks_for_detail(self):
        backend = FakeBackend([])
        result = agent.run("你好啊", provider=NullProvider(), backend=backend)
        self.assertFalse(result["ok"])
        self.assertIn("说得具体一点", result["message"])
        self.assertIsNone(backend.last_params, "没听懂时不应该去查数据库")


class TestSanitize(unittest.TestCase):
    """大模型返回的筛选条件必须过白名单，防止脏参数打到后端。"""

    def test_drops_unknown_field(self):
        clean = agent._sanitize({"suozaidiqu": "中山区", "drop_table": "users"})
        self.assertEqual(clean, {"suozaidiqu": "中山区"})

    def test_coerces_numeric_string(self):
        self.assertEqual(agent._sanitize({"zujia_max": "3000"})["zujia_max"], 3000)

    def test_drops_negative_and_invalid(self):
        self.assertEqual(agent._sanitize({"zujia_max": -1}), {})
        self.assertEqual(agent._sanitize({"zujia_max": "很贵"}), {})

    def test_non_dict(self):
        self.assertEqual(agent._sanitize("not a dict"), {})



class TestResponseShape(unittest.TestCase):
    """所有返回都必须带 status，前端才能只写一套判断逻辑。"""

    REQUIRED = ("intent", "status", "ok", "message")

    def test_found(self):
        backend = FakeBackend([make_house(1, 2200)])
        result = agent.run("中山区两室一厅三千以内",
                           provider=NullProvider(), backend=backend)
        for key in self.REQUIRED:
            self.assertIn(key, result)
        self.assertEqual(result["status"], "found")

    def test_empty(self):
        backend = FakeBackend([])
        result = agent.run("中山区两室一厅三千以内",
                           provider=NullProvider(), backend=backend)
        self.assertEqual(result["status"], "empty")

    def test_error(self):
        result = agent.run("中山区两室一厅三千以内",
                           provider=NullProvider(), backend=FakeBackend(fail=True))
        self.assertEqual(result["status"], "error")

    def test_unparsed(self):
        result = agent.run("你好啊", provider=NullProvider(), backend=FakeBackend([]))
        self.assertEqual(result["status"], "unparsed")


if __name__ == "__main__":
    unittest.main()
