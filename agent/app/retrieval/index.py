"""知识索引：多路建索引、多路检索、RRF 融合。

四路通道：

| 路 | 索引对象 | 存在的理由 |
|---|---|---|
| L1 正文 | 片正文分词 | 主通道 |
| L2 标题 | 片标题 + 别名分词 | 客服场景里「用户问句 vs FAQ 问句」的信号最强 |
| L3 同义 | 正文索引 × 同义扩展后的查询 | 用词对不上时补召回 |
| L4 向量 | 标题 + 正文的嵌入 | 配了嵌入服务才启用，句式差异大时补召回 |

L2 单独建索引而不是把标题拼进正文，是因为**拼接会被长度归一化吃掉**：
BM25 的 `b` 参数按文档长度惩罚词频，标题只有十几个字，混进几百字的正文里
几乎不产生区分度。单独建一路，标题就是一篇短文档，命中即高分。

索引是**全量重建**式的：语料几十条，重建耗时以毫秒计，
没有增量更新接口。少一套增量逻辑就少一类「半更新状态」的 bug。
"""

import math

from app.retrieval import rrf
from app.retrieval.bm25 import BM25Index
from app.retrieval.embed import EmbeddingUnavailable, cosine, normalize_vector
from app.retrieval.tokenize import WORD_LEVEL, tokenize, topic_words

# 虚词表：代词、助词、疑问词、程度词与最常见的功能动词。
# 只用于挑选「话题词」，不参与检索本身——检索交给 IDF 自己去压权重就够了，
# 这里要的是「哪个词代表这句话在问什么」，虚词一律不算。
_FUNCTION_CHARS = set(
    "我你他她它们的了着吗呢吧啊呀哦嗯么呗被把给让请帮"
    "是不没无有在和跟与及或者就还也都很太更最挺"
    "怎么什哪那这个些多少几要能会可以够行得该应当需想"
    "上下前后里外中间左右为了因所以但如果虽然"
)

# 通道编号，供解释与日志使用
CHANNEL_BODY = "body"
CHANNEL_TITLE = "title"
CHANNEL_SYNONYM = "synonym"
CHANNEL_VECTOR = "vector"

# 各路权重。标题略高于正文——客服问答里问句对问句的匹配更可信；
# 同义路是补召回，权重压低，避免扩展词把原意冲淡。
CHANNEL_WEIGHTS = {
    CHANNEL_BODY: 1.0,
    CHANNEL_TITLE: 1.2,
    CHANNEL_SYNONYM: 0.6,
    CHANNEL_VECTOR: 1.0,
}


