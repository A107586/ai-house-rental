"""可答性闸门（Answerability Gate）。

需求 AI-03 的原话是「知识库中无依据时必须回答『暂无相关信息』，**不得编造**」，
并且给了可量化的指标：无依据问题的拒答率 ≥ 90%。

**这一条靠提示词是保不住的。** 提示词里写「没有依据就说不知道」，
模型照做与否是概率问题；而这里要的是确定性。所以拒答做成代码里的硬闸门，
大模型只能让它更严，不能让它更松。

三条判据：

- **G1 覆盖率**：查询的 IDF 质量有多少落在选中的那一片上（`index.coverage`）
- **G2 关键词命中**：查询里区分度最高的那个词，在选中的材料里出现了吗
  （`index.key_term_hit`）。覆盖率处在中间带时由它拍板。
- **G3 大模型判据**：只在 G1/G2 判定「可答」但覆盖率不算高时才调用，
  且**只能推翻成拒答，不能把拒答改成可答**

G2 是试错换来的。原先用的是「查询里有多少词在整个语料里没出现过」，
两种算法都不成立：算 bigram 太严（任何新说法都会造出没见过的 bigram，
七条本该回答的问题全被拒，而它们的答案还都排在检索第一位），
只算单字又太松（「天气」的天、「发票」的发在租房语料里都是常见字，
拒答率掉到 64%）。结论是**「措辞新不新」不是「答得了答不了」的正确代理量**，
换成「问的那个东西提没提」才对上。

G2 后来又修过一次，坑在「那个东西」怎么挑：原先在字符 bigram 里挑
IDF 最高的，而没见过的组合 IDF 必然最高，于是系统性地挑中「久说」
「扫干」「约完」这种切在词缝上的碎片——不是词，语料里当然没有，
本该回答的问题被拒。改成先用词典判断是不是词，有依据正确率
80% → 85%，拒答率不变。见 `index._key_term_by_word`。

G3 单向生效是刻意的。需求把拒答率的下限写死在 90%，
如果允许大模型把规则的拒答改成可答，这个下限就变成了概率事件。
反过来只收紧，规则拒的一定拒，指标只会更好。
"""

from app.llm.base import LLMUnavailable

# 阈值在 `tests/fixtures/support_tune.py` 的调参集上标定，
# **不是在验收集上调的**——验收集全程不参与任何阈值选择。
# 标定脚本：`python -m tests.calibrate_gate`
#
# 覆盖率足够高就直接放行。这一档是为「错别字」和「换个说法」留的：
# 「压金能退吗」的「压」全库不存在，但它靠别名「押金能退吗」
# 拿到 0.65 的覆盖率，该答。
#
# **取值 0.42 是可行区间 [0.41, 0.43] 的中点，不是边缘。** 这一点有代价换来：
# 语料从 54 条扩到 70 条之后，IDF 分布变了，原先定在 0.45 的阈值把一条
# 覆盖率 0.4497 的正例卡在了门外——它的正确答案还排在检索第一位。
# 更糟的是标定脚本当时用 0.05 的粗网格，正好跨过了 0.41~0.43 这段
# **更优且更宽**的平台（回答率 86.2% vs 82.8%，拒答率同为 92.9%）。
#
# 两条教训都写进了标定脚本：网格细到 0.01，并且在可行区间里取中点——
# 取边缘的阈值经不起语料增长，取中点才留得住余量。
COVER_STRONG = 0.42
# 覆盖率的绝对下限。标定结果是 0，即不设独立下限——
# 覆盖率低到一定程度时关键词必然也不命中，中间带那一关自会拦下，
# 再叠一道下限只会误伤「那个钱啥时候能回来」这类几乎没有实词的口语问句。
COVER_MIN = 0.0

_JUDGE_SYSTEM = "你是知识库问答的依据审核员，只输出 JSON，不要解释。"

_JUDGE_PROMPT = """判断下面这些资料能不能回答用户的问题。

用户问题：「{question}」

资料：
{materials}

判断标准：
- 资料里**直接写明了**问题的答案 → yes
- 资料只是话题相近、但没有回答这个问题 → no
- 需要你自己补充资料之外的知识才能回答 → no

只输出：{{"answerable": "yes 或 no", "why": "不超过20字"}}"""


def _render(index, candidates, limit=3):
    lines = []
    for order, chunk_id in enumerate(candidates[:limit], start=1):
        chunk = index.get(chunk_id)
        if not chunk:
            continue
        lines.append("%d. 【%s】%s" % (order, chunk["title"], chunk["body"]))
    return "\n\n".join(lines)


def evaluate(index, query, candidates, provider=None):
    """判断这次检索的结果够不够回答。

    :param candidates: 精排后的片 id 列表，第一个是最相关的
    :returns: ``{"ok", "reason", "cover", "unseen", "llm", "checked"}``
    """
    result = {"ok": False, "reason": "", "cover": 0.0, "unseen": 0.0,
              "key_term": "", "key_hit": False, "llm": "", "checked": []}
    if not candidates:
        result["reason"] = "检索没有返回任何候选"
        return result

    top = candidates[0]
    cover = index.coverage(query, top)
    unseen = index.unseen_ratio(query)
    key_term = index.key_term(query)
    key_hit = index.key_term_hit(query, top)
    result.update({"cover": cover, "unseen": unseen, "key_term": key_term,
                   "key_hit": key_hit, "checked": candidates[:3]})

    if cover < COVER_MIN:
        result["reason"] = "覆盖率 %.2f 低于下限 %.2f" % (cover, COVER_MIN)
        return result

    if cover < COVER_STRONG and not key_hit:
        result["reason"] = ("覆盖率 %.2f 未达 %.2f，且问的「%s」"
                            "在这份资料里没有提到"
                            % (cover, COVER_STRONG, key_term))
        return result

    # 规则判定可答。覆盖率很高的直接过，不必再花一次大模型调用。
    if cover >= COVER_STRONG or provider is None or not provider.available:
        result["ok"] = True
        result["reason"] = ("覆盖率 %.2f，关键词「%s」%s"
                            % (cover, key_term, "命中" if key_hit else "未命中"))
        return result

    # G3：只在中间带调用，且只能收紧
    verdict = _ask_llm(provider, query, _render(index, candidates))
    result["llm"] = verdict
    if verdict == "no":
        result["reason"] = "大模型判定资料不足以回答"
        return result

    result["ok"] = True
    result["reason"] = "覆盖率 %.2f，大模型判定可答" % cover
    return result


def _ask_llm(provider, question, materials):
    """返回 ``"yes"`` / ``"no"`` / ``""``（不可用）。

    任何异常都返回空串而不是抛出——大模型不可用时闸门应当退回纯规则结论，
    而不是让整个请求失败（非功能需求 N-07）。
    """
    try:
        data = provider.complete_json(
            _JUDGE_SYSTEM,
            _JUDGE_PROMPT.format(question=question, materials=materials))
    except LLMUnavailable:
        return ""
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""
    answer = str(data.get("answerable", "")).strip().lower()
    return answer if answer in ("yes", "no") else ""
