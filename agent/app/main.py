"""智能体服务 HTTP 入口（FastAPI）。

前端与业务后端通过这些接口访问智能体能力，接口清单见
《需求分析说明书》第 6 节。
"""

import contextvars
import queue
import threading
import json
import sys
import re
import time
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app import fusion, memory, supervisor, tracing
from app.agents.assistant import loop as assistant_loop
from app.agents.assistant.toolkit import Toolkit
from app.retrieval.tokenize import WORD_LEVEL
from app.agents.booking import agent as booking
from app.agents.contract import agent as contract
from app.agents.describe import agent as describe
from app.agents.describe.tools import caption
from app.agents.handoff import agent as handoff
from app.agents.property_finder import agent as property_finder
from app.agents.property_finder.tools.parse_query import parse_query
from app.agents.recommend import agent as recommend
from app.agents.report import agent as report
from app.agents.repair import agent as repair
from app.agents.repair.tools import vision as repair_vision
from app.agents.support import agent as support
from app.agents.support.tools import greeting
from app.agents.support.tools import house as house_tool
from app.agents.support.tools import photo
from app.agents.support.tools import pitch
from app.agents.support.tools import router as support_router
from app.agents.toolbox import catalog as tool_catalog
from app.agents.toolbox import planner as tool_planner
from app.agents.toolbox import runner as tool_runner
from app.llm import budget as llm_budget
from app.llm.base import LLMUnavailable
from app.agents.viewing import agent as viewing
from app.agents.support.tools import actions as support_actions
from app.agents.support.tools import forms as support_forms
from app.backend import BackendError, client as backend_client
from app.config import settings
from app.llm.providers import build_provider

# Windows 上输出重定向到文件或管道时用的是本地编码（本机 cp1252），
# 打印中文会抛 UnicodeEncodeError。服务端昨天补过这一处，
# **智能体这边漏了**——下面那句中文告警当场把服务打挂在启动阶段：
#
#     UnicodeEncodeError: 'charmap' codec can't encode characters in position 7-10
#
# 原委见 server/app/console.py。这里内联同样四行：两个服务各自独立部署，
# 为一个四行的函数跨服务共享一个模块不划算。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

# 没装分词器就在启动时说一声。**这种「装没装结果不一样」的差异
# 不能留给队友自己发现**——它不报错、不崩溃，只是答对率悄悄掉一截。
if not WORD_LEVEL:
    print("[warn] 没有找到 jieba，可答性闸门降级运行"
          "（AI-03 有依据正确率会低一截）。装上：pip install jieba",
          flush=True)

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
    # 让前端脚本读得到请求编号（跨域时响应头默认只暴露几个标准字段）
    expose_headers=[tracing.HEADER],
)

# 请求编号 + 结构化访问日志，见 app/tracing.py。注册在 CORS 之后＝在它外层，
# 编号从请求一进来就有，耗时也包住整条链
tracing.setup()
app.middleware("http")(tracing.trace_requests)

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

# 这一轮之前的对话，**双方的话都在**（role = user / assistant）。
#
# 其余上下文都只存「用户说过的话」，那是给槽位重放用的——助手的话混进去
# 会被当成条件解析（见 user_turns 的文档）。可「我上一条问了什么」
# 「总结一下」「你刚才说的第二点什么意思」这类话，答案恰恰在助手说过的话里。
# 原来没有任何一条路径拿得到它们，于是这些最平常的追问一律答「没太理解」。
_turn_dialogue = contextvars.ContextVar("turn_dialogue", default=())
# 本轮检索拒答时挨得最近的几条 FAQ 问题。对话兜底拿它提示用户「可以看看这几条」，
# 见 _converse。
_turn_related = contextvars.ContextVar("turn_related", default=())

# 交给对话兜底的最近几条。再往前与当下基本无关，还白占上下文。
DIALOGUE_KEEP = 16

# 对话兜底至少要有这么多秒可用，见 _converse。开思考时一次回话实测 8–15 秒。
CONVERSE_RESERVE = 15.0


def _llm():
    """这一刻该用哪个 provider。"""
    return _turn_llm.get() or provider


def _dialogue_from(stored, history):
    """把后端存的会话记录（登录用户）或前端带上来的历史（未登录）统一成消息列表。"""
    out = []
    if stored:
        for m in stored:
            role = m.get("role")
            if role in ("user", "assistant") and m.get("content"):
                out.append({"role": role, "content": str(m["content"])[:600]})
    else:
        for item in (history or []):
            if isinstance(item, dict):
                role = item.get("role") or "user"
                content = item.get("content") or ""
            else:
                role, content = "user", item
            if role in ("user", "assistant") and content:
                out.append({"role": role, "content": str(content)[:600]})
    return tuple(out[-DIALOGUE_KEEP:])


class SearchRequest(BaseModel):
    text: str
    page: int = 1
    limit: int = 10


def user_turns(history):
    """把 `history` 归一成「用户自己说过的话」。

    这个字段的约定一直是**只放用户的话**，前端也确实只回传
    `role === 'user'` 的那些。但约定是口头的，端上谁都能调这个接口，
    而把问答交替地全传上来时，后果既严重又完全看不出来：
    助手自己的回答会被当成用户的条件去解析。

    实测过一次——助手说了句「2026-09 加入平台」「61 平」「平均 4.4」，
    重放出来的槽位就成了「租价 9–2026 元、面积 51–70 平」，
    下一句「还有更便宜的吗」于是答「建议把租价上限从 9 元提高一些」。
    没有报错，没有告警，只有一句看不懂的回复。

    纯字符串列表没法反推哪句是谁说的，所以这里不猜；
    改成**也接受** `{"role": ..., "content": ...}` 这种带署名的条目，
    带了署名就按署名筛。老的纯字符串调用方一个字都不用改。
    """
    out = []
    for item in (history or []):
        if isinstance(item, dict):
            if (item.get("role") or "user") != "user":
                continue
            item = item.get("content") or ""
        if isinstance(item, str) and item:
            out.append(item)
    return out


class ChatRequest(BaseModel):
    text: str
    # 客服代办的待办状态，见 SupportRequest.pending
    pending: dict | None = None
    # AI-09 多轮会话。登录用户的历史存在后端，用 session_id 取；
    # 未登录用户由前端把自己说过的话带上来，换设备记不住而已。
    session_id: str = ""
    token: str = ""
    # 纯字符串就是用户说过的话；也接受 {"role","content"}，见 `user_turns`
    history: list[str | dict] = []
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
    # 用户随这句话发的照片。只认本系统上传的地址（/static/...），
    # 前端先传到业务后端换地址再发过来，见 `_with_photos`。
    images: list[str] = []


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
    # 报修照片。只收 data URI 或本系统 /static/ 下的路径，
    # 交给模型之前由 `vision.safe_images` 过一遍，理由见那个函数。
    images: list[str] = []
    # 报修人的登录令牌。提交时转给后端，后端据此判断他是不是这套房的在住租客
    token: str = ""


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
            # **装没装分词器，必须能从外面看出来。**
            #
            # 这台机器上有两个 Python：测试跑的那个装了 jieba，
            # 服务起的那个（miniforge）没装。没装时闸门挑不出话题词、
            # AI-03 的有依据正确率会掉一截，而**从接口上完全看不出来**——
            # 排查了半天才发现问题不在代码在解释器。
            "word_level": WORD_LEVEL,
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
        provider=_llm(),
    )


