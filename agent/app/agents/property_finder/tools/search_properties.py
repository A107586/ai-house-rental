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

# 押几付几：等值，且 0 有意义
_EXACT_MAP = {
    "yajin_yue": "deposit_months",
    "fukuan_yue": "pay_months",
}

# 等值 / 模糊匹配字段
_PASS_THROUGH = {
    "suozaidiqu": "district",
    "fangwuhuxing": "layout",
    "fangyuanleixing": "rent_type",
    # 认不出是哪个区的地名（街道、小区、商圈）。后端的 keyword 会去比对
    # 标题、描述、小区、地址、商圈——那正是这类词该去的地方。
    "guanjianci": "keyword",
}


def build_params(filters, page=1, limit=10, features=None):
    """把筛选条件转换为后端 `/api/houses` 的查询参数。

    设施偏好（features）现在也一并交给后端。**原先是拿回来在本地过滤的**，
    那样有两个毛病：

    - **慢**：为了拿到正确的 total，得把结构化条件下的候选集整个翻完
      （每页 100 条循环拉取），一次找房打好几趟后端；
    - **错**：本地过滤靠子串匹配，「无电梯」里含「电梯」二字，
      要电梯的人会被推一套没电梯的房。

    后端按整词匹配，已经升格成独立列的（电梯/精装/集中供暖）直接比列，
    两个毛病一起没了。
    """
    params = {"page": page, "limit": limit}
    for semantic, backend_key in _PASS_THROUGH.items():
        if filters.get(semantic):
            params[backend_key] = filters[semantic]
    for semantic, backend_key in _RANGE_MAP.items():
        if filters.get(semantic) is not None:
            params[backend_key] = filters[semantic]
    # 押几付几按等值比，**0 是有效值**（押零付一），不能走上面的真值判断
    for semantic, backend_key in _EXACT_MAP.items():
        if filters.get(semantic) is not None:
            params[backend_key] = filters[semantic]
    if features:
        params["features"] = ",".join(features)
    return params


def search_properties(filters, features=None, page=1, limit=10, backend=None):
    """执行房源查询。

    返回 ``{"ok":bool, "items":[...], "total":int, "params":{...}, "error":str|None}``。
    后端不可用时不抛异常，而是返回 ``ok=False`` 并附上原因，
    由上层生成对用户可读的提示。
    """
    backend = backend or client
    params = build_params(filters, page=page, limit=limit, features=features)
    try:
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
