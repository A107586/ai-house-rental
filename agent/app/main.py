"""智能体服务 HTTP 入口（FastAPI）。

前端与业务后端通过这些接口访问智能体能力，接口清单见
《需求分析说明书》第 6 节。
"""

import re

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app import fusion, memory, supervisor
from app.agents.booking import agent as booking
from app.agents.contract import agent as contract
from app.agents.describe import agent as describe
from app.agents.handoff import agent as handoff
from app.agents.property_finder import agent as property_finder
from app.agents.recommend import agent as recommend
from app.agents.report import agent as report
from app.agents.repair import agent as repair
from app.agents.support import agent as support
from app.agents.support.tools import actions
from app.agents.support.tools import greeting
from app.agents.support.tools import router as support_router
from app.agents.viewing import agent as viewing
from app.agents.support.tools import actions as support_actions
from app.agents.support.tools import forms as support_forms
from app.backend import client as backend_client
from app.config import settings
from app.llm.providers import build_provider

app = FastAPI(
    title="房屋租赁智能体服务",
    description="Supervisor 总调度 + 子智能体 + 工具的三层架构",
    version="0.1.0",
)

# 前端开发服务器在 8081，用户网页端在 8080，均需跨域访问
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8080", "http://localhost:8081",
                   "http://127.0.0.1:8080", "http://127.0.0.1:8081"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

provider = build_provider(settings)


class SearchRequest(BaseModel):
    text: str
    page: int = 1
    limit: int = 10


class ChatRequest(BaseModel):
    text: str
    # 客服代办的待办状态，见 SupportRequest.pending
    pending: dict | None = None
    # AI-09 多轮会话。登录用户的历史存在后端，用 session_id 取；
    # 未登录用户由前端把自己说过的话带上来，换设备记不住而已。
    session_id: str = ""
    token: str = ""
    history: list[str] = []
    # 上一轮结果的房源编号，用于消解「第二套」「那套」这类指代
    last_house_nos: list[str] = []


class SupportRequest(BaseModel):
    text: str
    # 上一轮没做完的代办：问了哪个槽、已填了什么、或已摆出的确认卡片。
    # **服务端不存会话态**，它随响应发给前端、下一轮再带回来。
    pending: dict | None = None
    # 事务型问题（「我还欠多少房租」）要拿令牌去后端查这个人自己的数据
    token: str = ""
    session_id: str = ""
    # 多轮：用户自己说过的话，用于「那它多久到账」这类指代消解
    history: list[str] = []
    house_no: str = ""


class RepairRequest(BaseModel):
    text: str
    house_no: str = ""
    xingming: str = ""
    shouji: str = ""
    submit: bool = True


class RecommendRequest(BaseModel):
    token: str = ""
    limit: int = 6


class ContractRequest(BaseModel):
    text: str


class BookingRequest(BaseModel):
    text: str
    house_no: str
    token: str = ""
    zhanghao: str = ""
    xingming: str = ""
    shouji: str = ""


@app.get("/")
def root():
    """根路径引导。本服务只提供接口，没有页面。"""
    return {
        "service": "房屋租赁智能体服务",
        "提示": "本服务只提供接口，没有网页。网站入口在 http://localhost:5173",
        "接口文档": "http://localhost:8000/docs",
        "健康检查": "http://localhost:8000/health",
    }


@app.get("/health")
def health():
    """健康检查。返回内容**不含任何密钥**。"""
    return {"status": "ok", "config": settings.describe()}


@app.post("/agent/search")
def agent_search(payload: SearchRequest):
    """AI-01 自然语言找房。"""
    return property_finder.run(
        payload.text, page=payload.page, limit=payload.limit, provider=provider)


