"""房源智能体（PropertyFinder）：编排 parse_query 与 search_properties 完成 AI-01。

职责边界很窄——只负责「把一句话变成房源列表和一句说明」。
意图判断在 Supervisor，取数在后端，本层只做编排。
"""

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

只输出如下 JSON：
{{"filters": {{...}}, "features": ["地铁", "电梯"]}}
其中 features 是无法用上述字段表达的偏好，没有就给空数组。"""

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
    """把自然语言变成筛选条件，返回 ``(filters, features, source)``。

    规则解析置信度达标就直接用；不达标且大模型可用时才调用大模型。
    """
    rule_result = parse_query(text)
    if rule_result["confidence"] >= settings.rule_confidence_threshold:
        return rule_result["filters"], rule_result["features"], "rules"

    if provider is None or not provider.available:
        return rule_result["filters"], rule_result["features"], "rules_fallback"

    try:
        data = provider.complete_json(_PARSE_SYSTEM, _PARSE_PROMPT.format(text=text))
    except LLMUnavailable:
        return rule_result["filters"], rule_result["features"], "rules_fallback"

    if not isinstance(data, dict):
        return rule_result["filters"], rule_result["features"], "rules_fallback"

    filters = _sanitize(data.get("filters"))
    features = _sanitize_features(data.get("features"))
    # 规则词典对已覆盖的设施表达更稳定；合并大模型结果可避免近义词造成
    # 数据库标签不匹配，同时保留大模型识别出的其他受支持设施。
    features = list(dict.fromkeys(rule_result["features"] + features))[:5]

    # 只有设施条件也是有效的大模型解析结果。例如“可以养猫的房子”没有
    # 地区、价格等 filters，但 features 中的“可养宠物”足以执行搜索。
    # 旧逻辑只检查 filters，因而把成功调用误标成“大模型不可用”。
    if not filters and not features:
        return rule_result["filters"], rule_result["features"], "rules_fallback"

    return filters, features, "llm"


def run(text, page=1, limit=10, provider=None, backend=None,
        filters=None, features=None, source=None):
    """房源智能体入口。

    返回结构固定，便于前端统一渲染；任何一步失败都不抛异常给调用方。

    :param filters: 已经解析并合并好的条件。多轮会话会把历史轮次的槽位
                    合并进来（见 ``app.memory``），此时不能再按本轮原文
                    重新解析——那样「再便宜点」只会解析出一片空白。
    """
    if filters is None and features is None:
        filters, features, source = understand(text, provider=provider)
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