@app.post("/agent/repair")
def agent_repair(payload: RepairRequest):
    """AI-04 报修工单智能分派。

    带照片时先让模型把图片说成一句话，再走原来那套分派规则——
    识图只负责「看见了什么」，不负责「判成哪一类、急不急」。
    """
    return repair.run(
        payload.text,
        house_no=payload.house_no or None,
        applicant={"xingming": payload.xingming, "shouji": payload.shouji},
        provider=_llm(),
        backend=backend_client,
        submit=payload.submit,
        images=repair_vision.safe_images(payload.images,
                                         settings.backend_base_url),
        token=payload.token or None,
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


@app.post("/agent/recommend/explain")
def agent_recommend_explain(payload: RecommendRequest):
    """给「为你推荐」写一句推荐理由（大模型）。

    和推荐列表分开请求：列表是规则算的、立刻就有；这句话要等模型，
    前端先摆出卡片，话到了再换上去，首页不用等它。
    """
    result = recommend.run(token=payload.token, limit=payload.limit, backend=backend_client)
    text = recommend.explain(result, _llm())
    return {"ok": bool(text), "text": text, "source": "llm" if text else "rules"}


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


class ImageCaptionRequest(BaseModel):
    """给一张房源照片写图片说明。

    只收一张：这条是房东在相册里点某一张图触发的，
    一次传一叠再让他挨个核对，和让他自己写没什么区别。
    """

    # 本系统上传出来的 `/static/...` 路径，或者 data URI。
    # 收敛规则见 `app/images.py`，外链一律丢弃
    images: list[str] = []


@app.post("/agent/image-caption")
def agent_image_caption(payload: ImageCaptionRequest):
    """看一张房源照片，写一句图片说明的**草稿**。

    **只出草稿，不落库。** 图片说明是挂在房源上给租客看的公开文案，
    和标题、描述一个性质；模型写完直接存等于替房东对外发布一句
    他没看过的话。落库走后端的
    `POST /api/houses/{house_no}/images/{id}/caption`，
    在那里过内容审核，也在那里要求房东已经看过。

    写不出来就回空串，前端显示「这次没写出来，可以再试一次」——
    这条链路整体是纯增强，任何一环出问题都不该影响相册本身。
    """
    text = caption.draft(
        repair_vision.safe_images(payload.images, settings.backend_base_url),
        _llm())
    return {"caption": text, "ok": bool(text)}


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


# 对话里看一次照片最多等多久。开思考时一张图实测 5–10 秒
PHOTO_BUDGET = 25.0

# 路由办成了一件具体的事的标志：出了确认卡片、列了房源、查到了他的数据、
# 讲解/对比了房源、在填表。有这些就不拿看图的回答去覆盖它。
_ACTED_KEYS = ("action", "items", "account", "pitch", "compare", "form")


@app.post("/agent/chat")
def agent_chat(payload: ChatRequest):
    """统一入口。

    默认走 agent（`_agent_turn`）：模型用工具循环自己决定查什么、办什么、怎么说。
    `ASSISTANT_MODE=legacy` 时走原来的规则分流链路（带照片的轮次先经 `_with_photos`）。
    """
    if settings.assistant_mode != "legacy":
        return _agent_turn(payload)
    urls = support_actions._image_urls(payload.images)
    if not urls:
        return _agent_chat(payload)
    return _with_photos(payload, urls)


# 流式连接上多久没有新事件就发一行心跳。开思考时模型可能十几秒不吐正文，
# 中间的代理（nginx 等）看到连接一直没动静会以为断了
STREAM_HEARTBEAT = 10.0


@app.post("/agent/chat/stream")
def agent_chat_stream(payload: ChatRequest):
    """`/agent/chat` 的流式版本（SSE，Server-Sent Events）。

    同一轮对话、同一种结果，只是不用干等：

        event: step   data: {"type":"step","tool":"my_bills","label":"在查你的账单"}
        event: delta  data: {"type":"delta","text":"你有 36 笔"}      ← 回答边生成边推
        event: reset  data: {"type":"reset"}                         ← 推过的字作废
        event: done   data: {"type":"done","result":{...}}           ← 和 /agent/chat 的返回一模一样

    前端以 done 里的 result 为准（话术会去掉 Markdown 星号等）。
    确认卡片的「好的」、legacy 模式这类不经过 agent 循环的轮次，只会有一个 done。

    一轮对话在后台线程里跑，事件经队列交给这里一条条写出去：
    agent 循环是同步代码，这样不用把它改成异步。
    """
    events = queue.Queue()

    def work():
        try:
            if settings.assistant_mode != "legacy":
                result = _agent_turn(payload, on_event=events.put)
            else:
                result = agent_chat(payload)
            events.put({"type": "done", "result": result})
        except Exception:                                        # noqa: BLE001
            events.put({"type": "error", "message": "这一轮处理出错了，稍后再试一次。"})
        finally:
            events.put(None)

    # 新线程不会继承当前请求的上下文（请求编号存在 ContextVar 里），
    # 不复制的话这一轮调业务后端时就带不上编号，两边日志串不起来
    context = contextvars.copy_context()
    threading.Thread(target=context.run, args=(work,), daemon=True).start()

    def stream():
        while True:
            try:
                item = events.get(timeout=STREAM_HEARTBEAT)
            except queue.Empty:
                yield ": ping\n\n"                # 冒号开头是 SSE 的注释行，浏览器会忽略
                continue
            if item is None:
                return
            yield "event: %s\ndata: %s\n\n" % (
                item["type"], json.dumps(item, ensure_ascii=False, default=str))

    return StreamingResponse(stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",                # 让 nginx 别攒着，来一条转一条
    })


# ============================================================================ agent

_AGENT_UNAVAILABLE = ("助手这会儿连不上大模型，没法帮你查和办事。稍后再试一次；"
                      "急的话可以点「转人工」。")


def _agent_turn(payload, on_event=None):
    """agent 版的一轮对话。

    理解、选工具、多步查询、组织话术全归模型（`app/agents/assistant/loop.py`）。
    代码只接管两件**协议**上的事，它们不是「理解这句话」，而是界面约定：

    1. 对着一张确认卡片说「好的 / 算了 / 换个时间」——执行前的最后一道关，
       走原来那条确定性的两阶段流程（高风险的仍然要点按钮）；
    2. 助手正在要照片（报修「要拍张照片吗」），发来的是照片或「跳过」——
       照片就是答案，直接填进去。

    模型不可用时照实告诉用户，**不再退回关键词链路**。
    """
    urls = support_actions._image_urls(payload.images)
    pending = payload.pending or {}
    ask = pending.get("ask") or {}
    stored = backend_client.list_chat(payload.session_id, payload.token)
    past = ([m["content"] for m in stored if m.get("role") == "user"] if stored
            else user_turns(payload.history))
    dialogue = _dialogue_from(stored, payload.history)
    slots, feats = memory.replay(past)

    protocol_text = None
    if pending.get("card") and support.claims_pending(payload.text, pending):
        protocol_text = payload.text
    elif ask.get("kind") == "upload" and (
            urls or support.claims_pending(payload.text, pending)):
        protocol_text = ",".join(urls) if urls else payload.text
    if protocol_text is not None:
        result = support.run(protocol_text, token=payload.token, history=past,
                             provider=_llm(), backend=backend_client, settings=settings,
                             house_no=payload.house_no or None,
                             house_nos=payload.last_house_nos, pending=pending) or {}
        result.setdefault("items", [])
        result["route"] = {"source": "protocol", "tool": "pending_" + (
            "card" if pending.get("card") else "upload")}
        result["route_source"] = "protocol"
        result["context_text"] = memory.describe(slots, feats)
        return _log_agent_turn(payload, urls, result)

    if provider is None or not getattr(provider, "available", False):
        return _log_agent_turn(payload, urls, _agent_reply(
            _AGENT_UNAVAILABLE, {}, [], slots, feats, status="unavailable", ok=False))

    turn = llm_budget.for_turn(provider, settings.agent_turn_budget)
    _turn_llm.set(turn)
    who = _whoami(payload.token)
    runner_ctx = {
        "text": payload.text, "token": payload.token, "provider": turn,
        "backend": backend_client, "property_finder": property_finder,
        "fetch_houses": _fetch_houses,
        "last_house_nos": list(payload.last_house_nos or []),
        "house_no": payload.house_no, "slots": slots, "features": feats,
        "seen": payload.seen,
        # 讲单套房时把资料整份交回 agent，不在工具里再问一次模型（见 runner._one_house）
        "agent": True,
    }
    deps = SimpleNamespace(
        logged_in=who["logged_in"], role=who["role"], runner_ctx=runner_ctx,
        run_read=tool_runner.run_read,
        knowledge=lambda question: _agent_knowledge(payload, past, question),
        recommend=lambda: recommend.run(token=payload.token, limit=6, backend=backend_client),
        review=lambda text: contract.run(text, provider=turn),
        propose=lambda action, params, text, images: _agent_propose(
            payload, action, params, text, urls))
    images = repair_vision.safe_images(urls, settings.backend_base_url) if urls else []
    try:
        out = assistant_loop.run(
            payload.text, images, turn, Toolkit(deps),
            context_lines=_agent_context(payload, who, slots, feats, pending),
            dialogue=dialogue, on_event=on_event)
    except LLMUnavailable:
        return _log_agent_turn(payload, urls, _agent_reply(
            _AGENT_UNAVAILABLE, {}, [], slots, feats, status="unavailable", ok=False))

    result = _agent_reply(out["message"], out["artifacts"], out["trace"], slots, feats)
    # 分步计时（app/agents/assistant/loop.py 的 run）：放进 route 给前端和答辩看，
    # 再写一行汇总日志——带着请求编号，和访问日志、模型调用日志对得上
    timings = out.get("timings") or {}
    result["route"]["timings"] = timings
    steps = timings.get("steps") or []
    tracing.log("agent_turn", total_ms=timings.get("total_ms"),
                model_ms=round(sum(s["ms"] for s in steps if s["kind"] == "model"), 1),
                tool_ms=round(sum(s["ms"] for s in steps if s["kind"] == "tool"), 1),
                model_calls=sum(1 for s in steps if s["kind"] == "model"),
                tools=[s["tool"] for s in steps if s["kind"] == "tool"],
                stopped_by=out["stopped_by"])
    # 用户在流程里插了句别的：答完把待办原样交回去，下一句还能接着填
    if pending and not result.get("pending") and not out["stopped_by"]:
        result["pending"] = pending
    return _log_agent_turn(payload, urls, result)


