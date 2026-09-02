"""BM25 检索打分，纯标准库实现。

为什么不用 `rank_bm25`：那个包依赖 numpy，而本服务的依赖清单刻意保持
精简（只有 fastapi / uvicorn / pydantic / python-dotenv）。BM25 的公式本身
只有十几行，为它拉进一个数值计算库不划算，也会让「测试不依赖任何外部
环境」这条口径变脆。

公式用标准的 BM25 Okapi：

    score(q, d) = Σ_t IDF(t) · f(t,d)·(k1+1) / (f(t,d) + k1·(1 - b + b·|d|/avgdl))
    IDF(t) = ln(1 + (N - n(t) + 0.5) / (n(t) + 0.5))

IDF 用带平滑的形式，保证恒为正：语料只有几十条，
出现在半数以上文档里的词在经典 IDF 下会变成负分，
反而把包含该词的文档往下压，这在小语料上是实打实的坑。
"""

import math


class BM25Index(object):
    """一个词袋索引。构造时就把统计量算完，检索时只做加法。

    语料规模是几十条，全量重建的成本可以忽略，因此没有增量更新接口——
    公告变了直接重建整个索引，代码简单得多，也不会出现半更新的状态。
    """

    def __init__(self, documents, k1=1.5, b=0.75):
        """:param documents: ``[(doc_id, [token, ...]), ...]``"""
        self.k1 = k1
        self.b = b
        self.doc_ids = []
        self.doc_len = []
        # term -> {doc 序号: 词频}
        self.postings = {}

        for doc_id, tokens in documents:
            index = len(self.doc_ids)
            self.doc_ids.append(doc_id)
            self.doc_len.append(len(tokens))
            counts = {}
            for token in tokens:
                counts[token] = counts.get(token, 0) + 1
            for token, freq in counts.items():
                self.postings.setdefault(token, {})[index] = freq

        self.total = len(self.doc_ids)
        self.avgdl = (sum(self.doc_len) / self.total) if self.total else 0.0

        # IDF 预计算：查询侧只做查表
        self.idf = {}
        for token, posting in self.postings.items():
            n = len(posting)
            self.idf[token] = math.log(
                1 + (self.total - n + 0.5) / (n + 0.5))

    def __len__(self):
        return self.total

    def scores(self, query_tokens):
        """返回 ``{doc_id: score}``，只包含命中过至少一个查询词的文档。"""
        if not self.total or not query_tokens:
            return {}

        # 查询里重复出现的词只算一次。
        # 「押金押金什么时候退」不该比「押金什么时候退」得分更高——
        # 用户重复用词是口语习惯，不是相关性信号。
        raw = {}
        for token in set(query_tokens):
            posting = self.postings.get(token)
            if not posting:
                continue
            idf = self.idf[token]
            for index, freq in posting.items():
                norm = 1 - self.b + self.b * (
                    self.doc_len[index] / self.avgdl if self.avgdl else 1.0)
                weight = freq * (self.k1 + 1) / (freq + self.k1 * norm)
                raw[index] = raw.get(index, 0.0) + idf * weight

        return {self.doc_ids[i]: s for i, s in raw.items()}

    def search(self, query_tokens, top_k=10):
        """返回按分数降序的 ``[(doc_id, score), ...]``。

        同分时按 doc_id 排序，保证结果稳定——否则测试会随字典顺序抖动。
        """
        scored = self.scores(query_tokens)
        ranked = sorted(scored.items(), key=lambda kv: (-kv[1], str(kv[0])))
        return ranked[:top_k]
