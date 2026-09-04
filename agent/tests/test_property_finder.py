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
    build_params, summarize)
from app.backend import BackendError  # noqa: E402
from app.llm.base import LLMProvider, NullProvider  # noqa: E402


class FakeProvider(LLMProvider):
    def __init__(self, value):
        self.value = value

    def complete(self, system_prompt, user_prompt, max_tokens=None):
        import json
        return json.dumps(self.value, ensure_ascii=False)


class FakeBackend(object):
    """假后端：记录收到的参数，返回预设房源。"""

    def __init__(self, items=None, fail=False):
        self.items = items or []
        self.fail = fail
        self.last_params = None

    def list_properties(self, params):
        """按参数真的过滤一遍。

        以前这里无视 params 直接返回全部，测试靠智能体本地过滤才通过。
        设施筛选挪到后端之后（K-06），假后端必须照着真后端的语义来，
        否则「智能体有没有把条件传下去」这件事就测不出来了——
        它可以什么都不传，测试照样绿。

        设施按**整词**匹配，和 `server/app/main.py::_feature_clause` 一致：
        子串匹配会让「无电梯」命中「电梯」。
        """
        self.last_params = params
        if self.fail:
            raise BackendError("模拟后端不可用")
        wanted = [f for f in (params.get("features") or "").split(",") if f]
        items = [it for it in self.items
                 if all(f in (it.get("tags") or []) for f in wanted)]
        return {"list": items, "total": len(items)}


def make_house(house_id, price, layout="两室一厅", district="中山区",
               description="", tags=None):
    return {
        "id": house_id,
        "house_no": "DL%04d" % house_id,
        "title": "%s %s" % (district, layout),
        "rent_price": price,
        "layout": layout,
        "district": district,
        "area": 80.0,
        "description": description,
        "tags": tags or [],
        "rent_type": "整租",
        "address": "",
        "community": "",
    }


class TestBuildParams(unittest.TestCase):
    """业务语义字段必须正确转成后端接口的参数名。"""

    def test_range_mapping(self):
        params = build_params({"zujia_min": 2000, "zujia_max": 3000})
        self.assertEqual(params["rent_price_min"], 2000)
        self.assertEqual(params["rent_price_max"], 3000)
        self.assertNotIn("zujia_min", params)

    def test_area_mapping(self):
        params = build_params({"mianji_min": 80})
        self.assertEqual(params["area_min"], 80)

    def test_pass_through(self):
        params = build_params({"suozaidiqu": "中山区", "fangwuhuxing": "两室一厅"})
        self.assertEqual(params["district"], "中山区")
        self.assertEqual(params["layout"], "两室一厅")

    def test_pagination_defaults(self):
        params = build_params({})
        self.assertEqual(params["page"], 1)
        self.assertEqual(params["limit"], 10)


class TestFeatureParams(unittest.TestCase):
    """设施偏好要作为查询参数交给后端，不能在本地过滤（K-06）。

    本地过滤有两个毛病：为了拿到正确的 total 得把候选集整个翻完
    （一次找房打好几趟后端），而且靠子串匹配——「无电梯」里含「电梯」，
    要电梯的人会被推一套没电梯的房。
    """

    def test_features_go_into_params(self):
        params = build_params({}, features=["电梯", "可养宠物"])
        self.assertEqual(params["features"], "电梯,可养宠物")

    def test_no_features_means_no_param(self):
        """没有设施条件时不要发一个空参数过去。"""
        self.assertNotIn("features", build_params({}))

    def test_features_combine_with_structured_filters(self):
        params = build_params({"suozaidiqu": "中山区", "zujia_max": 3000},
                              features=["电梯"])
        self.assertEqual(params["district"], "中山区")
        self.assertEqual(params["rent_price_max"], 3000)
        self.assertEqual(params["features"], "电梯")


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

    def test_llm_feature_only_result_is_not_marked_unavailable(self):
        """只有设施、没有 filters 也是有效结果。

        「想找允许带小动物的住处」解析不出地区价格，但「可养宠物」
        这一条已经足够执行搜索。旧逻辑只看 filters，把成功的调用
        误标成了「大模型不可用」。

        source 现在是 fused 而不是 llm：规则也参与了这次解析，
        它的结论被当作参考答案交给了大模型。
        """
        backend = FakeBackend([make_house(1, 2200, tags=["可养宠物"])])
        provider = FakeProvider({"filters": {}, "features": ["可养宠物"]})
        result = agent.run("想找允许带小动物的住处",
                           provider=provider, backend=backend)
        self.assertEqual(result["source"], "fused")
        self.assertEqual(result["total"], 1)

    def test_llm_feature_synonym_is_normalized(self):
        backend = FakeBackend([make_house(1, 2200, tags=["可养宠物"])])
        provider = FakeProvider({"filters": {}, "features": ["允许养猫"]})
        result = agent.run("想找允许养猫的住处",
                           provider=provider, backend=backend)
        self.assertEqual(result["features"], ["可养宠物"])
        self.assertEqual(result["total"], 1)

    def test_full_flow(self):
        backend = FakeBackend([make_house(1, 2200), make_house(2, 2800)])
        result = agent.run("中山区两室一厅，租金三千以内",
                           provider=NullProvider(), backend=backend)
        self.assertTrue(result["ok"])
        self.assertEqual(result["source"], "rules")
        self.assertEqual(result["total"], 2)
        self.assertEqual(backend.last_params["district"], "中山区")
        self.assertEqual(backend.last_params["rent_price_max"], 3000)

    def test_features_filter_results(self):
        """明确提出的设施条件必须全部满足，不能只参与排序。"""
        backend = FakeBackend([
            make_house(1, 2200, tags=["有阳台"]),
            make_house(2, 2800, tags=["电梯", "地铁"]),
        ])
        result = agent.run("中山区两室一厅三千以内，最好有电梯离地铁近",
                           provider=NullProvider(), backend=backend)
        self.assertEqual([item["id"] for item in result["items"]], [2])
        self.assertEqual(result["total"], 1)

    def test_feature_only_query_does_not_return_every_house(self):
        backend = FakeBackend([
            make_house(1, 2200, tags=["电梯"]),
            make_house(2, 2800, tags=["电梯", "可养宠物"]),
            make_house(3, 2600, tags=["可养宠物"]),
        ])
        result = agent.run("想找带电梯、可以养猫的房子",
                           provider=NullProvider(), backend=backend)
        self.assertEqual([item["id"] for item in result["items"]], [2])
        self.assertEqual(result["total"], 1)

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
