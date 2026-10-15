"""报修智能体（RepairAgent）：完成 AI-04 报修工单智能分派。

流程：分析描述 → 得出故障类型、紧急程度与建议维修工种 → 提交工单。

与找房、预约保持一致的返回结构（含 `status` 字段），
前端只需处理一种形状。
"""

from app.agents.repair.tools import vision
from app.agents.repair.tools.classify import (URGENCY_LEVELS, classify_repair,
                                              describe)
from app import fusion
from app.config import settings

_SYSTEM = "你是房屋租赁平台的报修工单分派员，只输出 JSON，不要任何解释。"

_PROMPT = """分析下面这条报修描述，判断故障类型与紧急程度。

故障类型只能是：水暖 / 电路 / 家电 / 门窗 / 家具 / 燃气 / 其他
紧急程度只能是：紧急 / 一般 / 可延后

判定规则：涉及漏水、漏电、燃气泄漏、停电、门锁损坏等安全或居住底线问题，
一律判为「紧急」，即使租客自己说不着急。

报修描述：「{text}」

系统的规则引擎已经先判了一遍，结果如下，**供你参考**：

{reference}

规则引擎靠关键词表匹配，常见说法判得准；但换个说法、说得含糊，
它就只能落到「其他」。请在它的基础上确认与修正，直接给出最终判断。

只输出如下 JSON：
{{"category": "<故障类型>", "urgency": "<紧急程度>", "reason": "<一句话依据>"}}"""

_RESULT_LABELS = {"category": "故障类型", "urgency": "紧急程度",
                  "assignee": "建议派单"}

_VALID_CATEGORIES = {"水暖", "电路", "家电", "门窗", "家具", "燃气", "其他"}


def analyze(text, provider=None):
    """分析报修描述，返回 ``(result, source)``。

    **规则与大模型同时参与**：规则先判一遍，结论作为参考写进提示词，
    由大模型给出最终判断。大模型不可用时直接采用规则结论（需求 N-07）。

    紧急程度有一条**不接受覆盖**的护栏，见 `_never_downgrade_safety`。
    """
    from app.agents.repair.tools.classify import ASSIGNEE_BY_CATEGORY

    rule_result = classify_repair(text)

    def sanitize(raw):
        category = str(raw.get("category") or "").strip()
        urgency = str(raw.get("urgency") or "").strip()
        # 枚举外的值一律丢弃回退——这些字段会直接写进数据库，
        # 写进去一个「非常紧急」，前端的状态过滤就全乱了
        if category not in _VALID_CATEGORIES or urgency not in URGENCY_LEVELS:
            return None
        return {
            "category": category,
            "urgency": urgency,
            "assignee": ASSIGNEE_BY_CATEGORY.get(category, "综合维修工"),
            "confidence": 0.9,
            "matched": rule_result["matched"],
            "reason": str(raw.get("reason") or "")[:120],
        }

    reference = fusion.render_reference(
        {k: rule_result[k] for k in ("category", "urgency", "assignee")},
        _RESULT_LABELS)

    final, source, divergence = fusion.fuse(
        rule_result, provider, _SYSTEM,
        _PROMPT.format(text=text, reference=reference),
        sanitize, guards=(_never_downgrade_safety,),
        enabled=settings.fusion_enabled)

    if divergence:
        final = dict(final, divergence=divergence)
    return final, source


def _never_downgrade_safety(rule_result, fused):
    """护栏：规则判为紧急的，大模型不得降级。

    需求 AI-04 的原文是「涉及漏水、漏电、燃气泄漏等安全隐患，**必须**判为
    紧急」。这是硬性条款，不是倾向——漏判一次可能真出事，不能拿概率去赌。

    写在提示词里只是「请你注意」，大模型可以不照做；写在这里是无论它
    怎么答都改回来。反过来，大模型把「一般」升成「紧急」是允许的：
    多派一次工只是多跑一趟，代价与漏判完全不对等。
    """
    if rule_result.get("urgency") == "紧急" and fused.get("urgency") != "紧急":
        downgraded = fused.get("urgency")
        fused["urgency"] = "紧急"
        return fused, "紧急程度保留规则结果（大模型判为「%s」，涉及安全隐患不降级）" % downgraded
    return fused, None


def run(text, house_no=None, applicant=None, provider=None, backend=None,
        submit=True, images=None):
    """报修智能体入口。

    :param submit: 为 False 时只分析不落库，用于前端预览分派结果。
    :param images: 报修照片（本系统上传出来的地址或 data URI）。
                   看得出问题时，把看到的写成一句话**并进描述**再分派。
    """
    text = (text or "").strip()

    # 照片先翻译成文字，然后一切照旧走原来那条链路。
    # 分派仍由 `analyze` 定，不让识图另开一套判断——那套有 50 个用例盯着。
    seen = ""
    if images:
        has_fault, seen = vision.describe_fault(images, provider)
        if seen and not has_fault:
            # 模型明确说照片里没有损坏。**这句不能并进描述**，
            # 并进去会让「照片拍的是厨房」变成报修内容里的故障现象。
            seen = ""
        if seen:
            # 放在用户原话后面，并且标明是从照片看出来的。
            # 工单上要看得见这句话的来历，师傅和用户才好当场核对。
            if text:
                text = "%s（照片显示：%s）" % (text, seen)
            else:
                text = "照片显示：%s" % seen

    if not text:
        if images:
            return {
                "intent": "repair", "status": "need_content", "ok": False,
                "message": "照片里我没看出明确的损坏。麻烦用一句话说说"
                           "是哪里坏了，比如「卫生间水管漏水」。",
            }
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
        # 从照片读出来的那句话。前端要把它摆在用户眼前——
        # 拿一个用户没看过的句子去派单，出了错谁也说不清
        "vision": seen,
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
