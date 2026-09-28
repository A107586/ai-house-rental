"""统一工具路由：让大模型在全部能力里挑一个，规则兜底。

## 为什么要有这一层

原先动作识别靠一张关键词表。表写得再全也追不上口语——实测这几句
一条都没命中：

    我要看房第一个              要的是「约看房」「帮我约」
    预约明天上午看房第三套        要的是「预约看房」四个字连着
    查我目前的租约              「查」不在祈使词表里

用户于是被丢回旧的子智能体，得到一句「请到具体房源页面点预约看房」——
在一个号称能代办的助手里，这是最伤人的一种回答。

关键词表的问题不是写得不够多，是**方向错了**：中文口语的说法是开放的，
穷举不完。该做的是把能力清单交给大模型，让它判断用户想干什么——
这也正是千问那类助手的做法。

## 清单里有什么

网站上用户能做的事，在这里都是一个工具名，模型从同一张清单里挑：

    action   20 项  交房租、约看房、报修、续租、退租、收藏、上下架、
                    改手机号……（一律要确认）

**管理员的动作故意不放进来。** 审房源、封号、发公告都留在管理后台：
一是这些动作和租客那十几项混在同一张清单里，模型更容易选错；
二是「助手能不能封号」这种问题，答案越简单越好——它不能。
管理员照样可以用助手查数据、问规则，只是写操作走后台页面。
    query    13 项  账单、租约、工单、预约、收藏、足迹、消息、经营数据……（只读）
    service   3 项  找房、推荐、合同审阅（整条子链路）
    none            平台规则问答与闲聊，交给知识检索

分三类不是为了好看，是因为**这三类的风险边界不一样**：action 会写库，
所以必须先出确认卡片；query 与 service 只读，直接执行。

## 规则不是被取代，是变成了参考答案与护栏

    规则先跑 → 结论作为「参考答案」写进提示词 → 大模型给最终结果
             → 硬性护栏复核 → 采纳或退回规则

与 `app/fusion.py` 的思路一致。四条护栏：

1. **大模型不可用时照常工作**（N-07）。没有 Key、网络不通、返回超时，
   一律采用规则结论，整条链路不受影响。项目全部自动化测试跑在这个模式下。
2. **疑问句永远不能变成操作。** 这是最重要的一条：用户问「怎么退租」，
   大模型若判成 end_contract，这里改回知识型。多弹一张卡片是惊吓，
   而退租不可撤销——这个方向的错误代价不对称，只能一边倒地保守。
3. **只认清单里的名字。** 模型会编工具名，编出来的一律退回规则结论。
4. **模型说「都不是」，不能抹掉规则的明确结论。** 第 2 条的镜像：
   规则判出 pay_bill、模型回 none，那句「帮我把这个月房租交了」就会
   掉进知识检索，答成「没查到相关规定」——一条明确的指令被答成了查不到。
   模型可以改判成别的工具，但不能把有结论的一句话降级成没听懂。

## 为什么最坏情况可以接受

即使模型选错了动作，用户看到的也只是一张多余的确认卡片，点「取消」就没了——
**执行永远在用户确认之后**。这条两阶段边界让「让模型来判断」这件事
从「危险」变成了「可以试」。
"""

from app import memory
from app.agents.support.tools import actions, classify_ask
from app.llm.base import LLMUnavailable

KIND_ACTION = "action"
KIND_QUERY = "query"
KIND_SERVICE = "service"
KIND_KNOWLEDGE = "knowledge"

