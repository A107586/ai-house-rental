"""针对某一套具体房源的提问。

「讲解第一套的收费」「这套押几付几」「多少平米」——这类问题既不是平台规则
（FAQ 里没有、也不该有，每套房都不一样），也不是用户的个人数据。
答案就在那套房的详情里。

设计方案里本来就写了「房源详情：提问带房源编号时**定向**取」，
但一直没接上，于是这类问题一路走到检索、然后被闸门拒掉——
用户看到的是「这个我没查到相关规定」，而信息明明就在房源页上。

**为什么不把房源灌进检索索引**：平台有几百套房，全塞进去会把
「押金」「租金」这些词的 IDF 稀释掉，FAQ 的检索质量跟着一起垮。
定向取当前这一套，既准又不影响别人。

回答由代码按字段拼，不经过大模型——房源的价格、面积、朝向都是
硬事实，没有需要「组织语言」的余地，反倒有被改写错的风险。
"""

from app.backend import BackendError

# 问的是房源的哪一面。顺序即优先级：一句话里同时问了租金和面积时，
# 先答租金——那是用户最关心的。
ASPECTS = [
    ("rent", ["收费", "租金", "多少钱", "价格", "房租", "贵不贵", "月租",
              "押金", "押几付几", "付款"]),
    ("size", ["面积", "多大", "平米", "平方"]),
    ("layout", ["户型", "几室", "几居", "格局"]),
    ("floor", ["楼层", "几楼", "电梯"]),
    ("orientation", ["朝向", "朝南", "采光"]),
    ("location", ["位置", "在哪", "地址", "小区", "附近"]),
    ("status", ["还在吗", "租出去了", "能租吗", "可以租吗", "有没有人租"]),
]

# **规则型问法**：这些词一出现，问的就是平台规则而不是这套房的属性。
#
# 「押金」是最容易混的一个词：「这套押金多少」问的是房源属性，
# 「押金怎么退」问的是平台规则。两者都带「押金」，靠这张表分开。
# 分不开的后果是用户在房源页里问退款流程，被回一句「月租 4150 元」。
_RULE_WORDS = ["怎么退", "怎么算", "怎么交", "如何", "流程", "规定", "政策",
               "什么时候退", "退还", "能不能退", "多久退", "要不要",
               "谁承担", "谁交", "算谁的"]

# 判断这句话是不是在问「这套房」。带指代词或直接给了编号都算。
REFERS = ["这套", "那套", "这个房", "那个房", "第一套", "第二套", "第三套",
          "这房子", "该房源", "这间"]


def asks_about_house(text, house_no):
    """这句话是不是在问某一套具体房源。

    要求**同时**满足：有房源上下文（当前打开的房源，或上一轮解析出的
    「第一套」），并且问的是房源本身的属性。
    少了后一个条件，用户在房源详情页里问「押金怎么退」会被当成
    「这套房的押金」——那是平台规则，不是这套房的属性。
    """
    if not house_no:
        return ""
    raw = text or ""
    # 规则型问法优先让路——这类问题的答案在 FAQ 里，不在房源详情里
    if any(word in raw for word in _RULE_WORDS):
        return ""
    for aspect, keywords in ASPECTS:
        if any(kw in raw for kw in keywords):
            return aspect
    return ""


def _fmt(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value or "")
    return ("%.0f" % number) if number == int(number) else ("%.1f" % number)


STATUS_LABELS = {
    "available": "可租", "rented": "已出租",
    "pending": "待审核", "offline": "已下架",
}


def describe(backend, house_no, aspect):
    """按房源详情回答。返回 ``(message, house)``，取不到时 message 为空。"""
    try:
        house = backend.get("/api/houses/%s" % house_no)
    except BackendError:
        return "", None
    except Exception:
        return "", None
    if not isinstance(house, dict) or not house.get("house_no"):
        return "", None

    name = "%s %s" % (house.get("community") or "", house.get("layout") or "")
    rent = _fmt(house.get("rent_price"))

    if aspect == "rent":
        parts = ["%s（%s）月租 %s 元" % (name.strip(), house_no, rent)]
        deposit = house.get("deposit")
        if deposit:
            parts.append("押金 %s 元" % _fmt(deposit))
        # `rent_type` 是租赁方式（整租／合租／公寓），不是付款方式。
        # 一开始按「付款方式」输出，答出来是「付款方式 公寓」——
        # 字段名读岔了，说出来就是一句没人看得懂的话。
        rent_type = house.get("rent_type")
        if rent_type:
            parts.append("租赁方式 %s" % rent_type)
        # 水电费怎么算是平台统一规则，不是这套房的属性，这里点一句
        # 让用户知道去哪问，而不是让他以为这套房特殊
        return ("；".join(parts) + "。水电费按平台规则由租客自行缴纳，"
                "不含在租金里。"), house

    if aspect == "size":
        return ("%s（%s）建筑面积 %s 平方米。"
                % (name.strip(), house_no, _fmt(house.get("area")))), house

    if aspect == "layout":
        return ("%s（%s）是 %s，%s 平方米。"
                % (house.get("community") or "这套房", house_no,
                   house.get("layout") or "户型未标注",
                   _fmt(house.get("area")))), house

    if aspect == "floor":
        return ("%s（%s）楼层：%s。"
                % (name.strip(), house_no,
                   house.get("floor") or "房东未标注")), house

    if aspect == "orientation":
        return ("%s（%s）朝向：%s。"
                % (name.strip(), house_no,
                   house.get("orientation") or "房东未标注")), house

    if aspect == "location":
        return ("%s（%s）位于 %s %s %s。"
                % (name.strip(), house_no, house.get("district") or "",
                   house.get("community") or "",
                   house.get("address") or "")), house

    if aspect == "status":
        label = STATUS_LABELS.get(house.get("status"), house.get("status") or "")
        return ("%s（%s）当前状态：%s。" % (name.strip(), house_no, label)), house

    return "", house


# ---------------------------------------------------------------- 「我要租」

# 用户说「帮我租这套」时想要的是一步到位，但平台的租赁流程做不到——
# 得先看房，房东确认后才发起签约。
_WANT_RENT = ["我要租", "帮我租", "想租下", "租下来", "直接租", "怎么租下",
              "租这套", "租第一套", "租第二套", "订下来", "定下来"]


def wants_to_rent(text):
    return any(word in (text or "") for word in _WANT_RENT)


def rent_flow_message(house_no=""):
    """说清楚真实流程，并指向下一步能做的事。

    **不能假装能一键租下。** 平台的签约由房东在看房通过后发起，
    智能体没有这个接口，也不该有——这一步关系到双方确认合同条款。
    与其含糊地答「好的」，不如把三步讲清楚，再把眼下真正能做的
    （预约看房）递过去。
    """
    who = "这套房" if not house_no else "房源 %s" % house_no
    return ("租下来要走三步：**先预约看房 → 房东确认后发起签约 → "
            "双方确认合同、生成首期账单**。签约由房东发起，我这边代办不了，"
            "但可以先帮你把%s的看房约上。你想什么时候去看？"
            "比如「这周六下午两点」。" % who)
