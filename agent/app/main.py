"""智能体服务 HTTP 入口（FastAPI）。

前端与业务后端通过这些接口访问智能体能力，接口清单见
《需求分析说明书》第 6 节。
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app import memory, supervisor
from app.agents.booking import agent as booking
from app.agents.contract import agent as contract
from app.agents.describe import agent as describe
from app.agents.property_finder import agent as property_finder
from app.agents.recommend import agent as recommend
from app.agents.report import agent as report
from app.agents.repair import agent as repair
from app.agents.support import agent as support
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
    # AI-09 多轮会话。登录用户的历史存在后端，用 session_id 取；
    # 未登录用户由前端把自己说过的话带上来，换设备记不住而已。
    session_id: str = ""
    token: str = ""
    history: list[str] = []
    # 上一轮结果的房源编号，用于消解「第二套」「那套」这类指代
    last_house_nos: list[str] = []


class SupportRequest(BaseModel):
    text: str
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


@app.post("/agent/describe")
def agent_describe(payload: DescribeRequest):
    """AI-08 房源描述自动生成。"""
    return describe.run(payload.model_dump(), provider=provider)


@app.post("/agent/report")
def agent_report():
    """AI-05 经营分析报告。数字全部取自业务后端，不由大模型生成。"""
    try:
        stats = backend_client.stats()
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
                         house_no=payload.house_no)
    # 日志写在接口层而不是智能体里：`run()` 保持无副作用，方便测试
    log_id = support.record(result, backend_client, token=payload.token,
                            session_id=payload.session_id)
    if log_id:
        # 前端点「有用/没用」时要用它回指这一轮
        result["log_id"] = log_id
    return result


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


def _dispatch(payload, routed, intent, slots, feats):
    text = payload.text

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
                             settings=settings)
        if result.get("status") == "answered":
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
                             settings=settings)
        result["route"] = routed
        if result.get("status") == "refused":
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