# 工具清单里给大模型看的说明。写法刻意统一成「用户想…时选它」，
# 因为模型判断的是**用户的意图**，不是这个工具的技术实现。
ACTION_HINTS = {
    "pay_bill": "用户要交房租／缴账单",
    "book_viewing": "用户要预约看房、要去看某一套房",
    "cancel_viewing": "用户要取消已有的看房预约",
    "reschedule_viewing": "用户要把已经约好的看房**换个时间**，"
                          "不是新约一条，也不是单纯取消",
    "submit_repair": "用户要报修，说了某处坏了、不好使、断电漏水之类",
    "renew_contract": "用户要续租",
    "end_contract": "用户要退租、解约、搬走",
    "favorite_house": "用户要收藏某套房源",
    "unfavorite_house": "用户要取消收藏",
    "read_all_messages": "用户要把站内消息全部标记为已读",
    "clear_history": "用户要清空浏览足迹",
    "submit_review": "用户要给租约打分或写评价",
    "handle_appointment": "房东要确认或拒绝一条看房预约",
    "update_repair": "房东要把工单标记为已完成",
    "toggle_house": "房东要把房源上架或下架",
    "remind_tenant": "【房东】要提醒、催欠租的租客交房租",
    "publish_house": "【房东】要发布一套新房源",
    "sign_contract": "【房东】要给某套房源和某个租客发起签约",
    "edit_house": "【房东】要修改已有房源的某一项（租金、标题、地址等）",
    "activate_contract": "租客要确认签约、让待生效的合同生效",
    "verify_request": "房东要给自己的房源提交核验申请",
    "update_profile": "用户要改自己的手机号／联系方式",
}

QUERY_HINTS = {
    "bill": "查用户自己的账单：欠多少、几笔、什么时候到期",
    "contract": "查用户自己的租约：租了哪几套、合同号、租期、状态",
    "repair": "查用户自己的报修工单进度",
    "booking": "查用户自己的看房预约",
    "favorite": "查用户收藏了哪些房源",
    "history": "查用户浏览过哪些房源",
    "message": "查用户的站内消息、有几条未读",
    "review": "查用户有哪些租约还没评价",
    "handoff": "查用户提交的转人工工单进度",
    "profile": "查用户的账号信息与身份",
    "house": "【房东】查他自己发布的房源",
    "income": "【房东】查经营数据：收入、出租率、待收",
    "tenant": "【房东】查名下租客",
}

# 服务型能力：不是一次取数，而是整条子链路（找房要解析条件再检索、
# 推荐要先建画像）。这几项原先只能靠 supervisor 的意图关键词命中，
# 现在一并交给同一次路由判断——**网站的每个功能都在这张清单里**，
# 模型挑哪个就走哪条链。
#
# 经营分析报告（AI-05）**故意不在这里**。那份报告读的是全平台统计，
# 而聊天入口不校验管理员身份，放进来等于任何人问一句就能拿到平台经营数据。
# 房东要看自己的经营情况，走 query 里的 income——那条按令牌取数，只看得到自己的。
SERVICE_HINTS = {
    "search_house": "用户在找房，说了地段、价格、户型、朝向等条件",
    "recommend_house": "用户让你推荐房源、问有什么合适的、不给具体条件",
    "contract_review": "用户要你审阅合同正文：有没有坑、条款有没有风险",
    "describe_house": "用户指着已经列出来的某一套问「介绍一下」「怎么样」"
                      "「值不值」——他要的是判断，不是再看一遍清单",
    "house_reviews": "用户问某一套房的租后评价、评分、口碑、有没有人住过",
    "landlord_reviews": "用户问某一套房的房东怎么样、房东口碑、房东靠不靠谱",
    "compare_houses": "用户要在已经列出来的两套之间比较、问哪个好、哪个划算",
}

# 「介绍下第一套」「第一套和第二套哪个好」——实测这两句都被判成了找房，
# 于是把上一轮的列表原样又列了一遍。**用户已经看到列表了**，
# 他再开口要的是判断。这两条规则先认，模型仍可推翻。
_COMPARE_WORDS = ("对比", "比较", "哪个好", "哪套好", "哪个划算", "哪套划算",
                  "哪个值", "选哪个", "选哪套", "哪个更", "哪套更", "差别",
                  "区别")
