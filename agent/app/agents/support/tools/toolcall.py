"""事务型问题的工具选择与作答——「让大模型自己去查数据，再照着数据回答」。

和知识型问答走的是两条完全不同的路：知识型检索 FAQ，事务型**查真实数据库**。
这一条链是：

    用户问题 → 规则先判一遍 → 大模型在白名单工具里挑一个（拿规则结论当参考）
             → 调后端接口取真实数据 → 把数据交给大模型组织成回答 → 数字校验

**为什么不让模型直接写 SQL。** 非功能需求 N-06 写死了「禁止拼接执行任意 SQL」，
这不是保守，是必要的：智能体一旦能自由查库，越权就从「后端接口的鉴权问题」
变成了「模型会不会被诱导」的问题，没法测也没法保证。所以模型只能在
**四个白名单工具**里挑一个，能查到什么由后端按用户令牌决定，
和用户自己在页面上点开看到的完全一致。

**为什么规则和大模型都要跑。** 与 `app/fusion.py` 的思路一致：规则的结论
作为「参考答案」写进提示词，大模型在此基础上修正。规则覆盖不到
「我上个月那笔交了没」这类说法，大模型能兜住；大模型不可用时规则照常工作
（非功能需求 N-07）。
"""

import json

from app.agents.support.tools import account, classify_ask
from app.agents.support.tools.answer import numbers_in
from app.llm.base import LLMUnavailable

# 白名单工具。每一项都对应一个**已有的**后端接口，一个都不新造——
# 客服能查到的，恰好是用户自己在页面上点得开的那些。
# 加一类事务需要在三处各加一行：这里、`classify_ask.TOPICS`、`account.HANDLERS`。
TOOL_SPECS = [
    ("bill", "查这个用户自己的账单：待缴金额、笔数、到期日、缴费记录"),
    ("contract", "查这个用户自己的租约：合同编号、租期起止、当前状态"),
    ("repair", "查这个用户自己的报修工单：进度、状态、内容"),
    ("booking", "查这个用户自己的看房预约：时间、是否已被房东确认"),
    ("favorite", "查这个用户收藏了哪些房源"),
    ("history", "查这个用户最近浏览过哪些房源"),
    ("message", "查这个用户的站内消息：有几条未读、最新一条是什么"),
    ("review", "查这个用户有哪些租约还没评价"),
    ("handoff", "查这个用户提交的转人工工单处理到哪一步了"),
    ("profile", "查这个用户的账号信息：用户名、身份是租客还是房东"),
    ("house", "【房东】查他自己发布的房源：共几套、可租／已租／待审核各几套"),
    ("income", "【房东】查他的经营数据：本月已收租金、待收金额、出租率、逾期笔数"),
    ("tenant", "【房东】查他名下的租客：几位、谁有逾期账单"),
    ("none", "不需要查任何个人数据，这是一个关于平台规则的通用问题"),
]

_SELECT_SYSTEM = "你是客服系统的工具路由器，只输出 JSON，不要解释。"

_SELECT_PROMPT = """用户问了一句话，判断要不要查他**个人的**数据，查哪一类。

用户问题：「{question}」

可选工具：
{tools}

规则引擎的初步判断：{rule_hint}
（这只是参考，你可以推翻它。）

判断要点：
- 问「我的」「我还欠多少」「到哪一步了」这类，是查个人数据
- 问「押金一般几个月」「水电费怎么算」这类，是平台通用规则，选 none
- 带【房东】标记的工具只有房东会问，比如「我这个月能收多少租」选 income、
  「我发布的房子审核过了吗」选 house
- 「我还欠多少房租」是 bill，「我这个月收了多少租金」是 income，
  两者都含「租金」，区别在**交钱的人还是收钱的人**
- 拿不准就选 none——查错数据比不查更糟

只输出：{{"tool": "上面之一", "why": "不超过15字"}}"""

_COMPOSE_SYSTEM = ("你是房屋租赁平台的客服。**只能依据给定的数据回答**，"
                   "数据里没有的一个字都不要编。输出 JSON，不要解释。")

_COMPOSE_PROMPT = """用户问题：「{question}」

从数据库查到的该用户真实数据（JSON）：
{data}

要求：
1. 用一两句话直接回答他问的那一点，口语一点
2. **金额、笔数、日期必须与数据完全一致**，数据里没有的数字一个都不许出现
3. 如果数据是空的，就如实说没有记录
4. **别把这段 JSON 的存在说出来。** 不许出现「数据里」「字段」「JSON」
   「记录显示」这类词——用户问的是他自己的事，不是来看一份查询结果的
5. **状态一律说中文。** pending 是待处理、confirmed 是已确认、
   cancelled 是已取消、rejected 是已拒绝、done 是已完成、
   rent 是租金、deposit 是押金。英文原样吐出来等于没翻译
6. 他问的是「通过了吗」这种是非问句，就先给是或否，再补一句情况，
   别一上来罗列所有条目

只输出：{{"answer": "回答正文"}}"""


