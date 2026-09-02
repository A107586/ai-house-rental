"""报修智能体（RepairAgent）：完成 AI-04 报修工单智能分派。

流程：分析描述 → 得出故障类型、紧急程度与建议维修工种 → 提交工单。

与找房、预约保持一致的返回结构（含 `status` 字段），
前端只需处理一种形状。
"""

from app.agents.repair.tools.classify import (URGENCY_LEVELS, classify_repair,
                                              describe)
from app.config import settings
from app.llm.base import LLMUnavailable

_SYSTEM = "你是房屋租赁平台的报修工单分派员，只输出 JSON，不要任何解释。"

_PROMPT = """分析下面这条报修描述，判断故障类型与紧急程度。

故障类型只能是：水暖 / 电路 / 家电 / 门窗 / 家具 / 燃气 / 其他
紧急程度只能是：紧急 / 一般 / 可延后

判定规则：涉及漏水、漏电、燃气泄漏、停电、门锁损坏等安全或居住底线问题，
一律判为「紧急」，即使租客自己说不着急。

报修描述：「{text}」

只输出如下 JSON：
{{"category": "<故障类型>", "urgency": "<紧急程度>", "reason": "<一句话依据>"}}"""

_VALID_CATEGORIES = {"水暖", "电路", "家电", "门窗", "家具", "燃气", "其他"}


def analyze(text, provider=None):
    """分析报修描述，返回 ``(result, source)``。

    规则置信度达标就直接用；不达标且大模型可用时才调用大模型。
    """
    rule_result = classify_repair(text)
    if rule_result["confidence"] >= settings.rule_confidence_threshold:
        return rule_result, "rules"

    if provider is None or not provider.available:
        return rule_result, "rules_fallback"

    try:
        data = provider.complete_json(_SYSTEM, _PROMPT.format(text=text))
    except LLMUnavailable:
        return rule_result, "rules_fallback"

    if not isinstance(data, dict):
        return rule_result, "rules_fallback"

    category = str(data.get("category") or "").strip()
    urgency = str(data.get("urgency") or "").strip()
    # 大模型可能返回不在枚举里的值，一律丢弃回退，不能直接写进数据库
    if category not in _VALID_CATEGORIES or urgency not in URGENCY_LEVELS:
        return rule_result, "rules_fallback"

    from app.agents.repair.tools.classify import ASSIGNEE_BY_CATEGORY
    return {
        "category": category,
        "urgency": urgency,
        "assignee": ASSIGNEE_BY_CATEGORY.get(category, "综合维修工"),
        "confidence": 0.9,
        "matched": rule_result["matched"],
        "reason": str(data.get("reason") or "")[:120],
    }, "llm"


def run(text, house_no=None, applicant=None, provider=None, backend=None,
        submit=True):
    """报修智能体入口。

    :param submit: 为 False 时只分析不落库，用于前端预览分派结果。
    """
    if not text or not text.strip():
        return {
            "intent": "repair", "status": "need_content", "ok": False,
            "message": "请描述一下具体是哪里坏了，比如「卫生间水管漏水」。",
        }

    result, source = analyze(text, provider=provider)
    analysis = result.get("reason") or describe(result)

    payload = {
        "intent": "repair",
        "status": "analyzed",
        "ok": True,
        "source": source,
        "category": result["category"],
        "urgency": result["urgency"],
        "assignee": result["assignee"],
        "analysis": analysis,
        "message": "已识别为「%s」故障，紧急程度「%s」，建议派单给%s。"
                   % (result["category"], result["urgency"], result["assignee"]),
    }

    if not submit:
        return payload

    if backend is None or not house_no:
        payload["status"] = "need_house"
        payload["ok"] = False
        payload["message"] += "请到具体房源页面提交报修。"
        return payload

    applicant = applicant or {}
    try:
        created = backend.create_repair(
            house_no=house_no,
            content=text.strip(),
            category=result["category"],
            urgency=result["urgency"],
            assignee=result["assignee"],
            analysis=analysis,
            applicant=applicant,
        )
    except Exception:
        return {
            "intent": "repair", "status": "error", "ok": False,
            "source": source,
            "message": "报修提交失败，请稍后再试。",
        }

    payload["status"] = "created"
    # 把工单编号带回去，前端才能给刚提交的报修补照片
    payload["repair_id"] = (created or {}).get("repair", {}).get("id")
    payload["message"] = ("报修已提交。识别为「%s」故障，紧急程度「%s」，"
                          "已派单给%s。" % (result["category"], result["urgency"],
                                           result["assignee"]))
    return payload