# 「看看」单独出现不算——「看看有什么房源」是找房。但它要先过
# `_refers_to_house`，句子里已经指着某一套了，这时的「看看第六套」
# 只可能是要讲解。界面上那句「还有 5 套」旁边用户最常打的就是这句。
_DESCRIBE_WORDS = ("介绍", "讲讲", "说说", "详细说", "怎么样", "咋样",
                   "好不好", "值不值", "值得", "看看", "多讲", "详情")

# 指代：这句话在指着列表里的某一套。没有指代就不是讲解/对比——
# 「哪个好」也可能是在问「押一付三和押一付六哪个好」这种规则问题。
#
# 序号不再写死。**这里曾经只列到「第五」**，而助手自己会说
# 「我最推荐第 6 套」；用户回一句「看看第六套」，这张表认不出指代，
# 于是整句被当成一次新的找房请求，回一句「没听懂你的找房需求」。
# 同一张写死的表在 memory 和 house.py 里各有一份，三处都得改——
# 所以统一改成调 `memory.ordinal_indexes()` 解析数字，只留非数字的说法。
_REFERS = ("这套", "那套", "这个房", "那个房", "这间", "上面", "1号", "2号")


def _refers_to_house(raw):
    return bool(memory.ordinal_indexes(raw)) or any(w in raw for w in _REFERS)

# 问的是租后评价。**和「介绍一下」是两件事**：介绍出自房源字段，
# 评价出自住过的人，后者才是他真正拿不到、也最想要的那部分。
_REVIEW_HOUSE_WORDS = ("评价", "评分", "口碑", "住过", "有人租过",
                       "好评", "差评", "打分", "几分", "靠谱", "靠不靠谱",
                       "人怎么样")

# 「帮我看看合同有没有坑」——既有「合同」又有「帮我看看」，
# 不单独认出来就会被判成「查我的租约」，答出一份合同清单，
# 而用户要的是条款与风险点。规则先认一次，模型可以推翻。
_REVIEW_WORDS = ("有没有坑", "审阅", "有没有问题", "风险", "条款",
                 "看看合同", "帮我看合同", "分析合同", "解读合同")


def _rule_service(question):
    """规则能认出来的服务型能力。认不出返回空串。

    找房与推荐交给 supervisor 的意图分类，那张表已经调了很久，
    这里再判一遍只会两处打架。这里只认三件那张表管不着的事：
    合同审阅、讲解某一套、对比两套。
    """
    raw = (question or "").strip()
    if "合同" in raw and any(word in raw for word in _REVIEW_WORDS):
        return "contract_review"

    # 讲解与对比都必须**指着某一套**。少了这个条件，「押一付三和
    # 押一付六哪个好」这种规则问题会被拽进对比，而它该走知识库。
    if _refers_to_house(raw):
        # **评价要排在讲解前面。** 「第一套评价怎么样」里的「怎么样」
        # 是讲解词，先判讲解就会答出一段户型朝向的推销词——
        # 用户问的是别人住过之后怎么说，那是这套房**唯一不由房东撰写**
        # 的一段信息，拿别的东西顶替它等于把问题吃掉了。
        if any(word in raw for word in _REVIEW_HOUSE_WORDS):
            # 问的是房东还是房子？**同一句「评价怎么样」，
            # 主语不同答案完全不同**——房子好房东差是很常见的组合
            if any(word in raw for word in ("房东", "业主")):
                return "landlord_reviews"
            return "house_reviews"
        if any(word in raw for word in _COMPARE_WORDS):
            return "compare_houses"
        if any(word in raw for word in _DESCRIBE_WORDS):
            return "describe_house"
    # 两个序号同时出现，本身就是在要对比：「第一套第二套哪个好」
    # 里「哪个好」已经被上面吃掉了，但「第一套和第二套呢」没有动词。
    if len(memory.ordinal_indexes(raw)) >= 2:
        return "compare_houses"
    return ""


