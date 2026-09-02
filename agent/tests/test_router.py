# -*- coding: utf-8 -*-
"""统一工具路由 `tools/router.py` 的测试。

这一层决定「用户这句话该走网站的哪个功能」，是整个助手的分叉点。
它判错的代价不对称，所以测试也不对称地压在护栏上：

- 判成 query 判错了：多查一次自己的数据，用户看一眼就知道不对；
- 判成 action 判错了：弹出一张「确认退租」的卡片。**这是要防的那一类。**

全部运行在纯规则模式或假大模型下，不联网。
"""

import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agents.support.tools import router  # noqa: E402
from app.llm.base import LLMProvider, LLMUnavailable, NullProvider  # noqa: E402


class FakeProvider(LLMProvider):
    """可控的假大模型。payload 就是 complete_json 的返回值。"""

    available = True

    def __init__(self, payload=None, raises=None):
        self.payload = payload
        self.raises = raises
        self.last_prompt = ""

    def complete_json(self, system, prompt, max_tokens=None):
        self.last_prompt = prompt
        if self.raises:
            raise self.raises
        return self.payload

    def complete(self, system, prompt, max_tokens=None):
        return ""


class TestRulesOnly(unittest.TestCase):
    """没有大模型时，路由完全由规则给出——这是全部自动化测试跑的模式。"""

    def test_no_provider_falls_back_to_rules(self):
        got = router.route("帮我交房租")
        self.assertEqual(got["kind"], router.KIND_ACTION)
        self.assertEqual(got["name"], "pay_bill")
        self.assertEqual(got["source"], "rules")

    def test_null_provider_is_treated_as_absent(self):
        got = router.route("我还欠多少房租", provider=NullProvider())
        self.assertEqual(got["kind"], router.KIND_QUERY)
        self.assertEqual(got["source"], "rules")

    def test_plain_question_is_knowledge(self):
        got = router.route("押金怎么退")
        self.assertEqual(got["kind"], router.KIND_KNOWLEDGE)
        self.assertEqual(got["name"], "none")

    def test_booking_phrasings_that_used_to_miss(self):
        """这几句以前一条都没命中，用户被打发回页面上自己点。"""
        for text in ("我要看房第一个", "预约明天上午看房第三套",
                     "想看房", "约一下看房"):
            with self.subTest(text=text):
                got = router.route(text)
                self.assertEqual(got["kind"], router.KIND_ACTION, text)
                self.assertEqual(got["name"], "book_viewing", text)


class TestContractReview(unittest.TestCase):
    """审阅型合同请求要认成服务，不能当成「查我的租约」。"""

    def test_review_request_is_a_service(self):
        got = router.route("帮我看看合同有没有坑")
        self.assertEqual(got["kind"], router.KIND_SERVICE)
        self.assertEqual(got["name"], "contract_review")

    def test_review_wins_over_the_contract_query(self):
        """「合同」两个字也会命中查询规则，服务型必须排在它前面。

        排错了用户会拿到一份合同清单——问的是有没有坑，答的是合同号，
        这种答非所问比答不上来更让人不信任。
        """
        got = router.route("我这份合同的条款有没有风险")
        self.assertEqual(got["kind"], router.KIND_SERVICE)

    def test_contract_rule_question_stays_knowledge(self):
        """「能签几年合同」问的是平台规则，不该被支去点审阅按钮。"""
        for text in ("电子合同有效吗", "能签几年合同"):
            with self.subTest(text=text):
                self.assertNotEqual(router.route(text)["kind"],
                                    router.KIND_SERVICE, text)


class TestLLMGuards(unittest.TestCase):
    """大模型参与之后的三条护栏。"""

    def test_llm_can_override_the_rules(self):
        provider = FakeProvider({"kind": "action", "name": "book_viewing"})
        got = router.route("那套我想去现场瞧瞧", provider=provider)
        self.assertEqual(got["kind"], router.KIND_ACTION)
        self.assertEqual(got["name"], "book_viewing")
        self.assertEqual(got["source"], "llm")
        # 规则的结论要一并留下，答辩时能展示「规则说什么、模型改成了什么」
        self.assertEqual(got["rule"]["kind"], router.KIND_KNOWLEDGE)

    def test_question_never_becomes_an_action(self):
        """**最重要的一条。** 模型判成退租，句子却是在提问，改回知识型。

        多弹一张退租确认卡是惊吓，而用户真按下去就收不回来。
        """
        provider = FakeProvider({"kind": "action", "name": "end_contract"})
        got = router.route("怎么退租", provider=provider)
        self.assertEqual(got["kind"], router.KIND_KNOWLEDGE)
        self.assertEqual(got["source"], "llm_guarded")

    def test_explicit_command_is_not_blocked_by_the_guard(self):
        """护栏只拦疑问句。「帮我退租」是指令，该弹卡片就得弹。"""
        provider = FakeProvider({"kind": "action", "name": "end_contract"})
        got = router.route("帮我退租", provider=provider)
        self.assertEqual(got["kind"], router.KIND_ACTION)

    def test_invented_tool_name_falls_back_to_rules(self):
        provider = FakeProvider({"kind": "action", "name": "teleport_user"})
        got = router.route("帮我交房租", provider=provider)
        self.assertEqual(got["name"], "pay_bill")
        self.assertEqual(got["source"], "rules_fallback")

    def test_llm_failure_falls_back_to_rules(self):
        for boom in (LLMUnavailable("down"), RuntimeError("timeout")):
            with self.subTest(boom=type(boom).__name__):
                provider = FakeProvider(raises=boom)
                got = router.route("帮我交房租", provider=provider)
                self.assertEqual(got["kind"], router.KIND_ACTION)
                self.assertEqual(got["source"], "rules_fallback")

    def test_garbage_response_falls_back_to_rules(self):
        for payload in (None, "not a dict", []):
            with self.subTest(payload=repr(payload)):
                got = router.route("我还欠多少房租",
                                   provider=FakeProvider(payload))
                self.assertEqual(got["source"], "rules_fallback")

    def test_service_from_the_llm_is_accepted(self):
        provider = FakeProvider({"kind": "service", "name": "recommend_house"})
        got = router.route("有什么合适的房子吗", provider=provider)
        self.assertEqual(got["kind"], router.KIND_SERVICE)
        self.assertEqual(got["name"], "recommend_house")

    def test_service_is_not_blocked_by_the_question_guard(self):
        """服务型三项都是只读的，疑问句护栏不该拦它们。

        「有什么合适的房子吗」是个疑问句，但推荐房源不会写任何东西，
        最坏结果只是多给一份列表。
        """
        provider = FakeProvider({"kind": "service", "name": "recommend_house"})
        self.assertEqual(
            router.route("有什么合适的房子吗？", provider=provider)["kind"],
            router.KIND_SERVICE)