def _agent_reply(message, artifacts, trace, slots, feats, status="answered", ok=True):
    """agent 的结果 → 前端认得的那一种返回形状（和 legacy 一致，前端不用改）。"""
    result = {
        "intent": "search" if artifacts.get("items") else "support",
        "status": "confirm" if artifacts.get("action") else status,
        "ok": ok, "message": message,
        "items": [], "alternatives": [], "citations": [], "notes": [],
        "context_text": memory.describe(slots, feats),
        "route": {"source": "agent", "tools": [s.get("tool") for s in trace], "trace": trace},
        "route_source": "agent",
    }
    result.update({k: v for k, v in artifacts.items() if v not in (None, "", [], {})})
    if result.get("pending", {}).get("ask"):
        result["status"] = "need_slot"
    return result


def _agent_context(payload, who, slots, feats, pending):
    """「当前情况」那一段：身份、屏幕上有什么、记住了什么、有没有办到一半的事。"""
    lines = ["用户身份：%s。" % ({"tenant": "租客", "landlord": "房东", "agent": "中介",
                                 "admin": "管理员"}.get(who["role"], "已登录用户")
                                if who["logged_in"] else "未登录（查个人数据、办事都要先登录）")]
    if payload.house_no:
        lines.append("用户正打开着房源 %s 的详情页，「这套」指它。" % payload.house_no)
    listed = [n for n in (payload.last_house_nos or []) if n]
    if listed:
        rows = {h.get("house_no"): h for h in _fetch_houses(listed[:20])}
        shown = []
        for i, no in enumerate(listed[:20]):
            h = rows.get(no) or {}
            bits = [str(h.get(k)) for k in ("title", "rent_type") if h.get(k)]
            if h.get("rent_price"):
                bits.append("%g元/月" % float(h["rent_price"]))
            # 楼层、电梯、配套也带上。上一轮工具结果不会留到这一轮——
            # 只给标题和价钱时，「按房租排一下，说说哪套适合老人」这一句，
            # 模型得把 9 套挨个再查一遍电梯（实测 4 次调用、36 秒）
            if h.get("floor"):
                bits.append(str(h["floor"]))
            if "elevator" in h:
                bits.append("有电梯" if h.get("elevator") else "无电梯")
            tags = [t for t in (h.get("tags") or []) if t][:5]
            if tags:
                bits.append("、".join(tags))
            shown.append("%d. %s %s" % (i + 1, no, " ".join(bits)))
        lines.append("屏幕上的房源列表（用户说「第几套」按这个序号）：\n" + "\n".join(shown))
    remembered = memory.describe(slots, feats)
    if remembered:
        lines.append("之前对话里记住的找房条件：%s" % remembered)
    if pending.get("action"):
        ask = pending.get("ask") or {}
        filled = {k: v for k, v in (pending.get("slots") or {}).items() if v not in (None, "")}
        if pending.get("card"):
            lines.append("待办：已经给用户出了一张「%s」的确认卡片，等他点确认。"
                         % pending["card"].get("summary", pending["action"]))
        elif ask:
            lines.append("待办：上一轮你在帮用户办 %s，问了他「%s」（要填的是 %s）；已填：%s。"
                         % (pending["action"], ask.get("question", ""), ask.get("slot", ""),
                            json.dumps(filled, ensure_ascii=False) if filled else "无"))
    return lines


def _agent_knowledge(payload, past, question):
    """知识库问答：检索 + 可答性闸门 + 受约束生成，没依据就拒答。"""
    return support.run(
        question, token=payload.token, history=past, provider=_llm(),
        backend=backend_client, settings=settings,
        house_no=payload.house_no or None, house_nos=payload.last_house_nos,
        routing={"kind": support_router.KIND_KNOWLEDGE, "name": "none", "source": "agent",
                 "rule": {"kind": support_router.KIND_KNOWLEDGE, "name": "none"}})


# 这几件事办的是**用户自己租的那套房**，房源只能从他的合同/账单里来。
# 实测「厨房水龙头一直滴水，找人来修」时，模型把页面上正打开的 DL0001 填成了
# 报修对象——那是他在逛的房子，不是他住的。这里一律不收模型和页面给的编号，
# 交给提议器按他名下生效中的租约去找。
_OWN_RENTAL_ACTIONS = {"submit_repair", "pay_bill", "renew_contract", "end_contract"}


def _own_rentals(token):
    """他作为租客、生效中的租约对应的房源编号。查不到就当没有。"""
    if not token:
        return set()
    try:
        data = backend_client.get("/api/contracts/mine", params={"side": "tenant"},
                                  token=token) or {}
    except BackendError:
        return set()
    return {c.get("house_no") for c in data.get("list") or []
            if c.get("status") == "active" and c.get("house_no")}


def _agent_propose(payload, action, params, text, urls):
    """写工具：模型给的参数 + 待办里已填的 → 提议器 → 确认卡片或追问。只读后端。"""
    pending = payload.pending or {}
    context = {}
    if pending.get("action") == action:
        context.update(pending.get("slots") or {})
    context.update({k: v for k, v in params.items() if k not in ("request", "ordinal", "house_no")})
    if action in _OWN_RENTAL_ACTIONS:
        claimed = params.get("house_no")
        params = {k: v for k, v in params.items() if k not in ("house_no", "ordinal")}
        payload = payload.model_copy(update={"house_no": "", "last_house_nos": []})
        # 模型给的编号**是他在租的那套**就留着。一律丢掉的话，「那套续一年」
        # （模型知道是 DL0007）又被追问「你有 3 份租约，要续哪一份」
        if claimed and claimed in _own_rentals(payload.token):
            context["house_no"] = claimed

    listed = list(payload.last_house_nos or [])
    house_no = params.get("house_no") if params.get("house_no") in listed + [payload.house_no] else None
    if not house_no and params.get("ordinal"):
        index = int(params["ordinal"]) - 1
        if not 0 <= index < len(listed):
            return {"status": "not_applicable",
                    "message": "屏幕上只有 %d 套，没有第 %d 套。" % (len(listed), index + 1)}
        house_no = listed[index]
    if not house_no:
        resolved = memory.resolve_house(params.get("request") or text, listed, payload.house_no)
        if resolved is not memory.OUT_OF_RANGE:
            house_no = resolved
    if house_no:
        context["house_no"] = house_no
    if urls and action == "submit_repair":
        context["images"] = ",".join(urls)

    request = params.get("request") or text
    status, message, card, ask = support_actions.propose(
        action, request, payload.token, backend_client, context)
    shaped = lambda st, msg, **extra: support._shape(st, msg, question=request, **extra)  # noqa: E731
    return support._action_result(action, status, message, card, ask, context, shaped) or {
        "status": "not_applicable", "message": message or "这件事我这边办不了。"}


def _log_agent_turn(payload, urls, result):
    if payload.session_id and payload.token:
        said = (payload.text or "").strip()
        if urls:
            said += "（附 %d 张照片）" % len(urls)
        intent = result.get("intent") or "support"
        backend_client.log_chat(payload.session_id, "user", said, payload.token, intent=intent)
        backend_client.log_chat(payload.session_id, "assistant", result.get("message", ""),
                                payload.token, intent=intent)
    return result