@app.post("/agent/parse")
def agent_parse(payload: SearchRequest):
    """只把一句话解析成筛选条件，**不查房源**（K-06）。

    为什么要和 `/agent/search` 分开：

    网站原先只有 AI 找房这一条检索入口，条件识别和取数捆在一起。
    结果是两个问题——**智能体一挂，整站就搜不了房**；而且用筛选器会
    把 AI 的结果整个清空，用户先用一句话搜出来、再想点一下「有电梯」
    缩小范围，这一下就退回了全量列表。

    拆开之后职责就清楚了：智能体只负责**把人话翻译成条件**，
    翻完交给网站，取数统一走后端 `/api/houses` 一条路。这样
    条件看得见、可继续修改，智能体不可用时也只是回到手动选条件——
    从「唯一入口」降级成「一个便利入口」。

    返回里带着 `source` 和 `divergence`：规则说了什么、大模型改了什么，
    答辩时能当场展示，也方便排查「它为什么这么理解我」。
    """
    filters, features, source, divergence = property_finder.understand(
        payload.text, provider=provider)
    return {
        "ok": bool(filters or features),
        "filters": filters,
        "features": features,
        "source": source,
        "source_label": fusion.describe_source(source),
        "divergence": divergence,
    }


@app.post("/agent/route")
def agent_route(payload: ChatRequest):
    """仅做意图识别，不执行。用于前端预判与答辩时展示决策过程。"""
    result = supervisor.classify(payload.text, provider=provider)
    result["label"] = supervisor.label(result["intent"])
    return result


@app.post("/agent/booking")
def agent_booking(payload: BookingRequest):
    """AI-06 智能预约看房。"""
    return booking.run(
        payload.text,
        house_no=payload.house_no,
        applicant={"zhanghao": payload.zhanghao,
                   "xingming": payload.xingming,
                   "shouji": payload.shouji},
        token=payload.token,
        backend=backend_client,
    )


@app.post("/agent/repair")
def agent_repair(payload: RepairRequest):
    """AI-04 报修工单智能分派。"""
    return repair.run(
        payload.text,
        house_no=payload.house_no or None,
        applicant={"xingming": payload.xingming, "shouji": payload.shouji},
        provider=provider,
        backend=backend_client,
        submit=payload.submit,
    )


@app.post("/agent/contract/review")
def agent_contract(payload: ContractRequest):
    """AI-02 合同智能审阅。"""
    return contract.run(payload.text, provider=provider)


@app.post("/agent/recommend")
def agent_recommend(payload: RecommendRequest):
    """AI-07 房源智能推荐。

    需要用户令牌才能读到「这个人」的浏览足迹与收藏。
    智能体不持有身份，令牌由前端带来、原样转发给业务后端。
    """
    return recommend.run(token=payload.token, limit=payload.limit,
                         backend=backend_client)


class DescribeRequest(BaseModel):
    # 结构化字段，全部由前端从发布表单原样带过来。
    # 智能体只负责组织语言，不去后端查任何东西——
    # 房源还没提交，库里根本没有它。
    district: str = ""
    community: str = ""
    layout: str = ""
    area: float = 0
    rent_price: float = 0
    rent_type: str = ""
    floor: str = ""
    orientation: str = ""
    tags: list[str] = []


class HandoffRequest(BaseModel):
    text: str
    # 带上令牌就能把浏览足迹一并算进摘要——「他嘴上说的和实际在看的
    # 一致吗」是中介最想知道的一件事。没有令牌也能用，只是少一行
    token: str = ""


class ViewingSummaryRequest(BaseModel):
    text: str


@app.post("/agent/handoff")
def agent_handoff(payload: HandoffRequest):
    """J-09-a 需求摘要交接。

    把租客的一句话整理成一份摘要，随咨询一并发给中介，
    **省掉「请您再描述一遍需求」那一轮**。
    """
    viewed = []
    if payload.token:
        try:
            viewed = backend_client.list_history(payload.token)
        except Exception:
            # 足迹取不到就不要了。它是锦上添花的一行，
            # 不该让整份摘要一起失败
            viewed = []
    return handoff.run(payload.text, viewed=viewed, provider=provider)


@app.post("/agent/viewing-summary")
def agent_viewing_summary(payload: ViewingSummaryRequest):
    """J-09-c 带看纪要整理。口述几句 → 结构化纪要。"""
    return viewing.run(payload.text, provider=provider)


@app.post("/agent/describe")
def agent_describe(payload: DescribeRequest):
    """AI-08 房源描述自动生成。"""
    return describe.run(payload.model_dump(), provider=provider)


