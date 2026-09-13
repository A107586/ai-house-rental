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

## 这个脚本直接调 `gate.evaluate`，不再自己复述一遍规则

原先这里有个 `decide()` 把闸门的规则抄了一遍，注释写着「改一处两处都要改」。
**那句话没兜住。** 闸门后来加了同义词救场、又加了 G0 话题词门，
这个脚本一处都不知道，于是它算出来的最优阈值是 0.32，而代码里跑的是
0.42——**标定工具和被标定的东西已经不是一回事了**，
照它的建议改反而会把闸门调坏。

现在改成临时改写 `gate` 的两个模块常量、直接调 `gate.evaluate`。
闸门以后再加几道门，这里自动跟上。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agents.support import corpus  # noqa: E402
from app.agents.support.tools import gate  # noqa: E402
from app.retrieval.index import KnowledgeIndex  # noqa: E402
from tests.fixtures import support_tune as tune  # noqa: E402

# 需求写死的两条指标
REQUIRED_REFUSE = 0.90
REQUIRED_ANSWER = 0.80


def retrieve(index, question):
    """检索这一问，返回 (精排后的片 id 列表, top1, 覆盖率, 关键词命中)。

    **检索与阈值无关**，所以每问只跑一次，网格上反复用。
    """
    ranked, detail = index.search(
        question, expanded=corpus.expand_query(question), top_k=8)
    reranked = index.rerank(question, ranked, detail, top_k=3,
                            expanded=corpus.expand_query(question))
    if not reranked:
        return [], None, 0.0, False
    candidates = [chunk_id for chunk_id, _ in reranked]
    top = candidates[0]
    return (candidates, top, index.coverage(question, top),
            index.key_term_hit(question, top))


def signals(index, question):
    """老接口：只给 (top, 覆盖率, 关键词命中)。别处的用例还在用。"""
    _, top, cover, key_hit = retrieve(index, question)
    return top, cover, key_hit


def answers(index, question, candidates, cover_strong, cover_min):
    """在给定阈值下，闸门放不放行这一问。

    临时改写模块常量再调真闸门——**不复述规则**，理由见模块文档。
    `provider=None` 保证不联网：标定必须可复现。
    """
    saved = (gate.COVER_STRONG, gate.COVER_MIN)
    gate.COVER_STRONG, gate.COVER_MIN = cover_strong, cover_min
    try:
        return bool(gate.evaluate(index, question, candidates,
                                  provider=None)["ok"])
    finally:
        gate.COVER_STRONG, gate.COVER_MIN = saved


def main():
    index = KnowledgeIndex(corpus.load_faq())
    print("语料 %d 片；调参集 正例 %d / 负例 %d"
          % (len(index), len(tune.POSITIVES), len(tune.NEGATIVES)))

    positives = [(q, gold) + retrieve(index, q) for q, gold in tune.POSITIVES]
    negatives = [(q, None) + retrieve(index, q) for q, _ in tune.NEGATIVES]

    recalled = [p for p in positives if p[3] == p[1]]
    print("检索 top-1 命中 %d/%d（阈值救不了没召回的样本）"
          % (len(recalled), len(positives)))

    # 网格步长 0.01 而不是 0.05。粗网格出过事：语料扩容后，
    # 0.05 的步长正好跨过 0.41~0.43 这段更优的平台，
    # 选中的 0.45 让一条覆盖率 0.4497 的正例被卡在门外。
    best = None
    feasible = []
    for cover_strong in [x / 100.0 for x in range(30, 101)]:
        for cover_min in [x / 100.0 for x in range(0, 50, 5)]:
            if cover_min >= cover_strong:
                continue
            answered = sum(
                1 for q, gold, cands, top, _, _ in positives
                if top == gold and answers(index, q, cands, cover_strong, cover_min))
            refused = sum(
                1 for q, _, cands, _, _, _ in negatives
                if not answers(index, q, cands, cover_strong, cover_min))
            answer_rate = answered / len(positives)
            refuse_rate = refused / len(negatives)
            # 先过拒答率的门槛，再比回答率
            key = (refuse_rate >= REQUIRED_REFUSE, answer_rate, refuse_rate)
            if refuse_rate >= REQUIRED_REFUSE and answer_rate >= REQUIRED_ANSWER:
                feasible.append((cover_strong, cover_min, answer_rate, refuse_rate))
            if best is None or key > best[0]:
                best = (key, cover_strong, cover_min, answer_rate, refuse_rate)

    _, cover_strong, cover_min, answer_rate, refuse_rate = best

    # 在两项都达标的点里取覆盖率阈值的**中位数**，而不是让上面的
    # 最大化直接选出一个边缘值。边缘阈值经不起语料增长——
    # 语料一扩，IDF 一动，紧贴边界的正例就会掉出去。
    if feasible:
        best_answer = max(f[2] for f in feasible)
        plateau = sorted(f[0] for f in feasible if f[2] >= best_answer - 1e-9)
        if plateau:
            middle = plateau[len(plateau) // 2]
            for candidate in feasible:
                if candidate[0] == middle:
                    cover_strong, cover_min, answer_rate, refuse_rate = candidate
                    break
        print("可行区间：COVER_STRONG ∈ [%.2f, %.2f]，取中点 %.2f"
              % (plateau[0], plateau[-1], cover_strong))
    print()
    print("标定结果：")
    print("  COVER_STRONG = %.2f   （代码里当前是 %.2f）"
          % (cover_strong, gate.COVER_STRONG))
    print("  COVER_MIN    = %.2f   （代码里当前是 %.2f）"
          % (cover_min, gate.COVER_MIN))
    print("  另有两道与阈值无关的门：G0 话题词、同义词救场（都在 gate.evaluate 里）")
    print("调参集上：回答率 %.1f%%（要求 ≥ %.0f%%）· 拒答率 %.1f%%（要求 ≥ %.0f%%）"
          % (answer_rate * 100, REQUIRED_ANSWER * 100,
             refuse_rate * 100, REQUIRED_REFUSE * 100))
    print()
    print("注意：以上是**调参集**上的数字，天然偏乐观。")
    print("真实指标以 tests/test_acceptance.py 里的验收集为准。")

    print()
    print("被拒的正例（调参集）：")
    for question, gold, cands, top, cover, key_hit in positives:
        if top == gold and not answers(index, question, cands,
                                       cover_strong, cover_min):
            print("  %-22s cover=%.3f 键中=%d 话题词=%s"
                  % (question, cover, key_hit, index.topic_term(question) or "无"))
    print("被放行的负例（调参集）：")
    for question, _, cands, top, cover, key_hit in negatives:
        if answers(index, question, cands, cover_strong, cover_min):
            print("  %-22s cover=%.3f 键中=%d" % (question, cover, key_hit))

    # ---- 诊断：没有话题的短句 ----
    #
    # **不参与目标函数。** 它们是送分题，混进负例池会把拒答率灌高，
    # 标定就会以为「阈值还能再松」——那正是当初把「怎么办」
    # 答成改密码 FAQ 的原因（见 support_tune.NO_TOPIC 的说明）。
    leaked = []
    for question in tune.NO_TOPIC:
        cands, _, cover, _ = retrieve(index, question)
        if cands and answers(index, question, cands, cover_strong, cover_min):
            leaked.append((question, cover))
    print()
    print("没有话题的短句（诊断用，不参与标定）：%d 条里漏 %d 条"
          % (len(tune.NO_TOPIC), len(leaked)))
    for question, cover in leaked:
        print("  %-22s cover=%.3f 话题词=%s"
              % (question, cover, index.topic_term(question)))


if __name__ == "__main__":
    main()
