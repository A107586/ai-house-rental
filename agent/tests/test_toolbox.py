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

    def test_commission_amounts_come_from_the_response(self):
        """佣金金额取返回体算好的那两个数，不在这里自己求和。

        自己遍历 list 再加一遍，等于把「作废的佣金算不算」这个口径
        抄了第二份；后端哪天改了，中介看到的数和工作台就对不上。
        这里故意让 list 里的金额加起来**不等于**返回体给的总额，
        断言说出来的是返回体那个数。
        """
        text = runner._summarize(
            "agent_commissions",
            {"pending_amount": 3600, "settled_amount": 12000},
            [{"amount": 1}, {"amount": 1}, {"amount": 1}], "佣金")
        self.assertIn("3 笔", text)
        self.assertIn("3600", text)
        self.assertIn("12000", text)

    def test_commission_money_is_not_printed_as_float(self):
        """1250.0 元、1e+06 元这种写法不能出现在钱上。"""
        text = runner._summarize(
            "agent_commissions",
            {"pending_amount": 1250.0, "settled_amount": 1000000.0},
            [{}], "佣金")
        self.assertIn("1250 元", text)
        self.assertIn("1000000 元", text)
        self.assertNotIn("1250.0", text)
        self.assertNotIn("e+", text)

    def test_delegations_count_active_ones(self):
        rows = [{"status": "active"}, {"status": "active"}, {"status": "ended"}]
        text = runner._summarize("agent_delegations", {}, rows, "委托")
        self.assertIn("3 套", text)
        self.assertIn("2 套", text)

    def test_viewings_count_distinct_houses(self):
        """带看 4 次可能只涉及 2 套房，两个数都要说。"""
        rows = [{"house_no": "DL0001"}, {"house_no": "DL0001"},
                {"house_no": "DL0002"}, {"house_no": "DL0002"}]
        text = runner._summarize("agent_viewings", {}, rows, "带看")
        self.assertIn("4 次", text)
        self.assertIn("2 套", text)


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


class TestAccountCardShape(unittest.TestCase):
    """查库结果要能被网页画出来。

    网页只认 ``account = {tool: 主题, data: {...}}``（规则链路给的形状）。
    这一层原来给的是 ``{tool: 工具名, rows: [...]}``——凡是模型选中的查库
    都只剩一句话，卡片一张都画不出；收藏更糟，items 里只有编号，
    画出来是空标题加「NaN 元」。
    """

    def run_tool(self, name, payload):
        backend = mock.Mock()
        backend.get.return_value = payload
        return runner._account(name, {"token": "t", "backend": backend})

    def test_bills_card_uses_frontend_topic_and_unpaid_list(self):
        got = self.run_tool("my_bills", {
            "list": [{"bill_no": "B1", "status": "unpaid", "amount": 100},
                     {"bill_no": "B2", "status": "paid", "amount": 100}],
            "unpaid_count": 1, "unpaid_amount": 100})
        self.assertEqual(got["account"]["tool"], "bill")
        self.assertEqual([b["bill_no"] for b in got["account"]["data"]["unpaid"]],
                         ["B1"])

    def test_contracts_card_lists_active_first(self):
        """「在租的有哪些」——卡片只摆四行，生效中的要排在前面。"""
        got = self.run_tool("my_contracts", {"list": [
            {"contract_no": "old", "status": "ended"},
            {"contract_no": "now", "status": "active"}]})
        self.assertEqual(got["account"]["tool"], "contract")
        self.assertEqual(got["account"]["data"]["list"][0]["contract_no"], "now")

    def test_house_lists_carry_whole_rows_as_items(self):
        house = {"house_no": "DL0060", "title": "椒金山 一室一厅", "rent_price": 1650}
        for name in ("my_favorites", "my_history"):
            got = self.run_tool(name, {"list": [house]})
            self.assertEqual(got["items"][0]["rent_price"], 1650, name)
            self.assertEqual(got["items"][0]["title"], "椒金山 一室一厅", name)


