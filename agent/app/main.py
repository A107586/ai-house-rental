"""智能体服务 HTTP 入口（FastAPI）。

前端与业务后端通过这些接口访问智能体能力，接口清单见
《需求分析说明书》第 6 节。
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app import supervisor
from app.agents.booking import agent as booking
from app.agents.contract import agent as contract
from app.agents.property_finder import agent as property_finder
from app.agents.repair import agent as repair
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


class RepairRequest(BaseModel):
    text: str
    house_no: str = ""
    xingming: str = ""
    shouji: str = ""
    submit: bool = True


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


@app.post("/agent/chat")
def agent_chat(payload: ChatRequest):
    """统一入口：先判意图，再路由到对应子智能体。

    尚未实现的子智能体先返回占位说明，方便前端提前联调。
    """
    routed = supervisor.classify(payload.text, provider=provider)
    intent = routed["intent"]

    if intent == "search":
        result = property_finder.run(payload.text, provider=provider)
        result["route"] = routed
        return result

    if intent == "contract":
        # 统一入口拿不到合同正文，只能引导用户去合同页
        return {
            "intent": "contract", "status": "need_content", "ok": False,
            "route": routed, "alternatives": [],
            "message": "请到「我的租约」里打开合同，点「智能审阅」。",
        }

    if intent == "repair":
        # 统一入口只做分析预览，落库要走 /agent/repair 并带上 house_no
        result = repair.run(payload.text, provider=provider, submit=False)
        result["route"] = routed
        result["alternatives"] = []
        return result

    if intent == "booking":
        # 统一入口拿不到房源编号，只做时间解析与时段建议，
        # 真正落库要走 /agent/booking 并带上 house_no
        result = booking.run(payload.text, house_no=None, taken=[])
        result["route"] = routed
        return result

    # 返回结构与各子智能体保持一致，前端只需处理一种形状
    return {
        "intent": intent,
        "label": supervisor.label(intent),
        "route": routed,
        "status": "not_implemented" if intent != "unknown" else "unknown",
        "ok": False,
        "alternatives": [],
        "message": ("没太理解你的意思，可以试试「帮我找中山区两室一厅」"
                    "或「预约这周六下午看房」。" if intent == "unknown"
                    else "「%s」能力尚在开发中。" % supervisor.label(intent)),
    }