class TestPromptContent(unittest.TestCase):
    """提示词里必须真的带着能力清单与上下文，否则模型是在盲猜。"""

    def test_catalog_and_hint_are_in_the_prompt(self):
        provider = FakeProvider({"kind": "none", "name": "none"})
        router.route("帮我交房租", provider=provider)
        prompt = provider.last_prompt
        self.assertIn("pay_bill", prompt)
        self.assertIn("contract", prompt)
        self.assertIn("search_house", prompt)
        self.assertIn("要执行 pay_bill", prompt)   # 规则结论作为参考答案

    def test_pending_action_is_carried_into_the_prompt(self):
        """接上文的半句话（「都可以」）只有带上待办才判得出来。"""
        provider = FakeProvider({"kind": "none", "name": "none"})
        router.route("都可以", provider=provider,
                     pending={"action": "pay_bill"})
        self.assertIn("pay_bill", provider.last_prompt)
        self.assertIn("还没做完", provider.last_prompt)

    def test_catalog_covers_every_tool(self):
        """清单三张表都不能漏项——漏了模型就永远选不到那个功能。"""
        provider = FakeProvider({"kind": "none", "name": "none"})
        router.route("随便说点什么", provider=provider)
        for name in list(router.ACTION_HINTS) + list(router.QUERY_HINTS) \
                + list(router.SERVICE_HINTS):
            self.assertIn(name, provider.last_prompt, name)


class TestCatalogIntegrity(unittest.TestCase):
    """清单是路由的唯一事实来源，得跟真正接得住它的代码对齐。"""

    def test_actions_match_the_executable_ones(self):
        from app.agents.support.tools import actions
        self.assertEqual(set(router.ACTION_HINTS), set(actions.PROPOSERS))
        self.assertEqual(set(router.ACTION_HINTS), set(actions.EXECUTORS))

    def test_queries_match_the_backend_handlers(self):
        from app.agents.support.tools import account
        self.assertEqual(set(router.QUERY_HINTS), set(account.HANDLERS))

    def test_every_query_tool_has_a_card_in_the_chat_panel(self):
        """每个能查的工具，对话框里都得有对应的小卡片。

        少一项的表现很隐蔽：接口查到了数据，助手也答了一句话，
        但用户看不见「是哪几笔」——功能像做完了，其实只做了一半。
        跨语言检查看着别扭，可这类「两边各自都对、合起来对不上」的缺口，
        只有从外面看才发现得了（同 test_dispatch.py 的那次事故）。
        """
        from app.agents.support.tools import account

        panel = os.path.join(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))),
            "web", "src", "components", "ChatPanel.jsx")
        if not os.path.exists(panel):
            self.skipTest("没有前端源码（只部署了智能体服务）")
        with io.open(panel, encoding="utf-8") as handle:
            source = handle.read()

        # income 是四个数字，单独渲染成格子，不在 ACCOUNT_ROWS 表里
        missing = [topic for topic in account.HANDLERS
                   if ("  %s: (data)" % topic) not in source
                   and not (topic == "income" and "IncomeCard" in source)]
        self.assertEqual(missing, [],
                         "ChatPanel 的 ACCOUNT_ROWS 里缺这几类的卡片")

    def test_business_report_is_deliberately_absent(self):
        """经营报告读的是全平台统计，而聊天入口不校验管理员身份。

        放进清单等于任何人问一句就能拿到平台经营数据。房东看自己的，
        走按令牌取数的 income。
        """
        self.assertNotIn("business_report", router.SERVICE_HINTS)
        self.assertIn("income", router.QUERY_HINTS)


if __name__ == "__main__":
    unittest.main()