class KnowledgeIndex(object):
    """一组语料片上的可检索索引。"""

    def __init__(self, chunks, embedder=None, cache=None, model=""):
        self.chunks = {c["id"]: c for c in chunks}
        self.order = [c["id"] for c in chunks]

        body_docs = []
        title_docs = []
        # 变体索引的 doc_id -> 所属片，用于把变体得分归并回片
        self._variant_owner = {}
        for chunk in chunks:
            body_docs.append((chunk["id"], tokenize(
                "%s\n%s" % (chunk.get("title", ""), chunk.get("body", "")))))

            # 标题与每一条别名**各自作为一篇独立文档**，检索后按片取最大值。
            #
            # 这里踩过一个坑：最初把标题和别名拼成一篇文档，结果别名写得多的
            # 片反而排得靠后——BM25 的 b 参数按文档长度惩罚词频，4 条别名拼起来
            # 就成了「长文档」。实测「保证金能退吗」因此没能命中写了 4 条别名的
            # 「押金什么时候退」，而命中了只有 2 条别名的「提前退租押金还退吗」。
            #
            # 别名的语义是「同一个问题的另一种问法」，是**并列的变体**，不是
            # 一篇更长的文档。所以正确的做法是各自成篇、取最大值（max-pooling）：
            # 命中任何一种问法都算命中，且不因为写得全而受罚。
            for order, variant in enumerate(
                    [chunk.get("title", "")] + list(chunk.get("aliases") or [])):
                if not variant.strip():
                    continue
                key = "%s#%d" % (chunk["id"], order)
                self._variant_owner[key] = chunk["id"]
                title_docs.append((key, tokenize(variant)))

        self.body_index = BM25Index(body_docs)
        self.title_index = BM25Index(title_docs)

        # 每片的词集合，供可答性闸门算覆盖率。存集合而不是列表——
        # 闸门只问「有没有」，不问「有几次」。
        self._chunk_tokens = {}
        for chunk_id, tokens in body_docs:
            self._chunk_tokens[chunk_id] = set(tokens)
        # 只含问句与别名的词集合，单独留一份：闸门要问的是
        # 「用户这句话像不像这条 FAQ 的问句」，那是比「正文里提没提」
        # 强得多的信号——见 `title_coverage`。
        self._title_tokens = {}
        for key, tokens in title_docs:
            owner = self._variant_owner[key]
            self._chunk_tokens.setdefault(owner, set()).update(tokens)
            self._title_tokens.setdefault(owner, set()).update(tokens)

        # 语料里从未出现过的词该给多大权重：按「只在 0 篇文档里出现」代入
        # IDF 公式，得到的是这个语料下 IDF 的上界。
        # 这一步是闸门能不能拦住无关问题的关键——「发票」的「票」、
        # 「宠物」的「宠」在语料里根本不存在，正是「我们没有这条知识」
        # 最强的证据，必须给最高权重，而不是当作缺省的 0。
        self._unseen_idf = math.log(1 + (self.body_index.total + 0.5) / 0.5)

        # 全库词表 = 正文 ∪ 标题 ∪ 别名。
        #
        # 必须并上标题与别名，否则会出现这样的错判：「保证金」只写在
        # 「押金什么时候退」的别名里、正文里一次没出现，于是被当成
        # 「全库都不存在的词」——而它明明是我们特意写进去的同义问法。
        # 实测这条 bug 让「保证金能退吗」的覆盖率 1.000、未登录率却高达 0.684。
        self._vocabulary = set(self.body_index.postings)
        self._vocabulary.update(self.title_index.postings)

        self.vectors = {}
        self.embedder = embedder
        self.model = model
        self.cache = cache
        if embedder is not None and getattr(embedder, "available", False):
            self._build_vectors(chunks)

    # ------------------------------------------------------------ 向量

    def _vector_text(self, chunk):
        """嵌入用的文本：标题在前、正文在后。

        标题放前面不是随意排的——多数嵌入模型对开头的内容更敏感，
        而标题恰好是这一片「在讲什么」的最短表述。
        """
        return "%s\n%s" % (chunk.get("title", ""), chunk.get("body", ""))

    def _build_vectors(self, chunks):
        """构建语料向量，优先吃缓存，只对未命中的片发请求。"""
        pending = []
        for chunk in chunks:
            text = self._vector_text(chunk)
            cached = self.cache.get(text, self.model) if self.cache else None
            if cached:
                self.vectors[chunk["id"]] = cached
            else:
                pending.append((chunk["id"], text))

        if not pending:
            return

        try:
            raw = self.embedder.embed([text for _, text in pending])
        except EmbeddingUnavailable:
            # 嵌入服务不可用：已经从缓存拿到的向量作废，避免只有一部分片
            # 有向量——那会让向量通道系统性地偏向「恰好被缓存过」的片，
            # 比完全不用向量更糟。
            self.vectors = {}
            return

        for (chunk_id, text), vector in zip(pending, raw):
            unit = normalize_vector(vector)
            self.vectors[chunk_id] = unit
            if self.cache:
                self.cache.put(text, unit)
        if self.cache:
            self.cache.save(self.model)

    def _vector_search(self, query, top_k):
        if not self.vectors or self.embedder is None:
            return []
        try:
            raw = self.embedder.embed([query])
        except EmbeddingUnavailable:
            return []
        if not raw:
            return []
        unit = normalize_vector(raw[0])
        scored = [(cid, cosine(unit, vec)) for cid, vec in self.vectors.items()]
        scored = [item for item in scored if item[1] > 0]
        scored.sort(key=lambda kv: (-kv[1], str(kv[0])))
        return scored[:top_k]

    def _title_search(self, query_tokens, top_k):
        """标题路检索：变体各自打分，按片取最大值。

        取最大值而不是求和：一个片写了 4 条别名、查询同时沾上其中 3 条，
        不代表它比只沾上 1 条但沾得更准的片更相关。
        """
        scored = self.title_index.scores(query_tokens)
        pooled = {}
        for key, score in scored.items():
            owner = self._variant_owner.get(key, key)
            if score > pooled.get(owner, 0.0):
                pooled[owner] = score
        ranked = sorted(pooled.items(), key=lambda kv: (-kv[1], str(kv[0])))
        return ranked[:top_k]

    # ------------------------------------------------------------ 检索

    @property
    def vector_enabled(self):
        return bool(self.vectors)

    def __len__(self):
        return len(self.order)

    def search(self, query, expanded="", top_k=8, per_channel=None):
        """多路检索并融合。

        :param expanded: 同义扩展后的查询文本，空串表示跳过该路
        :returns: ``(ranked, detail)``
                  ``ranked`` 是 ``[(chunk_id, rrf_score), ...]``；
                  ``detail`` 记录每一路的原始排名，用于解释与日志。
        """
        # 各路多取一些候选再融合：融合的价值来自「这一路排第 6、那一路排第 1」
        # 这种互补，只取 top_k 会把互补信息切掉。
        per_channel = per_channel or max(top_k * 3, 15)

        query_tokens = tokenize(query)
        channels = []

        body = self.body_index.search(query_tokens, per_channel)
        channels.append((CHANNEL_BODY, body))

        title = self._title_search(query_tokens, per_channel)
        channels.append((CHANNEL_TITLE, title))

        if expanded:
            synonym = self.body_index.search(tokenize(expanded), per_channel)
            channels.append((CHANNEL_SYNONYM, synonym))

        vector = self._vector_search(query, per_channel) if self.vectors else []
        if vector:
            channels.append((CHANNEL_VECTOR, vector))

        ranked = rrf.fuse(
            [items for _, items in channels],
            weights=[CHANNEL_WEIGHTS.get(name, 1.0) for name, _ in channels],
            top_k=top_k)

        detail = {
            "channels": [name for name, _ in channels],
            "per_channel": {name: [cid for cid, _ in items[:5]]
                            for name, items in channels},
            # 闸门要用「原始 BM25 分数」而不是 RRF 分数：RRF 只反映名次，
            # 一堆烂结果里的第一名，RRF 分数和一个完美命中的第一名是一样的。
            "body_scores": dict(body),
            "title_scores": dict(title),
        }
        return ranked, detail

    # 精排权重。覆盖率占大头——它是唯一「跨查询可比」的量：
    # BM25 分数只有在同一次查询内部比较才有意义，覆盖率天然在 [0,1]。
    RERANK_COVERAGE = 0.55
    RERANK_TITLE = 0.30
    RERANK_BODY = 0.15

    def rerank(self, query, ranked, detail, top_k=3, expanded=""):
        """对 RRF 的候选做确定性精排，返回 ``[(chunk_id, score), ...]``。

        **为什么召回之后还要单独精排。** RRF 只用名次、丢掉分数量级，
        这让它对量纲免疫，但也让它丢掉「置信度」。实测撞上过一次：
        「保证金能退吗」在标题路上以 24.44 : 8.28 压倒性地指向
        「押金什么时候退」，可到了 RRF 里这只是「第 1 名 vs 第 2 名」，
        结果被正文路一个平庸的命中翻了盘，top-1 排成了「提前退租押金还退吗」。

        所以职责拆开：**RRF 负责召回**（把该看的都捞进来，对量纲免疫），
        **精排负责精度**（在小候选集内部把分数量级用起来）。
        分数在候选集内归一化后加权——只在同一次查询的候选之间比较，
        不跨查询比，这正是 BM25 分数唯一站得住的用法。

        **`expanded` 传进来的话，覆盖率取「原句」与「扩展句」的较大者。**

        不传的话这里只按原句算覆盖率，而召回用的是扩展句——同一次排序里
        两个环节看的是两份不同的查询。实测后果：「到期退租有哪些步骤」
        召回阶段把「到期正常退租的流程是什么」排在第一（0.0451），
        精排把它压到第二（0.4807 vs 0.5020），让位给「提前退租押金还退吗」。
        原因就是「步骤」和「流程」是同义组里的两个词，扩展句知道，
        原句不知道。

        取**较大者**而不是直接用扩展句：扩展会把整组同义词并进查询，
        分母跟着变大，只按扩展句算会稀释掉原话里那些确切的词。
        实测三种口径（原句 / 扩展句 / 较大者）在 52 条有金标的问题上，
        top-1 是 48 / 47 / **49**，top-3 是 50 / 51 / **51**。
        """
        if not ranked:
            return []

        title_scores = detail.get("title_scores", {})
        body_scores = detail.get("body_scores", {})
        candidates = [cid for cid, _ in ranked]

        max_title = max([title_scores.get(c, 0.0) for c in candidates] + [0.0])
        max_body = max([body_scores.get(c, 0.0) for c in candidates] + [0.0])

        scored = []
        for chunk_id in candidates:
            cover = self.coverage(query, chunk_id)
            if expanded:
                cover = max(cover, self.coverage(expanded, chunk_id))
            title = (title_scores.get(chunk_id, 0.0) / max_title) if max_title else 0.0
            body = (body_scores.get(chunk_id, 0.0) / max_body) if max_body else 0.0
            score = (self.RERANK_COVERAGE * cover
                     + self.RERANK_TITLE * title
                     + self.RERANK_BODY * body)
            scored.append((chunk_id, round(score, 4)))

        scored.sort(key=lambda kv: (-kv[1], str(kv[0])))
        return scored[:top_k]

    def coverage(self, query, chunk_id):
        """查询在某一片上的 **IDF 加权覆盖率**，取值 [0, 1]。

    这是可答性闸门的主判据，也是整个功能里最容易做错的一处。

    最初的写法是「查询里的字有多少出现在材料里」，直接失败了：
    「能不能开发票」的字是 能/不/开/发/票，前四个在任何一篇租房 FAQ 里
    都能找到，覆盖率算出来 0.60，比真该回答的「保证金能退吗」（0.40）还高。
    按这个数字定阈值，要么放行发票、要么错拒保证金，两头不讨好。

    问题不在阈值，在于**把每个字当成同等证据**。真正决定「这条知识在不在库里」
    的是罕见词：「票」「宠」「股」在语料里根本不存在，而「能」「不」「怎」
    出现在几乎每一片里、不携带任何信息。IDF 恰好就是这个权重。

    未登录词按语料 IDF 的上界计权（见 `_unseen_idf`），
    所以问题里只要有一个语料里压根没有的关键概念，覆盖率就会被显著拉低。
        """
        tokens = set(tokenize(query))
        if not tokens:
            return 0.0
        present = self._chunk_tokens.get(chunk_id, set())

        total = 0.0
        matched = 0.0
        for token in tokens:
            weight = self.body_index.idf.get(token, self._unseen_idf)
            total += weight
            if token in present:
                matched += weight
        return round(matched / total, 4) if total else 0.0

    def unseen_ratio(self, query):
        """查询里有多少 IDF 质量落在**整个语料都不存在**的词上，取值 [0, 1]。

        与 `coverage` 是两个不同层面的判据，缺一不可：

        - `coverage` 问「**这一片**有没有覆盖你的问题」——它依赖检索排得对不对；
        - `unseen_ratio` 问「**整个知识库**里有没有这个概念」——与排序完全无关。

        为什么必须有第二条：实测「能不能开发票」的覆盖率是 0.426，
        比真该回答的「提前搬走要赔钱吗」（0.383）还高，光靠覆盖率无论
        阈值定在哪都会错。但换个角度看就一目了然——「票」这个字在 54 条
        语料里一次都没出现过。「宠」「股」「地铁」「诗」同理。
        **全库缺失是比「这一片不够像」强得多的拒答证据。**

        只统计有区分度的词：语料里出现得极其普遍的词（IDF 很低）
        即使缺失也说明不了什么，不参与计算。
        """
        # **只看单字，不看 bigram。** 这是踩了一次才改对的地方。
        #
        # 最初把 bigram 也算进来，结果七条本该回答的问题全被拒了：
        # 「退租要打扫干净吗」的 unseen 是 0.675、「修东西的钱谁出」是 0.511,
        # 可它们的正确答案明明排在检索第一位（Recall@3 = 7/7）。
        # 原因是 54 条语料里的 bigram 组合本来就稀疏，**任何一种新说法**
        # 都会造出一堆没见过的 bigram——这个指标量的是「措辞新不新」，
        # 而不是「话题在不在库里」，两者完全是两回事。
        #
        # 话题缺失的信号落在**单字**上：「发票」的票、「宠物」的宠、
        # 「地铁」的铁、「补贴」的贴，语料里一次都没出现过。
        # 措辞再新，只要用的都是库里有的字，就说明谈的还是库里的事。
        tokens = {t for t in set(tokenize(query)) if len(t) == 1 or t.isascii()}
        if not tokens:
            return 0.0

        total = 0.0
        unseen = 0.0
        for token in tokens:
            in_vocab = token in self._vocabulary
            weight = self.body_index.idf.get(token, self._unseen_idf)
            # 低区分度的词不参与：它在不在都不改变结论
            if weight < self._unseen_idf * 0.35:
                continue
            total += weight
            if not in_vocab:
                unseen += weight
        return round(unseen / total, 4) if total else 0.0

    def title_coverage(self, query, chunk_id):
        """查询在这条 FAQ 的**问句与别名**上的 IDF 加权覆盖率。

        与 `coverage` 的区别只有一个：那个算的是整片（问句 + 别名 + 正文），
        这个只算问句和别名。**「问句 vs 问句」比「问句 vs 答案正文」
        强得多**，因为答案正文里充满了与提问无关的解释性文字，
        它们会把覆盖率稀释掉。

        这条判据是被一对反例逼出来的：

            退租要打扫干净吗   该回答，整片覆盖率只有 0.38
            装修能报销吗       该拒答，整片覆盖率高达 0.74

        整片覆盖率在这一对上是**反的**——负例的正文恰好用词很像。
        换成只看问句就正过来了：前者对上的是「退租时房子要打扫吗」，
        几乎是同一个问题；后者对上的是「我自己找人修完能报销吗」，
        问的根本是另一件事。
        """
        tokens = set(tokenize(query))
        if not tokens:
            return 0.0
        present = self._title_tokens.get(chunk_id, set())

        total = 0.0
        matched = 0.0
        for token in tokens:
            weight = self.body_index.idf.get(token, self._unseen_idf)
            total += weight
            if token in present:
                matched += weight
        return round(matched / total, 4) if total else 0.0

    def key_term(self, query):
        """查询里区分度最高的那个词——近似「这句话在问什么」。

        **先判断它到底是不是一个词，再比区分度。** 装了词级分词时走
        `_key_term_by_word`；没装才退回下面的字符 bigram 近似。
        """
        if WORD_LEVEL:
            term = self._key_term_by_word(query)
            if term:
                return term

        # **保序去重，不能用 set。**
        #
        # 原来写的是 `set(tokenize(query))`，下面 `max` 在 IDF 打平时
        # 取哪一个，就取决于这次运行的字符串哈希种子——**同一个问题
        # 两次跑出不同的关键词**，可答性闸门跟着翻转，测试于是时红时绿
        # （实测同一份代码连跑三次，失败数在 2 和 3 之间跳）。
        #
        # 这类不确定性最坏的地方不是测试不稳，是**线上同一个用户
        # 问同一句话，今天能答明天拒答**，而谁也复现不出来。
        #
        # 去重后按出现顺序排：并列时取句子里先出现的那个。这个选择
        # 本身是任意的，但**它是稳定的**，而且和句子有关系，
        # 比按哈希取讲得通。
        tokens = list(dict.fromkeys(tokenize(query)))
        if not tokens:
            return ""

        # 只保留**不含虚词**的双字组合。
        #
        # 不加这个过滤会挑出「帮我」「西的」「约完」「期退」这种跨词边界的
        # 垃圾二元组——它们 IDF 恰好很高（语料里没有这种搭配），
        # 于是被当成了「这句话在问什么」。实测「帮我订个外卖」的关键词
        # 被选成「帮我」，还在语料里命中了，闸门直接放行。
        #
        # 中文里话题词几乎总是实词构成的双字词，虚词一旦参与就说明
        # 这个二元组是切在词的接缝上，不是一个词。
        # 试过再加一条「只在语料里出现过的词里挑」（把语料当词典），**更差**：
        # 它会让负例挑到一个次要的真词而绕开真正陌生的话题词——
        # 「装修能报销吗」挑到「报销」、「可以养宠物吗」退回单字挑到「物」，
        # 调参集拒答率从 92.9% 掉到 57.1%。再叠一条「存在库外实词就硬拒」
        # 也不行，会误伤「压金」「宽带」这些正例，回答率掉到 70.4%。
        # 结论是虚词过滤到此为止，别再往上叠规则。
        candidates = [t for t in tokens
                      if len(t) == 2 and not t.isascii()
                      and not (t[0] in _FUNCTION_CHARS
                               or t[1] in _FUNCTION_CHARS)]
        if not candidates:
            candidates = [t for t in tokens
                          if len(t) == 1 and t not in _FUNCTION_CHARS]
        if not candidates:
            return ""
        return max(candidates,
                   key=lambda t: self.body_index.idf.get(t, self._unseen_idf))

    def _key_term_by_word(self, query):
        """在**真词**里挑区分度最高的那个。挑不出来返回空串。

        下面那套字符 bigram 的近似有个系统性缺陷：候选池里真词与
        跨词边界的碎片混在一起，而**没见过的组合 IDF 必然最高**——
        于是它偏偏总挑中碎片。实测「退租要提前多久说」挑到「久说」、
        「退租要打扫干净吗」挑到「扫干」、「预约完多久房东会确认」挑到
        「约完」。这些都不是词，语料里当然不会出现，闸门于是拒掉了
        一批答案就排在检索第一位的问题（验收集 4 条里占 3 条）。

        虚词过滤治不了这个：「久说」「扫干」四个字全是实词。
        要分开「陌生的话题词」和「压根不是词」，只能靠**词典**。
        注意这与「把语料当词典」是两回事，后者试过且更差（见下面那段
        注释）：语料没有的「装修」仍然是词，仍然该挑中、仍然该拒答；
        「扫干」不是词，就不该参与竞选。

        **不限定两个字。**「公积金」「物业费」「暖气费」都是三字词，
        而且恰恰就是那句话在问的东西——两字的限制原本只是为了对付
        字符 bigram，有了词典就没有理由留着它。

        再往前一步是**词性**（`tokenize.topic_words`）：光是词还不够，
        「一般」「干净」「合适」都是词，但它们是修饰语不是话题。
        只在名词和动词里挑之后，负例挑出来的也变成了「股市」「天气」
        「地铁」——**拒答的理由这才说得出口**。

        两步都标定在调参集上：回答 24/29 → 25/29 → 27/29，
        拒答 13/14 全程不变。
        """
        # 同样保序去重：`topic_words` 可能返回重复词，而并列时
        # 取哪一个必须是确定的（理由见 `key_term` 里那段）
        words = [w for w in dict.fromkeys(topic_words(query))
                 if len(w) >= 2 and not w.isascii()
                 and not (w[0] in _FUNCTION_CHARS or w[-1] in _FUNCTION_CHARS)]
        if not words:
            return ""
        return max(words,
                   key=lambda t: self.body_index.idf.get(t, self._unseen_idf))

    def topic_term(self, query):
        """这句话里有没有一个能当**话题**的词，有就返回其中区分度最高的。

        `key_term` 挑不出真词时会退回字符 bigram，总能给出点什么；
        这里不退——**「挑不出来」本身就是一个结论**：一句话里连一个
        名词或动词都没有，那就不存在「知识库里讲没讲过它」这个问题。
        闸门拿它当第一道门用（见 `gate.evaluate` 的 G0）。
        """
        return self._key_term_by_word(query)

    def term_in_corpus(self, term):
        """这个词在**整个语料**里出现过吗。

        和 `key_term_hit` 的区别是范围：那个问「在选中的这一片里」，
        这个问「在知识库任何一处」。两者回答的是不同的问题——
        前者判「排得对不对」，后者判「这个概念我们到底讲没讲过」。

        闸门的翻案侧用它兜底：关键词全库都没出现过时，
        **再像的材料也不该放行**——那时候大模型只能靠自己的知识作答，
        而 AI-03 禁止的正是这件事。
        """
        if not term:
            return False
        # BM25Index 没有单独的 df 表，倒排表本身就是答案：
        # postings 里有这个词，就说明至少一片文档里出现过
        return bool(self.body_index.postings.get(term))

    def key_term_hit(self, query, chunk_id):
        """区分度最高的那个词，在这一片里出现了吗。

        **这是可答性闸门最有效的一条判据**，而且是试错试出来的。

        先试过「查询词有多少比例在语料里没出现过」：算 bigram 太严
        （任何新说法都会造出没见过的 bigram，七条该答的问题全被拒），
        只算单字又太松（「天气」的天、「发票」的发在租房语料里都是常见字，
        负例几乎全放行，拒答率掉到 64%）。两头都不行，说明
        **「措辞新不新」根本不是「答得了答不了」的正确代理量**。

        换成这一条就干净了：不看整句话有多新，只看**问的那个东西**
        在选中的材料里到底提没提。「装修能报销吗」检索到「自己找人修能报销吗」，
        覆盖率高达 0.74，但 `装修` 这个词在那一片里一次都没有——
        它答的是「维修报销」，不是「装修」。调参集上正例命中率 0.48、
        负例只有 0.07，是所有试过的特征里分得最开的。
        """
        term = self.key_term(query)
        if not term:
            # 连一个「库里见过的实词」都挑不出来 —— 这本身就说明
            # 这个话题不在知识库里，按未命中处理。
            return False
        return self.key_term_hit_for(term, chunk_id)

    def doc_ratio(self, term):
        """这个词出现在多大比例的片子里，取值 [0, 1]。

        用比例而不是 IDF 的绝对值：**比例的含义和语料规模无关**——
        「出现在五分之一的片子里」这句话，语料从 70 条涨到 700 条依然成立，
        而 IDF 的绝对值会跟着整体漂移，按它定的阈值过一阵就不准了。
        """
        if not term or not self.body_index.total:
            return 0.0
        posting = self.body_index.postings.get(term) or {}
        return len(posting) / self.body_index.total

    def key_term_hit_for(self, term, chunk_id):
        """指定一个词，问它在这一片里出现过吗。

        和 `key_term_hit` 的区别只是词从哪来：那个自己从查询里挑，
        这个由调用方给。闸门拿同义组逐个试时用它——
        挑词的逻辑没必要跑第二遍。
        """
        if not term:
            return False
        return term in self._chunk_tokens.get(chunk_id, set())

    def get(self, chunk_id):
        return self.chunks.get(chunk_id)

    def titles(self, chunk_ids):
        return [self.chunks[cid]["title"] for cid in chunk_ids
                if cid in self.chunks]
