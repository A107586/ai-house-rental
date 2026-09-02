# -*- coding: utf-8 -*-
"""AI-03 智能客服子智能体的单元测试。

覆盖：语料解析、同义扩展、事务型分流、可答性闸门、引用与数字校验、
多轮指代消解、以及整条链路的返回形状。

全部**不依赖网络、数据库与大模型**——需要大模型的地方用桩替身，
桩既能模拟正常返回，也能模拟它胡说八道（编 id、编数字），
后一类才是校验逻辑真正要挡的。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agents.support import agent as support  # noqa: E402
from app.agents.support import corpus  # noqa: E402
from app.agents.support.tools import (account, answer as answer_tool,  # noqa: E402
                                      classify_ask, gate, toolcall)
from app.backend import BackendError  # noqa: E402
from app.retrieval.index import KnowledgeIndex  # noqa: E402


class StubProvider(object):
    """可控的大模型替身。`available` 为假时模拟未配置。"""

    def __init__(self, payload=None, available=True, raises=None):
        self.payload = payload
        self.available = available
        self.raises = raises
        self.calls = []

    def complete_json(self, system, prompt, max_tokens=None):
        self.calls.append(prompt)
        if self.raises:
            raise self.raises
        return self.payload


SAMPLE_FAQ = """# 头部说明，没有 id，解析器应当跳过

## 押金什么时候退？
id: faq-deposit
category: 押金
alias: 保证金什么时候退 / 押金能退吗

租期届满后 7 个工作日内原路退还。若有人为损坏，扣款后退还剩余部分。

---

## 水电费怎么算？
id: faq-utility
category: 费用