def select_tool(question, provider=None):
    """选一个工具。返回 ``(tool, source, rule_tool)``。

    `source` 取值 ``rules`` / ``llm`` / ``rules_fallback``，
    写进日志后答辩时能展示「规则说什么、模型改成了什么」。
    """
    ruled = classify_ask.classify(question)
    rule_tool = ruled["topic"] if ruled["kind"] == classify_ask.KIND_ACCOUNT else "none"

    if provider is None or not getattr(provider, "available", False):
        return rule_tool, "rules", rule_tool

    tools = "\n".join("- %s：%s" % (name, desc) for name, desc in TOOL_SPECS)
    hint = ("要查「%s」" % account.TOPIC_LABELS.get(rule_tool, rule_tool)
            if rule_tool != "none" else "不需要查个人数据")
    try:
        data = provider.complete_json(
            _SELECT_SYSTEM,
            _SELECT_PROMPT.format(question=question, tools=tools, rule_hint=hint))
    except LLMUnavailable:
        return rule_tool, "rules_fallback", rule_tool
    except Exception:
        return rule_tool, "rules_fallback", rule_tool

    if not isinstance(data, dict):
        return rule_tool, "rules_fallback", rule_tool

    picked = str(data.get("tool", "")).strip()
    valid = {name for name, _ in TOOL_SPECS}
    if picked not in valid:
        return rule_tool, "rules_fallback", rule_tool

    return picked, "llm", rule_tool


def compose(provider, question, topic, data, fallback):
    """把查到的真实数据交给大模型组织成回答。

    返回 ``(message, source, note)``。

    **数字校验和 RAG 那边是同一条规则**：回答里出现的每个数字都必须能在
    查到的数据里找到。客服把「还欠 2400」说成「还欠 4200」，
    比说「我查不到」严重得多——用户会照着这个数字去转账。
    校验不过就退回规则拼出来的那句话，它由代码直接格式化，不可能说错数。
    """
    if provider is None or not getattr(provider, "available", False):
        return fallback, "rules", "未配置大模型"

    payload = json.dumps(data, ensure_ascii=False, default=str)
    # 数据可能很长（比如 5 条账单），截断避免 Token 失控（非功能需求 N-08）
    if len(payload) > 2000:
        payload = payload[:2000] + " …（已截断）"

    try:
        raw = provider.complete_json(
            _COMPOSE_SYSTEM,
            _COMPOSE_PROMPT.format(question=question, data=payload))
    except LLMUnavailable:
        return fallback, "rules", "大模型不可用"
    except Exception:
        return fallback, "rules", "大模型调用异常"

    if not isinstance(raw, dict):
        return fallback, "rules", "输出不是 JSON 对象"

    message = str(raw.get("answer", "")).strip()
    if not message:
        return fallback, "rules", "回答为空"
    if len(message) > 300:
        return fallback, "rules", "回答过长"

    # 数字必须在数据里出现过。比对的是原始 JSON 文本——
    # 金额可能是 2400 也可能是 2400.0，两种写法都在里面。
    allowed = numbers_in(payload) | numbers_in(fallback)
    for number in numbers_in(message):
        if number not in allowed:
            return (fallback, "rules",
                    "回答里的数字「%s」在数据中找不到" % number)

    leak = _leaked(message)
    if leak:
        return fallback, "rules", "回答里漏出了「%s」" % leak

    return message, "llm", ""


# 不该出现在回答里的词。分两类，都是实测撞出来的：
#
# - **实现细节**：模型答过「数据里能看到有状态是 confirmed 的预约」。
#   用户问的是他自己的事，不是来看一份查询结果的；一提「数据」「字段」，
#   这句话立刻从「客服在回答我」变成「系统在念它的数据库」。
# - **英文枚举**：pending / confirmed / rent 直接吐出来等于没翻译。
#   提示词里已经交代过，但提示词是软约束——照这个项目对数字的做法，
#   说漏了就退回规则那句，它由代码格式化，不可能说错。
_LEAK_WORDS = (
    "数据里", "数据中", "字段", "JSON", "json", "记录显示", "查询结果",
    "pending", "confirmed", "cancelled", "rejected", "processing",
    "available", "unpaid", "overdue", "deposit",
)


def _leaked(message):
    """回答里有没有漏出实现细节或英文枚举。返回漏出的那个词。

    「rent」「done」「paid」这类太短的词故意不收：中文句子里嵌一个
    「rent」确实该拦，但「current」「different」这些正常词里也含它，
    误伤的代价比漏掉一次高。真正常见的几个长词已经覆盖住了。
    """
    lowered = message.lower()
    for word in _LEAK_WORDS:
        if word.islower() and word.isascii():
            if word in lowered:
                return word
        elif word in message:
            return word
    return ""


def run(question, token, backend, provider=None, tool=None):
    """事务型问答的完整链路。返回 ``(status, message, detail)``。

    `tool` 由统一路由（`tools/router.py`）算好传进来时直接用，
    省掉一次重复的大模型调用——路由那一步已经在全部能力里挑过一遍了。
    """
    if tool:
        source, rule_tool = "router", tool
    else:
        tool, source, rule_tool = select_tool(question, provider=provider)
    detail = {"tool": tool, "rule_tool": rule_tool, "select_source": source}

    if tool == "none":
        detail["note"] = "判定为通用问题，转由知识检索回答"
        return "knowledge", "", detail

    if not token:
        detail["note"] = "需要登录才能查个人数据"
        return ("need_login",
                "这个要看你自己的记录，先登录一下我才查得到。", detail)

    status, fallback, data = account.lookup(backend, token, tool)
    detail["backend_status"] = status
    if status != "ok":
        return status, fallback, detail

    message, answer_source, note = compose(
        provider, question, tool, data, fallback)
    detail["answer_source"] = answer_source
    detail["answer_note"] = note
    detail["data"] = data
    return "account", message, detail
