"""智能体服务 HTTP 入口（FastAPI）。

前端与业务后端通过这些接口访问智能体能力，接口清单见
《需求分析说明书》第 6 节。
"""

import contextvars
import re
import time

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app import fusion, memory, supervisor
from app.agents.booking import agent as booking
from app.agents.contract import agent as contract
from app.agents.describe import agent as describe
from app.agents.handoff import agent as handoff
from app.agents.property_finder import agent as property_finder
from app.agents.property_finder.tools.parse_query import parse_query
from app.agents.recommend import agent as recommend
from app.agents.report import agent as report
from app.agents.repair import agent as repair
from app.agents.support import agent as support
from app.agents.support.tools import actions
from app.agents.support.tools import greeting
from app.agents.support.tools import house as house_tool
from app.agents.support.tools import pitch
from app.agents.support.tools import router as support_router
from app.agents.toolbox import catalog as tool_catalog
from app.agents.toolbox import planner as tool_planner
from app.agents.toolbox import runner as tool_runner
from app.llm import budget as llm_budget
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

# 本轮对话的 provider（带时间预算）。
#
# **用 ContextVar 而不是全局变量**：`provider` 是模块级单例，多个请求
# 共用它；把「本轮还剩多少时间」写进单例，两个并发请求会互相踩。
# ContextVar 天然按执行上下文隔离，FastAPI 把同步端点丢进线程池时
# 也会带着当前上下文一起过去。
#
# 默认是全局 provider：没有设过的调用方（其他端点）行为一点不变。
_turn_llm = contextvars.ContextVar("turn_llm", default=None)


def _llm():
    """这一刻该用哪个 provider。"""
    return _turn_llm.get() or provider


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
    # 上一轮结果的房源编号，用于消解「第二套」「那套」这类指代。
    # **顺序必须和用户屏幕上看到的一致**：序号是双方唯一的共同坐标
    last_house_nos: list[str] = []
    # 已经看过几套（翻页用）。前端把当前列表的长度回传上来——
    # 服务端不存会话态，「接着往下翻」这件事只能由持有列表的一方来说。
    seen: int = 0
    # 用户此刻正打开的那套房（从房源详情页唤起助手时才有）。
    #
    # 单开一个字段，而不是把它插进 last_house_nos 的队首。插队首会让
    # 整份列表的下标后移一位——屏幕上标着 1 的那套，助手数出来是第 2 套。
    # 「这套」指哪一套和「第几套」是两个问题，用两个字段回答。
    house_no: str = ""


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
    """健康检查。返回内容**不含任何密钥**。

    `llm` 那一段是实测撞出来的需求：中转站把某个模型下线之后，每次调用
    返回 503，系统按设计静默退回规则——功能全在，只是模型没参与，
    而这里当时只写着「大模型 + 规则」。**配置上了不等于调得通**，
    所以把这个进程里的调用成败也摆出来。
    """
    return {"status": "ok", "config": settings.describe(),
            "llm": provider.stats()}


@app.post("/agent/search")
def agent_search(payload: SearchRequest):
    """AI-01 自然语言找房。"""
    return property_finder.run(
        payload.text, page=payload.page, limit=payload.limit, provider=_llm())


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
        payload.text, provider=_llm())
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
    result = supervisor.classify(payload.text, provider=_llm())
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
        provider=_llm(),
        backend=backend_client,
        submit=payload.submit,
    )


@app.post("/agent/contract/review")
def agent_contract(payload: ContractRequest):
    """AI-02 合同智能审阅。"""
    return contract.run(payload.text, provider=_llm())


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
    return handoff.run(payload.text, viewed=viewed, provider=_llm())


@app.post("/agent/viewing-summary")
def agent_viewing_summary(payload: ViewingSummaryRequest):
    """J-09-c 带看纪要整理。口述几句 → 结构化纪要。"""
    return viewing.run(payload.text, provider=_llm())


