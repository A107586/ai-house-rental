"""合同智能审阅（ContractAgent）：完成 AI-02。

流程：抽取关键条款 → 识别不利条款 → 给出整体结论。

平台自己生成的合同是模板化的，正则抽取覆盖率很高，
不需要过大模型。只有抽取覆盖率低（多半是用户上传的第三方合同）
才交给大模型补齐。
"""

from app.agents.contract.tools.extract import CLAUSE_KEYS, extract_clauses
from app.agents.contract.tools.risk import check_risks, summarize
from app.config import settings
from app.llm.base import LLMUnavailable

_SYSTEM = "你是房屋租赁合同审阅助手，只输出 JSON，不要任何解释。"

_PROMPT = """从下面这份租赁合同中抽取关键条款。

需要抽取的字段（找不到就省略，不要臆造）：
- 租期：起止日期或月数
- 租金：月租金数字，单位元
- 押金：押金数字，单位元
- 付款方式：如「押一付三」「每 3 个月一付」
- 违约责任：相关条款原文摘要
- 维修责任：相关条款原文摘要

合同正文：
「{text}」

只输出如下 JSON：
{{"租期": "...", "租金": 3000, "押金": 3000, "付款方式": "...",
  "违约责任": "...", "维修责任": "..."}}"""

_INT_KEYS = ("租金", "押金")


def _sanitize(data):
    """只保留白名单字段并校验类型。大模型会臆造字段名与格式。"""
    if not isinstance(data, dict):
        return {}
    clean = {}
    for key in CLAUSE_KEYS:
        value = data.get(key)
        if value in (None, ""):
            continue
        if key in _INT_KEYS:
            try:
                number = int(float(value))
            except (TypeError, ValueError):
                continue
            if number > 0:
                clean[key] = number
        else:
            text = str(value).strip()
            if text:
                clean[key] = text[:200]
    return clean


def understand(text, provider=None):
    """抽取条款，返回 ``(extracted, source)``。

    **规则和大模型同时参与。** 原先规则抽到一半以上条款就不问大模型了——
    平台模板合同永远走不到大模型，界面上一直写着「按常见条款自动检查」。
    现在只要配了大模型就一起抽，冲突时规则的值优先（数字它抽得更准）。
    大模型不可用时：规则本来就有把握，标 rules；没把握，标 rules_fallback。
    """
    rule_result = extract_clauses(text)
    confident = rule_result["coverage"] >= settings.rule_confidence_threshold
    fallback = "rules" if confident else "rules_fallback"

    if provider is None or not provider.available:
        return rule_result, fallback

    try:
        # 合同可能很长，截断以控制 Token 消耗（非功能需求 N-08）
        data = provider.complete_json(_SYSTEM, _PROMPT.format(text=text[:3000]))
    except LLMUnavailable:
        return rule_result, fallback

    clauses = _sanitize(data)
    if not clauses:
        return rule_result, fallback

    # 大模型抽到的与规则抽到的合并，规则结果优先——它更可靠
    merged = dict(clauses)
    merged.update(rule_result["clauses"])
    return {
        "clauses": merged,
        "missing": [k for k in CLAUSE_KEYS if k not in merged],
        "coverage": round(len(merged) / len(CLAUSE_KEYS), 2),
    }, "llm"


_RISK_PROMPT = """下面是一份房屋租赁合同。规则已经查出这些问题：{known}。
请再看一遍，找出**规则没查出来**、对租客明显不利的条款，最多 3 条；没有就返回空数组。
每条必须摘抄合同里的原文（逐字，不要改写），找不到原文就不要写。
只输出 JSON：{{"risks": [{{"title": "一句话说是什么问题", "quote": "合同原文",
"why": "为什么对租客不利", "advice": "签之前该怎么谈"}}]}}

合同正文：
「{text}」"""


def _squash(text):
    return "".join(str(text or "").split())


def llm_risks(text, known, provider):
    """让大模型补查规则没覆盖到的风险条款。

    规则只认得写死的那几种坑（押金不退、违约金过高……），换个说法就漏。
    大模型给的每一条都要**摘抄原文**，原文在合同里找不到就丢掉——
    它说「第 7 条有问题」而合同里根本没有第 7 条，比不说更糟。
    """
    if provider is None or not provider.available:
        return []
    try:
        data = provider.complete_json(_SYSTEM, _RISK_PROMPT.format(
            known="、".join(r["title"] for r in known) or "（无）", text=text[:3000]))
    except LLMUnavailable:
        return []
    rows = data.get("risks") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return []
    body = _squash(text)
    seen = {_squash(r.get("title")) for r in known}
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title, quote = str(row.get("title") or "").strip(), str(row.get("quote") or "").strip()
        if not title or len(_squash(quote)) < 4 or _squash(quote) not in body:
            continue
        if _squash(title) in seen:
            continue
        seen.add(_squash(title))
        out.append({"id": "llm_%d" % (len(out) + 1), "level": "medium", "source": "llm",
                    "title": title[:40], "quote": quote[:120],
                    "why": str(row.get("why") or "").strip()[:200] or "大模型认为这一条对租客不利。",
                    "advice": str(row.get("advice") or "").strip()[:200] or "签之前和房东确认清楚。"})
        if len(out) >= 3:
            break
    return out


def run(text, provider=None):
    """合同审阅入口。"""
    if not text or not text.strip():
        return {
            "intent": "contract", "status": "need_content", "ok": False,
            "message": "没有拿到合同正文，无法审阅。",
        }

    extracted, source = understand(text, provider=provider)
    result = check_risks(text, extracted["clauses"])
    # 规则查完再让大模型补查。分数仍按规则算（可复现）；补出来的条目单独标「AI 补充」
    extra = llm_risks(text, result["risks"], provider)
    if extra:
        result["risks"] = result["risks"] + extra
        result["levels"]["medium"] = result["levels"].get("medium", 0) + len(extra)
        source = "llm"

    return {
        "intent": "contract",
        "status": "reviewed",
        "ok": True,
        "source": source,
        "clauses": extracted["clauses"],
        "missing": extracted["missing"],
        "coverage": extracted["coverage"],
        "risks": result["risks"],
        # 对租客有保障的条款也要给出来。**只报风险的审阅是偏的**——
        # 用户看不到「这份合同哪里写得清楚」，就没法判断它到底比别的好在哪
        "favourable": result.get("favourable", []),
        "levels": result["levels"],
        "score": result["score"],
        "message": summarize(result, extracted),
    }
