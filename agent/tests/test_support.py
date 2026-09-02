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
from app.agents.support.tools import (answer as answer_tool,  # noqa: E402
                                      classify_ask, gate, toolcall)
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


if __name__ == "__main__":
    unittest.main()