_SYSTEM = "你是租房平台助手的路由器，只输出 JSON，不要任何解释。"

_PROMPT = """判断用户这句话想做什么，从下面选**一个**。

用户说：「{question}」
{context}

## 要他去做一件事（action）
{action_list}

## 查他自己的数据（query）
{query_list}

## 帮他用平台的某项服务（service）
{service_list}

## 其它（none）
问平台规则（押金怎么退、水电费怎么算、能签几年）、闲聊，都选 none。

规则引擎的初步判断：{hint}
（仅供参考，你可以推翻它。）

判断要点：
- **提问不是指令**。「怎么退租」「退租要打扫吗」是在问规则，选 none；
  「我要退租」「帮我退租」才是 action。这一条最重要，宁可选错成 none。
- 「我要看房第一个」「预约明天上午看房第三套」是 book_viewing，
  用户已经在要求你去约了。
- 「查我的租约」「我租了几套」是 query 里的 contract，不是 action。
- 「帮我找中山区两室一厅」是 search_house，「有合适的推荐吗」是
  recommend_house；「帮我看看合同有没有坑」是 contract_review，
  而「能签几年合同」是在问规则，选 none。
- 用户只回了「都可以」「第一个」「好的」这种半句话，说明在接上文，
  按上文的意图选。

只输出：{{"kind": "action|query|service|none", "name": "上面的名字或 none",
"why": "不超过15字"}}"""


def _catalog(hints):
    return "\n".join("- %s：%s" % (name, desc) for name, desc in hints.items())


