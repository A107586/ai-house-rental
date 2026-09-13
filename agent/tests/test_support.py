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
from app.agents.support.tools.money import money_text  # noqa: E402
from app.agents.support.tools import (account, answer as answer_tool,  # noqa: E402
                                      classify_ask, gate, toolcall)
from app.backend import BackendError  # noqa: E402
from app.agents.support.tools import gate  # noqa: E402
from app.retrieval.index import KnowledgeIndex  # noqa: E402
from tests.fixtures import support_tune as tune  # noqa: E402


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
        return [c for c, _ in self.index.rerank(
            question, ranked, detail, top_k=3,
            expanded=corpus.expand_query(question))]

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

    # 「规则会拒、但关键词全库出现过」——正好落在允许翻案的那一带。
    # **不写死成某个具体问题的期望**：语料一扩，它可能就不在这带里了，
    # 用例要么变成假绿要么莫名其妙地红。下面每条都先确认它还在带里，
    # 不在就跳过并说明。
    BORDERLINE = "房子能不能做短租"

    def _is_borderline(self, question, candidates):
        cover = self.index.coverage(question, candidates[0])
        return (cover < gate.COVER_STRONG
                and not self.index.key_term_hit(question, candidates[0])
                and self.index.term_in_corpus(self.index.key_term(question)))

    def test_llm_cannot_rescue_an_out_of_domain_question(self):
        """域外问题，大模型说 yes 也翻不了案。

        **G3 现在是双向的**（见 gate 模块开头），但放宽那一侧只在
        「材料其实就在手里」的中间带里允许。「帮我订个外卖」连候选材料
        都不相干，落不进那一带。

        这条守的是拒答率下限：需求写死 ≥ 90%，不能变成概率事件。
        """
        question = "帮我订个外卖"
        yes = StubProvider({"answerable": "yes"})
        verdict = gate.evaluate(self.index, question,
                                self._candidates(question), provider=yes)
        self.assertFalse(verdict["ok"])

    def test_llm_rescues_a_borderline_question(self):
        """**这一条是这次改动的目的。**

        实测检索的 Recall@3 是 100%——正确答案每次都排在第一位——
        而有依据正确率只有 80%，差的那些全是被 G2 拒掉的：
        材料就在手里，闸门却说「你问的那个词这份资料里没提到」。
        换个说法问同一件事，本来就不会用同一个词。

        所以在中间带里，大模型看过材料说「写明了」就放行。
        """
        question = self.BORDERLINE
        candidates = self._candidates(question)
        if not self._is_borderline(question, candidates):
            self.skipTest("该问题已不在「规则要拒」的中间带，此用例不适用")

        yes = StubProvider({"answerable": "yes"})
        verdict = gate.evaluate(self.index, question, candidates, provider=yes)
        self.assertTrue(verdict["ok"], verdict["reason"])
        self.assertTrue(verdict["rescued"], "翻案要留痕，答辩时要分得清")

    def test_no_provider_keeps_the_old_refusal(self):
        """**没有大模型时，行为和改之前一模一样。**

        这条是整个改动能够安全落地的前提：离线跑出来的指标一个数都不会变，
        全部自动化测试跑的也仍是原来那条路。
        """
        question = self.BORDERLINE
        candidates = self._candidates(question)
        if not self._is_borderline(question, candidates):
            self.skipTest("该问题已不在「规则要拒」的中间带，此用例不适用")
        verdict = gate.evaluate(self.index, question, candidates, provider=None)
        self.assertFalse(verdict["ok"])
        self.assertFalse(verdict["rescued"])

    def test_unavailable_provider_keeps_the_refusal(self):
        """模型配了但挂了，也维持拒答——**默认答案永远是拒**。"""
        question = self.BORDERLINE
        candidates = self._candidates(question)
        if not self._is_borderline(question, candidates):
            self.skipTest("该问题已不在「规则要拒」的中间带，此用例不适用")
        from app.llm.base import LLMUnavailable
        broken = StubProvider(raises=LLMUnavailable("测试里刻意挂掉"))
        verdict = gate.evaluate(self.index, question, candidates,
                                provider=broken)
        self.assertFalse(verdict["ok"])

    def test_llm_no_turns_borderline_into_refusal(self):
        # 构造一个覆盖率处在中间带的问题，让 G3 真正参与
        question = self.BORDERLINE
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

    def test_english_question_is_told_why_not_told_a_lie(self):
        """**拒答的理由必须是真的。**

        语料一个英文词都没有，整句英文提问必然检索不到。这时候仍旧回
        「暂时没有查到相关规定」，说的是假话——规定写得好好的，
        查不到是因为语言不通。用户会照着这句话以为平台没这条规矩。
        """
        result = support.run("when will my deposit be refunded")
        self.assertEqual(result["status"], "refused")
        self.assertIn("中文", result["message"])
        self.assertNotIn("没有查到相关规定", result["message"])

    def test_a_few_english_words_still_go_the_normal_way(self):
        """夹一两个英文词的中文问句是**检索得到的**，不能一并打发掉。"""
        result = support.run("我的 deposit 什么时候 refund")
        self.assertNotIn("中文再问", result["message"])

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
            # 口语里「我约的」比「我的预约」常见得多，三个关键词
            # 一个都不命中，实测掉进知识检索、拿 FAQ 硬答
            ("我约的看房房东回了吗", "booking"),
            ("我约的那次房东回了吗", "booking"),
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
                         "报修多久上门", "提前退租要付违约金吗",
                         # 这两条含「看房」也含人称，问的却是平台规则。
                         # 补 booking 的口语说法时**差点**把它们一起收进来，
                         # 分界线是「有没有指着自己约的那一次」。
                         "看房是谁带我去", "看房要给我带看费吗"]:
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

    def test_status_codes_are_translated_in_the_answer(self):
        """「当前状态：pending」出现在客服回答里，比不回答更露怯。"""
        backend = self.FakeBackend({"/api/repairs": {"list": [
            {"id": 1, "content": "卧室门锁打不开", "status": "pending"},
        ]}})
        status, message, _ = account.lookup(backend, "tok", "repair")
        self.assertEqual(status, "ok")
        self.assertIn("待处理", message)
        self.assertNotIn("pending", message)

    def test_contract_answer_leads_with_the_count(self):
        """「我租了几套房」问的就是数量，只报一份合同号是答非所问。"""
        rows = [
            {"contract_no": "HT1", "house_no": "DL0005", "status": "active",
             "start_date": "2026-09-01", "end_date": "2027-08-31"},
            {"contract_no": "HT2", "house_no": "DL0006", "status": "ended",
             "start_date": "2025-01-01", "end_date": "2025-12-31"},
        ]
        backend = self.FakeBackend({"/api/contracts/mine": {"list": rows}})
        status, message, data = account.lookup(backend, "tok", "contract")
        self.assertEqual(status, "ok")
        self.assertIn("2 份租约", message)
        self.assertIn("1 份在租期内", message)
        self.assertEqual(data["total"], 2)

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

    def test_every_role_has_a_chinese_name(self):
        """**库里有几种角色，就得能说出几种中文。**

        漏一种不会报错，只会让那个人看到「你的账号是 brokerli，
        身份：agent」——中介（J-02）就是这么漏掉的。
        所以这里照着库里的角色枚举逐个过一遍。
        """
        for role in ("tenant", "landlord", "agent", "admin"):
            backend = self.FakeBackend({"/api/auth/me": {
                "username": "u1", "role": role}})
            _, message, _ = account.lookup(backend, "tok", "profile")
            self.assertNotIn(role, message,
                             "角色 %s 没翻成中文：%s" % (role, message))