def _with_photos(payload, urls):
    """带照片的一轮。

    **路由不看照片**：用户那句话原样交给 `_agent_chat`，报修、找房、客服
    那几十条路径一行没改。照片在三个地方起作用：

    1. 助手这一问要的就是照片（报修的「要拍张照片吗」）：照片就是答案。
    2. 路由刚走到报修、问要不要拍照：照片已经在手上了，直接填进去——
       不然用户会看到一个莫名其妙的追问，图明明刚发过。
    3. 路由没有办成一件具体的事（见 `_ACTED_KEYS`）：看着照片作答，
       路由原本的回答当参考交给模型。只发了图没说话的，直接看图作答。

    这一轮的聊天记录由这里来写（原路由那几次调用不写）：否则存下来的是
    被看图回答替换之前的那句话，下一轮追问的上下文就错了。
    """
    said = payload.text.strip()
    quiet = payload.model_copy(update={"session_id": ""})

    ask = (payload.pending or {}).get("ask") or {}
    if ask.get("kind") == "upload":
        result = _agent_chat(quiet.model_copy(update={"text": ",".join(urls)}))
        return _log_photo_turn(payload, said, urls, result)

    result = None
    if said:
        result = _agent_chat(quiet)
        asked = ((result.get("pending") or {}).get("ask") or {})
        if asked.get("kind") == "upload":
            result = _agent_chat(quiet.model_copy(
                update={"text": ",".join(urls), "pending": result["pending"]}))
        if any(result.get(k) for k in _ACTED_KEYS) or (
                result.get("pending") and result.get("pending") != payload.pending):
            return _log_photo_turn(payload, said, urls, result)

    seen = photo.answer(
        repair_vision.safe_images(urls, settings.backend_base_url),
        llm_budget.for_turn(provider, PHOTO_BUDGET), said=said,
        reference=(result or {}).get("message", ""))
    if result is None:
        result = {"intent": "support", "items": [], "alternatives": [],
                  "route": {"tool": "photo", "reason": "只发了照片"}, "route_source": "photo",
                  "context_text": memory.describe(*memory.replay(user_turns(payload.history)))}
        if payload.pending:
            # 流程填到一半发了张图，待办不能就此丢了
            result["pending"] = payload.pending
    if seen:
        result.update(message=seen, status="answered", ok=True, photo_answer=True)
        result.setdefault("route", {})
        if isinstance(result["route"], dict):
            result["route"]["photo"] = "看图作答"
    elif not result.get("message"):
        result.update(message="这张照片我没能看清，能用文字说说想问什么吗？",
                      status="answered_no_basis", ok=True)
    return _log_photo_turn(payload, said, urls, result)


def _log_photo_turn(payload, said, urls, result):
    """带照片那一轮的聊天记录：用户的原话 + 附了几张图，和最终给出的回答。"""
    if payload.session_id and payload.token:
        intent = result.get("intent") or "support"
        backend_client.log_chat(payload.session_id, "user",
                                (said + "（附 %d 张照片）" % len(urls)).strip(),
                                payload.token, intent=intent)
        backend_client.log_chat(payload.session_id, "assistant",
                                result.get("message", ""), payload.token, intent=intent)
    return result


def _agent_chat(payload: ChatRequest):
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
        past = user_turns(payload.history)
    _turn_dialogue.set(_dialogue_from(stored, payload.history))
    _turn_related.set(())

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
        elif (len(ruled["filters"]) + len(ruled.get("features") or [])) >= 1                 and (_HOUSE_NOUN.search(payload.text)
                     or _HUNT_VERB.search(payload.text)):
            # **一个条件 + 一个「房」字头的名词，也是在找房。**
            #
            # 上面那条卡在「两个字段」，是怕一个字段太弱（「中山区的」
            # 可能是别的请求的半句话）。可实测被挡在外面的全是最自然的
            # 说法：「中山区的房子」「沙河口区有什么房」「有电梯的房子」
            # 「三千以内的房子」——各只有一个条件，于是一路掉到客服兜底，
            # 回一句「这个我没查到相关规定」。
            #
            # **用户说的是最普通的中文，系统答的是「听不懂」，
            # 这比答错更劝退。**
            #
            # 补的那个条件是「房子 / 房源 / 有什么房」这类指向住处的
            # 名词——没人会为了问平台规则说「中山区的房子」。
            # 用词表里 48 个客服问题逐条验过，一条都不受影响
            # （「可以养宠物吗」「附近有地铁吗」解析得出特征，
            # 但都不含这类名词，仍旧走客服）。
            intent = "search"
            routed = dict(routed, intent="search",
                          reason="解析出找房条件，且问的是「房」")

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
    pending = payload.pending or {}
    # 待办挂着的时候，**先问一句：这句话是在回应它吗？**
    #
    # 原先是「有待办就跳过模型」——于是房东被问「要处理哪条预约」之后，
    # 说「这个月我收了多少租金」「帮我提醒欠租的人」，全部只走规则、
    # 被塞进预约编号那一格。是回应就照原流程接着办；不是就当新话题，
    # 像平常一样让模型挑工具，待办留着等他回来。
    # 只带 ask 不带 action 的是别的子智能体自己的追问，保持原样。
    if pending.get("action"):
        engaged = support.claims_pending(payload.text, pending)
    else:
        engaged = bool(pending.get("ask"))

    # 整句寒暄、问「你会什么」——答案固定，**别先花 5 秒让模型挑工具**。
    # 实测「谢谢」13.7 秒、「在吗」10.6 秒，全耗在选择器上。
    quick = None if engaged else _small_talk(payload.text)
    if quick:
        if payload.session_id and payload.token:
            backend_client.log_chat(payload.session_id, "user", payload.text,
                                    payload.token, intent="support")
            backend_client.log_chat(payload.session_id, "assistant", quick,
                                    payload.token, intent="support")
        reply = {"intent": "support", "status": "answered", "ok": True,
                 "route": dict(routed, tool="small_talk"), "message": quick,
                 "items": [], "alternatives": [], "route_source": "rules",
                 "context_text": memory.describe(slots, feats)}
        # 表单填到一半说句「谢谢」，待办不能就此丢了——前端拿不到 pending
        # 就当事情已经结束，前面填的几项白填
        if pending.get("action"):
            reply["pending"] = pending
        return reply

    # 「刚才那几套里哪个最便宜」「这几套哪个采光好」——屏幕上有列表、问的是
    # 列表里哪一套，答案直接从字段里算，**不必等模型**。实测机器忙、选择器超时的
    # 那一轮，这句话掉进规则链路，被检索答成了「退租后没到期的账单还要交吗」。
    listed = [n for n in (payload.last_house_nos or []) if n]
    if not engaged and len(listed) >= 2 and _ASKS_WHICH_OF_LIST.search(payload.text or ""):
        direct = tool_runner.direct_list_answer(
            payload.text, memory.resolve_houses(payload.text, listed), listed,
            _fetch_houses)
        if direct:
            return dict(direct, intent="support", status="answered", ok=True,
                        route=dict(routed, tool="compare_houses", reason="列表里直接比"),
                        route_source="rules", alternatives=[],
                        context_text=memory.describe(slots, feats))

    if not engaged:
        picked = _try_toolbox(payload, routed, slots, feats, plan_note)
        if picked is not None and pending.get("ask") and not picked.get("pending"):
            picked["pending"] = pending
            picked["message"] = _with_pending_reminder(picked.get("message", ""),
                                                       pending)
        # **确认卡片也留着。** 原先只留追问：卡片挂着时问一句「可以养宠物吗」，
        # 答完卡片就从待办里消失了——他再说「好的」，助手已经不记得要确认什么。
        # 另起了一件新事（picked 自带 pending 或 action）就不留旧卡，免得两张卡打架。
        elif (picked is not None and pending.get("card")
              and not picked.get("pending") and not picked.get("action")):
            picked["pending"] = pending
            picked["message"] = _with_card_reminder(picked.get("message", ""),
                                                    pending)
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

    # **任何一条路最后落到「拒答」，都先让模型拿着对话正常回一句。**
    #
    # 找房、客服、代办各有自己的拒答出口，原来只有「意图不明」那一个
    # 接了对话兜底；实测「合同期内我能涨房租吗」「帮我写一份退租申请书」
    # 走的是客服分支，照样拿到一句「暂时没有查到相关规定，这个我不敢乱说」。
    # 这里告诉模型「知识库查过、没依据」，它只会说没写到并建议转人工，
    # 不会把常识说成平台规定。待办还挂着的轮次不动，免得冲掉那句「刚才办到哪了」。
    # 待办挂着的也一样兜：实测房东在「要处理哪条预约」挂着时问「合同期内我能涨房租吗」
    # 「帮我提醒欠租的人」，拿到的都是「不敢乱说」。待办原样留着、提醒照旧给。
    if result.get("status") == "refused":
        keep = result.get("pending")
        talked = _converse(payload, routed, "检索没有依据，按对话回应",
                           no_basis=True, related=result.get("alternatives"))
        if talked is not None:
            result = _keep_escape_hatches(talked, result)
            if keep and keep.get("card"):
                # 卡片没有「继续」可说，要么确认要么算了
                result["pending"] = keep
                result["message"] = _with_card_reminder(result["message"], keep)
            elif keep:
                result["pending"] = keep
                result["message"] = _with_pending_reminder(result["message"], keep)
                result["options"] = [{"value": "继续", "label": "继续刚才那件事"}]

    # **这一句不是在回应待办、也没开出新的事，待办就原样留着。**
    #
    # 工具那条路上早就这么做了，规则这条路漏了：约看房的确认卡挂着时问
    # 「明天天气怎么样」，走的是规则链路，答完卡片就没了——他接着说「算了」，
    # 助手已经不知道要算了什么，当成一次新的「取消预约」去处理。
    if (not engaged and pending.get("action") and not result.get("pending")
            and not result.get("action")
            and result.get("status") not in ("cancelled", "done", "confirm")):
        result["pending"] = pending
        remind = _with_card_reminder if pending.get("card") else _with_pending_reminder
        result["message"] = remind(result.get("message", ""), pending)

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
# 寒暄。**整句都是寒暄才算**（`fullmatch`）——「你好，我想找个两室」
# 里的「你好」不能把后半句吃掉。
#
# 不接的话，「你好」会一路掉进客服检索，回一句「这个我没查到相关规定」。
# 用户打的第一个招呼就被告知查无此事，后面那些功能他多半不会再试了。
_GREETING = re.compile(r"[你您]好[啊呀吗]?|哈[喽罗]|hi|hello|在[吗不]|在不在|"
                       r"有人[吗在]", re.I)