def route(question, provider=None, history=None, pending=None):
    """决定这句话该走哪条路。

    返回 ``{"kind", "name", "source", "rule": {...}}``，
    `source` 取 ``rules`` / ``llm`` / ``rules_fallback``，写进日志后
    答辩时能展示「规则说什么、模型改成了什么」。
    """
    rule_action = actions.detect(question)
    ruled = classify_ask.classify(question)
    rule_topic = (ruled["topic"]
                  if ruled["kind"] == classify_ask.KIND_ACCOUNT else "")
    rule_service = _rule_service(question)

    if rule_action:
        rule = {"kind": KIND_ACTION, "name": rule_action}
    elif rule_service:
        # 服务型排在查询前面：「帮我看看合同有没有坑」两条都会命中，
        # 而用户要的是审阅，不是一份合同清单。
        rule = {"kind": KIND_SERVICE, "name": rule_service}
    elif rule_topic:
        rule = {"kind": KIND_QUERY, "name": rule_topic}
    else:
        rule = {"kind": KIND_KNOWLEDGE, "name": "none"}

    if provider is None or not getattr(provider, "available", False):
        return dict(rule, source="rules", rule=dict(rule))

    hint = {
        KIND_ACTION: "要执行 %s",
        KIND_QUERY: "要查 %s",
        KIND_SERVICE: "要用 %s",
        KIND_KNOWLEDGE: "是普通问题（%s）",
    }[rule["kind"]] % rule["name"]

    context = ""
    if pending and pending.get("action"):
        context = "（上一轮正在办：%s，还没做完）" % pending["action"]
    elif history:
        context = "（上一句是：%s）" % history[-1][:40]

    try:
        data = provider.complete_json(_SYSTEM, _PROMPT.format(
            question=question, context=context, hint=hint,
            action_list=_catalog(ACTION_HINTS),
            query_list=_catalog(QUERY_HINTS),
            service_list=_catalog(SERVICE_HINTS)))
    except LLMUnavailable:
        return dict(rule, source="rules_fallback", rule=dict(rule))
    except Exception:
        return dict(rule, source="rules_fallback", rule=dict(rule))

    if not isinstance(data, dict):
        return dict(rule, source="rules_fallback", rule=dict(rule))

    kind = str(data.get("kind", "")).strip()
    name = str(data.get("name", "")).strip()

    if kind == KIND_ACTION and name in ACTION_HINTS:
        # **护栏：疑问句不能变成操作。**
        #
        # 这个方向的错误代价是不对称的——多弹一张退租确认卡是惊吓，
        # 而用户真按了就收不回来。所以只要句子读起来是在提问，
        # 无论模型多有把握，都改回知识型。
        if not actions.detect(question) and _looks_like_question(question):
            return {"kind": KIND_KNOWLEDGE, "name": "none", "source": "llm_guarded",
                    "rule": dict(rule),
                    "note": "大模型判为操作，但句子是疑问句，已改回知识型"}
        # 「提醒我交房租」要的是欠多少、哪天到期，不是现在就付
        if name == "pay_bill" and actions.reminds_me(question):
            return {"kind": KIND_QUERY, "name": "bill", "source": "llm_guarded",
                    "rule": dict(rule),
                    "note": "大模型判为缴费，但用户是要提醒，改为查账单"}
        return {"kind": KIND_ACTION, "name": name, "source": "llm",
                "rule": dict(rule)}

    if kind == KIND_QUERY and name in QUERY_HINTS:
        return {"kind": KIND_QUERY, "name": name, "source": "llm",
                "rule": dict(rule)}

    # 服务型不需要疑问句护栏：三条都是只读的，最坏结果是多给一份房源列表。
    if kind == KIND_SERVICE and name in SERVICE_HINTS:
        return {"kind": KIND_SERVICE, "name": name, "source": "llm",
                "rule": dict(rule)}

    if kind in ("none", KIND_KNOWLEDGE) or name == "none":
        # **护栏二的镜像：模型说「都不是」，不能把规则的明确结论抹掉。**
        #
        # 实测撞出来的：用假模型跑「帮我把这个月房租交了」，规则判得好好的
        # pay_bill，模型回了 none，于是这句话掉进知识检索，
        # 用户拿到一句「暂时没有查到相关规定，这个我不敢乱说」——
        # **一条明确的指令，被答成了查不到。**
        #
        # 两个方向的代价还是不对称的，只是这次反过来：
        # 采纳规则，最坏是多一张确认卡，点「取消」就没了（与护栏二同一个理由）；
        # 采纳 none，用户直接失去这个功能，而且看不出为什么。
        #
        # 所以模型可以**改判**（把 pay_bill 改成 bill、改成别的动作），
        # 但不能把有结论的一句话降级成「没听懂」。
        if rule["kind"] != KIND_KNOWLEDGE:
            return dict(rule, source="llm_kept_rule", rule=dict(rule),
                        note="大模型判为普通问题，但规则有明确结论，按规则走")
        return {"kind": KIND_KNOWLEDGE, "name": "none", "source": "llm",
                "rule": dict(rule)}

    # 模型编了个不存在的名字，退回规则
    return dict(rule, source="rules_fallback", rule=dict(rule))


def _looks_like_question(text):
    """这句话读起来是不是在提问。

    比 `actions._ASKING` 更宽：那张表是给规则用的、要精确；
    这里是给护栏用的、宁可宽。宽的代价只是把一句指令当成了提问，
    用户再说一遍就是；窄的代价是弹出一张不该弹的高风险确认卡。
    """
    raw = (text or "").strip()
    if not raw:
        return False
    # 要一份文书（「帮我写一份退租申请书」）不是要去办这件事，
    # 对护栏来说和提问是一回事：都不该变成确认卡。
    if any(word in raw for word in actions._DOCUMENT):
        return True
    if any(word in raw for word in actions._ASKING):
        return True
    if (any(word in raw for word in actions._SOFT_ASKING)
            and not any(verb in raw for verb in actions._DO_VERBS)):
        return True
    # 疑问语气词结尾，且没有祈使词
    if raw.endswith(("吗", "呢", "吧", "?", "？")):
        return not any(verb in raw for verb in actions._DO_VERBS)
    return False
