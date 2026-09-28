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
    # 「评价怎么样」原来一个分支都不命中，于是掉进通用讲解里，
    # 回了一段房源介绍——**问的是别人住过之后怎么说，答的是户型朝向**。
    # 放在最后一位：它的关键词最泛（「口碑」「评分」），
    # 排前面会把「押金评价」这种半截话也抢走
    ("reviews", ["评价", "评分", "口碑", "住过", "怎么说", "有人租过",
                 "好评", "差评", "打分"]),
]

# **规则型问法**：这些词一出现，问的就是平台规则而不是这套房的属性。
#
# 「押金」是最容易混的一个词：「这套押金多少」问的是房源属性，
# 「押金怎么退」问的是平台规则。两者都带「押金」，靠这张表分开。
# 分不开的后果是用户在房源页里问退款流程，被回一句「月租 4150 元」。
_RULE_WORDS = ["怎么退", "怎么算", "怎么交", "如何", "流程", "规定", "政策",
               "什么时候退", "退还", "能不能退", "多久退", "要不要",
               "谁承担", "谁交", "算谁的",
               # 真实房东测试补的：句子里有「租金」二字就被当成「问这套房
               # 月租多少」，答了一段房源介绍。
               #   「这个月我收了多少租金」——问的是经营数据
               #   「我想把空着的那套租金降两百，怎么操作」——问的是怎么改
               "怎么操作", "怎么改", "怎么调", "怎么设置", "怎么修改",
               "降价", "降租", "涨价", "涨租", "降两", "降一", "涨两", "涨一",
               "收了多少", "收入", "进账", "赚了", "谁出", "谁负责", "谁付"]

# 判断这句话是不是在问「这套房」。带指代词或直接给了编号都算。
#
# 序号从这张表里拿掉了：原来写死到「第三套」，第四套往后一律认不出来。
# 数字交给 `memory.ordinal_indexes()` 解析，这里只留非数字的指代词。
REFERS = ["这套", "那套", "这个房", "那个房", "这房子", "该房源", "这间"]


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

    if aspect == "reviews":
        return reviews(backend, house_no, name.strip()), house

    return "", house


def _grade(score):
    """把分数说成一句人话。

    **不能只给「4.2 分」就完事。** 用户问的是「怎么样」，要的是一个判断，
    而 4.2 分算好算坏，他心里并没有标尺。
    """
    if score >= 4.5:
        return "很好"
    if score >= 4.0:
        return "不错"
    if score >= 3.0:
        return "一般"
    return "偏低"


def _quote_review(row):
    """摘一条评价原文。**照抄不改写**——这是别人说的话，
    替他润色一遍就不再是他说的了。"""
    text = " ".join((row.get("content") or "").split())
    if len(text) > 60:
        text = text[:60] + "…"
    return "%s（%s，%d 分）：%s" % (row.get("tenant_name") or "租客",
                                row.get("created_at") or "",
                                int(row.get("rating") or 0), text)


def landlord_reviews(backend, house_no, name):
    """这套房的房东口碑。

    和房源评价分开答：**「房子怎么样」和「房东怎么样」是两个问题**，
    而租客最怕的那类事（报修不管、押金不退）全在后一个问题里。
    数据来自房东概要接口，它按这个房东名下**所有**房源汇总——
    单套房的两三条评价说明不了任何事。
    """
    try:
        data = backend.get("/api/houses/%s/landlord" % house_no)
    except Exception:                                        # noqa: BLE001
        return "房东信息暂时取不到，稍后再问我一次。"
    if not isinstance(data, dict):
        return "房东信息暂时取不到，稍后再问我一次。"

    count = int(data.get("review_count") or 0)
    parts = ["%s的房东是%s，%s加入平台。"
             % (name, data.get("name") or "（未公开）",
                data.get("joined_at") or "")]
    if data.get("verified"):
        parts.append("已完成实名。")
    if count:
        score = float(data.get("rating") or 0)
        parts.append("%d 位租客给他打了分，平均 %.1f，%s。"
                     % (count, score, _grade(score)))
    else:
        parts.append("还没有租客评价过他。")

    hours = data.get("avg_repair_hours")
    # 后端已经把负数滤掉了，这里再挡一次。**这句话会被用户当成
    # 「这个房东靠不靠谱」的依据**，宁可少说一句，不能说一句错的
    if hours is not None and hours >= 0:
        # 报修响应是房东口碑里**唯一可量化**的一项，比任何形容词都硬
        parts.append("名下报修平均 %s 小时处理完。" % _fmt(hours))

    for row in (data.get("comments") or [])[:2]:
        text = " ".join((row.get("content") or "").split())
        if not text:
            continue
        if len(text) > 60:
            text = text[:60] + "…"
        parts.append("%s（%s 分）：%s" % (row.get("name") or "租客",
                                      row.get("rating") or 0, text))
    return " ".join(parts)


def reviews(backend, house_no, name):
    """这套房的租后评价。

    三条自我约束：

    - **一条评价都没有时要说「还没有」，不能沉默。** 空着退回一段房源
      介绍，用户会以为助手没听懂；说「还没人评价过」他才知道该实地看房。
    - **差评一起说。** 只挑好评念是把这一栏变成广告位，
      而看房的人当场就能验证——骗到一次，以后这行字就没人看了。
    - **数字从字段里取，不经过大模型。** 平均分错一位，
      整段话就从「懂行」变成「不可信」。
    """
    try:
        data = backend.get("/api/houses/%s/reviews" % house_no)
    except Exception:                                        # noqa: BLE001
        return "评价暂时取不到，稍后再问我一次。"
    rows = (data or {}).get("list") or []
    if not rows:
        return ("%s（%s）还没有人评价过——这套房要么刚上架，"
                "要么还没有租客住满一期。没有评价不代表不好，"
                "但更建议实地看一次。" % (name, house_no))

    average = float((data or {}).get("average") or 0)
    good = [r for r in rows if int(r.get("rating") or 0) >= 4]
    bad = [r for r in rows if int(r.get("rating") or 0) <= 2]

    parts = ["%s（%s）共 %d 条评价，平均 %.1f 分，%s。"
             % (name, house_no, len(rows), average, _grade(average))]
    if good:
        parts.append("%d 条给了 4 分以上。" % len(good))
    if bad:
        # 差评条数单独说。埋进平均分里等于没说——4.2 分背后是
        # 「全是 4 分」还是「一片 5 分加两个 1 分」，
        # 对「要不要去看这套房」是完全不同的信息
        parts.append("有 %d 条 2 分及以下。" % len(bad))

    # 各摘一条原文，**先说差的**：好话用户默认会有，
    # 真正影响决定的是那条不好的
    quoted = [r for r in bad if (r.get("content") or "").strip()][:1]
    quoted += [r for r in good if (r.get("content") or "").strip()][:1]
    parts.extend(_quote_review(row) for row in quoted)

    shots = sum(len(r.get("images") or []) for r in rows)
    if shots:
        parts.append("其中 %d 张实拍图，点开房源详情能看。" % shots)
    return " ".join(parts)


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
