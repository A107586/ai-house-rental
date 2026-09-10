# -*- coding: utf-8 -*-
"""工具调用层（G1）的回归测试。

这一层是新的**第一道路由**：每一句话都先经过它。所以它必须先满足
一条性质，才谈得上好不好用——

    **接不住的时候，一定要原样退回规则链路，不能吃掉这一轮。**

下面一半的用例测的都是这一条：模型没配、超时、输出坏了、挑了个
不存在的工具、把握不够、参数类型不对……每一种都得让路。

另一半测「翻译」是否正确：`ordinal: 3` 要还原成上一轮列表里的第三个，
不是让模型自己编一个 DL 编号；越界要单独回话，不能悄悄取第一套。

全部不依赖网络与真实大模型：provider 用假的，后端调用打桩。
"""

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agents.toolbox import catalog, planner, runner  # noqa: E402
from app.llm.base import LLMProvider, LLMUnavailable  # noqa: E402


class _Fake(LLMProvider):
    """按脚本回话的假模型。`payload` 为 None 表示这一问不可用。"""

    name = "fake"

    def __init__(self, payload=None, raise_exc=None, raw=None):
        self.payload = payload
        self.raise_exc = raise_exc
        self.raw = raw
        self.calls = 0

    def complete(self, system_prompt, user_prompt, max_tokens=None):
        self.calls += 1
        if self.raise_exc:
            raise self.raise_exc
        if self.raw is not None:
            return self.raw
        if self.payload is None:
            raise LLMUnavailable("测试里刻意不可用")
        return json.dumps(self.payload, ensure_ascii=False)

    @property
    def available(self):
        return True


class TestFallsBack(unittest.TestCase):
    """接不住就让路。**这一组比功能用例更重要**——
    它保证接上这一层不会让任何原本能用的路径变得不可用。"""

    def plan(self, provider):
        return planner.plan("随便说一句", provider)

    def test_no_provider(self):
        self.assertEqual(planner.plan("找房", None)["tool"], "none")

    def test_provider_unavailable(self):
        got = self.plan(_Fake(payload=None))
        self.assertEqual(got["tool"], "none")

    def test_provider_raises(self):
        """模型层抛了个意料之外的异常，也不能让这一轮炸掉。"""
        got = self.plan(_Fake(raise_exc=RuntimeError("网络炸了")))
        self.assertEqual(got["tool"], "none")

    def test_not_json(self):
        got = self.plan(_Fake(raw="我觉得应该找房吧"))
        self.assertEqual(got["tool"], "none")

    def test_unknown_tool(self):
        """模型编了一个不存在的工具名。"""
        got = self.plan(_Fake(payload={"tool": "buy_the_house",
                                       "confidence": 0.99}))
        self.assertEqual(got["tool"], "none")

    def test_explicit_none(self):
        got = self.plan(_Fake(payload={"tool": "none", "confidence": 0}))
        self.assertEqual(got["tool"], "none")

    def test_login_only_tool_when_logged_out(self):
        """未登录却挑了「查我的账单」。工具表里根本没给它这一项，
        采信它只会换来一句「请先登录」，而用户问的可能是别的事。"""
        got = planner.plan("我欠多少钱", _Fake(payload={
            "tool": "my_bills", "confidence": 0.95}), logged_in=False)
        self.assertEqual(got["tool"], "none")

    def test_staff_tool_for_tenant(self):
        got = planner.plan("谁约了我的房", _Fake(payload={
            "tool": "landlord_appointments", "confidence": 0.9}),
            logged_in=True, role="tenant")
        self.assertEqual(got["tool"], "none")


