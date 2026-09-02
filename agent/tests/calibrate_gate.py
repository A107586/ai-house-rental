# -*- coding: utf-8 -*-
"""可答性闸门的阈值标定脚本。

    python -m tests.calibrate_gate

**只在 `fixtures/support_tune.py` 的调参集上跑，绝不碰验收集。**
这不是形式主义：在验收集上选阈值，选出来的比率必然偏高，而且偏高多少
无从估计——那份数字就没有任何意义了。

目标函数照抄需求写的两条硬指标，并且**先满足拒答率**：

    在「无依据拒答率 ≥ 90%」的前提下，最大化「有依据回答率」

顺序不能反。需求把拒答率的下限写成 90%、回答率写成 80%，
说明这个系统宁可少答，也不能乱答——客服说错一条规定的代价，
比说一句「我不确定」大得多。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agents.support import corpus  # noqa: E402
from app.retrieval.index import KnowledgeIndex  # noqa: E402
from tests.fixtures import support_tune as tune  # noqa: E402

# 需求写死的两条指标
REQUIRED_REFUSE = 0.90
REQUIRED_ANSWER = 0.80


def signals(index, question):
    """算出一条问题的闸门判据。与 `gate.evaluate` 用的是同一套量。"""
    ranked, detail = index.search(
        question, expanded=corpus.expand_query(question), top_k=8)
    reranked = index.rerank(question, ranked, detail, top_k=3)
    if not reranked:
        return None, 0.0, 1.0
    top = reranked[0][0]
    return (top, index.coverage(question, top),
            index.key_term_hit(question, top))


def decide(cover, key_hit, cover_strong, cover_min):
    """与 `gate.evaluate` 的规则部分保持一致，改一处两处都要改。

    覆盖率很高 → 直接答；中间带 → 还得问的那个词真的出现在材料里。
    """
    if cover < cover_min:
        return False
    if cover >= cover_strong:
        return True
    return bool(key_hit)


def main():
    index = KnowledgeIndex(corpus.load_faq())
    print("语料 %d 片；调参集 正例 %d / 负例 %d"
          % (len(index), len(tune.POSITIVES), len(tune.NEGATIVES)))

    positives = []
    for question, gold in tune.POSITIVES:
        top, cover, unseen = signals(index, question)
        # 检索本身就没召回的样本，调阈值也救不回来，单独记出来
        positives.append((question, gold, top, cover, unseen))
    negatives = [(q,) + signals(index, q)[1:] for q, _ in tune.NEGATIVES]

    recalled = [p for p in positives if p[2] == p[1]]
    print("检索 top-1 命中 %d/%d（阈值救不了没召回的样本）"
          % (len(recalled), len(positives)))

    best = None
    for cover_strong in [x / 100.0 for x in range(30, 105, 5)]:
        for cover_min in [x / 100.0 for x in range(0, 50, 5)]:
            if cover_min >= cover_strong:
                continue
            answered = sum(
                1 for _, gold, top, c, k in positives
                if top == gold and decide(c, k, cover_strong, cover_min))
            refused = sum(
                1 for _, c, k in negatives
                if not decide(c, k, cover_strong, cover_min))
            answer_rate = answered / len(positives)
            refuse_rate = refused / len(negatives)
            # 先过拒答率的门槛，再比回答率
            key = (refuse_rate >= REQUIRED_REFUSE, answer_rate, refuse_rate)
            if best is None or key > best[0]:
                best = (key, cover_strong, cover_min, answer_rate, refuse_rate)

    _, cover_strong, cover_min, answer_rate, refuse_rate = best
    print()
    print("标定结果：")
    print("  COVER_STRONG = %.2f" % cover_strong)
    print("  COVER_MIN    = %.2f" % cover_min)
    print("  中间带附加条件：问的那个词必须出现在材料里（key_term_hit）")
    print("调参集上：回答率 %.1f%%（要求 ≥ %.0f%%）· 拒答率 %.1f%%（要求 ≥ %.0f%%）"
          % (answer_rate * 100, REQUIRED_ANSWER * 100,
             refuse_rate * 100, REQUIRED_REFUSE * 100))
    print()
    print("注意：以上是**调参集**上的数字，天然偏乐观。")
    print("真实指标以 tests/test_acceptance.py 里的验收集为准。")

    print()
    print("被拒的正例（调参集）：")
    for question, gold, top, cover, key_hit in positives:
        if top == gold and not decide(cover, key_hit, cover_strong, cover_min):
            print("  %-22s cover=%.3f 键中=%d" % (question, cover, key_hit))
    print("被放行的负例（调参集）：")
    for question, cover, key_hit in negatives:
        if decide(cover, key_hit, cover_strong, cover_min):
            print("  %-22s cover=%.3f 键中=%d" % (question, cover, key_hit))


if __name__ == "__main__":
    main()