class ReportRequest(BaseModel):
    # 管理员令牌。经营分析里的钱数要管理员身份才取得到，
    # 没带令牌时后端只回房源侧的公开数字，报告会照实说「取不到经营数据」
    token: str = ""


@app.post("/agent/report")
def agent_report(payload: ReportRequest = ReportRequest()):
    """AI-05 经营分析报告。数字全部取自业务后端，不由大模型生成。"""
    try:
        stats = backend_client.stats(token=payload.token or None)
    except Exception:
        return {"intent": "report", "status": "error", "ok": False,
                "message": "统计服务暂时不可用，请稍后再试。"}
    return report.run(stats, provider=provider)


@app.post("/agent/support")
def agent_support(payload: SupportRequest):
    """AI-03 智能客服问答（RAG）。

    知识型问题走检索 + 可答性闸门 + 受约束生成；
    事务型问题（问「我自己的」账单/租约/工单/预约）走后端接口取真实数据。
    无依据时拒答，绝不编造。
    """
    result = support.run(payload.text, token=payload.token,
                         history=payload.history, provider=provider,
                         backend=backend_client, settings=settings,
                         house_no=payload.house_no, pending=payload.pending)
    # 日志写在接口层而不是智能体里：`run()` 保持无副作用，方便测试
    log_id = support.record(result, backend_client, token=payload.token,
                            session_id=payload.session_id)
    if log_id:
        # 前端点「有用/没用」时要用它回指这一轮
        result["log_id"] = log_id
    return result


class ConfirmRequest(BaseModel):
    """执行一个已被用户确认的操作。

    `name` 与 `params` 原样来自上一轮返回的确认卡片。
    """

    name: str
    params: dict = {}
    token: str = ""


@app.post("/agent/support/confirm")
def agent_support_confirm(payload: ConfirmRequest):
    """执行代办操作。**只有用户点过确认，前端才允许调这个接口。**

    对话主流程（`/agent/support`）永远走不到这里——它最多返回一张确认卡片。
    把执行与对话分成两个接口，是为了让「一句话就把钱划走」在架构上
    不可能发生，而不是靠提示词约束模型别乱来。

    参数走白名单校验：卡片是从前端回传的，多一个没见过的字段就拒绝，
    不原样透传给后端。至于伪造——不构成提权，所有写操作都用用户自己的
    令牌调后端受控接口，他打开网页也能做同样的事。
    """
    ok, message, data = support_actions.execute(
        payload.name, payload.params, payload.token, backend_client)
    return {
        "intent": "support",
        "status": "done" if ok else "failed",
        "ok": ok,
        "message": message,
        "action": payload.name,
        "data": data,
    }


class GreetingRequest(BaseModel):
    """开场白请求。令牌用来看这个人手头有什么没办完。"""

    token: str = ""
    # 用户此刻正在看的那套房。从房源详情页点进助手时带上，
    # 第一句就能是关于这套房的，用户不必再把编号打一遍。
    house_no: str = ""
    name: str = ""


@app.post("/agent/support/greeting")
def support_greeting(payload: GreetingRequest):
    """会话开场白：**先看这个人有什么没办完，再决定说什么。**

    原先是一段固定的自我介绍，讲完把「该问点什么」原样丢回给用户。
    购物软件的客服不这么做——点开就是「你有一笔待付款、一个包裹在派送」，
    每条后面跟一个能点的按钮。这一步把被动应答改成主动服务。

    **不调大模型**：内容完全由数据算出来，没有需要生成的部分，
    而面板弹出的那一瞬间等不起三四秒。
    """
    house = None
    if payload.house_no:
        try:
            house = backend_client.get("/api/houses/%s" % payload.house_no)
        except Exception:
            house = None

    try:
        return greeting.build(backend_client, payload.token,
                              house=house, name=payload.name)
    except Exception:
        # 开场白挂掉绝不能让面板打不开
        return {"message": greeting.FALLBACK,
                "chips": list(greeting.GUEST_CHIPS), "personalised": False}


