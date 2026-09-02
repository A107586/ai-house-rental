"""房源智能体（PropertyFinder）：编排 parse_query 与 search_properties 完成 AI-01。

职责边界很窄——只负责「把一句话变成房源列表和一句说明」。
意图判断在 Supervisor，取数在后端，本层只做编排。
"""

from app import fusion
from app.config import settings
from app.llm.base import LLMUnavailable
from app.agents.property_finder.tools.parse_query import FEATURES, parse_query
from app.agents.property_finder.tools.search_properties import (
    search_properties, summarize)

_PARSE_SYSTEM = "你是房屋租赁平台的查询解析器，只输出 JSON，不要任何解释。"

_PARSE_PROMPT = """把用户的找房需求解析成结构化筛选条件。

可用字段（没提到的一律省略，不要臆造）：
- suozaidiqu：所在地区，如「中山区」
- fangwuhuxing：户型，如「两室一厅」
- zujia_min / zujia_max：租价下限 / 上限，单位元，整数
- mianji_min / mianji_max：面积下限 / 上限，单位平方米，整数
- fangyuanleixing：房源类型，只能是 整租 / 合租 / 公寓 / 住宅 / 别墅 之一

用户输入：「{text}」

系统的规则引擎已经先解析了一遍，结果如下，**供你参考**：

{reference}

规则引擎穷举了常见说法，它解析出来的字段通常是对的；但口语、错别字、
绕着说的表达它认不出来，就会漏掉。请在它的基础上：

1. 逐项确认已有字段是否正确，错了就改；
2. 补上它漏掉的字段；
3. 直接给出你认为正确的**最终结果**，不要解释，也不要保留错误的项。

只输出如下 JSON：
{{"filters": {{...}}, "features": ["地铁", "电梯"]}}
其中 features 是无法用上述字段表达的偏好，没有就给空数组。"""

# 规则结论在提示词里用中文字段名，比 suozaidiqu 这种拼音好读得多
_FIELD_LABELS = {
    "suozaidiqu": "区域", "fangwuhuxing": "户型",
    "fangyuanleixing": "房源类型",
    "zujia_min": "租价下限", "zujia_max": "租价上限",
    "mianji_min": "面积下限", "mianji_max": "面积上限",
}

_ALLOWED_FILTERS = {
    "suozaidiqu", "fangwuhuxing", "fangyuanleixing",
    "zujia_min", "zujia_max", "mianji_min", "mianji_max",
}
_INT_FILTERS = {"zujia_min", "zujia_max", "mianji_min", "mianji_max"}


def _sanitize(filters):
    """只保留白名单字段并做类型校验。

    大模型的输出不可全信：字段名可能臆造、数值可能是字符串或负数。
    这一层把不合法的内容直接丢弃，避免脏参数传到后端接口。
    """
    if not isinstance(filters, dict):
        return {}
    clean = {}
    for key, value in filters.items():
        if key not in _ALLOWED_FILTERS or value in (None, ""):
            continue
        if key in _INT_FILTERS:
            try:
                number = int(float(value))
            except (TypeError, ValueError):
                continue
            if number > 0:
                clean[key] = number
        else:
            text = str(value).strip()
            if text:
                clean[key] = text
    return clean


def _sanitize_features(features):
    """把大模型返回的近义表达映射成数据库使用的统一设施标签。"""
    if not isinstance(features, list):
        return []
    clean = []
    for value in features:
        text = str(value).strip()
        canonical = next((name for name, aliases in FEATURES.items()
                          if name == text or any(alias in text for alias in aliases)), None)
        if canonical and canonical not in clean:
            clean.append(canonical)
    return clean[:5]


