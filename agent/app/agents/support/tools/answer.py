"""受约束的回答生成与校验。

两条产出路径：

- **抽取式**（`extract`）：直接把最相关那一片的正文首句返回。
  没有大模型时走这条，是项目全部自动化测试跑的模式。
  客服问答的答案本来就写在 FAQ 里，返回原文并附引用是**可用回答**，
  不是坏掉的降级——这也是 AI-03 能在离线环境下成立的根据。
- **生成式**（`generate`）：把候选材料编号后交给大模型组织语言。
  更自然，但**必须过校验**才允许输出。

校验有三道，任何一道不过就整条丢弃、退回抽取式：

1. **引用合法性**——模型会凭空编造 id，编出来的引用点开就是 404
2. **数字可追溯**——回答里的金额、天数、比例必须在被引用的原文里出现过。
   这条与 AI-05 经营报告「不允许模型编造数据」是同一条规则：
   客服说错「押金 15 个工作日退」比说「我不知道」严重得多
3. **无依据措辞**——「根据我的了解」「一般来说」这类话意味着模型在用
   自己的先验答题，而不是在用给它的材料
"""

import re

from app.llm.base import LLMUnavailable

# 回答长度上限。客服场景里长回答不等于好回答，
# 而且过长往往意味着模型开始自由发挥了。
MAX_ANSWER_CHARS = 300

# 无依据措辞黑名单
_UNGROUNDED = [
    "根据我的", "据我所知", "我认为", "一般来说", "通常情况下",
    "众所周知", "作为AI", "作为人工智能", "我猜", "可能是因为",
]

# **声称动作已完成的措辞，一律拒绝。**
#
# 这是实测撞到的最严重的一次幻觉：用户说「这周六下午两点」，
# 检索层生成了一句「好的，已为您预约这周六下午两点的看房」——
# 听起来办妥了，实际上一条预约记录都没有。
#
# 检索与生成这一层**永远不执行任何操作**，它没有资格说「已经」。
# 真正的执行走 /agent/support/confirm，那条路径的回执由代码拼，
# 不经过模型。所以这里看到「已为您」就整条丢弃，宁可退回抽取式。
_CLAIMS_DONE = [
    "已为您", "已为你", "已帮您", "已帮你", "已经为您", "已经为你",
    "已提交", "已预约", "已安排", "已办理", "已完成", "已受理",
    "已经提交", "已经预约", "帮您预约了", "帮你预约了", "已下单",
]

# 数字：金额、天数、比例、月数都在内。匹配整数与小数。
_NUMBER = re.compile(r"\d+(?:\.\d+)?")

# 句末标点，用于抽取首句
_SENTENCE_END = re.compile(r"[。！？!?\n]")

_ANSWER_SYSTEM = ("你是房屋租赁平台的客服。**只能依据给定资料回答**，"
                  "资料没写的一律不要说。输出 JSON，不要解释。")

_ANSWER_PROMPT = """用户问题：「{question}」

可用资料（只能用这些）：
{materials}

要求：
1. 用两三句话直接回答，口语一点，别照抄原文的书面语
2. **不许出现资料里没有的数字**（金额、天数、比例、月数）
3. citations 填你实际用到的资料编号，必须是上面出现过的 id
4. 资料不足以回答就把 refuse 置为 true

只输出：
{{"answer": "回答正文", "citations": ["id1"], "refuse": false}}"""


def first_sentence(text):
    """取正文首句。FAQ 的写作约定要求首句就是一句话结论。"""
    text = (text or "").strip()
    if not text:
        return ""
    # 去掉 Markdown 的强调标记，抽取式回答直接面向用户，不该带 **
    text = text.replace("**", "")
    matched = _SENTENCE_END.search(text)
    if not matched:
        return text[:MAX_ANSWER_CHARS]
    return text[:matched.end()].strip()