_THANKS = re.compile(r"谢谢[你您]?|多谢|感谢|辛苦了?|好的?[，,。!！]?|收到|明白了?", re.I)
_BYE = re.compile(r"再见|拜拜|bye|886", re.I)

_SMALL_TALK = (
    (_GREETING, "你好，我是租房助手。找房可以一句一句地补充条件，我都记得住；"
                "预约、报修、账单也都能在这儿办。"),
    (_THANKS, "不客气。还有别的事随时说。"),
    (_BYE, "再见，有需要随时找我。"),
)


_CONVERSE_SYSTEM = """你是一个租房平台的智能助手，正在和用户对话。上面是你们之前的对话，最后一条是用户刚说的话。

你能做的：
- 回答关于这段对话本身的问题：用户上一条问了什么、你刚才说的是什么意思、把刚才的内容总结一下、刚才列出的几套房有什么区别。
- 用户在抱怨、追问、嫌你回答草率时，认真回应：把刚才对话里已有的信息讲得更完整、更有条理。
- 用户的意思不清楚时，说出你的理解并问一句确认。
- 用户在抱怨、着急、生气时，先回应情绪，再给出能做的下一步（比如点「转人工」让管理员处理）。
- 用户要一段文字（给房东的留言、申请书、情况说明），可以帮他起草，并说明这只是草稿、没有替他提交。
- 寒暄、道谢之类正常回话。

你（这个助手）自己能做的事——用户问「怎么操作」时，直接告诉他可以对你说哪句话，
不要说「平台资料里没写」。**只说当前这个用户的身份能用的那几项**（身份见最后一行）：
- 所有人：找房——直接说条件就行，比如「西岗区一室，两千以内，有电梯」，还能介绍、对比、看评价；
- 租客：交房租、约看房、改约、取消预约、报修、续租、退租、收藏房源；
- 房东：上架或下架房源、修改房源（例如「把 DL0023 的租金改成 2300」）、发布新房源、
  确认或拒绝看房预约、给租客发起签约、给欠租的租客发催缴提醒、查定价参考；
- 中介：查佣金、查代理的房源和带看记录、帮客户找房。
代办都要先出确认卡片，用户点确认才执行。
你做不到的（替用户打电话、改别人的数据、不属于他身份的操作），就直说做不到。

绝对不能做的：
- 不能编造对话里没出现过的具体信息：房源、价格、面积、评分、评价内容、账单金额、合同日期、平台规则和政策。对话里没有的，就直说「这个我需要去查」，并给出一句用户可以直接对我说的话，例如「帮我查我的账单」「介绍下第一套」「押金怎么退」。
- 不能假装已经替用户办完了事（交费、预约、报修、签约都需要用户在界面上确认）。

用中文回答，自然口语，一般 2 到 5 句。不要用表格，不要用标题。"""


_NO_BASIS_HINT = """
补充说明：刚才已经在平台知识库里查过用户这句话，**没有找到依据**。
所以如果用户问的是规则、费用、责任划分、能不能这样做，你不能给出结论——
哪怕你知道一般的做法，也不能说成平台的规定。说「没查到能直接回答这一点的规定」，
**不要说成平台对这件事什么都没写**——常常是相关的条目在，只是没提到用户问的那个细节
（比如问「逾期要交滞纳金吗」，逾期的后果写了，滞纳金没写）。
建议用户点「转人工」问管理员；能给的只有换个说法再问的建议。
如果用户要的是一段文字（给房东的留言、申请书之类），可以帮他起草，
但要说明这只是草稿，没有替他提交任何东西。"""


# 实测「楼上漏水把我家天花板泡了，怎么办」：闸门判没有依据（资料里没写楼上漏水
# 谁负责），对话兜底只会说「资料里没写，转人工」。可知识库里明明有
# 「什么算紧急报修」——漏水正是紧急报修，助手也能直接代办报修。
# 把挨得最近的几条问题标题告诉模型：**只给标题，不给正文**，
# 它拿不到可以当结论说的内容，只能用来指路。
_RELATED_HINT = """
知识库里挨得最近的几条问题是：%s。它们不一定就是用户问的这件事，**不能拿来下结论**；
但如果对得上，可以建议用户点下面「猜你想问」里的那条看看，或者提醒他你能直接代办
（比如报修、约看房、查账单）。"""


# 问的是**自己手上那份合同写了什么**——这类才需要合同正文
_ABOUT_MY_CONTRACT = re.compile(
    r"我(的|这份|那份)?(合同|租约)|这份合同|合同(里|上|条款)|条款|有没有坑|审阅|帮我看看合同")


_ROLE_NAMES = {"tenant": "租客", "landlord": "房东", "agent": "中介", "admin": "管理员"}


