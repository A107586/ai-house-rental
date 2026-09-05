"""需求摘要交接智能体（HandoffAgent）：完成 J-09-a。

贝壳的原文是「根据您向贝壳布丁发起的咨询向经纪人发送您的**个人需求
摘要**」。我们的链路更短，因为前半段 K-06 已经有了：

    租客的一句话
       ├─ property_finder.understand()  → 硬条件（能拿去查库的）
       ├─ soft.extract()                → 软需求（条件表达不了的）
       └─ 浏览足迹                       → 他实际在看什么
              ↓
         一段摘要，随咨询一并发给中介

**为什么这条最该做**：它把 K-06 的产出接到了一个新场景上，证明
「智能体解析出来的结构化条件」是一份有复用价值的资产，
而不是喂给一次查询就丢掉的中间产物。这一点比功能本身更值得讲。

**价值在哪**：中介不必让租客把需求再复述一遍。让用户重复说一遍刚才
已经说过的话，是所有客服系统最招人烦的地方。

三条纪律：

1. **硬条件那一行完全由规则拼，不过大模型。** 中介会照着它挑房源，
   数字必须原样搬运。大模型只允许润色最后那句自然语言概述。
2. **不许编。** 摘要里出现的每一条都要能指回原话里的某个词
   （`matched` 记着是哪个词）。编出「有小孩」而实际没有，
   中介会照着推学区房，租客一头雾水。
3. **大模型不可用时照常出摘要**（N-07），只是概述那句话由规则拼。
"""

from app import fusion
from app.agents.handoff.tools.soft import SOFT_KEYWORDS, extract, render_hard
from app.agents.property_finder import agent as property_finder
from app.config import settings

_SYSTEM = "你是房屋租赁平台的需求摘要助手，只输出 JSON，不要任何解释。"

_SOFT_LABELS = [label for label, _ in SOFT_KEYWORDS]

_PROMPT = """把租客这句话里**筛选条件表达不了的那部分需求**整理出来。

租客原话：「{text}」

已经解析出的硬条件（不用你管，这部分由系统负责）：{hard}

标签只能从下面这张表里选，**不许自己造词**：
{labels}

**只写原话里真有依据的。** 宁可少写，也不要推测——
这段摘要会原样摆在经纪人面前，编出一条不存在的需求，
他会照着它推错房源。

系统的规则引擎已经先抽了一遍，结果如下，**供你参考**：

{reference}

只输出如下 JSON：
{{"soft": ["<标签>"], "note": "<一句话概述这位租客在找什么，写给经纪人看>"}}"""


def _pick(values, allowed):
    out = []
    for item in values or []:
        text = str(item).strip()
        if text in allowed and text not in out:
            out.append(text)
    return out


def _compose_note(hard_line, soft, viewed):
    """规则版的概述。**大模型不可用时这就是最终输出**，得自己能读通。

    刻意**不重复硬条件**——摘要里硬条件已经单独占一行了，概述再抄一遍，
    中介读到的是同一句话说两遍。这里只补那一行装不下的东西。
    """
    parts = []
    if soft:
        parts.append("看重" + "、".join(soft))
    if viewed:
        parts.append("已浏览过 %d 套同类房源" % len(viewed))
    if not parts:
        return ("需求已列在上方。" if hard_line
                else "租客尚未说明具体需求。")
    return "；".join(parts) + "。"


def run(text, viewed=None, provider=None):
    """整理一份需求摘要。

    :param viewed: 浏览足迹（``[{house_no, title, district}]``），可空。
                   它回答的是「他嘴上说的和实际在看的一致吗」——
                   说要两室却一直在看一室，中介该问的是这个。
    """
    viewed = viewed or []
    if not text or not text.strip():
        return {"intent": "handoff", "status": "need_content", "ok": False,
                "message": "先说说你的找房需求，我整理好一并发给经纪人。"}

    filters, features, parse_source, _div = property_finder.understand(
        text, provider=provider)
    hard_line = render_hard(filters, features)
    rule_soft = extract(text)

    rule_result = {"soft": rule_soft["soft"],
                   "note": _compose_note(hard_line, rule_soft["soft"], viewed)}

    def sanitize(raw):
        note = str(raw.get("note") or "").strip()
        if not note:
            return None
        return {"soft": _pick(raw.get("soft"), _SOFT_LABELS),
                "note": note[:200]}

    reference = fusion.render_reference({"soft": rule_soft["soft"]},
                                        {"soft": "软需求"})

    final, source, divergence = fusion.fuse(
        rule_result, provider, _SYSTEM,
        _PROMPT.format(text=text, hard=hard_line or "（没解析出硬条件）",
                       labels="、".join(_SOFT_LABELS), reference=reference),
        sanitize, enabled=settings.fusion_enabled)

    payload = {
        "intent": "handoff",
        "status": "ok",
        "ok": True,
        "source": source,
        "source_label": fusion.describe_source(source),
        # 条件解析走的是 K-06 那条链路，来源单独标出来——
        # 摘要这一段和条件那一段可能一个用了大模型、一个没用
        "parse_source": parse_source,
        "summary": {
            # 硬条件这一行**没经过大模型**，见模块开头第 1 条
            "hard": hard_line,
            "filters": filters,
            "features": features,
            "soft": final.get("soft", []),
            "note": final.get("note", ""),
            "viewed": [
                {"house_no": v.get("house_no", ""),
                 "title": v.get("title", "")} for v in viewed[:5]],
        },
        "matched": rule_soft["matched"],
        "divergence": divergence,
        "message": "已整理成需求摘要，发起咨询时会一并带给经纪人。",
    }
    # 渲染好的纯文本一并返回。**渲染只写一处**——前端再实现一遍的话，
    # 两边迟早会不一样，而这段文字是要原样存进会话的
    payload["summary_text"] = render_text(payload["summary"])
    return payload


def render_text(summary):
    """把摘要渲染成一段纯文本，直接当会话的第一条消息发出去。

    **做成一条消息，而不是一个挂在会话上的字段。**
    挂字段就要改表、要在每一处会话展示里加渲染分支；做成消息则天然
    带时间、天然进历史、天然在中介的手机上也看得到——
    会话本来就是「按时间排列的一串内容」，摘要没有理由是个例外。
    """
    lines = ["【需求摘要 · 由智能体整理，供参考】"]
    if summary.get("hard"):
        lines.append("硬条件：" + summary["hard"])
    if summary.get("soft"):
        lines.append("其他：" + "、".join(summary["soft"]))
    if summary.get("viewed"):
        lines.append("已浏览：" + "、".join(
            v.get("house_no", "") for v in summary["viewed"] if v.get("house_no")))
    if summary.get("note"):
        lines.append(summary["note"])
    return "\n".join(lines)