水电费由租客自行缴纳，不通过平台代收。
"""


class TestCorpus(unittest.TestCase):

    def test_parses_id_category_and_aliases(self):
        chunks = corpus.parse_faq(SAMPLE_FAQ)
        self.assertEqual([c["id"] for c in chunks], ["faq-deposit", "faq-utility"])
        self.assertEqual(chunks[0]["aliases"],
                         ["保证金什么时候退", "押金能退吗"])
        self.assertEqual(chunks[0]["category"], "押金")

    def test_section_without_id_is_skipped_not_fatal(self):
        """文件开头的说明文字也是普通段落，跳过即可，不该让服务起不来。"""
        self.assertEqual(len(corpus.parse_faq(SAMPLE_FAQ)), 2)

    def test_separator_stripped_from_body(self):
        chunks = corpus.parse_faq(SAMPLE_FAQ)
        self.assertFalse(chunks[0]["body"].rstrip().endswith("-"))

    def test_duplicate_id_raises(self):
        """重复 id 会让引用校验指向错误的材料，必须当场炸掉。"""
        with self.assertRaises(ValueError):
            corpus.parse_faq(SAMPLE_FAQ + SAMPLE_FAQ)

    def test_expand_query_pulls_in_whole_synonym_group(self):
        expanded = corpus.expand_query("保证金能退吗")
        self.assertIn("押金", expanded)

    def test_expand_query_empty_when_nothing_matches(self):
        self.assertEqual(corpus.expand_query("今天天气不错"), "")

    def test_real_corpus_loads(self):
        chunks = corpus.load_faq()
        self.assertGreaterEqual(len(chunks), 40)
        self.assertEqual(len({c["id"] for c in chunks}), len(chunks))


class TestClassifyAsk(unittest.TestCase):

    def test_personal_question_is_transactional(self):
        result = classify_ask.classify("我的账单还差多少没交")
        self.assertEqual(result["kind"], classify_ask.KIND_ACCOUNT)
        self.assertEqual(result["topic"], "bill")

    def test_generic_question_stays_knowledge(self):
        """「押金一般几个月」问的是规则，不是他自己那一笔。"""
        result = classify_ask.classify("押金一般收几个月")
        self.assertEqual(result["kind"], classify_ask.KIND_KNOWLEDGE)

    def test_omitted_pronoun_still_transactional(self):
        """「账单还差多少没交」省略了「我的」，但显然问的是自己的。"""
        result = classify_ask.classify("账单还差多少没交")
        self.assertEqual(result["kind"], classify_ask.KIND_ACCOUNT)

    def test_handoff_detected(self):
        self.assertTrue(classify_ask.wants_handoff("我要转人工"))
        self.assertFalse(classify_ask.wants_handoff("押金怎么退"))


class TestGate(unittest.TestCase):

    def setUp(self):
        self.index = KnowledgeIndex(corpus.load_faq())

    def _candidates(self, question):
        ranked, detail = self.index.search(
            question, expanded=corpus.expand_query(question), top_k=8)
        return [c for c, _ in self.index.rerank(question, ranked, detail, top_k=3)]

    def test_passes_for_well_supported_question(self):
        question = "押金什么时候退"
        verdict = gate.evaluate(self.index, question, self._candidates(question))
        self.assertTrue(verdict["ok"])

    def test_refuses_out_of_domain_question(self):
        question = "帮我订个外卖"
        verdict = gate.evaluate(self.index, question, self._candidates(question))
        self.assertFalse(verdict["ok"])
        self.assertTrue(verdict["reason"])

    def test_refuses_when_no_candidates(self):
        verdict = gate.evaluate(self.index, "随便问问", [])
        self.assertFalse(verdict["ok"])

    def test_llm_can_only_tighten_never_loosen(self):
        """需求把拒答率下限写死在 90%。

        如果允许大模型把规则的拒答改成可答，这个下限就成了概率事件。
        所以规则拒掉的，大模型说 yes 也没用。
        """
        question = "帮我订个外卖"
        yes = StubProvider({"answerable": "yes"})
        verdict = gate.evaluate(self.index, question,
                                self._candidates(question), provider=yes)
        self.assertFalse(verdict["ok"])
        self.assertEqual(yes.calls, [], "规则已拒答，不该再花一次大模型调用")

    def test_llm_no_turns_borderline_into_refusal(self):
        # 构造一个覆盖率处在中间带的问题，让 G3 真正参与
        question = "修东西的钱谁出"
        candidates = self._candidates(question)
        cover = self.index.coverage(question, candidates[0])
        if cover >= gate.COVER_STRONG:
            self.skipTest("该问题不在中间带，此用例不适用")
        no = StubProvider({"answerable": "no"})
        verdict = gate.evaluate(self.index, question, candidates, provider=no)
        self.assertFalse(verdict["ok"])


class TestAnswerValidation(unittest.TestCase):

    def setUp(self):
        self.index = KnowledgeIndex(corpus.load_faq())
        self.candidates = ["faq-deposit-refund"]

    def test_rejects_fabricated_citation(self):
        """模型编 id 的频率比想象中高，编出来的引用点开就是空的。"""
        ok, reason = answer_tool.validate(
            "押金 7 个工作日内退还。", ["faq-不存在"], self.index, self.candidates)
        self.assertFalse(ok)
        self.assertIn("不存在", reason)

    def test_rejects_fabricated_number(self):
        """把 7 个工作日说成 15 个工作日——用户会照着这个数字去等钱。"""
        ok, reason = answer_tool.validate(
            "押金 15 个工作日内退还。", self.candidates, self.index, self.candidates)
        self.assertFalse(ok)
        self.assertIn("15", reason)

    def test_accepts_number_present_in_source(self):
        ok, _ = answer_tool.validate(
            "押金 7 个工作日内退还。", self.candidates, self.index, self.candidates)
        self.assertTrue(ok)

    def test_rejects_ungrounded_phrasing(self):
        ok, reason = answer_tool.validate(
            "据我所知押金会退的。", self.candidates, self.index, self.candidates)
        self.assertFalse(ok)

    def test_rejects_empty_citations(self):
        ok, _ = answer_tool.validate("押金会退。", [], self.index, self.candidates)
        self.assertFalse(ok)

    def test_extract_returns_first_sentence_only(self):
        body, cites = answer_tool.extract(self.index, self.candidates)
        self.assertTrue(body.endswith("。"))
        self.assertEqual(body.count("。"), 1)
        self.assertEqual(cites, self.candidates)

    def test_generate_falls_back_when_model_fabricates(self):
        """模型输出不可信时必须整条丢弃、退回抽取式，而不是硬发出去。"""
        bad = StubProvider({"answer": "押金 99 个工作日内退还。",
                            "citations": ["faq-deposit-refund"],
                            "refuse": False})
        message, citations, source, note = answer_tool.generate(
            bad, self.index, "押金什么时候退", self.candidates)
        self.assertEqual(source, "extractive")
        self.assertIn("99", note)
        self.assertNotIn("99", message)


class TestToolCall(unittest.TestCase):

    def test_rules_pick_tool_without_model(self):
        tool, source, _ = toolcall.select_tool("我的账单还差多少", provider=None)
        self.assertEqual(tool, "bill")
        self.assertEqual(source, "rules")

    def test_model_choice_is_validated(self):
        """模型返回一个不存在的工具名时退回规则结论，不能照单全收。"""
        bogus = StubProvider({"tool": "drop_table"})
        tool, source, _ = toolcall.select_tool("我的账单还差多少", provider=bogus)
        self.assertEqual(tool, "bill")
        self.assertEqual(source, "rules_fallback")

    def test_compose_rejects_number_not_in_data(self):
        """照着数据说话，是这条链路唯一的价值。编了数字就退回代码拼的句子。"""
        data = {"unpaid": [{"amount": 2400}]}
        liar = StubProvider({"answer": "你还欠 4200 元。"})
        message, source, note = toolcall.compose(
            liar, "我欠多少", "bill", data, "你还有 1 笔待缴账单，合计 2400 元。")
        self.assertEqual(source, "rules")
        self.assertIn("4200", note)
        self.assertIn("2400", message)

    def test_compose_accepts_faithful_answer(self):
        data = {"unpaid": [{"amount": 2400}]}
        honest = StubProvider({"answer": "你还有一笔 2400 元没交。"})
        message, source, _ = toolcall.compose(
            honest, "我欠多少", "bill", data, "你还有 1 笔待缴账单，合计 2400 元。")
        self.assertEqual(source, "llm")
        self.assertIn("2400", message)


class TestRewrite(unittest.TestCase):

    def test_anaphora_is_expanded_with_previous_turn(self):
        query, carried = support.rewrite("那它多久到账", ["押金怎么退"])
        self.assertIn("押金", query)
        self.assertEqual(carried, "押金怎么退")

    def test_long_question_is_left_alone(self):
        text = "这个押金到底要等多少天才能退到我的银行卡里"
        query, carried = support.rewrite(text, ["押金怎么退"])
        self.assertEqual(query, text)
        self.assertEqual(carried, "")

    def test_no_history_no_rewrite(self):
        query, carried = support.rewrite("那它多久到账", [])
        self.assertEqual(query, "那它多久到账")


class TestSupportFlow(unittest.TestCase):

    def setUp(self):
        support.reset_knowledge_base()

    def test_answered_shape(self):
        result = support.run("押金什么时候退")
        self.assertEqual(result["status"], "answered")
        self.assertTrue(result["ok"])
        self.assertTrue(result["citations"])
        self.assertIn("id", result["citations"][0])
        self.assertIn("gate", result["retrieval"])

    def test_refused_offers_a_way_out(self):
        """拒答不能只丢一句「不知道」——要给猜你想问和转人工，
        这是客服产品与检索 demo 的区别。"""
        result = support.run("帮我订个外卖")
        self.assertEqual(result["status"], "refused")
        self.assertFalse(result["ok"])
        self.assertEqual(result["citations"], [])
        self.assertTrue(result["alternatives"])
        self.assertIn("handoff", result)

    def test_handoff_short_circuits_retrieval(self):
        result = support.run("我要转人工")
        self.assertEqual(result["status"], "escalate")
        self.assertEqual(result["retrieval"], {})

    def test_transactional_without_token_asks_to_login(self):
        result = support.run("我的账单还差多少没交")
        self.assertEqual(result["status"], "need_login")
        self.assertEqual(result["retrieval"]["mode"], "account")

    def test_empty_input_is_guided_not_crashed(self):
        result = support.run("   ")
        self.assertEqual(result["status"], "unknown")
        self.assertTrue(result["message"])

    def test_retrieval_detail_is_exposed_for_the_ui(self):
        """前端要用它渲染思考态，答辩要用它解释「为什么是这条」。"""
        result = support.run("水电费怎么算")
        detail = result["retrieval"]
        self.assertIn("candidates", detail)
        self.assertIn("channels", detail)
        self.assertEqual(detail["mode"], "lexical")


class TestTransactionalCoverage(unittest.TestCase):
    """13 类事务型数据的分流。

    这一组的价值不在「某一条对不对」，而在**三张表必须保持同步**：
    `TOPICS`、`account.HANDLERS`、`toolcall.TOOL_SPECS`。
    加一类事务要在三处各加一行，漏掉任何一处，
    用户问出来的表现是「查不了这类记录」——一句看不出原因的话。
    """

    def test_three_registries_stay_in_sync(self):
        topics = {name for name, _ in classify_ask.TOPICS}
        handlers = set(account.HANDLERS)
        specs = {name for name, _ in toolcall.TOOL_SPECS} - {"none"}
        self.assertEqual(topics, handlers,
                         "TOPICS 与 account.HANDLERS 不一致：%s" % (topics ^ handlers))
        self.assertEqual(handlers, specs,
                         "account.HANDLERS 与 TOOL_SPECS 不一致：%s" % (handlers ^ specs))

    def test_every_topic_has_a_label(self):
        for name in account.HANDLERS:
            self.assertIn(name, account.TOPIC_LABELS,
                          "%s 缺中文名，出错时用户会看到英文 topic" % name)

    def test_routing_across_all_topics(self):
        cases = [
            ("我还欠多少房租", "bill"),
            ("我这个月收了多少租金", "income"),
            ("预期收入", "income"),
            ("我的房源审核过了吗", "house"),
            ("我的租客有谁欠费", "tenant"),
            ("我的租约什么时候到期", "contract"),
            ("我的报修到哪一步了", "repair"),
            ("我预约的看房通过了吗", "booking"),
            ("我收藏了哪些房子", "favorite"),
            ("我看过哪些房源", "history"),
            ("我有未读消息吗", "message"),
            ("我有哪些租约没评价", "review"),
            ("我的转人工工单处理了吗", "handoff"),
            ("我的账号是什么身份", "profile"),
        ]
        misses = []
        for question, want in cases:
            result = classify_ask.classify(question)
            got = result["topic"] if result["kind"] == classify_ask.KIND_ACCOUNT else "knowledge"
            if got != want:
                misses.append("%s -> %s（应为 %s）" % (question, got, want))
        self.assertEqual(misses, [], "分流错误：\n" + "\n".join(misses))

    def test_priority_order_narrow_beats_generic(self):
        """含义更窄的类型必须排在通用业务名词前面。

        三条都是实测撞出来的：「转人工工单」被 repair 的「工单」抢走、
        「租约没评价」被 contract 的「租约」抢走、
        「收了多少租金」被 bill 的「租金」抢走。
        """
        pairs = [("我的转人工工单处理了吗", "handoff", "repair"),
                 ("我有哪些租约没评价", "review", "contract"),
                 ("我这个月收了多少租金", "income", "bill")]
        for question, want, loser in pairs:
            got = classify_ask.classify(question)["topic"]
            self.assertEqual(got, want,
                             "「%s」被 %s 抢走了，应归 %s" % (question, got, want))


    def test_status_query_is_not_a_handoff_request(self):
        """问进度不能被当成「我要转人工」。

        实测撞到过：「我的转人工工单处理了吗」里有「转人工」三个字，
        `wants_handoff()` 先跑，于是又给用户建了一张新工单——
        他只是想知道上一张的进度。
        """
        for question in ["我的转人工工单处理了吗", "人工回复了吗"]:
            self.assertFalse(classify_ask.wants_handoff(question),
                             "「%s」是问进度，不该新建工单" % question)
            self.assertEqual(classify_ask.classify(question)["topic"], "handoff")

    def test_real_handoff_request_still_works(self):
        for question in ["我要转人工", "转人工", "找个人问问"]:
            self.assertTrue(classify_ask.wants_handoff(question),
                            "「%s」是真要转人工" % question)

    def test_progress_words_imply_ownership(self):
        """「报修处理好了吗」省略了「我的」，但显然问的是自己那一条。"""
        for question, topic in [("报修处理好了吗", "repair"),
                                ("预约有结果了吗", "booking")]:
            result = classify_ask.classify(question)
            self.assertEqual(result["kind"], classify_ask.KIND_ACCOUNT)
            self.assertEqual(result["topic"], topic)

    def test_generic_question_never_becomes_transactional(self):
        """通用规则问题绝不能走查库——那会把 FAQ 的答案说成「你的」数据。"""
        for question in ["押金一般收几个月", "水电费怎么算", "怎么评价房东",
                         "报修多久上门", "提前退租要付违约金吗"]:
            result = classify_ask.classify(question)
            self.assertEqual(result["kind"], classify_ask.KIND_KNOWLEDGE,
                             "「%s」被误判成事务型" % question)


class TestAccountTools(unittest.TestCase):
    """取数函数本身。用假后端喂数据，不连真库。"""

    class FakeBackend(object):
        def __init__(self, payloads):
            self.payloads = payloads
            self.calls = []

        def get(self, path, params=None, token=None):
            self.calls.append(path)
            if path not in self.payloads:
                raise AssertionError("用例没有为 %s 准备数据" % path)
            return self.payloads[path]

    def test_landlord_houses_summarised_by_status(self):
        backend = self.FakeBackend({"/api/landlord/houses": {"list": [
            {"house_no": "H1", "status": "available"},
            {"house_no": "H2", "status": "rented"},
            {"house_no": "H3", "status": "rented"},
        ]}})
        status, message, data = account.lookup(backend, "tok", "house")
        self.assertEqual(status, "ok")
        self.assertIn("3 套", message)
        self.assertIn("已出租 2 套", message)
        self.assertEqual(data["by_status"]["rented"], 2)

    def test_income_reports_only_backend_numbers(self):
        """经营数据里的每个数字都必须来自后端，不能由智能体推算。"""
        backend = self.FakeBackend({"/api/landlord/dashboard": {
            "house_total": 4, "house_rented": 3, "occupancy_rate": 75.0,
            "month_income": 6600, "income_pending": 2200, "overdue_count": 1,
        }})
        status, message, _ = account.lookup(backend, "tok", "income")
        self.assertEqual(status, "ok")
        for number in ("4", "3", "75", "6600", "2200"):
            self.assertIn(number, message)
        self.assertIn("逾期", message)

    def test_landlord_only_topic_explains_permission_to_tenant(self):
        """租客问「我的房源」，后端会拒。要说清楚是身份问题，
        而不是丢一句「数据取不到」让人反复重试。"""
        class Denied(object):
            def get(self, path, params=None, token=None):
                raise BackendError("需要房东权限")

        status, message, _ = account.lookup(Denied(), "tok", "house")
        self.assertEqual(status, "error")
        self.assertIn("房东端", message)

    def test_missing_token_asks_for_login(self):
        status, message, _ = account.lookup(None, "", "bill")
        self.assertEqual(status, "need_login")

    def test_unknown_topic_is_reported_not_crashed(self):
        status, _, _ = account.lookup(None, "tok", "不存在的类型")
        self.assertEqual(status, "error")

    def test_empty_results_read_naturally(self):
        """空数据也要说人话，不能返回「共 0 条」这种机器口吻。"""
        backend = self.FakeBackend({"/api/favorites": {"list": []}})
        _, message, _ = account.lookup(backend, "tok", "favorite")
        self.assertIn("还没有收藏", message)


if __name__ == "__main__":
    unittest.main()
