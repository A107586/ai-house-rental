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
    """抽取条款，返回 ``(extracted, source)``。"""
    rule_result = extract_clauses(text)
    if rule_result["coverage"] >= settings.rule_confidence_threshold:
        return rule_result, "rules"

    if provider is None or not provider.available:
        return rule_result, "rules_fallback"

    try:
        # 合同可能很长，截断以控制 Token 消耗（非功能需求 N-08）
        data = provider.complete_json(_SYSTEM, _PROMPT.format(text=text[:3000]))
    except LLMUnavailable:
        return rule_result, "rules_fallback"

    clauses = _sanitize(data)
    if not clauses:
        return rule_result, "rules_fallback"

    # 大模型抽到的与规则抽到的合并，规则结果优先——它更可靠
    merged = dict(clauses)
    merged.update(rule_result["clauses"])
    return {
        "clauses": merged,
        "missing": [k for k in CLAUSE_KEYS if k not in merged],
        "coverage": round(len(merged) / len(CLAUSE_KEYS), 2),
    }, "llm"


def run(text, provider=None):
    """合同审阅入口。"""
    if not text or not text.strip():
        return {
            "intent": "contract", "status": "need_content", "ok": False,
            "message": "没有拿到合同正文，无法审阅。",
        }

    extracted, source = understand(text, provider=provider)
    result = check_risks(text, extracted["clauses"])

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