@app.post("/agent/describe")
def agent_describe(payload: DescribeRequest):
    """AI-08 房源描述自动生成。"""
    return describe.run(payload.model_dump(), provider=_llm())


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
    return report.run(stats, provider=_llm())


@app.post("/agent/support")
def agent_support(payload: SupportRequest):
    """AI-03 智能客服问答（RAG）。

    知识型问题走检索 + 可答性闸门 + 受约束生成；
    事务型问题（问「我自己的」账单/租约/工单/预约）走后端接口取真实数据。
    无依据时拒答，绝不编造。
    """
    result = support.run(payload.text, token=payload.token,
                         history=payload.history, provider=_llm(),
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
    # **一轮对话的大模型时间预算。**
    #
    # 单次超时和单次调用的重试预算都设好之后，一轮仍然可能等 43.9 秒——
    # 因为一轮会调好几次模型，每次各守各的。这里给整轮套一个墙钟：
    # 到点之后所有子智能体拿到的都是「模型不可用」，
    # 各自降级到规则。**降级路径是完整可用的，让它提前接手
    # 比让用户干等强。**
    #
    # 包一层薄的、每轮新建，而不是把截止时间写进全局单例——
    # 后者会让两个并发请求互相踩。
    _turn_llm.set(llm_budget.for_turn(provider, settings.llm_turn_budget))

    stored = backend_client.list_chat(payload.session_id, payload.token)
    if stored:
        past = [m["content"] for m in stored if m.get("role") == "user"]
    else:
        past = list(payload.history or [])

    # 历史槽位一律用规则重放，不为了重建上下文再调一遍大模型
    slots, feats = memory.replay(past)

    # **意图分类先只用规则。**
    #
    # 统一路由马上还要判一次「这句话要干什么」，两次做的是重叠的事。
    # 而路由认领了这一轮（要办事、要查数据、要找房）时，意图分类的结果
    # 只是个标签，压根不参与决策——为它多花一次大模型调用不划算。
    #
    # 实测：接上中转站之后一句「帮我把这个月房租交了」要 17.7 秒、
    # 两次调用；「押金怎么退」42 秒、四次。**演示里没人等得了。**
    # 所以这里先跑规则，真正需要意图的那条路（知识型兜底）再补一次。
    routed = supervisor.classify(payload.text, provider=None)
    intent = routed["intent"]

    # 承接上文：已经在找房的会话里，「三千以内的」「再便宜点」这类
    # 半句话单独分类必然是 unknown，只有结合上文才讲得通。
    if intent == "unknown" and slots and memory.continues_search(payload.text):
        intent = "search"
        routed = dict(routed, intent="search", carried_over=True,
                      reason="承接上文的找房需求")

    # 「再多看几套」里一个筛选条件都没有，`continues_search` 认不出来
    # （它要求「确实说出了可执行的条件变化」，这句话没有）。
    # 但只要上文在找房，它就只可能是在翻页——**这是界面亲口教用户说的话，
    # 必须认得**。放在上面那条之后：它是承接的一种特例，不是新需求。
    if intent != "search" and slots and _wants_more(payload.text):
        intent = "search"
        routed = dict(routed, intent="search", carried_over=True,
                      reason="承接上文，翻下一批")

    # 关键词表没认出来，就问一句规则解析器：**这句话里有没有找房的条件？**
    #
    # 实测「金州区三千以内的两室」被判成 unknown，回了一句「没太理解你
    # 想做什么」——而规则解析器同一句话解析出了区域、租价上限、户型
    # 三个字段，置信度 0.80。意图表里写的是「室一厅」，少一个「厅」就
    # 认不出，可「三千以内的两室」是再自然不过的说法。
    #
    # 与其继续往那张表里塞词，不如让**已经调好的那个解析器**来回答：
    # 一句话里同时出现两个以上的找房条件，它就是在找房。
    # 门槛卡在「两个字段」是保守的一侧：「中山区的」只有一个字段，
    # 可能是别的请求的一部分，交给上面那条承接规则去管。
    if intent == "unknown":
        ruled = parse_query(payload.text)
        if len(ruled["filters"]) >= 2 and ruled["confidence"] >= 0.5:
            intent = "search"
            routed = dict(routed, intent="search",
                          reason="规则解析出 %d 个找房条件"
                                 % len(ruled["filters"]))

    # ---- G1：先让大模型挑工具，挑不出再走规则链路 ----
    #
    # **顺序是「模型在前、规则在后」，不是「模型代替规则」。**
    #
    # 三层关键词表调了很久，覆盖到的那部分很可靠；它的毛病是
    # **覆盖不到的那部分会掉进另一条完全无关的路**——「看看第六套」
    # 被当成新的找房、「评价怎么样」答出一段户型朝向。这类错误
    # 光靠往表里加词是补不完的，因为说法是无穷的。
    #
    # 所以让模型先看一眼。它挑得准就用它的结论，挑不准（或者没配、
    # 超时、输出坏了）就原样退回规则——**规则链路一行没删**，
    # 它仍然是这套系统的兜底，也是所有单元测试跑的那条路。
    #
    # 带待办的轮次跳过：那一句多半是在回答助手的追问（「都可以」
    # 「第二笔」），语义只在待办的上下文里成立，单独交给模型
    # 挑工具必然挑错。
    plan_note = {}
    if not (payload.pending or {}).get("action") and not (
            payload.pending or {}).get("ask"):
        picked = _try_toolbox(payload, routed, slots, feats, plan_note)
        if picked is not None:
            picked.setdefault("context_text", memory.describe(slots, feats))
            if payload.session_id and payload.token:
                backend_client.log_chat(payload.session_id, "user",
                                        payload.text, payload.token,
                                        intent=picked.get("intent") or intent)
                backend_client.log_chat(payload.session_id, "assistant",
                                        picked.get("message", ""),
                                        payload.token,
                                        intent=picked.get("intent") or intent)
            return picked

    result = _dispatch(payload, routed, intent, slots, feats)

    # 模型挑过但没被采信时，把它的结论留在 route 里。
    # **答辩要能当场说清「它为什么走了规则」**，而不是只看到一个结果
    if plan_note.get("plan"):
        result.setdefault("route", {})
        if isinstance(result["route"], dict):
            result["route"]["tool_plan"] = plan_note["plan"]

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


# 一次给几套。**这个数字同时是三件事的口径**：搜索取几条、
# 界面列几条、助手说「第几套」时数到几——三者必须是同一个数。
PAGE_SIZE = 10

# 「再多看几套」的各种说法。写成前缀匹配而不是精确相等：
# 界面上印的是「再多看几套」，但人打字时会写成「多看几套」「还有呢」
_WANT_MORE = ("再多看几套", "多看几套", "再看几套", "再来几套", "还有别的",
              "还有其他", "还有呢", "下一页", "换一批", "再换一批",
              "更多房源", "看更多")


def _wants_more(text):
    return any(word in (text or "") for word in _WANT_MORE)


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

    if name == "describe_house":
        return _describe_house(payload, routed, decision)

    if name == "house_reviews":
        return _house_reviews(payload, routed, decision)

    if name == "landlord_reviews":
        return _house_reviews(payload, routed, decision, subject="landlord")

    if name == "compare_houses":
        return _compare_houses(payload, routed, decision)

    # search_house 交回去走原来的找房分支：那里要合并历史槽位
    # （「三千以内的」得接上上一轮的「中山区两室一厅」），这份上下文只有那里有。
    return None


def _push_recommendation(result, features=None, offset=0):
    """结果出来之后**主动多说一句**：这几套里哪一套最值得先看，
    以及接下来可以说什么。

    列表本身只回答「有哪些」，回答不了「我该看哪个」。用户拿到三套
    条件相近的房，下一步不是挑，是**发愣**——实测里他接着打的三句话
    全是「哪个好」「对比一下」「介绍下第一套」。既然十有八九要问，
    不如先说。

    两条自我约束：

    - **挑不出来就不挑。** 头两名分差不够时 `best_of` 返回空，
      这里也就不加那句话。硬推一套「其实差不多」的，用户点进去
      发现没区别，这行字下次就没人看了。
    - **推荐词从这一组结果里长出来。** 写死一张清单，迟早会推出
      「只看带电梯的」而这批房一套电梯都没有。
    """
    items = result.get("items") or []
    if result.get("status") != "found" or len(items) < 2:
        return

    lead = pitch.lead_line(items, features, offset=offset)
    if lead:
        result["message"] = "%s %s" % (result.get("message", ""), lead)
        result["recommended"] = pitch.best_of(items, features)[0]

    # 已有的可点选项优先，推荐词补在后面——追问永远比建议要紧
    options = list(result.get("options") or [])
    result["options"] = options + [
        chip for chip in pitch.followups(items, result.get("filters"), features)
        if chip not in options]


def _fetch_houses(house_nos):
    """按编号取房源详情，取不到的跳过。"""
    rows = []
    for no in house_nos:
        if not no:
            continue
        try:
            row = backend_client.get("/api/houses/%s" % no)
        except Exception:                                    # noqa: BLE001
            continue
        if isinstance(row, dict) and row.get("house_no"):
            rows.append(row)
    return rows


def _no_reference(routed, decision, what):
    """指代解析不出来时的回话。**不能沉默地退回找房**——
    那正是实测里「介绍下第一套」被当成新一轮搜索的原因。"""
    return {
        "intent": "support", "status": "answered", "ok": True,
        "route": routed, "route_source": decision["source"],
        "alternatives": [],
        "message": "我这边还没有可以%s的房源。先说说你的条件，"
                   "或者点开一套房再问我。" % what,
    }


def _out_of_range(routed, decision, count):
    """用户说了个不存在的序号。**要说清楚有几套**——
    回一句「没听懂」会让人以为是自己表达有问题，其实是数字超了。"""
    return {
        "intent": "support", "status": "answered", "ok": True,
        "route": routed, "route_source": decision["source"],
        "alternatives": [],
        "message": ("这一组我只列了 %d 套，没有你说的那一套。"
                    "说个 1 到 %d 之间的序号，或者说「再多看几套」。"
                    % (count, count)),
    }


def _describe_house(payload, routed, decision):
    """讲解某一套。"""
    resolved = memory.resolve_house(payload.text, payload.last_house_nos,
                                    payload.house_no)
    if resolved is memory.OUT_OF_RANGE:
        return _out_of_range(routed, decision, len(payload.last_house_nos))
    target = resolved or payload.house_no or (payload.last_house_nos
                                              or [None])[0]
    rows = _fetch_houses([target]) if target else []
    if not rows:
        return _no_reference(routed, decision, "介绍")

    message, data = pitch.describe(backend_client, rows[0])
    return {
        "intent": "support", "status": "answered", "ok": True,
        "route": routed, "route_source": decision["source"],
        "alternatives": [], "message": message,
        "house_no": rows[0].get("house_no"),
        "pitch": data,
        # 讲完就给下一步。用户听完「适合谁」之后最想做的就是这两件事，
        # 让他再打一遍字是白白丢掉一次转化。
        "options": ["帮我约看房", "收藏这套", "看看类似的"],
    }


def _house_reviews(payload, routed, decision, subject="house"):
    """某一套房的租后评价。

    单独成一路，不并进讲解：**讲解出自房源字段，评价出自住过的人**。
    前者由房东填写，后者是这套房唯一不由卖方撰写的信息——
    用户问「评价怎么样」而得到一段户型朝向的推销词，
    等于把他最想要的那部分吃掉了。
    """
    resolved = memory.resolve_house(payload.text, payload.last_house_nos,
                                    payload.house_no)
    if resolved is memory.OUT_OF_RANGE:
        return _out_of_range(routed, decision, len(payload.last_house_nos))
    target = resolved or payload.house_no or (payload.last_house_nos
                                              or [None])[0]
    rows = _fetch_houses([target]) if target else []
    if not rows:
        return _no_reference(routed, decision, "查评价")

    house = rows[0]
    name = "%s %s" % (house.get("community") or "", house.get("layout") or "")
    if subject == "landlord":
        message = house_tool.landlord_reviews(backend_client,
                                              house["house_no"], name.strip())
    else:
        message = house_tool.reviews(backend_client, house["house_no"],
                                     name.strip())
    return {
        "intent": "support", "status": "answered", "ok": True,
        "route": routed, "route_source": decision["source"],
        "alternatives": [], "message": message,
        "house_no": house.get("house_no"),
        "options": ["介绍下这套", "帮我约看房", "看看类似的"],
    }


def _compare_houses(payload, routed, decision):
    """对比两套。序号解析不出第二套时，默认拿列表前两条。"""
    nos = [n for n in (payload.last_house_nos or []) if n]
    picked = memory.resolve_houses(payload.text, nos)
    if len(picked) < 2:
        picked = nos[:2]
    rows = _fetch_houses(picked[:3])
    if len(rows) < 2:
        return _no_reference(routed, decision, "对比")

    message, data = pitch.compare(backend_client, rows)
    return {
        "intent": "support", "status": "answered", "ok": True,
        "route": routed, "route_source": decision["source"],
        "alternatives": [], "message": message,
        "compare": data,
        "options": ["介绍下第一套", "介绍下第二套", "帮我约看房"],
    }


# 令牌 → (身份, 取到的时刻)。**只缓存「是不是登录 / 什么角色」，
# 不缓存任何个人数据**——这两样只用来决定给模型列哪几个工具，
# 存错了最多是多列一个选项，而权限判定始终在后端。
#
# 60 秒足够覆盖一次连续对话；再长的话，用户刚在另一个标签页升级成房东，
# 助手还会有一分钟不认这件事。
_WHOAMI_TTL = 60.0
_whoami_cache = {}


def _whoami(token):
    """当前用户是谁。取不到就当未登录——**不抛异常**：
    工具选择只用它来少列几个选项，为此让整轮对话失败不值得。

    带一层短缓存：原来每轮对话都打一次 `/api/auth/me`，
    而**这个请求对答案没有任何贡献**，纯粹是为了少给模型几个选项。
    连着聊十句就是十次白往返。
    """
    if not token:
        return {"logged_in": False, "role": ""}

    hit = _whoami_cache.get(token)
    if hit and (time.time() - hit[1]) < _WHOAMI_TTL:
        return hit[0]

    try:
        me = backend_client.get("/api/auth/me", token=token)
    except Exception:                                        # noqa: BLE001
        # **失败不写缓存。** 写进去的话，一次网络抖动会让接下来
        # 一整分钟里所有工具都按「未登录」过滤掉
        return {"logged_in": False, "role": ""}
    if not isinstance(me, dict) or not me.get("id"):
        return {"logged_in": False, "role": ""}

    who = {"logged_in": True, "role": me.get("role") or "tenant"}
    # 缓存不设上限会随令牌数无限长。**令牌本身有有效期**，
    # 过期的条目再也不会被查到，只是白占内存——超过一千条就整个清掉，
    # 比按 LRU 逐条淘汰简单，代价是偶尔多几次 /me
    if len(_whoami_cache) > 1000:
        _whoami_cache.clear()
    _whoami_cache[token] = (who, time.time())
    return who


# 低于这个把握就不采信，退回规则链路。
#
# 0.6 是这么定的：**选错的代价不对称。** 选对了省一次误判，选错了
# 用户会拿到一个和他的问题毫无关系的回答，而且看不出哪里错了。
# 所以门槛往高了卡，宁可多走几次规则。
MIN_CONFIDENCE = 0.6


def _try_toolbox(payload, routed, slots, feats, decision_cache):
    """G1：先让大模型挑工具。挑不出或不采信时返回 None，退回规则链路。

    **这一层没有任何独占能力**——它能做的每一件事，下面的规则链路都
    做得了（只是更容易误判）。所以它随时可以整段跳过：模型没配、超时、
    输出坏了、把握不够，通通退回去。这条性质是刻意保住的，
    它意味着接不接大模型都不影响功能完整性。
    """
    turn = _llm()
    if turn is None or not getattr(turn, "available", False):
        return None

    # 选择器只花整轮预算里的一小块。**它是优化，不是主力**——
    # 让它挂死之后把时间吃光，后面真正答题的调用就没得用了，
    # 结果是每一步都降级、而且还慢。
    picker = llm_budget.for_turn(turn, settings.llm_plan_budget)

    who = _whoami(payload.token)
    plan = tool_planner.plan(
        payload.text, picker,
        logged_in=who["logged_in"], role=who["role"],
        house_no=payload.house_no,
        last_house_nos=list(payload.last_house_nos or []),
        context_text=memory.describe(slots, feats))
    decision_cache["plan"] = plan

    name = plan["tool"]
    if name == "none" or plan["confidence"] < MIN_CONFIDENCE:
        return None

    tool = tool_catalog.BY_NAME.get(name) or {}

    # 写操作**一步都不执行**，转成已有的两阶段确认卡片。
    # 这一层只负责说「他想做的是 book_viewing」，卡片长什么样、
    # 缺哪个槽位要追问、参数能不能过白名单、最后谁去调后端，
    # 全在原来那条路径上，一行没动。
    if tool.get("kind") == "write":
        routed = dict(routed, intent="support", tool=name,
                      reason=tool_planner.describe(plan))
        return _support_action(payload, routed, tool["action"], plan)

    # 平台规则类问题直接交给知识层，**跳过路由那一次大模型调用**。
    #
    # 实测「押一付三是什么意思」原来要 17.6 秒：选择器 1 次、
    # 路由 1 次、组织回答 1 次，三次往返。而选择器已经判定这是知识型
    # 问题了，路由再判一遍是重复劳动——**同一个问题不该问两遍模型**。
    if name == "knowledge":
        answered = _knowledge(payload, routed, plan)
        if answered is not None:
            return answered
        return None

    ctx = {
        "text": payload.text,
        "token": payload.token,
        "provider": turn,
        "backend": backend_client,
        "property_finder": property_finder,
        "fetch_houses": _fetch_houses,
        "last_house_nos": list(payload.last_house_nos or []),
        "house_no": payload.house_no,
        "slots": slots,
        "features": feats,
        "seen": payload.seen,
    }
    result = tool_runner.run_read(name, plan["params"], ctx)
    if result is None:
        return None

    if result.get("_out_of_range"):
        return _out_of_range(routed, {"source": "toolbox"},
                             len(payload.last_house_nos or []))

    result.setdefault("intent", "search" if name in
                      ("search_houses", "more_houses") else "support")
    result.setdefault("status", "answered")
    result.setdefault("ok", True)
    result.setdefault("alternatives", [])
    result["route"] = dict(routed, intent=result["intent"], tool=name,
                           reason=tool_planner.describe(plan))
    result["route_source"] = "toolbox"
    if name in ("search_houses", "more_houses"):
        _push_recommendation(
            result, feats,
            offset=int(payload.seen or 0) if result.get("page", 1) > 1 else 0)
    return result


def _knowledge(payload, routed, plan):
    """走知识检索。路由结论由工具选择器给，不再单独问一次模型。"""
    result = support.run(
        payload.text, token=payload.token,
        history=list(payload.history or []),
        provider=_llm(), backend=backend_client,
        settings=settings, house_no=payload.house_no or None,
        house_nos=payload.last_house_nos,
        pending=payload.pending,
        routing={"kind": support_router.KIND_KNOWLEDGE, "name": "none",
                 "source": "toolbox",
                 "rule": {"kind": support_router.KIND_KNOWLEDGE,
                          "name": "none"}})
    if result is None:
        return None
    # **闸门拒答时不要硬吞。** 退回去让规则链路再试一次：
    # 它的检索通道和这里是同一套，但意图分流不同，偶尔能捞回来
    if result.get("status") not in ("answered", "account"):
        return None
    result["route"] = dict(routed, intent="support", tool="knowledge",
                           reason=tool_planner.describe(plan))
    result["route_source"] = "toolbox"
    return result


def _support_action(payload, routed, action, plan):
    """把一个写操作交给客服层出确认卡片。

    **序号要在这里就还原成编号。** 「帮我约第二套看房」里的「第二套」
    只有结合上一轮的列表才讲得通，而代办层拿不到那份上下文——
    不还原的话，用户明明指了一套，助手却回一句「想看哪一套？」，
    还把刚才那句话白问了一遍。
    """
    house_no = memory.resolve_house(payload.text, payload.last_house_nos,
                                    payload.house_no)
    if house_no is memory.OUT_OF_RANGE:
        return _out_of_range(routed, {"source": "toolbox"},
                             len(payload.last_house_nos or []))

    result = support.run(
        payload.text, token=payload.token,
        history=list(payload.history or []),
        provider=_llm(), backend=backend_client,
        settings=settings, house_no=house_no or payload.house_no or None,
        house_nos=payload.last_house_nos,
        pending=payload.pending,
        routing={"kind": support_router.KIND_ACTION, "name": action,
                 "source": "toolbox"})
    if result is None:
        return None
    result["route"] = routed
    result["route_source"] = "toolbox"
    return result


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
    resolved_house = memory.resolve_house(text, payload.last_house_nos,
                                          payload.house_no)
    # 序号超了就当没解析出来，交给客服层去问「哪一套」。
    # **不能把哨兵原样传下去**——它不是编号，会被当成房源号拿去查库
    if resolved_house is memory.OUT_OF_RANGE:
        resolved_house = None
    decision = support_router.route(text, provider=_llm(),
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

    # 规则认不出意图、路由又说这是普通问题——只有这种时候意图才真正影响
    # 后面走哪条路，值得为它补一次大模型调用。其余情况上面已经定了。
    if (decision["kind"] == support_router.KIND_KNOWLEDGE
            and intent == "unknown" and not pending_action):
        routed = supervisor.classify(text, provider=_llm())
        intent = routed["intent"]

    if (not interrupts_pending and
            (decision["kind"] in (support_router.KIND_ACTION,
                                  support_router.KIND_QUERY) or pending_action)):
        result = support.run(text, token=payload.token,
                             history=list(payload.history or []),
                             provider=_llm(), backend=backend_client,
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
        # 路由判成「要办一件事」时，客服层给出的答复就是最终答复——
        # 包括「你没这个身份」这种。原先它不在放行名单里，于是
        # 「我要发布房源」被租客说出来，那句「发布房源是房东端的功能」
        # 被丢掉、重新按意图跑成找房，回一句「没听懂你的找房需求」。
        claimed = (decision["kind"] == support_router.KIND_ACTION
                   and result.get("status") == "answered"
                   and bool(result.get("message")))
        if pending_action or claimed or result.get("status") in (
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
            property_finder.understand(text, provider=_llm())
        merged, features, notes = memory.apply_turn(
            slots, feats, text, filters=cur_filters, feats=cur_features)

        # 「再多看几套」——**这句话原来是界面上一句没人接的空话。**
        #
        # 列表底下印着「还有 51 套，说『再多看几套』」，用户照做，
        # 却走进了普通的找房分支：这句话里一个条件都解析不出来，
        # 于是回一句「没听懂你的找房需求」。**在界面上教用户说的话，
        # 代码里必须有人接。**
        more = _wants_more(text)
        page = 1
        if more and slots:
            # 接着上一批往下翻。条件用合并后的 merged——它是历史轮次
            # 重放出来的，正是上一批用的那一组
            page = max(1, int(payload.seen or 0) // PAGE_SIZE + 1)

        result = property_finder.run(text, provider=_llm(),
                                     filters=merged, features=features,
                                     source=source, page=page,
                                     limit=PAGE_SIZE)

        # 一个条件都没说、而话里又在要推荐 —— 给推荐，别把问题推回去。
        if result.get("status") == "unparsed" and _wants_recommendation(text):
            picked = _recommend(payload, routed, slots, feats)
            if picked is not None:
                return picked

        result["route"] = routed
        result["context"] = merged
        result["context_text"] = memory.describe(merged, features)
        result["notes"] = notes
        result["page"] = page
        if page > 1:
            # 追加而不是另起一列。**序号必须接着数下去**——
            # 翻一页就从 1 重新数，用户刚记住的「第 3 套」就换了一套房
            result["append"] = True
            start = int(payload.seen or 0) + 1
            end = start + len(result.get("items") or []) - 1
            if end >= start:
                result["message"] = ("接着往下，第 %d–%d 套（共 %d 套）。"
                                     % (start, end, result.get("total") or 0))
            else:
                result["message"] = ("没有更多了，这 %d 套就是全部符合条件的。"
                                     % (result.get("total") or 0))
        # 大模型改了规则的哪些字段。答辩时能当场展示「规则说什么、
        # 模型改了什么」，比只给一个最终结果有说服力。
        result["divergence"] = divergence
        # 承接了上文才说明，免得单轮对话也啰嗦一句「已记住」
        if slots and merged != cur_filters:
            result["message"] = "（延续上文：%s）%s" % (
                result["context_text"], result.get("message", ""))

        # 翻页时序号要接着上一批数下去
        _push_recommendation(result, features,
                             offset=int(payload.seen or 0) if page > 1 else 0)
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
                             provider=_llm(), backend=backend_client,
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
        # **分析完要给一个办事的入口，不能停在「建议派单给水暖工」。**
        #
        # 这是验收意见「智能体只告诉你该怎么做，而不是帮你做」最典型的
        # 一处：用户说「水管漏水了」，得到的是「已识别为水暖故障，
        # 紧急程度紧急，建议派单给水暖工」——**建议谁派？他自己去哪派？**
        # 分析得再准，事情还是没人办。
        #
        # 所以登录用户一律转到代办那条路：出确认卡片，他点一下就落库。
        # 代办层缺房源编号时会自己追问「哪一套」，那也是在办事，
        # 不是把问题原样退回。
        #
        # 未登录时才退回纯分析——那时候确实办不了，
        # 但**分析本身仍然有用**（他至少知道这算紧急故障）。
        if payload.token:
            handled = _support_action(payload, routed, "submit_repair", None)
            if handled is not None:
                return handled

        result = repair.run(text, provider=_llm(), submit=False)
        result["route"] = routed
        result["alternatives"] = []
        # 明说一句怎么才能办。**不说的话，未登录用户拿到一段分析就
        # 走进死胡同了**——他不知道差的只是一次登录
        result["message"] = (result.get("message") or "") +             "　登录之后我可以直接帮你把报修单提交给房东。"
        return result

    if intent == "support":
        # AI-03。统一入口同样要把令牌带下去——用户在对话里问
        # 「我还欠多少房租」，走的是同一个入口。
        result = support.run(text, token=payload.token,
                             history=list(payload.history or []),
                             provider=_llm(), backend=backend_client,
                             settings=settings)
        result["route"] = routed
        return result

    if intent == "booking":
        # 统一入口本来拿不到房源编号，多轮会话下可以从「第二套」
        # 这类指代里还原出来，省掉用户回头去翻列表的一步
        house_no = memory.resolve_house(text, payload.last_house_nos,
                                        payload.house_no)
        if house_no is memory.OUT_OF_RANGE:
            # 约看房说错序号必须当场纠正。让它退化成「没指定房源」，
            # 用户就会拿到一份选房列表，而**他刚才明明已经指了一套**，
            # 只是指错了——不说破，他会以为助手没听见
            return _out_of_range(routed, {"source": "rules"},
                                 len(payload.last_house_nos))
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
                             provider=_llm(), backend=backend_client,
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