class TestParams(unittest.TestCase):
    """参数按 schema 收口：认识的留下、能修的修、修不了的丢。"""

    def pick(self, params):
        return planner.plan("找房", _Fake(payload={
            "tool": "search_houses", "params": params, "confidence": 0.9}))

    def test_number_as_string(self):
        """模型把数字写成字符串是常事，属于格式问题不是意图问题。"""
        got = self.pick({"rent_max": "3000"})
        self.assertEqual(got["params"]["rent_max"], 3000)

    def test_unknown_key_dropped(self):
        """schema 里没有的键一律丢掉。原样传给后端的话，
        它就成了一个谁也没审过的输入通道。"""
        got = self.pick({"district": "中山区", "sql": "drop table houses"})
        self.assertEqual(got["params"], {"district": "中山区"})

    def test_bad_type_dropped(self):
        """把区名填进租金上限——修不了，丢掉。带着坏参数去查库，
        查出来的空结果会被当成「没有符合的房源」。"""
        got = self.pick({"rent_max": "中山区"})
        self.assertNotIn("rent_max", got["params"])

    def test_scalar_to_list(self):
        got = self.pick({"features": "地铁"})
        self.assertEqual(got["params"]["features"], ["地铁"])

    def test_empty_values_dropped(self):
        got = self.pick({"district": "", "layout": None, "keyword": "桂林街"})
        self.assertEqual(got["params"], {"keyword": "桂林街"})

    def test_confidence_clamped(self):
        got = planner.plan("找房", _Fake(payload={
            "tool": "search_houses", "confidence": 9.9}))
        self.assertEqual(got["confidence"], 1.0)


class TestOrdinal(unittest.TestCase):
    """序号 → 编号的还原。**只有这一层知道「第三套」是屏幕上的哪一套**。"""

    NOS = ["DL0001", "DL0002", "DL0003"]

    def test_ordinal_resolves(self):
        self.assertEqual(
            runner._house_from({"ordinal": 3}, self.NOS, ""), "DL0003")

    def test_house_no_wins(self):
        """用户原话里就有编号时，以编号为准。"""
        self.assertEqual(
            runner._house_from({"house_no": "dl0009", "ordinal": 1},
                               self.NOS, ""), "DL0009")

    def test_out_of_range(self):
        """越界要能被认出来，**不能悄悄退成第一套**——
        用户会以为第二十套长第一套那样。"""
        from app import memory
        self.assertIs(runner._house_from({"ordinal": 20}, self.NOS, ""),
                      memory.OUT_OF_RANGE)

    def test_current_house_when_nothing_said(self):
        self.assertEqual(runner._house_from({}, self.NOS, "DL7777"), "DL7777")

    def test_compare_mixes_both(self):
        got = runner._houses_from({"ordinals": [1, 2]}, self.NOS)
        self.assertEqual(got, ["DL0001", "DL0002"])


class TestFilterMapping(unittest.TestCase):
    """工具参数 → 内部条件键的映射。

    **这一组的第一版是错的，而且测试是绿的。** 我按后端接口的键名
    （rent_price_max）写了映射，也按同一套名字写了断言——两边一起错，
    自然对得上。实际跑起来 `build_params` 一个都认不出来，
    「租金3000以内」返回了全部 95 套。

    所以现在的断言**直接引用被调用方的键表**（`_PASS_THROUGH` /
    `_RANGE_MAP`），而不是我自己写一遍字符串：拿自己以为的名字去断言，
    测的只是「我和我自己想的一样」。
    """

    def target_keys(self):
        from app.agents.property_finder.tools import search_properties as sp
        return set(sp._PASS_THROUGH) | set(sp._RANGE_MAP)

    def test_every_produced_key_is_recognized(self):
        """产出的每一个键，被调用方都必须认识。"""
        got = runner._filters_from({
            "district": "中山区", "layout": "两室一厅", "keyword": "桂林街",
            "rent_min": 1000, "rent_max": 3000,
            "area_min": 50, "area_max": 90})
        unknown = set(got) - self.target_keys()
        self.assertFalse(unknown, "这些键 build_params 不认识：%s" % unknown)

    def test_price_keys(self):
        got = runner._filters_from({"rent_min": 1000, "rent_max": 3000})
        self.assertEqual(got, {"zujia_min": 1000, "zujia_max": 3000})

    def test_area_keys(self):
        got = runner._filters_from({"area_min": 50, "area_max": 90})
        self.assertEqual(got, {"mianji_min": 50, "mianji_max": 90})

    def test_passthrough_keys(self):
        got = runner._filters_from({"district": "中山区", "layout": "两室一厅",
                                    "keyword": "桂林街"})
        self.assertEqual(got, {"suozaidiqu": "中山区",
                               "fangwuhuxing": "两室一厅",
                               "guanjianci": "桂林街"})

    def test_zero_is_kept(self):
        """0 是个有效的下限。用 `if params.get(k)` 判断会把它当成没填。"""
        self.assertEqual(runner._filters_from({"rent_min": 0}),
                         {"zujia_min": 0})

    def test_filters_actually_reach_the_backend_params(self):
        """端到端钉一次：走完 build_params，后端参数里必须有租金上限。

        **上面几条只证明我这一步没写错，这一条才证明整条链是通的。**
        """
        from app.agents.property_finder.tools import search_properties as sp
        params = sp.build_params(runner._filters_from({"rent_max": 3000}))
        self.assertEqual(params.get("rent_price_max"), 3000)