@app.get("/agent/support/actions")
def agent_support_actions():
    """可代办操作的清单。答辩时用来说明「它到底能做哪些事」。"""
    return {
        "total": len(support_actions.PROPOSERS),
        "list": [{"name": name,
                  "keywords": keywords[:4]}
                 for name, keywords in support_actions.ACTION_KEYWORDS],
        "not_exposed": list(support_actions.NOT_EXPOSED),
    }


@app.get("/agent/support/knowledge")
def agent_support_knowledge():
    """知识库现状：片数、来源、是否启用了向量通道。

    调试与答辩用——被问「你们知识库里到底有什么」时能当场打开看。
    """
    kb = support.knowledge_base(backend=backend_client, settings=settings)
    index = kb.refresh()
    return {
        "total": len(index) if index else 0,
        "sources": kb.sources,
        "origin": kb.origin,
        "vector_enabled": bool(index and index.vector_enabled),
        "refreshed_at": kb.loaded_at,
        "items": [{"id": cid,
                   "title": (index.get(cid) or {}).get("title", ""),
                   "source": (index.get(cid) or {}).get("source", ""),
                   "category": (index.get(cid) or {}).get("category", "")}
                  for cid in (index.order if index else [])],
    }


@app.post("/agent/chat")
def agent_chat(payload: ChatRequest):
    """统一入口：先取上下文，再判意图，最后路由到对应子智能体。

    上下文的来源有两条，优先用后端存的（跨设备、可追溯），
    拿不到就用前端带上来的——未登录用户走的就是这条。
    """
    stored = backend_client.list_chat(payload.session_id, payload.token)
    if stored:
        past = [m["content"] for m in stored if m.get("role") == "user"]
    else:
        past = list(payload.history or [])

    # 历史槽位一律用规则重放，不为了重建上下文再调一遍大模型
    slots, feats = memory.replay(past)

    routed = supervisor.classify(payload.text, provider=provider)
    intent = routed["intent"]

    # 承接上文：已经在找房的会话里，「三千以内的」「再便宜点」这类
    # 半句话单独分类必然是 unknown，只有结合上文才讲得通。
    if intent == "unknown" and slots and memory.continues_search(payload.text):
        intent = "search"
        routed = dict(routed, intent="search", carried_over=True,
                      reason="承接上文的找房需求")

    result = _dispatch(payload, routed, intent, slots, feats)

    # 非找房的轮次也要把上下文带回去，否则界面上「当前记住」会被
    # 一句「帮我报修」清空，用户以为条件丢了
    result.setdefault("context_text", memory.describe(slots, feats))

    # 记录这一轮。写失败不影响回答，见 backend.log_chat
    if payload.session_id and payload.token:
        backend_client.log_chat(payload.session_id, "user", payload.text,
                                payload.token, intent=intent)
        backend_client.log_chat(payload.session_id, "assistant",
                                result.get("message", ""), payload.token,
                                intent=intent)
    return result


# 「你看着办」型的找房请求。这类话里一个可解析的条件都没有，
# 走找房只会回一句「说得具体一点」——而用户正是因为说不具体才来问的。
# 该给的是 AI-07 的推荐。
_WANT_RECOMMEND = ("推荐", "有什么合适", "有合适的", "帮我挑", "帮我选",
                   "给我挑", "有没有好的", "有啥好", "介绍几套", "看看有什么")


def _wants_recommendation(text):
    return any(word in (text or "") for word in _WANT_RECOMMEND)


def _recommend(payload, routed, slots, feats):
    """给一份推荐。给不出（没登录、足迹太少）时返回 None，交回原来的话术。"""
    picked = recommend.run(token=payload.token, limit=6, backend=backend_client)
    if picked.get("status") != "ok":
        return None
    picked["route"] = routed
    picked.setdefault("alternatives", [])
    picked["context_text"] = memory.describe(slots, feats)
    return picked


# 合同审阅要正文，而统一入口只拿得到一句话。引导到合同页是唯一诚实的回答——
# 编一段「你的合同没什么问题」比不回答更糟。
_CONTRACT_REDIRECT = ("这个得看你那份合同的正文。请到「我的租约」里打开合同，"
                      "点「智能审阅」，我会把关键条款和风险点列出来。")