def extract(index, candidates):
    """抽取式回答：最相关那一片的首句 + 该片作为引用。

    只取首句而不是整段，是因为 FAQ 正文常有「详细说明」的第二三句，
    对话框里贴一大段读起来很差。用户想看全文可以点引用卡片。
    """
    if not candidates:
        return "", []
    chunk = index.get(candidates[0])
    if not chunk:
        return "", []
    body = first_sentence(chunk.get("body", ""))
    return body, [chunk["id"]]


def render_materials(index, candidates, limit=3):
    """把候选材料渲染成提示词里的资料段落，**带 id**。

    id 必须写进去：不给 id，模型就没法引用；给了 id，
    引用校验才有得可校。
    """
    blocks = []
    for chunk_id in candidates[:limit]:
        chunk = index.get(chunk_id)
        if not chunk:
            continue
        blocks.append("[%s] 【%s】\n%s"
                      % (chunk["id"], chunk["title"], chunk["body"]))
    return "\n\n".join(blocks)


def numbers_in(text):
    return set(_NUMBER.findall(text or ""))


def validate(answer, citations, index, candidates):
    """三道校验。返回 ``(ok, reason)``。"""
    answer = (answer or "").strip()
    if not answer:
        return False, "回答为空"
    if len(answer) > MAX_ANSWER_CHARS * 2:
        return False, "回答过长（%d 字）" % len(answer)

    allowed = set(candidates)
    if not citations:
        return False, "没有给出引用"
    unknown = [c for c in citations if c not in allowed]
    if unknown:
        # 模型编 id 的频率比想象中高，而编出来的引用在界面上点开就是空的
        return False, "引用了不存在的资料：%s" % "、".join(unknown)

    for phrase in _UNGROUNDED:
        if phrase in answer:
            return False, "出现无依据措辞「%s」" % phrase

    for phrase in _CLAIMS_DONE:
        if phrase in answer:
            return (False,
                    "回答声称操作已完成（「%s」），但检索层从不执行任何操作"
                    % phrase)

    # 数字必须在被引用的材料里出现过
    cited_text = " ".join(
        (index.get(c) or {}).get("body", "") + (index.get(c) or {}).get("title", "")
        for c in citations)
    source_numbers = numbers_in(cited_text)
    for number in numbers_in(answer):
        if number in source_numbers:
            continue
        # 「7 个工作日」写成「七个工作日」不该被判违规，
        # 但反过来模型把 7 说成 15 必须拦下。这里只查阿拉伯数字，
        # 中文数字由 FAQ 的写作约定保证（正文一律用阿拉伯数字）。
        return False, "回答里的数字「%s」在引用材料中找不到" % number

    return True, ""


def generate(provider, index, question, candidates):
    """生成式回答。返回 ``(answer, citations, source, note)``。

    `source` 取值：``llm`` 采用了模型输出 / ``extractive`` 退回抽取式。
    `note` 说明为什么退回，会写进检索日志，答辩时能拿出来讲。
    """
    fallback_answer, fallback_citations = extract(index, candidates)

    if provider is None or not provider.available:
        return fallback_answer, fallback_citations, "extractive", "未配置大模型"

    materials = render_materials(index, candidates)
    try:
        data = provider.complete_json(
            _ANSWER_SYSTEM,
            _ANSWER_PROMPT.format(question=question, materials=materials))
    except LLMUnavailable:
        return fallback_answer, fallback_citations, "extractive", "大模型不可用"
    except Exception:
        return fallback_answer, fallback_citations, "extractive", "大模型调用异常"

    if not isinstance(data, dict):
        return fallback_answer, fallback_citations, "extractive", "输出不是 JSON 对象"

    if data.get("refuse") is True:
        # 模型自己说答不了。闸门已经放行到这一步，但模型看过全文后仍然
        # 认为不够——采信更保守的一方。
        return "", [], "refused", "大模型在生成阶段判定资料不足"

    answer = str(data.get("answer", "")).strip()
    citations = data.get("citations") or []
    if not isinstance(citations, list):
        citations = []
    citations = [str(c).strip() for c in citations if str(c).strip()]

    ok, reason = validate(answer, citations, index, candidates)
    if not ok:
        return fallback_answer, fallback_citations, "extractive", reason

    return answer, citations, "llm", ""
