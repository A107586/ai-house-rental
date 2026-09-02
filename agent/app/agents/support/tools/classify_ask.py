"""把客服问题分成「事务型」与「知识型」。

**这一步是本功能与「接一个 RAG」最主要的区别。**

「押金一般收几个月」是知识型——答案写在 FAQ 里，全平台用户看到的一样。
「我的押金退了没」是事务型——答案在数据库里，每个用户都不一样。
拿 RAG 去回答第二类问题，模型只能照着 FAQ 的「7 个工作日内退还」编一句
听起来很像样的话，而用户真正想知道的是**他那一笔**到底退没退。
这种回答比拒答有害得多，因为它听上去是有依据的。

所以事务型问题走后端接口取真实数据（并且需要用户令牌），
知识型才走检索。判定采用「人称标记 + 业务对象」双命中，
两个都有才算事务型——只有这样才不会把
「押金一般多久退」误判成「我的押金多久退」。
"""

import re

# 人称标记：这句话问的是不是「我自己的」
_PERSONAL = [
    "我的", "我", "本人", "咱的", "俺的", "帮我查", "给我查", "帮我看看",
]

# 业务对象 -> (关键词, 后端取数动作)
# 动作名对应 `account.py` 里的函数，加一类事务只需在两处各加一行。
TOPICS = [
    ("bill", ["账单", "房租", "租金", "欠费", "欠多少", "交没交", "缴费",
              "交过", "该交", "要交", "催缴", "逾期"]),
    ("contract", ["租约", "合同", "租期", "到期", "什么时候到期", "签约"]),
    ("repair", ["报修", "工单", "维修", "修好", "师傅"]),
    ("booking", ["预约", "看房申请", "约看"]),
]

# 明确指向「查我自己的记录」的说法。命中这些即使没有人称代词也算事务型，
# 比如「账单还差多少没交」——省略了「我的」，但显然问的是自己的。
_EXPLICIT_QUERY = [
    "还差多少", "还欠多少", "欠多少", "退了没", "退了吗", "到哪一步",
    "什么时候到期", "还有几天", "处理好了吗", "通过了吗", "审核过了吗",
    "交到几月", "剩多少",
]

KIND_ACCOUNT = "account"
KIND_KNOWLEDGE = "knowledge"

# 「一般」「通常」这类词把问题拉回知识型：
# 「我的押金一般多久退」问的其实还是规则，不是他那一笔的状态。
_GENERIC = ["一般", "通常", "规定", "政策", "怎么算", "标准是", "都是多久"]

_HANDOFF = ["转人工", "人工客服", "找人工", "真人", "客服电话", "联系客服",
            "找个人问", "人工"]


def wants_handoff(text):
    """用户是不是明确要求转人工。"""
    return any(word in (text or "") for word in _HANDOFF)


def classify(text):
    """返回 ``{"kind", "topic", "hits"}``。

    `kind` 为 ``account`` 时 `topic` 指明查哪一类数据；
    为 ``knowledge`` 时 `topic` 为空。
    """
    raw = (text or "").strip()
    if not raw:
        return {"kind": KIND_KNOWLEDGE, "topic": "", "hits": []}

    topic = ""
    hits = []
    for name, keywords in TOPICS:
        matched = [kw for kw in keywords if kw in raw]
        if matched:
            topic, hits = name, matched
            break

    if not topic:
        return {"kind": KIND_KNOWLEDGE, "topic": "", "hits": []}

    # 泛化词把问题拉回知识型
    if any(word in raw for word in _GENERIC):
        return {"kind": KIND_KNOWLEDGE, "topic": "", "hits": hits}

    personal = [word for word in _PERSONAL if word in raw]
    explicit = [word for word in _EXPLICIT_QUERY if word in raw]
    if personal or explicit:
        return {"kind": KIND_ACCOUNT, "topic": topic,
                "hits": hits + personal + explicit}

    return {"kind": KIND_KNOWLEDGE, "topic": "", "hits": hits}