def _serve(decision, payload, routed):
    """执行一项服务型能力。返回 None 表示「交回上面按意图走」。"""
    name = decision["name"]

    if name == "contract_review":
        return {
            "intent": "contract", "status": "need_content", "ok": False,
            "route": routed, "alternatives": [],
            "route_source": decision["source"],
            "message": _CONTRACT_REDIRECT,
        }

    if name == "recommend_house":
        # AI-07。推荐读的是「这个人」的足迹与收藏，没令牌就没画像，
        # 此时 recommend.run 自己会返回 fallback 文案，不用在这里判。
        result = recommend.run(token=payload.token, limit=6,
                               backend=backend_client)
        result["route"] = routed
        result["route_source"] = decision["source"]
        result.setdefault("alternatives", [])
        return result

    # search_house 交回去走原来的找房分支：那里要合并历史槽位
    # （「三千以内的」得接上上一轮的「中山区两室一厅」），这份上下文只有那里有。
    return None


def _dispatch(payload, routed, intent, slots, feats):
    text = payload.text

    # ---- 统一工具路由优先于意图分类 ----
    #
    # supervisor 会把「报修」判成 repair、「预约看房」判成 booking，
    # 各自路由到专门的子智能体。可那些子智能体只会分析与追问，
    # **没有确认卡片，也不会真的落库**——用户说「帮我报修，断电了」，
    # 得到的是一句「已识别为电路故障、紧急程度紧急」，然后就没有然后了。
    # 他追问「你不能像查房源一样帮我弹框办理吗」，问的正是这个。
    #
    # 所以先问一句「这句话是不是在要求做事」。是的话一律走客服的
    # 两阶段流程：出确认卡片、用户点了才执行。不是的话再按意图分流。
    #
    # 房源编号在这里就要解析好——「第一套帮我预约看房」里的「第一套」
    # 只有结合上一轮的搜索结果才还原得出来，而代办层拿不到那份上下文。
    resolved_house = memory.resolve_house(text, payload.last_house_nos)
    decision = support_router.route(text, provider=provider,
                                    history=list(payload.history or []),
                                    pending=payload.pending)

    # 上一轮的代办还没做完时，这一句多半是在回答助手的追问（「都可以」
    # 「第二笔」），一律先交回代办层，别让它被当成一次新的请求。
    pending_action = (payload.pending or {}).get("action")
    pending_ask = (payload.pending or {}).get("ask") or {}

    # **这一句是在回答上一个问题吗？** 是的话，无论它长得多像找房，
    # 都不能当成改口。
    #
    # 助手问「房子在哪个区？」并给了六个可点选项，用户答「中山区」——
    # 下面那条改口规则里区名赫然在列，于是每答一项就丢一次待办，
    # 十一项的表单一项都填不完。实测撞的就是这个。
    answers_pending = bool(
        pending_ask.get("options")
        and support_actions.pick_option(text, pending_ask["options"]))

    # 长表单里的字段值本来就长得像找房词（区、户型、面积），
    # 所以对表单只认**明确的祈使找房**，光出现一个区名不算。
    in_form = bool(support_forms.spec(pending_action or ""))
    explicit_search = bool(
        re.search(r"找房|房源|租.{0,3}(?:房|一套)|(?:中山|西岗|沙河口|甘井子|旅顺口|金州)区|"
                  r"[一二三四五六七八九两三]\s*室", text))
    if in_form:
        explicit_search = bool(re.search(r"帮我找|我要找|找房|搜一?下房", text))

    interrupts_pending = bool(
        pending_action and intent == "search" and explicit_search
        and not answers_pending)
    if interrupts_pending:
        pending_action = None

    # 服务型能力：找房 / 推荐 / 合同审阅。这三项不是一次取数，而是整条子链路，
    # 所以不进客服层，直接交给对应的子智能体。
    #
    # 放在代办层之前：「帮我看看合同有没有坑」既像审阅又像「查我的租约」，
    # 先认出审阅，才不会答出一份合同清单。
    # 明确的新服务请求应当打断旧待办。例如助手正在追问预约时间时，
    # 用户改口说“帮我找中山区两室一厅”，必须重新找房，不能把整句
    # 当作 viewing_time 填进上一笔预约。
    if (not pending_action or interrupts_pending) and \
            decision["kind"] == support_router.KIND_SERVICE:
        served = _serve(decision, payload, routed)
        if served is not None:
            return served
        # search_house：路由改了意图，继续往下走原来的找房分支
        intent = "search"
        routed = dict(routed, intent="search", carried_over=False,
                      reason="统一工具路由判为找房")

    if (not interrupts_pending and
            (decision["kind"] in (support_router.KIND_ACTION,
                                  support_router.KIND_QUERY) or pending_action)):
        result = support.run(text, token=payload.token,
                             history=list(payload.history or []),
                             provider=provider, backend=backend_client,
                             settings=settings, house_no=resolved_house,
                             house_nos=payload.last_house_nos,
                             pending=payload.pending, routing=decision)
        # 代办层认领了这句话（出卡片、追问、要登录、查到了数据、已取消）
        # 才返回；它自己也没辙时（status 是 refused 之类）继续按意图走，
        # 免得把一句普通问题堵死在代办分支里
        # **带着待办进来的，结果一律原样返回。**
        #
        # 原先只认几个「办成了」的状态，别的就往下走意图路由——于是
        # 用户在长表单里问一句「租金一般填多少」，客服层答完（refused
        # 也好 answered 也好）把待办挂在返回值上，这里却把整个结果丢掉，
        # 重新按意图跑了一遍，**待办和已填的九项一起没了**。
        # 只有这一层知道 pending 的存在，它的结论就是最终结论。
        if pending_action or result.get("status") in (
                "confirm", "need_slot", "need_login",
                "cancelled", "escalate", "failed", "account"):
            result["route"] = routed
            if resolved_house:
                result["house_no"] = resolved_house
            return result

    if intent == "search":
        # 本轮走完整的「规则与大模型融合」解析，再把结果合并到历史槽位上。
        #
        # 这里的解包长度必须与 `property_finder.understand()` 一致。
        # 它曾经返回三个值，融合层上线后变成四个（多了 divergence），
        # 而这个调用点当时漏改了——`/agent/chat` 里所有找房请求因此 500，
        # 前端显示成「智能体暂时联系不上」。
        # 单元测试没能发现，是因为它们直接测 `understand()` 与 `run()`，
        # 没有任何一条走过 `_dispatch`。补的回归测试见 test_dispatch.py。
        cur_filters, cur_features, source, divergence = \
            property_finder.understand(text, provider=provider)
        merged, features, notes = memory.apply_turn(
            slots, feats, text, filters=cur_filters, feats=cur_features)

        result = property_finder.run(text, provider=provider,
                                     filters=merged, features=features,
                                     source=source)

        # 一个条件都没说、而话里又在要推荐 —— 给推荐，别把问题推回去。
        if result.get("status") == "unparsed" and _wants_recommendation(text):
            picked = _recommend(payload, routed, slots, feats)
            if picked is not None:
                return picked

        result["route"] = routed
        result["context"] = merged
        result["context_text"] = memory.describe(merged, features)
        result["notes"] = notes
        # 大模型改了规则的哪些字段。答辩时能当场展示「规则说什么、
        # 模型改了什么」，比只给一个最终结果有说服力。
        result["divergence"] = divergence
        # 承接了上文才说明，免得单轮对话也啰嗦一句「已记住」
        if slots and merged != cur_filters:
            result["message"] = "（延续上文：%s）%s" % (
                result["context_text"], result.get("message", ""))
        return result

    if intent == "contract":
        # 合同类问题分两种，不能一律当成「要审阅我的合同」：
        #
        # - **规则型**：「能签几年」「电子合同有效吗」「合同签错了能改吗」，
        #   答案写在知识库里，直接答就好；
        # - **审阅型**：「帮我看看我这份合同有没有坑」，需要合同正文，
        #   而统一入口拿不到，只能引导到合同页。
        #
        # 原先不加区分地回「请到我的租约里打开合同」，问「能签几年合同」
        # 的用户会被莫名其妙地支去点审阅按钮。先让检索试一次，
        # 答得上来就答，答不上来才是真的需要正文。
        result = support.run(text, token=payload.token,
                             history=list(payload.history or []),
                             provider=provider, backend=backend_client,
                             settings=settings, house_no=resolved_house,
                             pending=payload.pending)
        # **「查我目前的租约」曾被支去点「智能审阅」。**
        # 那句话走到这里返回的是 account（查库成功），而这里只认 answered，
        # 于是掉进了下面的兜底。查库和检索都算答上了。
        if result.get("status") in ("answered", "account", "confirm",
                                    "need_slot", "need_login"):
            result["route"] = routed
            return result
        return {
            "intent": "contract", "status": "need_content", "ok": False,
            "route": routed, "alternatives": result.get("alternatives", []),
            "message": "这个得看你那份合同的正文。请到「我的租约」里打开合同，"
                       "点「智能审阅」，我会把关键条款和风险点列出来。",
        }

    if intent == "repair":
        # 统一入口只做分析预览，落库要走 /agent/repair 并带上 house_no
        result = repair.run(text, provider=provider, submit=False)
        result["route"] = routed
        result["alternatives"] = []
        return result

    if intent == "support":
        # AI-03。统一入口同样要把令牌带下去——用户在对话里问
        # 「我还欠多少房租」，走的是同一个入口。
        result = support.run(text, token=payload.token,
                             history=list(payload.history or []),
                             provider=provider, backend=backend_client,
                             settings=settings)
        result["route"] = routed
        return result

    if intent == "booking":
        # 统一入口本来拿不到房源编号，多轮会话下可以从「第二套」
        # 这类指代里还原出来，省掉用户回头去翻列表的一步
        house_no = memory.resolve_house(text, payload.last_house_nos)
        result = booking.run(text, house_no=house_no, taken=[])
        result["route"] = routed
        if house_no:
            result["house_no"] = house_no
            result["message"] = "（你说的是 %s）%s" % (
                house_no, result.get("message", ""))
        return result

    if intent == "unknown":
        # **意图识别不出来时兜底走客服检索，而不是直接说「没太理解」。**
        #
        # 意图关键词表再全也覆盖不了所有说法：「预期收入」「能签几年」
        # 这类问题一个关键词都不命中，可它们的答案很可能就写在知识库里。
        # 与其让用户撞一句「没太理解你的意思」，不如让检索去试一次——
        # 反正有可答性闸门兜底，检索不到照样会拒答，不存在编造的风险。
        #
        # 这一步把客服变成了整个对话入口的兜底通道：**能答的答上，
        # 答不上的给「猜你想问」和转人工**，而不是把用户丢在原地。
        result = support.run(text, token=payload.token,
                             history=list(payload.history or []),
                             provider=provider, backend=backend_client,
                             settings=settings, house_no=resolved_house,
                             pending=payload.pending)
        result["route"] = routed
        if result.get("status") == "refused":
            # 「帮我推荐几套」这类话一个强特征词都不命中，意图分类给不出结果，
            # 检索也没依据。但它的意思是清楚的，别用一句「没太理解」打发掉。
            if _wants_recommendation(text):
                picked = _recommend(payload, routed, slots, feats)
                if picked is not None:
                    return picked
            # 连检索都没依据，这才是真的没听懂。此时把找房与预约的
            # 例句一并给出——用户多半是想用这两个功能但没说对话。
            result["message"] = (
                "这个我没查到相关规定，也没太理解你想做什么。"
                "找房可以说「帮我找中山区两室一厅」，"
                "预约可以说「预约这周六下午看房」。")
        return result

    # 返回结构与各子智能体保持一致，前端只需处理一种形状
    return {
        "intent": intent,
        "label": supervisor.label(intent),
        "route": routed,
        "status": "not_implemented",
        "ok": False,
        "alternatives": [],
        "message": "「%s」能力尚在开发中。" % supervisor.label(intent),
    }