def understand(text, provider=None):
    """把自然语言变成筛选条件，返回 ``(filters, features, source, divergence)``。

    **规则与大模型同时参与**：规则先完整解析一遍，结论作为「参考答案」
    写进提示词，再由大模型给出最终结果。

    与旧的「置信度够就不调大模型」相比：那样规则确定时大模型完全不参与，
    可规则再全也覆盖不了所有说法；规则不确定时又把它的结论整个丢掉，
    大模型得从零猜。现在两边的信息都用上了。

    大模型不可用时直接采用规则结论——这是需求 N-07 的要求，不是可选项。
    """
    rule_result = parse_query(text)
    rule_payload = {
        "filters": dict(rule_result["filters"]),
        "features": list(rule_result["features"]),
    }

    def sanitize(raw):
        filters = _sanitize(raw.get("filters"))
        features = _sanitize_features(raw.get("features"))
        # 规则词典对已覆盖的设施表达更稳定；与大模型的结果合并，既避免
        # 近义词造成数据库标签不匹配，也保留它多认出来的设施。
        features = list(dict.fromkeys(rule_result["features"] + features))[:5]
        # 条件与设施全空说明这次输出没有任何信息量，不如用规则的。
        # 注意只有设施也算有效——「可以养猫的房子」没有地区价格，
        # 但「可养宠物」这一条已经足够执行搜索。
        if not filters and not features:
            return None
        return {"filters": filters, "features": features}

    reference = fusion.render_reference(rule_result["filters"], _FIELD_LABELS)
    if rule_result["features"]:
        reference += "\n- 设施偏好：" + "、".join(rule_result["features"])

    final, source, divergence = fusion.fuse(
        rule_payload, provider, _PARSE_SYSTEM,
        _PARSE_PROMPT.format(text=text, reference=reference),
        sanitize, guards=(_keep_rule_district,),
        enabled=settings.fusion_enabled)

    return final["filters"], final["features"], source, divergence


def _keep_rule_district(rule_payload, fused):
    """护栏：规则明确认出的行政区不接受改写。

    大连的区名是一张穷举表（六个区，含「沙河口」这类简称补全），
    匹配上就是确定的。大模型偶尔会把「沙河口区」写成「沙河口」，
    或者干脆换一个区——那不是纠正，是幻觉，而区域写错会让整页结果全错。
    """
    rule_district = (rule_payload.get("filters") or {}).get("suozaidiqu")
    got = fused.get("filters", {}).get("suozaidiqu")
    if rule_district and got != rule_district:
        fused["filters"]["suozaidiqu"] = rule_district
        return fused, "区域保留规则结果「%s」（大模型给的是「%s」）" % (
            rule_district, got or "空")
    return fused, None


def run(text, page=1, limit=10, provider=None, backend=None,
        filters=None, features=None, source=None):
    """房源智能体入口。

    返回结构固定，便于前端统一渲染；任何一步失败都不抛异常给调用方。

    :param filters: 已经解析并合并好的条件。多轮会话会把历史轮次的槽位
                    合并进来（见 ``app.memory``），此时不能再按本轮原文
                    重新解析——那样「再便宜点」只会解析出一片空白。
    """
    divergence = []
    if filters is None and features is None:
        filters, features, source, divergence = understand(text, provider=provider)
    else:
        filters = filters or {}
        features = features or []
        source = source or "rules"

    if not filters and not features:
        return {
            "intent": "search",
            "status": "unparsed",
            "filters": {},
            "features": [],
            "source": source,
            "items": [],
            "total": 0,
            "message": "没听懂你的找房需求，可以说得具体一点，"
                       "比如「中山区两室一厅，租金三千以内」。",
            "ok": False,
        }

    result = search_properties(filters, features=features, page=page,
                               limit=limit, backend=backend)

    if not result["ok"]:
        return {
            "intent": "search",
            "status": "error",
            "filters": filters,
            "features": features,
            "source": source,
            "items": [],
            "total": 0,
            "message": "房源服务暂时不可用，请稍后再试。",
            "ok": False,
            "error": result["error"],
        }

    return {
        "intent": "search",
        "status": "found" if result["total"] else "empty",
        "filters": filters,
        "features": features,
        "source": source,
        "items": result["items"],
        "total": result["total"],
        "message": summarize(result["items"], result["total"], filters, features),
        "ok": True,
    }
