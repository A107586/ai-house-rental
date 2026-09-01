"""智能体服务 HTTP 入口（FastAPI）。

前端与业务后端通过这些接口访问智能体能力，接口清单见
《需求分析说明书》第 6 节。
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app import supervisor
from app.agents.property_finder import agent as property_finder
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

    return {
        "intent": intent,
        "label": supervisor.label(intent),
        "route": routed,
        "ok": False,
        "message": "「%s」能力尚在开发中。" % supervisor.label(intent),
    }
