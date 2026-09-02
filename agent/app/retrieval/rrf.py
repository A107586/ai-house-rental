"""多路检索结果的排名融合（Reciprocal Rank Fusion）。

    RRF(d) = Σ_路 w_路 / (k + rank_路(d))

**为什么用 RRF 而不是分数加权**：本项目的检索通道有正文 BM25、标题
BM25、同义扩展 BM25，将来可能再加向量余弦。BM25 分数没有上界、
随语料规模漂移，余弦相似度固定在 [-1,1]——直接加权必须先做分数归一化，
而归一化本身又要标定参数。RRF 只用**排名**，天然免疫量纲问题，
是多路召回场景下最省事且不容易出错的做法。

`k=60` 是文献里的常用取值，含义是「排名靠前的差距别拉得太夸张」：
第 1 名与第 2 名的权重比是 61/62 ≈ 0.98，而不是 1/2。
对小语料尤其合适——几十条文档里第 3 名和第 1 名的真实差距往往很小。

注意与 `app/fusion.py` 区分：那个是「规则结论与大模型结论」的融合，
服务于 AI-01/AI-04 的结构化解析；这里是检索排名的融合，两者无关。
"""

DEFAULT_K = 60


def fuse(ranked_lists, k=DEFAULT_K, weights=None, top_k=None):
    """融合多路排序结果。

    :param ranked_lists: ``[[(doc_id, score), ...], ...]``，每一路已按相关性降序。
                         只用其中的顺序，`score` 会被忽略。
    :param weights:      每一路的权重，默认全为 1.0。长度不足的按 1.0 补齐。
    :returns: ``[(doc_id, rrf_score), ...]``，降序。
    """
    if not ranked_lists:
        return []

    weights = list(weights or [])
    weights += [1.0] * (len(ranked_lists) - len(weights))

    totals = {}
    # 记录每个文档在各路里的最好名次，供调试与答辩展示「它是被哪一路捞出来的」
    best_rank = {}

    for channel, ranked in enumerate(ranked_lists):
        weight = weights[channel]
        if not weight:
            continue
        for rank, item in enumerate(ranked, start=1):
            doc_id = item[0] if isinstance(item, (tuple, list)) else item
            totals[doc_id] = totals.get(doc_id, 0.0) + weight / (k + rank)
            if doc_id not in best_rank or rank < best_rank[doc_id][1]:
                best_rank[doc_id] = (channel, rank)

    # 同分按 doc_id 排序，保证结果可复现
    ranked = sorted(totals.items(), key=lambda kv: (-kv[1], str(kv[0])))
    return ranked[:top_k] if top_k else ranked


def explain(ranked_lists, doc_id, k=DEFAULT_K):
    """给出某个文档在各路中的名次，用于「为什么是这条」的解释。

    答辩时被问「凭什么排第一」，能当场答出「正文第 3、标题第 1、
    同义扩展第 2」，比只给一个融合分数有说服力。
    """
    out = []
    for channel, ranked in enumerate(ranked_lists):
        for rank, item in enumerate(ranked, start=1):
            current = item[0] if isinstance(item, (tuple, list)) else item
            if current == doc_id:
                out.append({"channel": channel, "rank": rank,
                            "contribution": round(1.0 / (k + rank), 6)})
                break
        else:
            out.append({"channel": channel, "rank": None, "contribution": 0.0})
    return out