def _converse(payload, routed, reason, no_basis=False, related=None):
    """对话兜底：把完整对话交给模型，正常说话。

    **只在其他路都接不住时才走到这里**——工具、规则、知识库检索都没给出答案。
    原来这一步直接回「这个我没查到相关规定，也没太理解你想做什么」，
    于是「我上一条问了什么」「你这太草率了，总结下呗」这种最普通的话，
    用户得到的是一句机器味十足的拒绝。

    编造风险由提示词里的硬约束挡：对话里没出现过的数字与规则一律不说，
    改为告诉用户该怎么问。模型不可用时返回 None，调用方照旧给原来那句兜底。
    """
    turn = _llm()
    # **整轮预算快见底时，给这一步单独留一小段。**
    #
    # 它总是排在最后：工具、规则、检索都试过了才轮到它。前面几次带思考的
    # 调用加起来常常就把 30 秒用光——实测注入测试那句跑满 30.4 秒，
    # 最后只剩一句「没太理解你想做什么」。这是用户**唯一**能拿到的一句话，
    # 为它多等十几秒，比给一句死路强。
    left = getattr(turn, "left", None)
    if left is not None and left < CONVERSE_RESERVE:
        turn = llm_budget.for_turn(provider, CONVERSE_RESERVE)
    if turn is None or not getattr(turn, "available", False):
        return None
    messages = list(_turn_dialogue.get()) + [
        {"role": "user", "content": payload.text}]
    related = [t.get("title") if isinstance(t, dict) else t
               for t in (related or _turn_related.get())]
    related = [t for t in related if t]
    try:
        system = _CONVERSE_SYSTEM + (_NO_BASIS_HINT if no_basis else "")
        # 不告诉它是谁，它会把房东的功能许给中介（实测：「带看完客户想签」
        # 答「我可以帮你发起签约」，而中介账号发起不了）
        system += "\n\n当前用户身份：%s。" % _ROLE_NAMES.get(
            _whoami(payload.token)["role"], "未登录的访客")
        if no_basis and related:
            system += _RELATED_HINT % "、".join("「%s」" % t for t in related)
        text = turn.chat(system, messages, max_tokens=400, temperature=0.3)
    except LLMUnavailable:
        return None
    except Exception:                                        # noqa: BLE001
        return None
    text = (text or "").strip()
    if not text:
        return None
    out = {"intent": "support", "status": "answered", "ok": True,
           "message": text, "items": [], "alternatives": [],
           "route": dict(routed or {}, tool="converse", reason=reason),
           "route_source": "converse"}
    if no_basis:
        # 模型会说「可以点转人工」——那个按钮得真的在。
        # 网页看 handoff 字段决定画不画，这里补上，和客服层拒答时一个形状。
        out["status"] = "answered_no_basis"
        out["handoff"] = {"question": payload.text, "reason": "知识库没有依据"}
        if related:
            out["alternatives"] = related
    elif _COMPLAINT.search(payload.text or ""):
        # 生气、投诉的人最需要的是能找到真人。
        out["handoff"] = {"question": payload.text, "reason": "用户在投诉或抱怨"}
    return out


_COMPLAINT = re.compile(r"生气|投诉|不满|管不管|太差|气死|骗子|被骗|举报|不回我|没人管")


def _with_card_reminder(message, pending):
    """卡片还挂着时补一句。和追问不同，这里没有「继续」可说——
    卡片本身就在上面，要么确认要么算了。"""
    card = pending.get("card") or {}
    return "%s\n\n（刚才那张「%s」确认卡还在：%s。要办就说「好的」或点卡片上的确认，不办就说「算了」。）" % (
        message or "", card.get("label") or support._pending_label(pending),
        (card.get("summary") or "").rstrip("。"))


def _with_pending_reminder(message, pending):
    """在回答后面补一句「刚才那件事还没办完」。

    追问句本身常以问号结尾，直接接「。」会出现「要拍张照片吗？。」——
    先把末尾标点剥掉再拼。
    """
    where = support._pending_where(pending).rstrip("。.!！?？ ")
    return "%s\n\n（刚才的「%s」还没办完，%s。说「继续」就接着来。）" % (
        message or "", support._pending_label(pending), where)


def _keep_escape_hatches(talked, refused):
    """对话兜底替掉了拒答的那句话，但拒答附带的出路要留着：
    「猜你想问」和转人工按钮。用户正是答不上来的时候最需要它们。"""
    for key in ("alternatives", "handoff", "log_id"):
        if refused.get(key):
            talked[key] = refused[key]
    talked["status"] = "answered_no_basis"
    return talked


# 「你能帮我干什么」。知识库里有一条「智能客服能帮我做哪些事」，
# 但实测这句话检索不中，回了一句「暂时没有查到相关规定」——
# 用户第一次问助手会什么，助手说没查到。这件事答案是固定的，不必检索。
_CAPABILITY = re.compile(
    r"(你|您)?(都|还)?(能|可以|会)(帮我|帮忙|为我)?(做|干|办)(些|点)?(什么|啥|哪些)(事|事情)?(呀|啊|呢|吗)?|"
    r"(你|您)(有|都有)(什么|哪些)(功能|本事|能力)|(你|您)是干(什么|啥)的")

_CAPABILITY_REPLY = (
    "我能帮你做这几类事：\n"
    "· 找房：一句一句补条件就行（「中山区两室一厅」「三千以内」「近地铁」），"
    "还能介绍某一套、看评价、对比两套；\n"
    "· 查你自己的：账单、租约、看房预约、报修进度、收藏、浏览记录，"
    "或者直接问「我还有什么没处理的」；\n"
    "· 代你办：交房租、约看房、改约或取消、报修、续租、退租、收藏——"
    "我会先把要办的事列成一张卡片，你点确认才执行；\n"
    "· 房东还能查名下房源、谁约了看房、租客和收入，处理预约、上下架；\n"
    "· 平台规则（押金、退租、水电、报修时限）按知识库回答，查不到会直说。")


def _small_talk(text):
    """整句寒暄就回一句寒暄，返回 None 表示这不是寒暄。"""
    stripped = (text or "").strip().strip("。.!！~、,，?？ ")
    if stripped and len(stripped) <= 14 and _CAPABILITY.fullmatch(stripped):
        return _CAPABILITY_REPLY
    if not stripped or len(stripped) > 8:
        return None
    for pattern, reply in _SMALL_TALK:
        if pattern.fullmatch(stripped):
            return reply
    return None


# 这句话是不是在**问**什么。
#
# 「接不了就转去问知识库」这条兜底有个前提：用户确实在提问。
# 「我想租房」「随便看看」也解析不出条件，但它们不是问题，转去检索
# 只会拿回一条风马牛不相及的规定——实测「我想租房」换来一句
# 「任一方提前解除合同的，应提前 30 日书面通知对方」。
#
# 闸门对这类没有实质内容的短句本来就偏松（「怎么办」两个词就能把
# 覆盖率顶到 1.0），所以**别把不是问题的话送进去**。
_ASKING = re.compile(
    r"[?？]|怎么|如何|什么|哪|谁|多久|多长|几[点天个日月年]|"
    r"为什么|能不能|可不可以|要不要|算不算|是否|有没有|吗|呢")


def _is_question(text):
    return bool(_ASKING.search(text or ""))


# 问钱、问责任、问要多久——这类问题里常带着业务名词（维修、退租、押金），
# 但要的是规矩，不是去办。
#
# 时长只认「多久/几天 + 能修好/来人」这种**问法**：光一个「几天」会把
# 「空调坏了好几天了」这种正经报故障也拦成提问，报修单再也开不出来。
_COST_OR_LIABILITY = re.compile(
    r"谁出|谁付|谁负责|谁承担|谁来付|算谁的|多少钱|收费|要钱|扣多少|赔多少|费用(多少|怎么)"
    r"|(多久|几天|多长时间)(才|能|会|可以)?(修|来|到|处理|上门|好|有人)")


# 指向「住处」的名词。意图词表认不出的说法太多，但只要句子里出现
# 这些词，再配上任意一个解析出来的找房条件，就不会是别的意思。
# 「房租」「房东」不在内——它们含「房」字，说的却不是住处本身。
_HOUSE_NOUN = re.compile(r"房子|房源|房屋|有什么房|有房|住处|住的地方|公寓|单间")

# 只在**某一套房的范围内**才讲得通的问题。
#
# 「周边有什么」「几楼」「朝向」——这些问的是一套具体的房子。
# 没有任何房源在场时它们答不了，而**答不了要说答不了**：
# 实测路由器把「周边有什么」当成了要推荐（它认「有什么」这个词），
# 于是推了六套房，用户会以为那是他问的那套房的周边配套。
_HOUSE_SCOPED = re.compile(
    r"周边|附近|几楼|楼层|朝向|采光|电梯|阳台|家具|宠物|做饭|开火|"
    r"押金多少|租金多少|多少钱一个月|这套|那套")

# 「找一个 / 想租 / 有没有」这类**要东西的说法**。和上面的名词是一对：
# 「我想找个两室」里没有「房」字，可它和「中山区的房子」一样确定。
# 收得很窄（只认成组的说法，不认单个「找」「有」字），
# 免得把「遇到假房源找谁」这类问句一并划过去。
_HUNT_VERB = re.compile(r"找房|租房|想找|要找|帮我找|找个|找一个|找一套|找套|"
                        r"租个|租一个|想租|要租|有没有|推荐")

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