class TestTodos(unittest.TestCase):

    def test_summarizes_only_unfinished_things(self):
        def get(path, params=None, token=None):
            return {
                "/api/bills/mine": {"list": [], "unpaid_count": 2,
                                    "unpaid_amount": 3000, "overdue_count": 1},
                "/api/contracts/mine": {"list": [
                    {"house_no": "DL0007", "status": "pending"},
                    {"house_no": "DL0005", "status": "active"}]},
                "/api/repairs": {"list": [{"status": "done"}]},
            }.get(path, {"list": []})

        backend = mock.Mock()
        backend.get.side_effect = get
        got = runner._todos({"token": "t", "backend": backend})
        self.assertIn("2 笔待缴账单共 3000 元", got["message"])
        self.assertIn("1 笔已逾期", got["message"])
        self.assertIn("DL0007", got["message"])
        # 已完成的报修不算待办
        self.assertNotIn("报修", got["message"])

    def test_one_failing_lookup_does_not_sink_the_answer(self):
        def get(path, params=None, token=None):
            if path == "/api/repairs":
                raise RuntimeError("boom")
            return {"list": [], "unpaid_count": 1, "unpaid_amount": 500}

        backend = mock.Mock()
        backend.get.side_effect = get
        got = runner._todos({"token": "t", "backend": backend})
        self.assertIn("1 笔待缴账单", got["message"])
        self.assertIn("报修暂时没查到", got["message"])


class TestPlannerSeesDialogue(unittest.TestCase):

    def test_recent_dialogue_is_in_the_prompt(self):
        seen = {}

        class Capture(_Fake):
            def complete(self, system_prompt, user_prompt, max_tokens=None):
                seen["prompt"] = user_prompt
                return json.dumps({"tool": "none", "confidence": 0})

        planner.plan("总结下呗", Capture(), logged_in=True, role="tenant",
                     dialogue=[{"role": "user", "content": "第一套评价怎么样"},
                               {"role": "assistant", "content": "共 1 条评价"}])
        self.assertIn("第一套评价怎么样", seen["prompt"])
        self.assertIn("共 1 条评价", seen["prompt"])


class TestSearchAppliesThisTurn(unittest.TestCase):
    """模型那一路找房原先绕过了 apply_turn：本轮的相对调整和取消全被跳过。"""

    def run_search(self, text, params, slots):
        seen = {}

        class Finder:
            def run(self, text, provider=None, filters=None, features=None,
                    source=None, page=1, limit=10):
                seen["filters"] = dict(filters)
                return {"message": "ok", "items": [], "total": 0}

        ctx = {"text": text, "slots": slots, "features": [], "provider": None,
               "property_finder": Finder(), "seen": 0}
        result = runner._search(params, ctx)
        return seen.get("filters"), result

    def test_cheaper_is_applied(self):
        """实测「便宜点的呢」原样返回了同一批房。"""
        filters, _ = self.run_search("便宜点的呢", {},
                                     {"suozaidiqu": "沙河口区", "zujia_max": 3000})
        self.assertLess(filters["zujia_max"], 3000)

    def test_llm_layout_is_normalized(self):
        """模型填「单间」一套也筛不出，库里写的是「一室」。"""
        filters, _ = self.run_search("金州区有没有便宜的单间",
                                     {"district": "金州区", "layout": "单间"}, {})
        self.assertEqual(filters["fangwuhuxing"], "一室")

    def test_unknown_district_is_explained_not_filtered(self):
        filters, result = self.run_search(
            "公司在高新园区，想租个房子，三千以内", {"district": "高新园区",
                                            "rent_max": 3000}, {})
        self.assertNotIn("suozaidiqu", filters)
        self.assertIn("高新园区", result["message"])


class TestAccountSubsets(unittest.TestCase):
    """问一部分，就答那一部分。"""

    def run_tool(self, name, payload, params):
        backend = mock.Mock()
        backend.get.return_value = payload
        return runner._account(name, {"token": "t", "backend": backend}, params)

    def test_overdue_only(self):
        got = self.run_tool("my_bills", {"list": [
            {"bill_no": "A", "status": "unpaid", "overdue": True, "amount": 750,
             "due_date": "2026-09-01"},
            {"bill_no": "B", "status": "unpaid", "overdue": False, "amount": 750,
             "due_date": "2026-10-01"}]}, {"overdue_only": True})
        self.assertIn("1 笔逾期", got["message"])
        self.assertEqual([b["bill_no"] for b in got["account"]["data"]["unpaid"]], ["A"])

    def test_vacant_houses_only(self):
        got = self.run_tool("landlord_houses", {"list": [
            {"house_no": "DL1", "status": "available"},
            {"house_no": "DL2", "status": "rented"}]}, {"status": "available"})
        self.assertEqual([h["house_no"] for h in got["items"]], ["DL1"])
        self.assertIn("DL1", got["message"])

    def test_planner_keeps_boolean_params(self):
        self.assertIs(planner._coerce("true", "boolean"), True)
        self.assertIs(planner._coerce(False, "boolean"), False)