class TestCatalog(unittest.TestCase):

    def test_every_write_tool_has_an_action(self):
        """写工具必须指向一个真实存在的 proposer。

        指错的话这一层会把用户的请求交给一个不存在的动作，
        而**报错要等到用户已经说完整句话之后**才出现。
        """
        from app.agents.support.tools import actions
        for tool in catalog.TOOLS:
            if tool.get("kind") != "write":
                continue
            self.assertIn(tool["action"], actions.PROPOSERS,
                          "%s 指向了不存在的动作 %s"
                          % (tool["name"], tool["action"]))

    def test_names_unique(self):
        names = [t["name"] for t in catalog.TOOLS]
        self.assertEqual(len(names), len(set(names)))

    def test_visible_shrinks_when_logged_out(self):
        out = {t["name"] for t in catalog.visible_tools(False, "")}
        self.assertNotIn("pay_bill", out)
        self.assertIn("search_houses", out)

    def test_catalog_renders_params(self):
        text = catalog.render_catalog(catalog.visible_tools(True, "tenant"))
        self.assertIn("search_houses", text)
        self.assertIn("rent_max", text)


class TestAccountSummary(unittest.TestCase):
    """查库结果的那一句话。**数字全部来自返回体，不经过大模型**——
    用户会照着这句话去交钱。"""

    def test_bills_all_paid(self):
        text = runner._summarize("my_bills",
                                 {"unpaid_count": 0, "unpaid_amount": 0},
                                 [{"bill_no": "x"}], "账单")
        self.assertIn("没有待缴", text)

    def test_bills_overdue_called_out(self):
        text = runner._summarize(
            "my_bills",
            {"unpaid_count": 3, "unpaid_amount": 5400, "overdue_count": 1},
            [{}, {}, {}], "账单")
        self.assertIn("3 笔", text)
        self.assertIn("5400", text)
        self.assertIn("逾期", text)

    def test_appointments_two_sides_read_differently(self):
        rows = [{"status": "pending"}, {"status": "confirmed"}]
        mine = runner._summarize("my_appointments", {}, rows, "预约")
        theirs = runner._summarize("landlord_appointments", {}, rows, "预约")
        self.assertIn("你约的看房", mine)
        self.assertIn("别人约你的房", theirs)


class TestAccountNeedsLogin(unittest.TestCase):

    def test_no_token_says_login(self):
        ctx = {"token": "", "backend": None}
        got = runner._account("my_bills", ctx)
        self.assertTrue(got["need_login"])
        self.assertIn("登录", got["message"])

    def test_backend_error_is_not_raised(self):
        """后端连不上要回一句人话，不能把异常抛给用户。"""
        backend = mock.Mock()
        backend.get.side_effect = RuntimeError("boom")
        got = runner._account("my_bills", {"token": "t", "backend": backend})
        self.assertIn("稍后", got["message"])


if __name__ == "__main__":
    unittest.main()
