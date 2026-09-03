"""工具：把结构化筛选条件转成后端房源接口参数并执行查询（AI-01 的第二步）。

`parse_query` 输出的字段名是面向业务语义的中文拼音（`zujia_min` / `zujia_max`），
而后端接口用的是英文命名（`rent_price_min` / `rent_price_max`），
两者的转换集中在本模块，避免后端字段命名泄漏到解析层。
"""

from app.backend import BackendError, client

# 业务语义字段 -> 后端接口参数
_RANGE_MAP = {
    "zujia_min": "rent_price_min",
    "zujia_max": "rent_price_max",
    "mianji_min": "area_min",
    "mianji_max": "area_max",
}

# 等值 / 模糊匹配字段
_PASS_THROUGH = {
    "suozaidiqu": "district",
    "fangwuhuxing": "layout",
    "fangyuanleixing": "rent_type",
}


def build_params(filters, page=1, limit=10):
    """把筛选条件转换为后端 `/api/houses` 的查询参数。"""
    params = {"page": page, "limit": limit}
    for semantic, backend_key in _PASS_THROUGH.items():
        if filters.get(semantic):
            params[backend_key] = filters[semantic]
    for semantic, backend_key in _RANGE_MAP.items():
        if filters.get(semantic) is not None:
            params[backend_key] = filters[semantic]
    return params


def score_by_features(item, features):
    """计算房源命中的设施/偏好条件数量。"""
    if not features:
        return 0
    parts = [str(item.get(k) or "") for k in
             ("description", "title", "address", "community", "rent_type", "orientation")]
    parts.extend(str(t) for t in (item.get("tags") or []))
    haystack = " ".join(parts)
    return sum(1 for feature in features if feature in haystack)


def matches_all_features(item, features):
    """用户明确提出的多个设施条件采用 AND 语义，必须全部满足。"""
    return not features or score_by_features(item, features) == len(features)


def search_properties(filters, features=None, page=1, limit=10, backend=None):
    """执行房源查询。

    返回 ``{"ok":bool, "items":[...], "total":int, "params":{...}, "error":str|None}``。
    后端不可用时不抛异常，而是返回 ``ok=False`` 并附上原因，
    由上层生成对用户可读的提示。
    """
    backend = backend or client
    params = build_params(filters, page=page, limit=limit)
    try:
        if features:
            # 设施条件不属于后端结构化字段。先取完结构化条件下的候选集，
            # 再本地过滤和分页，避免仅过滤当前一页导致 total 与翻页错误。
            fetch_params = build_params(filters, page=1, limit=100)
            result = backend.list_properties(fetch_params)
            candidates = list(result.get("list") or [])
            candidate_total = result.get("total") or len(candidates)
            fetch_page = 2
            while len(candidates) < candidate_total:
                next_params = build_params(filters, page=fetch_page, limit=100)
                next_result = backend.list_properties(next_params)
                next_items = next_result.get("list") or []
                if not next_items:
                    break
                candidates.extend(next_items)
                fetch_page += 1

            matched = [item for item in candidates if matches_all_features(item, features)]
            matched.sort(key=lambda it: score_by_features(it, features), reverse=True)
            start = (page - 1) * limit
            items = matched[start:start + limit]
            total = len(matched)
        else:
            result = backend.list_properties(params)
            items = result.get("list") or []
            total = result.get("total") or len(items)
    except BackendError as exc:
        return {"ok": False, "items": [], "total": 0, "params": params,
                "error": str(exc)}
    except Exception as exc:
        # BackendError 之外的意外同样要降级。原先只捕获 BackendError，
        # 后端返回了非预期结构、或注入进来的客户端实现自己抛了异常，
        # 都会**直接把整个智能体请求打崩**——用户看到的是 500，
        # 而不是「房源服务暂时不可用」。
        return {"ok": False, "items": [], "total": 0, "params": params,
                "error": "房源服务异常：%s" % type(exc).__name__}

    return {
        "ok": True,
        "items": items,
        "total": total,
        "params": params,
        "error": None,
    }


def summarize(items, total, filters, features):
    """生成一句面向用户的结果说明。

    不接大模型时由本函数产出，接了大模型则作为兜底，
    保证任何情况下用户都能拿到一句人话（非功能需求 N-07）。
    """
    if total == 0:
        hint = _relax_hint(filters)
        return "没有找到符合条件的房源。" + (("建议" + hint) if hint else "建议放宽筛选条件再试。")

    parts = []
    if filters.get("suozaidiqu"):
        parts.append(filters["suozaidiqu"])
    if filters.get("fangwuhuxing"):
        parts.append(filters["fangwuhuxing"])
    condition = "".join(parts) if parts else "符合条件"

    prices = []
    for it in items:
        try:
            prices.append(int(float(it.get("rent_price"))))
        except (TypeError, ValueError):
            continue
    price_text = ""
    if prices:
        low, high = min(prices), max(prices)
        price_text = "，租价 %d 元" % low if low == high else "，租价 %d–%d 元" % (low, high)

    feature_text = ""
    if features:
        feature_text = "，均满足「%s」要求" % "、".join(features)

    return "为你找到 %d 套%s的房源%s%s。" % (total, condition, price_text, feature_text)


def _relax_hint(filters):
    """没结果时，给出最可能奏效的放宽建议。"""
    if filters.get("zujia_max"):
        return "把租价上限从 %d 元提高一些。" % filters["zujia_max"]
    if filters.get("mianji_min"):
        return "把面积要求从 %d 平米降低一些。" % filters["mianji_min"]
    if filters.get("fangwuhuxing"):
        return "放宽户型要求。"
    if filters.get("suozaidiqu"):
        return "扩大到周边区域看看。"
    return ""
