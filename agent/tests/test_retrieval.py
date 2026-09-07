# -*- coding: utf-8 -*-
"""检索工具包的单元测试。

分工：本文件测**检索本身的性质**（分词、排序、融合、判据），
`test_support.py` 测客服智能体的流程，`test_acceptance.py` 统计整体命中率。

几条用例是**回归测试**——它们对应开发中真实踩过的坑，注释里写明了是哪一个。
删掉它们，那些坑会原样回来。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.retrieval import rrf  # noqa: E402
from app.retrieval.bm25 import BM25Index  # noqa: E402
from app.retrieval.index import KnowledgeIndex  # noqa: E402
from app.retrieval.tokenize import (char_tokens, content_chars,  # noqa: E402
                                    normalize, tokenize)


def chunk(chunk_id, title, body, aliases=None, category="测试"):
    return {"id": chunk_id, "title": title, "body": body,
            "aliases": aliases or [], "category": category,
            "source": "faq", "link": None}


class TestTokenize(unittest.TestCase):

    def test_normalize_folds_fullwidth_and_strips_punctuation(self):
        self.assertEqual(normalize("押金，怎么退？"), "押金 怎么退")
        # 全角字母数字要折成半角，否则「ＷＩＦＩ」和「wifi」检索不到一起
        self.assertEqual(normalize("ＷＩＦＩ１２"), "wifi12")

    def test_chinese_produces_unigram_and_bigram(self):
        tokens = char_tokens("押金")
        self.assertIn("押", tokens)
        self.assertIn("金", tokens)
        self.assertIn("押金", tokens)

    def test_ascii_word_kept_whole(self):
        """「wifi」不该被拆成 w/i/f/i，那样任何含这些字母的词都会误召回。"""
        self.assertIn("wifi", tokenize("有wifi吗"))

    def test_stopword_excluded_from_unigram_but_bigram_kept(self):
        tokens = char_tokens("退了吗")
        self.assertNotIn("吗", tokens)
        # bigram 保留：问句词一旦被拆断，问句之间的相似度就没了
        self.assertIn("了吗", tokens)

    def test_content_chars_drops_stopwords(self):
        self.assertEqual(content_chars("押金吗"), {"押", "金"})


class TestBM25(unittest.TestCase):

    def setUp(self):
        self.index = BM25Index([
            ("a", tokenize("押金七个工作日内退还")),
            ("b", tokenize("水电费由租客自行缴纳")),
            ("c", tokenize("押金扣款需要双方确认")),
        ])

    def test_relevant_document_ranks_first(self):
        ranked = self.index.search(tokenize("押金退还"), top_k=3)
        self.assertEqual(ranked[0][0], "a")

    def test_unrelated_document_not_returned(self):
        scored = self.index.scores(tokenize("押金"))
        self.assertNotIn("b", scored)

    def test_idf_always_positive(self):
        """小语料上经典 IDF 会给高频词负分，把包含该词的文档反向压下去。

        我们用的是带平滑的形式，必须恒为正。
        """
        self.assertTrue(all(v > 0 for v in self.index.idf.values()))

    def test_repeated_query_terms_do_not_inflate_score(self):
        """「押金押金什么时候退」不该比「押金什么时候退」得分更高——
        用户重复用词是口语习惯，不是相关性信号。"""
        once = self.index.scores(tokenize("押金"))
        twice = self.index.scores(tokenize("押金押金"))
        self.assertEqual(once, twice)

    def test_empty_index_is_safe(self):
        self.assertEqual(BM25Index([]).search(tokenize("押金")), [])

    def test_ties_are_deterministic(self):
        """同分按 id 排序。否则结果随字典顺序抖动，测试会时好时坏。"""
        index = BM25Index([("b", ["x"]), ("a", ["x"])])
        self.assertEqual([i for i, _ in index.search(["x"], 2)], ["a", "b"])


class TestRRF(unittest.TestCase):

    def test_item_ranked_well_in_both_lists_wins(self):
        fused = rrf.fuse([[("a", 9), ("b", 1)], [("a", 5), ("b", 4)]])
        self.assertEqual(fused[0][0], "a")

    def test_only_rank_matters_not_score(self):
        """RRF 对分数量纲免疫——这正是选它做召回融合的原因。"""
        low = rrf.fuse([[("a", 0.001), ("b", 0.0005)]])
        high = rrf.fuse([[("a", 9999), ("b", 5000)]])
        self.assertEqual([i for i, _ in low], [i for i, _ in high])

    def test_zero_weight_channel_is_ignored(self):
        fused = rrf.fuse([[("a", 1)], [("b", 1)]], weights=[1.0, 0.0])
        self.assertEqual([i for i, _ in fused], ["a"])

    def test_explain_reports_rank_per_channel(self):
        detail = rrf.explain([[("a", 1), ("b", 1)], [("b", 1)]], "b")
        self.assertEqual([d["rank"] for d in detail], [2, 1])


class TestKnowledgeIndex(unittest.TestCase):

    def setUp(self):
        self.index = KnowledgeIndex([
            chunk("deposit", "押金什么时候退",
                  "租期届满后 7 个工作日内原路退还押金。",
                  ["保证金什么时候退", "押金退款流程", "押金能退吗",
                   "押金多久到账"]),
            chunk("early", "提前退租押金还退吗",
                  "提前解约要先付一个月租金的违约金。",
                  ["提前搬走押金"]),
            chunk("utility", "水电费怎么算",
                  "水电费由租客自行向供水供电单位缴纳。"),
        ])

    def test_alias_variants_are_max_pooled_not_concatenated(self):
        """**回归测试**：别名曾被拼成一篇长文档，结果写别名越多的片排得越靠后。

        BM25 的 b 参数按文档长度惩罚词频，4 条别名拼起来就成了「长文档」。
        实测「保证金能退吗」因此命中了只有 1 条别名的「提前退租押金还退吗」，
        而不是写了 4 条别名、其中一条就是「押金能退吗」的那一片。

        别名是**并列的问法变体**，不是更长的文档，必须各自成篇取最大值。
        """
        ranked, detail = self.index.search("保证金能退吗", top_k=3)
        reranked = self.index.rerank("保证金能退吗", ranked, detail, top_k=3)
        self.assertEqual(reranked[0][0], "deposit")

    def test_coverage_is_idf_weighted_not_raw_char_ratio(self):
        """**回归测试**：覆盖率曾按「查询的字有多少出现在材料里」算，
        结果「能不能开发票」（该拒答）算出 0.60，比「保证金能退吗」
        （该回答）的 0.40 还高——因为「能/不/开/发」到处都是。

        IDF 加权之后，罕见字说了算，两者必须拉开。
        """
        good = self.index.coverage("押金什么时候退", "deposit")
        bad = self.index.coverage("能不能开发票", "deposit")
        self.assertGreater(good, bad)

    def test_key_term_skips_function_words(self):
        """**回归测试**：关键词曾选出「帮我」这种跨词边界的组合，
        还在语料里命中了，导致「帮我订个外卖」被判为可答。

        含虚词的二元组说明切在词缝上，不是一个词。
        """
        self.assertNotIn("帮我", self.index.key_term("帮我订个外卖"))

    def test_key_term_hit_false_for_out_of_domain_topic(self):
        self.assertFalse(
            self.index.key_term_hit("帮我订个外卖",
                                    self.index.order[0]))

    def test_key_term_is_a_real_word_not_a_fragment(self):
        """**回归测试**：关键词曾系统性地挑中切在词缝上的碎片。

        字符 bigram 的候选池里，真词和碎片混在一起，而**没见过的组合
        IDF 必然最高**——「退租要提前多久说」于是挑中「久说」，
        「退租要打扫干净吗」挑中「扫干」。两个都不是词，语料里当然
        不会出现，闸门就把答案排在检索第一位的问题拒了。

        这里断言的是**词性**而不是某个具体取值：挑出来的东西必须是
        这句话里真实存在的一个词。
        """
        for question in ("退租要提前多久说", "退租要打扫干净吗",
                         "预约完多久房东会确认", "物业费是房东交还是我交"):
            term = self.index.key_term(question)
            self.assertTrue(term, question)
            self.assertIn(term, question)
            self.assertNotIn(term, ("久说", "扫干", "约完", "东交"),
                             "「%s」挑出了词缝上的碎片「%s」"
                             % (question, term))

    def test_multi_character_topic_words_are_eligible(self):
        """**三个字的词恰恰常常就是那句话在问的东西。**

        「公积金」「物业费」「暖气费」都是三字词。两字的限制原本只是
        为了对付字符 bigram，有了词典就没有理由留着——留着的后果是
        「可以用公积金付房租吗」退而挑到「房租」，而「房租」在这个
        语料里到处都是，一命中闸门就放行了一个本该拒答的问题。
        """
        self.assertEqual(self.index.key_term("可以用公积金付房租吗"),
                         "公积金")

    def test_modifiers_are_not_topics(self):
        """**「一般」是个词，但它不是这句话在问的东西。**

        挡住了词缝碎片之后，漏网的下一类是修饰语：「退租一般要提前
        多久说」挑中「一般」、「退租要打扫干净吗」挑中「干净」。
        两个都是词、IDF 也高，可它们描述的是程度和状态，不是话题——
        于是闸门又一次拒掉了知识库里写得清清楚楚的问题。
        """
        for question, modifier in (("退租一般要提前多久说", "一般"),
                                   ("退租要打扫干净吗", "干净"),
                                   ("租金定多少合适", "合适")):
            self.assertNotEqual(self.index.key_term(question), modifier,
                                "「%s」把修饰语当成了话题" % question)

    def test_vector_channel_absent_without_embedder(self):
        """没有嵌入服务时向量通道必须整条关掉，而不是半开着。"""
        self.assertFalse(self.index.vector_enabled)
        ranked, detail = self.index.search("押金", top_k=3)
        self.assertNotIn("vector", detail["channels"])

    def test_search_returns_nothing_for_empty_query(self):
        ranked, _ = self.index.search("", top_k=3)
        self.assertEqual(ranked, [])


if __name__ == "__main__":
    unittest.main()