# 「推荐」后面跟的**明显不是房子**的东西。
#
# `_WANT_RECOMMEND` 只认「推荐」两个字，而这个兜底挂在「客服检索
# 答不上来」之后——于是任何它答不上、又恰好带「推荐」的句子都会
# 拿到六套房。实测「帮我推荐几只股票」就是这么变成房源推荐的：
# 规则判 unknown 0.0、模型明写「与租房平台业务无关」，两边都对，
# 却被这一行兜底推翻。
#
# **答非所问比说「不知道」更糟**——用户问股票拿到房子，会以为
# 这个助手连中文都没读懂。这条兜底的本意是接住「帮我挑一套」，
# 不是接住所有带「推荐」的话。
_NOT_HOUSE = ("股票", "基金", "理财", "彩票", "电影", "电视剧", "音乐",
              "歌", "书", "餐厅", "饭店", "菜", "外卖", "旅游", "景点",
              "酒店", "手机", "电脑", "车", "医生", "医院挂号", "学校报名")


def _wants_recommendation(text):
    body = text or ""
    if any(word in body for word in _NOT_HOUSE):
        return False
    return any(word in body for word in _WANT_RECOMMEND)


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
        # **「周边有什么」这类问的是某一套房，不是要推荐。**
        #
        # 路由器把「有什么」当成了要推荐的信号（它的描述里写着
        # 「问有什么合适的」），于是用户指着一套房问周边配套，
        # 拿到的是六套别的房子。实测：列表为空时问「周边有什么」，
        # 系统推了 6 套房，用户以为自己问的那套房周边有这六样东西。
        #
        # 这类问题该落在 house_detail 上；落不到（没有任何房源在场）
        # 时正确的回答是问他指哪一套，而不是拿推荐去填这个空。
        if (_HOUSE_SCOPED.search(payload.text or "")
                and not (getattr(payload, "house_no", "")
                         or payload.last_house_nos)):
            return {
                "intent": "support", "status": "answered", "ok": True,
                "route": routed, "route_source": decision["source"],
                "alternatives": [],
                "message": "你想问哪一套的情况？先告诉我条件我帮你找几套，"
                           "或者直接说房源编号（比如 DL0007）。",
            }
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
    回一句「没听懂」会让人以为是自己表达有问题，其实是数字超了。

    `count == 0` 要单独说。套用同一句模板会生成「说个 1 到 0 之间的
    序号」——**这比「没听懂」更让人困惑**，因为它听起来像个正经指令，
    用户会真的去想 1 到 0 之间是什么。这一支恰恰不是「序号超了」，
    是**还没有列表**，该给的引导也完全不同。
    """
    if count <= 0:
        message = ("我还没给你列过房源，所以没有「第几套」可以指。"
                   "先说说你想要什么样的房子，比如「中山区两室一厅三千以内」。")
    else:
        message = ("这一组我只列了 %d 套，没有你说的那一套。"
                   "说个 1 到 %d 之间的序号，或者说「再多看几套」。"
                   % (count, count))
    return {
        "intent": "support", "status": "answered", "ok": True,
        "route": routed, "route_source": decision["source"],
        "alternatives": [],
        "message": message,
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

    # 先看这句话是不是在问某个**具体条件**（能养宠物吗、有电梯吗、
    # 周边有什么）。是的话就回答那一条——`pitch.describe()` 只看房源、
    # 不看问题，所以这类是非题会统统拿到同一段推销词，用户看不出
    # 自己的问题有没有被回答，只能再问一遍。
    direct = pitch.answer_attribute(rows[0], payload.text)
    if direct:
        # 答「没有标注」时顺手给条出路：整站有别的房子符合，
        # 不然用户只知道这套不行，不知道下一步该干什么。
        tail = ""
        if "没有标注" in direct:
            tail = " 想看有这个条件的房子，直接跟我说，我帮你筛。"
        message, data = direct + tail, {"house": rows[0]}
    else:
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
    direct = tool_runner.direct_list_answer(payload.text, picked, nos, _fetch_houses)
    if direct:
        return dict(direct, intent="support", status="answered", ok=True,
                    route=routed, route_source=decision["source"], alternatives=[])
    if len(picked) < 2:
        picked = nos[:2]
    rows = _fetch_houses(picked[:3])
    if len(rows) < 2:
        return _no_reference(routed, decision, "对比")

    message, data = pitch.compare(backend_client, rows)
    message = tool_runner.distance_note(payload.text, rows) + message
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


# 问的是**列表里哪一套**：「这几套哪个最便宜」「第一套和第二套哪个采光好」
_ASKS_WHICH_OF_LIST = re.compile(r"哪(个|套|一套|间)|这几套|那几套|刚才那几|第.{1,3}套和")

# 问的是「这个词什么意思 / 规则怎样」，不是在加筛选条件
_ASKS_MEANING = re.compile(r"什么意思|是什么|啥意思|怎么|为什么|如何|区别|能不能|可以吗|合法|规定")


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
        context_text=memory.describe(slots, feats),
        dialogue=list(_turn_dialogue.get())[-4:])
    decision_cache["plan"] = plan

    name = plan["tool"]
    if name == "none" or plan["confidence"] < MIN_CONFIDENCE:
        return None

    # 「押一付三的呢」——正在找房，这句话里又解析得出一个找房条件，就是在改条件。
    # 实测选择器挑了 converse，回了一句「你确认一下，我按这个条件再帮你搜？」；
    # 他要的就是搜。条件是规则解析出来的，确定性的，不必再问模型一遍。
    # 挑成 knowledge 也一样（「押一付三」恰好是知识库里的词）；
    # 但「押一付三是什么意思」是真在问规则，不能改。
    if (name in ("converse", "knowledge") and slots
            and not _ASKS_MEANING.search(payload.text or "")
            and memory.parse_query(memory.strip_negated(payload.text))["filters"]):
        plan = dict(plan, tool="search_houses", params={},
                    why="选中了%s，但句子里有找房条件且正在找房，改为找房" % name)
        name = "search_houses"
        decision_cache["plan"] = plan

    if name == "converse":
        # 接不住（模型不可用、没说出话）就退回规则，和其他工具同一个约定
        return _converse(payload, routed, tool_planner.describe(plan))

    tool = tool_catalog.BY_NAME.get(name) or {}

    # 「提醒我交房租」——选择器常挑 pay_bill。他要的是欠多少、哪天到期，
    # 不是现在就付；按查账单处理（规则那一路由 actions.detect 同样放过）。
    if name == "pay_bill" and support_actions.reminds_me(payload.text):
        name, tool = "my_bills", tool_catalog.BY_NAME["my_bills"]
        plan = dict(plan, tool=name, params={},
                    why="选中了缴费，但用户是要提醒，改为查账单")
        decision_cache["plan"] = plan

    # 写操作**一步都不执行**，转成已有的两阶段确认卡片。
    # 这一层只负责说「他想做的是 book_viewing」，卡片长什么样、
    # 缺哪个槽位要追问、参数能不能过白名单、最后谁去调后端，
    # 全在原来那条路径上，一行没动。
    if tool.get("kind") == "write":
        # **疑问句护栏，与 support_router 里那一道同一个理由。**
        #
        # 那道护栏只装在老路由上，模型选择器这条新路没有——实测
        # 房东问「租客说热水器坏了，维修费谁出」，选择器挑了 submit_repair，
        # 直接弹出一张报修卡。提问被当成指令的代价是不对称的：
        # 多一张确认卡是惊吓，手滑点了是真的去办。
        if (not support_actions.detect(payload.text)
                and support_actions.asks_rather_than_requests(payload.text)):
            plan = dict(plan, why="选中了%s，但句子是提问，改为知识型" % name)
            decision_cache["plan"] = plan
            answered = _knowledge(payload, routed, plan)
            if answered is not None:
                return answered
            # 知识库没依据就直接对话兜底。**别退回规则**——规则链路会把
            # 检索和闸门整套再跑一遍，实测「帮我写一份退租申请书」两轮检索
            # 耗满 30 秒预算，轮到对话兜底时已经没时间了。
            return _converse(payload, routed, plan["why"], no_basis=True)
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
        # 选择器有把握说这是规则问题、知识库又答不上时，直接对话兜底。
        # 退回规则链路的话，规则会按关键词再判一次——实测房东问
        # 「租客说热水器坏了，维修费谁出」，选择器判对了（knowledge，0.95），
        # 退回去之后被规则的「坏了」判成报修，开了一张报修单。
        if plan.get("confidence", 0) >= 0.85:
            return _converse(payload, routed, tool_planner.describe(plan),
                             no_basis=True)
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
        history=user_turns(payload.history),
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
        _turn_related.set(tuple(result.get("alternatives") or ()))
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
        history=user_turns(payload.history),
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
                                    history=user_turns(payload.history),
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
                             history=user_turns(payload.history),
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
        # 解析的是**去掉否定那几截之后**的话：「不用两室一厅，三室的有吗」
        # 整句去解析，户型取到的是被否定的两室一厅
        cur_filters, cur_features, source, divergence = \
            property_finder.understand(memory.strip_negated(text) or text,
                                       provider=_llm())
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
        run_out = False
        if more and slots:
            # 接着上一批往下翻。条件用合并后的 merged——它是历史轮次
            # 重放出来的，正是上一批用的那一组
            seen = int(payload.seen or 0)
            page = max(1, seen // PAGE_SIZE + 1)
            # **上一批不满一页，就说明那已经是最后一批。**
            #
            # 只按 seen // PAGE_SIZE 算页码时，seen=3、PAGE_SIZE=10
            # 算出来还是第 1 页：用户问「还有别的吗」，拿到的是一模一样
            # 的三张卡片，配着一句「为你找到 3 套」——读起来像是又搜了
            # 一次、又找到三套，他根本分不清这是新的还是刚才那批。
            #
            # seen 不是整页数的唯一原因就是上一批没取满，所以这里
            # 直接判定到底，不必再查一次库。
            run_out = bool(seen) and seen % PAGE_SIZE != 0

        result = property_finder.run(text, provider=_llm(),
                                     filters=merged, features=features,
                                     source=source, page=page,
                                     limit=PAGE_SIZE)

        # 一个条件都没说、而话里又在要推荐 —— 给推荐，别把问题推回去。
        if result.get("status") == "unparsed" and _wants_recommendation(text):
            picked = _recommend(payload, routed, slots, feats)
            if picked is not None:
                return picked

        # **「遇到假房源找谁」问的是规矩，不是要找房。**
        #
        # 词表里「房源」是找房的强关键词，于是这类问题一律被送进找房，
        # 而句子里一个筛选条件也没有，回一句「没听懂你的找房需求」——
        # 这句话的答案明明就写在知识库里。
        #
        # 判据同预约、报修那两处：找房自己说 `unparsed`，就是它接不了。
        # 检索答得上来才替换，答不上来仍旧回原来那句，
        # **真的在找房、只是没说清条件的人不受影响**。
        if result.get("status") == "unparsed" and _is_question(text):
            asked = support.run(text, token=payload.token,
                                history=user_turns(payload.history),
                                provider=_llm(), backend=backend_client,
                                settings=settings, house_no=resolved_house)
            if asked.get("status") == "answered":
                asked["route"] = routed
                return asked

        result["route"] = routed
        result["context"] = merged
        result["context_text"] = memory.describe(merged, features)
        result["notes"] = notes
        # 认不出区名要说在正文里——实测「公司在高新园区」只得到一句
        # 「没有找到符合条件的房源」，看不出是卡在了区名上
        unknown = [n for n in notes if memory.speaks_up(n)]
        if unknown:
            result["message"] = " ".join(unknown) + " " + (result.get("message") or "")
        result["page"] = page
        if page > 1 or run_out:
            # 追加而不是另起一列。**序号必须接着数下去**——
            # 翻一页就从 1 重新数，用户刚记住的「第 3 套」就换了一套房
            result["append"] = True
            if run_out:
                # 已经到底：把这一页的结果丢掉，不然又是刚才那几张卡片
                result["items"] = []
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
                             history=user_turns(payload.history),
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
        # 「去合同页点智能审阅」只回给**问自己那份合同条款**的人。
        # 实测「最短能租几个月」「带看完客户想签，怎么操作」都被支去点审阅，
        # 他们手上根本没有要审的合同。
        if not _ABOUT_MY_CONTRACT.search(text):
            talked = _converse(payload, routed, "合同类问题检索无依据",
                               no_basis=True, related=result.get("alternatives"))
            if talked is not None:
                return _keep_escape_hatches(talked, result)
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
        # **「报修要不要拍照」问的是规矩，不是在报故障。**
        #
        # 和预约那边一样：词表见到「报修」就往报修流程里送，于是
        # 「什么情况算紧急报修」「报修多久上门」「屋里的家电坏了算谁的」
        # 都被分析成一条「其他 / 一般」的故障单——分析了个寂寞。
        #
        # 判据是故障分类器**自己说没认出来**（类别落到「其他」）：
        # 真的在报故障的话，「漏水」「不亮」「冲不下去」都归得进具体
        # 类别。这时让检索先试一次；检索答不上来，照旧走报修流程，
        # 所以**「洗衣机坏了」这类归不了类的真故障也不会被吞掉**。
        probe = repair.run(text, provider=None, submit=False)
        # **故障归得进类，也可能是在问钱和责任。**
        #
        # 实测房东问「租客说热水器坏了，维修费谁出」：「热水器坏了」归得进
        # 电器类，于是上面那条「归不了类才先问知识库」放过了它，直接开了
        # 一张报修单，还追问要不要拍照。问的是谁掏钱，不是要报修。
        # 「水管漏水了怎么办」这种真在报故障的不受影响——它问的是怎么办，
        # 不是钱、责任、时长。
        asks_terms = bool(_COST_OR_LIABILITY.search(text)) and \
            not support_actions.detect(text)
        if ((probe.get("category") or "其他") == "其他" and _is_question(text)) \
                or asks_terms:
            asked = support.run(text, token=payload.token,
                                history=user_turns(payload.history),
                                provider=_llm(), backend=backend_client,
                                settings=settings, house_no=resolved_house)
            if asked.get("status") == "answered":
                asked["route"] = routed
                return asked
            if asks_terms:
                talked = _converse(payload, routed, "问的是费用或责任，不开报修单",
                                   no_basis=True, related=asked.get("alternatives"))
                if talked is not None:
                    return _keep_escape_hatches(talked, asked)
                # 模型也用不上时，给知识库那句拒答（带转人工），**仍然不开报修单**
                asked["route"] = routed
                return asked

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
                             history=user_turns(payload.history),
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

        # **「预约了怎么取消」问的是规则，不是要约看房。**
        #
        # 关键词表见到「预约」「看房」就交给预约流程，于是
        # 「几点可以看房」「看房要给带看费吗」「预约完多久房东会确认」
        # 统统得到一句「没听清你想什么时候看房」——用户问的是规矩，
        # 助手追问的是时间，两边说的不是一件事。
        #
        # 判据不靠猜：预约流程连日期带时刻**一个都没解析出来**
        # （`matched` 为空），就说明这句话里根本没有时间，它接不了。
        # 这时候让检索试一次，答得上来就答，答不上来仍旧回原来那句
        # 追问——**真的要约看房的人不受任何影响**，他们句子里有时间。
        if (result.get("status") == "need_time"
                and not (result.get("parsed") or {}).get("matched")
                and _is_question(text)):
            asked = support.run(text, token=payload.token,
                                history=user_turns(payload.history),
                                provider=_llm(), backend=backend_client,
                                settings=settings, house_no=resolved_house)
            if asked.get("status") == "answered":
                asked["route"] = routed
                return asked

        result["route"] = routed
        if house_no:
            result["house_no"] = house_no
            result["message"] = "（你说的是 %s）%s" % (
                house_no, result.get("message", ""))
        return result

    if intent == "unknown":
        chat_back = _small_talk(text)
        if chat_back:
            return {"intent": "support", "status": "answered", "ok": True,
                    "route": routed, "message": chat_back, "items": []}

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
                             history=user_turns(payload.history),
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
            # 工具、规则、检索都接不住。这一步之前直接回「没太理解」，
            # 现在先让模型拿着完整对话正常说话——它答得出的是对话层面的事
            # （回顾、总结、澄清），答不出的也会告诉用户怎么问。
            talked = _converse(payload, routed, "规则与检索都未命中，按对话回应",
                               no_basis=True, related=result.get("alternatives"))
            if talked is not None:
                return _keep_escape_hatches(talked, result)
            # 连模型也用不上，这才是真的没听懂。此时把找房与预约的
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