class TestLandlordTools(unittest.TestCase):

    def test_owing_tenants_only(self):
        """「有没有租客拖欠房租的」原先查成了房东自己的账单。"""
        backend = mock.Mock()
        backend.get.return_value = {"list": [
            {"username": "tenant1", "overdue_count": 2, "unpaid_total": 6100},
            {"username": "tenant9", "overdue_count": 0, "unpaid_total": 0}]}
        got = runner._account("landlord_tenants", {"token": "t", "backend": backend},
                              {"overdue_only": True})
        self.assertIn("tenant1", got["message"])
        self.assertNotIn("tenant9", got["message"])
        self.assertIn("6100", got["message"])

    def test_income_uses_the_dashboard_numbers(self):
        backend = mock.Mock()
        backend.get.return_value = {"house_total": 9, "house_rented": 1,
                                    "occupancy_rate": 11, "month_income": 3050,
                                    "income_pending": 6100}
        got = runner.run_read("landlord_income", {}, {"token": "t", "backend": backend})
        self.assertIn("3050", got["message"])
        self.assertEqual(got["account"]["tool"], "income")

    def test_landlord_side_repairs_and_contracts(self):
        """「有租客给我报修吗」查的是名下房源的单，不是房东自己报的。"""
        backend = mock.Mock()
        backend.get.return_value = {"list": [
            {"house_no": "DL1", "status": "pending", "content": "热水器不出热水"},
            {"house_no": "DL2", "status": "done", "content": "门锁"}]}
        got = runner._account("landlord_repairs", {"token": "t", "backend": backend})
        self.assertEqual(backend.get.call_args.kwargs["params"], {"side": "landlord"})
        self.assertIn("1 张还没受理", got["message"])
        self.assertIn("热水器", got["message"])
        self.assertEqual(got["account"]["tool"], "repair")

    def test_pricing_reads_district_layout_area_from_the_sentence(self):
        backend = mock.Mock()
        backend.get.return_value = {"ok": True, "message": "建议定价 2500–2900 元。"}
        got = runner.run_read("pricing_suggest", {}, {
            "token": "t", "backend": backend, "text": "西岗区两室一厅70平一般租多少"})
        self.assertEqual(backend.get.call_args.kwargs["params"],
                         {"district": "西岗区", "layout": "两室一厅", "area": 70.0})
        self.assertIn("2500–2900", got["message"])

    def test_admin_todos_counts_each_queue(self):
        import datetime
        today = datetime.date.today().isoformat()

        def get(path, params=None, token=None):
            if path == "/api/admin/houses":
                return {"total": 3, "list": [{}, {}, {}]}
            if path == "/api/admin/handoffs":
                return {"total": 2, "list": [{"created_at": today + " 09:00"},
                                             {"created_at": "2026-01-01 09:00"}]}
            if path == "/api/admin/reports":
                raise RuntimeError("down")
            return {"total": 0, "list": []}
        backend = mock.Mock()
        backend.get.side_effect = get
        got = runner.run_read("admin_todos", {}, {"token": "t", "backend": backend})
        self.assertIn("3 套房源待审核", got["message"])
        self.assertIn("2 张转人工工单没回复", got["message"])
        self.assertIn("今天新提交的转人工有 1 张", got["message"])
        self.assertIn("1 项没查到", got["message"])
        self.assertNotIn("admin_todos", [t["name"] for t in
                                         catalog.visible_tools(True, "landlord")])

    def test_who_is_the_landlord(self):
        """原先答「房源信息里没写房东」，字段其实在；名字只给姓。"""
        got = runner.pitch.answer_attribute({"landlord_name": "张伟"},
                                            "第一套房东是谁，能联系吗")
        self.assertIn("张**", got)
        self.assertNotIn("张伟", got)
        self.assertIn("预约看房被房东确认后", got)

    def test_superlative_looks_at_the_whole_list(self):
        """「刚才那几套里哪个最便宜」原先只比了前两套。"""
        rows = {"DL1": {"house_no": "DL1", "title": "甲", "rent_price": 2000},
                "DL2": {"house_no": "DL2", "title": "乙", "rent_price": 1800},
                "DL3": {"house_no": "DL3", "title": "丙", "rent_price": 900}}
        fetch = lambda nos: [rows[n] for n in nos if n in rows]   # noqa: E731
        got = runner.direct_list_answer("刚才那几套里哪个最便宜", [],
                                        ["DL1", "DL2", "DL3"], fetch)
        self.assertIn("第 3 套", got["message"])
        self.assertIn("900", got["message"])
        self.assertEqual(got["house_no"], "DL3")
        self.assertIsNone(runner.direct_list_answer("哪个更划算", [],
                                                    ["DL1", "DL2"], fetch))

    def test_light_question_answers_orientation(self):
        rows = [{"house_no": "DL1", "title": "甲", "orientation": "南"},
                {"house_no": "DL2", "title": "乙", "orientation": ""}]
        got = runner.direct_list_answer("第一套和第二套哪个采光好", ["DL1", "DL2"],
                                        ["DL1", "DL2"], lambda nos: rows)
        self.assertIn("第 1 套 甲（DL1） 南", got["message"])
        self.assertIn("没标朝向", got["message"])

    def test_distance_question_says_there_is_no_distance_data(self):
        """「哪个离我公司最近」挑中了对比，对比讲的却是价钱和面积。"""
        rows = [{"house_no": "DL1", "tags": "近地铁"}, {"house_no": "DL2", "tags": ""}]
        ctx = {"last_house_nos": ["DL1", "DL2"], "fetch_houses": lambda nos: rows,
               "backend": None, "text": "这几套里哪个离我公司最近"}
        with mock.patch.object(runner.pitch, "compare", return_value=("对比正文", {})):
            got = runner._compare({}, ctx)
            self.assertIn("没法按远近排", got["message"])
            self.assertIn("DL1", got["message"])
            ctx["text"] = "第一套和第二套哪个划算"
            self.assertEqual(runner._compare({}, ctx)["message"], "对比正文")

    def test_houses_ranked_by_rating(self):
        backend = mock.Mock()
        backend.get.return_value = {"list": [
            {"house_no": "DL1", "rating": 4.7, "review_count": 3},
            {"house_no": "DL2", "rating": 3.2, "review_count": 2},
            {"house_no": "DL3", "rating": 0, "review_count": 0}]}
        got = runner._account("landlord_houses", {"token": "t", "backend": backend},
                              {"by_rating": "worst"})
        self.assertIn("最差的是 DL2", got["message"])
        self.assertIn("1 套还没有人评价", got["message"])
        self.assertEqual([h["house_no"] for h in got["items"]], ["DL2", "DL1"])

    def test_contracts_expiring_soon(self):
        import datetime
        today = datetime.date.today()
        soon = (today + datetime.timedelta(days=20)).isoformat()
        later = (today + datetime.timedelta(days=200)).isoformat()
        backend = mock.Mock()
        backend.get.return_value = {"list": [
            {"house_no": "DL1", "status": "active", "end_date": soon},
            {"house_no": "DL2", "status": "active", "end_date": later},
            {"house_no": "DL3", "status": "ended", "end_date": soon}]}
        got = runner._account("landlord_contracts", {"token": "t", "backend": backend},
                              {"expiring_days": 31})
        self.assertEqual(backend.get.call_args.kwargs["params"], {"side": "landlord"})
        self.assertIn("1 份出租合同到期", got["message"])
        self.assertIn("DL1", got["message"])
        self.assertNotIn("DL2", got["message"])

    def test_unknown_house_code_says_so(self):
        """报了编号却查不到，就说查不到——别回「先说说你的条件」。"""
        ctx = {"last_house_nos": [], "house_no": "", "fetch_houses": lambda nos: [],
               "backend": None, "text": "DL9999 这套房怎么样"}
        got = runner._one_house("house_detail", {"house_no": "DL9999"}, ctx)
        self.assertIn("没有找到编号 DL9999", got["message"])


if __name__ == "__main__":
    unittest.main()