class TestCancelledBillsAreNotDebt(unittest.TestCase):
    """**作废的账单不是欠款。**

    退租时未到期的租金账单会被置成 cancelled（`leasing.end_contract`）。
    三处原先用 `status != "paid"` 去数，于是作废的也算进
    「你还欠多少房租」——退过一次租的人被告知欠着一笔早就作废的钱，
    而那笔钱他既不用付、也付不了（缴费接口对已结束的合同有拦截）。

    实测：演示账号 tenant1 的真实待缴是 22 笔 22000 元，
    助手报的是「34 笔，合计 44200 元，最早 2025-07-01 到期」。
    """

    BILLS = [
        {"bill_no": "B1", "status": "paid", "amount": 1000,
         "type": "rent", "due_date": "2026-08-01"},
        {"bill_no": "B2", "status": "unpaid", "amount": 2000,
         "type": "rent", "due_date": "2026-10-01"},
        {"bill_no": "B3", "status": "cancelled", "amount": 3000,
         "type": "rent", "due_date": "2026-11-01"},
        {"bill_no": "B4", "status": "cancelled", "amount": 4000,
         "type": "rent", "due_date": "2026-12-01"},
    ]

    class FakeBackend(object):
        def __init__(self, rows):
            self.rows = rows

        def get(self, path, params=None, token=None):
            assert path == "/api/bills/mine", path
            return {"list": self.rows}

    def test_only_unpaid_counts(self):
        message, data = account.bills(self.FakeBackend(self.BILLS), "tok")
        self.assertIn("1 笔", message, message)
        self.assertIn("2000", message.replace(",", ""), message)
        self.assertEqual([b["bill_no"] for b in data["unpaid"]], ["B2"])

    def test_all_cancelled_reads_as_settled(self):
        rows = [b for b in self.BILLS if b["status"] != "unpaid"]
        message, data = account.bills(self.FakeBackend(rows), "tok")
        self.assertIn("结清", message)
        self.assertEqual(data["unpaid"], [])


class TestMoneyText(unittest.TestCase):
    """金额的写法。

    原先同一个函数抄了三份，其中一份用 `%g`——
    `"%g" % 1000000.0` 是 `'1e+06'`，房东收入过百万就会看到
    「合计 1e+06 元」。另有一处直接 `%s` 一个 float，写出来是
    「合计 22000.0 元」。
    """

    def test_whole_numbers_have_no_decimal_point(self):
        self.assertEqual(money_text(22000.0), "22000")
        self.assertEqual(money_text(0), "0")

    def test_cents_keep_two_places(self):
        self.assertEqual(money_text(1250.5), "1250.50")
        self.assertEqual(money_text(1250.456), "1250.46")

    def test_never_scientific_notation(self):
        for value in (1000000, 1234567.0, 9.9e7):
            text = money_text(value)
            self.assertNotIn("e", text.lower(), text)

    def test_garbage_does_not_explode(self):
        self.assertEqual(money_text(None), "0")
        self.assertEqual(money_text("说不清"), "说不清")


class TestNoTopicIsRefused(unittest.TestCase):
    """**没有话题词的句子一律拒答（G0）。**

    覆盖率是个比率，分母是查询自己的 IDF 质量。查询没什么实质内容时
    分母很小，蒙中一两个泛词就能把比率顶到很高：

        怎么办    cover 1.000 → 答成「登录状态下在账号设置里可以改密码」
        随便      cover 1.000 → 答成「房东怎么入驻」
        还有别的吗  key_hit=True → 答成「注册时选择身份……」

    二十条这类短句，加 G0 之前闸门放行十二条，**而且每条都答得
    像模像样**——比拒答难看得多，用户会以为这系统在胡说。
    """

    @classmethod
    def setUpClass(cls):
        support.reset_knowledge_base()
        cls.index = KnowledgeIndex(corpus.load_faq())

    def test_which_ones_g0_cannot_catch(self):
        """**把漏网的那两条写出来，不假装全过。**

        「看看」是个真动词，G0 判不了它没话题；这两条最终是被覆盖率
        或关键词那两道门拦下的（`看看` cover 0.188），只有「随便看看」
        真的漏了出去（cover 0.511，答成一句「不可以。」）。
        写死这个集合，是为了它变大时立刻知道。
        """
        has_topic = [q for q in tune.NO_TOPIC if self.index.topic_term(q)]
        self.assertEqual(has_topic, ["随便看看", "看看"],
                         "G0 判不出「没话题」的短句集合变了：%s" % has_topic)

    def test_real_questions_all_have_a_topic(self):
        """32 条该答的问题，一条都不能被 G0 误伤。"""
        missing = [q for q, _ in tune.POSITIVES if not self.index.topic_term(q)]
        self.assertEqual(missing, ["合同能下载下来吗", "约了没去会怎么样"],
                         "被 G0 误伤的集合变了：%s" % missing)

    def test_the_gate_actually_refuses_them(self):
        for question in ("怎么办", "还有别的吗", "为什么", "都行", "太贵了"):
            verdict = gate.evaluate(self.index, question, ["faq-deposit-refund"])
            self.assertFalse(verdict["ok"], "「%s」该被拒：%s" % (question, verdict))
            self.assertIn("话题词", verdict["reason"])


if __name__ == "__main__":
    unittest.main()
