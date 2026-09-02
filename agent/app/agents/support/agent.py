"""AI-03 智能客服问答（RAG）。

一次问答的完整链路：

    ① 转人工请求？     → 直接走转人工，不浪费一次检索
    ② **代办请求？**    → 生成确认卡片，等用户点确认才执行（绝不直接做）
    ③ 事务型还是知识型？ → 事务型查后端真实数据，知识型才走检索
    ④ 多轮指代消解      → 「那它多久到账」补成「押金多久到账」
    ⑤ 同义扩展 + 多路检索 + RRF 融合
    ⑥ 确定性精排，取 top-3
    ⑦ 可答性闸门       → 不过就拒答，给「猜你想问」和转人工出口
    ⑧ 受约束生成 + 引用校验 → 不过就退回抽取式

第 ② 步是这个客服从「能查」走到「能做」的分界。它**只提议不执行**：
真正的写操作在 `/agent/support/confirm`，必须由用户点过确认才会被调用。

每一步的中间结果都塞进返回值的 `retrieval` 字段里。这不是调试残留：
前端要用它渲染「正在理解问题 → 正在检索知识库（命中 3 条）→ 正在核对依据」
的思考态，答辩时也要用它当场说清楚「为什么是这条、为什么拒答」。
"""

import time

from app.agents.support import corpus
from app.agents.support.tools import (actions, answer as answer_tool,
                                      classify_ask, gate, house as house_tool,
                                      toolcall)
from app.backend import BackendError
from app.retrieval.embed import VectorCache, build_embedder
from app.retrieval.index import KnowledgeIndex

# 检索候选数与最终送进生成的材料数。
#
# 取 8 精排到 3，依据是检索实验里的一条结论：精排的增益集中在很小的 k 上，
# top-2 明显、top-5 归零。所以宁可少送几条准的，也不要靠数量堆召回——
# 材料越多，模型越容易从不相关的那几条里抓一句话来编。
CANDIDATE_K = 8
CONTEXT_K = 3

# 知识库刷新间隔（秒）。公告是管理员随时可能发的，不能只在启动时拉一次；
# 但也不必每次提问都拉——十分钟的滞后对公告完全可以接受。
REFRESH_INTERVAL = 600

# 指代词：出现这些且句子很短，说明这一轮在承接上文
_ANAPHORA = ["它", "那个", "这个", "那", "这", "此", "上面说的", "刚才"]


class KnowledgeBase(object):
    """常驻的知识索引，带过期刷新。

    语料来源的优先级是**后端优先、本地兜底**：

    - 后端 `/api/faqs` 是权威源，管理员在后台改完立即生效；
    - 拿不到后端（没启动、网络不通、测试环境）就用
      `knowledge/faq.md`。

    这个兜底不是可有可无的。项目的自动化测试口径是「不依赖网络与数据库」，
    没有本地语料，这个功能的全部测试就都得起一个后端才能跑。
    """

    def __init__(self, backend=None, settings=None):
        self.backend = backend
        self.settings = settings
        self.index = None
        self.loaded_at = 0.0
        self.sources = {"faq": 0, "notice": 0}
        self.origin = "none"

    def _load_faq(self):
        """FAQ：后端优先，失败退回本地 Markdown。"""
        if self.backend is not None:
            try:
                data = self.backend.get("/api/faqs")
                rows = data.get("list", []) if isinstance(data, dict) else data
                chunks = [c for c in (corpus.faq_to_chunk(r) for r in rows or [])
                          if c]
                if chunks:
                    self.origin = "backend"
                    return chunks
            except BackendError:
                pass
            except Exception:
                pass
        self.origin = "local"
        return corpus.load_faq()

    def _load_notices(self):
        if self.backend is None:
            return []
        try:
            data = self.backend.get("/api/notices")
            rows = data.get("list", []) if isinstance(data, dict) else data
        except BackendError:
            return []
        except Exception:
            return []
        return [c for c in (corpus.notice_to_chunk(r) for r in rows or []) if c]

    def refresh(self, force=False):
        if (self.index is not None and not force
                and time.time() - self.loaded_at < REFRESH_INTERVAL):
            return self.index

        faq = self._load_faq()
        notices = self._load_notices()
        chunks = faq + notices

        embedder = None
        cache = None
        model = ""
        if self.settings is not None and getattr(self.settings, "embed_base_url", ""):
            embedder = build_embedder(self.settings)
            model = self.settings.embed_model
            cache = VectorCache(self.settings.embed_cache_path).load()

        self.index = KnowledgeIndex(chunks, embedder=embedder,
                                    cache=cache, model=model)
        self.sources = {"faq": len(faq), "notice": len(notices)}
        self.loaded_at = time.time()
        return self.index


# 进程内单例。索引重建是毫秒级的，但每次请求都重建会白白吃掉几十毫秒，
# 而且会让向量通道每次都去读缓存文件。
_kb = None


def knowledge_base(backend=None, settings=None):
    global _kb
    if _kb is None:
        _kb = KnowledgeBase(backend=backend, settings=settings)
    return _kb


def reset_knowledge_base():
    """测试用：丢掉单例，让下一次调用重新装载。"""
    global _kb
    _kb = None


# ---------------------------------------------------------------- 多轮

def rewrite(text, history):
    """指代消解：把「那它多久到账」补成能独立检索的问句。

    只在**句子短且含指代词**时才补，而且是把上一句原样拼在前面，
    不调大模型。理由是这一步的收益全在检索侧——检索是词袋模型，
    多几个上文的关键词就够把候选拉对了，不需要真正通顺的改写。
    调一次大模型来换一个更漂亮的句子，不划算。
    """
    raw = (text or "").strip()
    if not history or len(raw) > 15:
        return raw, ""
    if not any(word in raw for word in _ANAPHORA):
        return raw, ""
    previous = history[-1].strip()
    if not previous:
        return raw, ""
    return "%s %s" % (previous, raw), previous


# ---------------------------------------------------------------- 主流程

def _shape(status, message, **extra):
    """统一返回形状，与其它子智能体保持一致，前端只处理一种结构。"""
    payload = {
        "intent": "support",
        "question": "",
        "label": "客服问答",
        "status": status,
        "ok": status in ("answered", "account"),
        "message": message,
        "citations": [],
        "alternatives": [],
        # 可点选项与未完成的代办。前端把 pending 原样带回来，
        # 下一轮就能接上——服务端不存会话态。
        "options": [],
        "pending": None,
        "retrieval": {},
    }
    payload.update(extra)
    return payload


# 检索完全没沾边时，「猜你想问」改用这几类里的代表问题。
# 覆盖率低于这个值就认为 top-3 是噪声。
SUGGEST_FLOOR = 0.20

# 兜底建议的分类顺序：按用户最常问的排
FALLBACK_CATEGORIES = ["押金", "费用", "看房", "报修", "退租"]


def suggest(index, query, candidates, cover):
    """拒答时的「猜你想问」。

    **检索完全没沾边时不能拿 top-3 充数。** 问「预期收入」时检索的前三名是
    「入住时水电表读数要核对吗」「站内消息在哪看」这种毫不相关的条目，
    把它们当建议推给用户，比不给建议更糟——用户会觉得这个客服在乱答。

    所以分两种情况：沾了点边（覆盖率过得去）就用检索结果，
    确实一点没沾就退回按分类挑的常见问题，等于告诉用户
    「我会答的是这几类」。
    """
    if cover >= SUGGEST_FLOOR:
        titles = [(index.get(cid) or {}).get("title", "") for cid in candidates[:3]]
        titles = [t for t in titles if t]
        if titles:
            return titles

    # 每个分类取第一条，保证建议之间是分散的而不是挤在同一个话题里
    picked = []
    for category in FALLBACK_CATEGORIES:
        for chunk_id in index.order:
            chunk = index.get(chunk_id) or {}
            if chunk.get("category") == category:
                picked.append(chunk.get("title", ""))
                break
        if len(picked) >= 3:
            break
    return [p for p in picked if p][:3]


def _citation(index, chunk_id):
    chunk = index.get(chunk_id)
    if not chunk:
        return None
    body = chunk.get("body", "").replace("**", "")
    return {
        "id": chunk["id"],
        "title": chunk.get("title", ""),
        "source": chunk.get("source", "faq"),
        "category": chunk.get("category", ""),
        "snippet": body[:120] + ("…" if len(body) > 120 else ""),
        "link": chunk.get("link"),
    }



# ---------------------------------------------------------------- 多轮代办

def _fill_slot(ask, text):
    """把用户这一轮说的话填进上一轮问的那个槽。

    有选项就先按选项匹配（用户多半是点了按钮或说了「第一个」），
    没选项就把整句话当值——时间这类槽本来就得靠解析器去读原话。
    """
    if not ask:
        return ""
    options = ask.get("options") or []
    if options:
        picked = actions.pick_option(text, options)
        if picked:
            return picked
        # 有选项但一个都没对上：可能用户输入的是选项之外的值（比如直接
        # 报了个房源编号），仍然收下，交给后端去判断合不合法
    return (text or "").strip()


def _resume(raw, pending, token, backend, house_no, shaped):
    """承接上一轮没做完的代办。返回结果字典，或 None 表示接不上、走正常流程。

    **「接不上就放手」是这里最重要的一条。** 用户问到一半改主意去问别的
    很常见，如果硬把新的一句话往旧槽位里塞，就会出现
    「你问押金怎么退，我却把它当成了房源编号」这种荒唐事。
    所以只有在能明确接上时才继续，其余一律放弃 pending。
    """
    name = pending.get("action")
    if not name:
        return None

    # ---- 用户在回应确认卡片 ----
    card = pending.get("card")
    if card:
        if actions.is_deny(raw):
            return shaped("cancelled", "好，那就不做了。")
        if actions.is_affirm(raw):
            # 高风险操作不接受口头确认。「好的」两个字太容易脱口而出，
            # 而这一类是花钱或不可逆的——多点一下按钮不算麻烦。
            if card.get("risk") == actions.RISK_HIGH:
                return shaped(
                    "confirm",
                    "这一步涉及金额或不可撤销，请点一下「确认执行」我再动手。",
                    action=card, pending={"action": name, "card": card},
                    retrieval={"mode": "action", "action": name,
                               "note": "高风险操作不接受口头确认"})
            return shaped("confirm", "好，这就去办。", action=card,
                          auto_confirm=True,
                          retrieval={"mode": "action", "action": name,
                                     "note": "用户口头确认，前端可直接执行"})
        # 既不肯定也不否定，说明改话题了，放弃这张卡片
        return None

    # ---- 用户在回答上一轮的追问 ----
    ask = pending.get("ask")
    if not ask:
        return None

    # **否定要先于填槽判断。** 漏了这一条会出大问题：
    # 「取消预约」→「要取消哪一条？」→ 用户说「算了」，
    # 「算了」被当成了槽位值填进去，反而生成了一张取消预约的确认卡片——
    # 用户明明在喊停，系统却把事情往前推了一步。
    if actions.is_deny(raw):
        return shaped("cancelled", "好，那就先不弄了。")

    # 用户中途改口去做别的事，就别再往旧槽里塞
    if actions.detect(raw) and actions.detect(raw) != name:
        return None

    value = _fill_slot(ask, raw)
    if not value:
        return None

    slots = dict(pending.get("slots") or {})
    slots[ask["slot"]] = value
    context = dict(slots)
    context.setdefault("house_no", house_no)

    status, message, new_card, new_ask = actions.propose(
        name, raw, token, backend, context=context)
    return _action_result(name, status, message, new_card, new_ask, slots, shaped)


def _action_result(name, status, message, card, ask, slots, shaped):
    """把提议的结果整理成统一的返回形状。"""
    if status == actions.CONFIRM:
        return shaped("confirm", "要我帮你%s吗？" % card["summary"],
                      action=card,
                      pending={"action": name, "card": card},
                      retrieval={"mode": "action", "action": name,
                                 "risk": card["risk"],
                                 "note": "已生成确认卡片，等待用户确认后才执行"})
    if status == actions.NEED_SLOT:
        return shaped("need_slot", message,
                      options=(ask or {}).get("options", []),
                      pending={"action": name, "slots": slots, "ask": ask},
                      retrieval={"mode": "action", "action": name,
                                 "note": "缺少 %s，已追问"
                                         % (ask or {}).get("slot", "信息")})
    if status in (actions.NEED_LOGIN, actions.FAILED):
        return shaped(
            "need_login" if status == actions.NEED_LOGIN else status, message,
            retrieval={"mode": "action", "action": name, "note": message})
    if message:
        return shaped("answered", message,
                      retrieval={"mode": "action", "action": name})
    return None


def run(text, token="", history=None, provider=None, backend=None,
        settings=None, house_no="", pending=None):
    """回答一个客服问题。

    `pending` 是上一轮没做完的代办：问了哪个槽、已经填了什么、
    或者已经摆出的确认卡片。它**不存在服务端**——随响应发给前端、
    下一轮再带回来，与确认卡片同一个理由：无状态就没有
    「接的是上一条还是上上条」的竞态。
    """
    started = time.time()
    raw = (text or "").strip()
    if not raw:
        return _shape("unknown", "你想问什么？比如「押金怎么退」「水电费怎么算」。")

    def shaped(status, message, **extra):
        # 每条返回都带上原问题，日志与前端反馈都要用它定位这一轮
        extra.setdefault("question", raw)
        return _shape(status, message, **extra)

    # ⓪ 先看这一句是不是在接上一轮没做完的事：回答追问，或者对着
    # 确认卡片说「好的」。接得上就继续，接不上就当新话题往下走。
    if pending:
        resumed = _resume(raw, pending, token, backend, house_no, shaped)
        if resumed is not None:
            return resumed

    # ① 孤零零一句「好的」「算了」，前面却没有待办。
    # 不拦的话会一路走到检索，回一句「暂时没有查到相关规定」——
    # 用户只是应了一声，却被告知知识库里查不到，莫名其妙。
    #
    # **必须先排除这句话本身是个指令。** 否定词「取消」与动作词
    # 「取消预约」是撞的——不加这一层，用户说「取消预约」会被当成
    # 一句「算了」应付掉，整个取消功能就废了。实测踩过。
    if (not actions.detect(raw)
            and (actions.is_affirm(raw) or actions.is_deny(raw))):
        return shaped("answered",
                      "嗯，还有什么要我帮忙的吗？可以问平台规则，"
                      "也可以让我帮你交房租、约看房、报修。")

    # ② 明确要转人工，不必先检索一遍
    if classify_ask.wants_handoff(raw):
        return shaped("escalate",
                      "好的，我把你的问题转给管理员，稍后会通过站内消息回复你。",
                      handoff={"question": raw, "reason": "用户主动要求转人工"})

    # ③ 代办请求：生成确认卡片，**不执行**。
    #
    # 必须排在事务型查询前面。「帮我把房租交了」里有「房租」，
    # 放到后面会被 bill 当成查询，回一句「你还有 35 笔待缴」——
    # 用户要的是把它交掉，不是再看一遍余额。
    #
    # 这一步只读后端（查出要操作的是哪一笔），绝不写。真正的执行在
    # `/agent/support/confirm`，必须由用户点过确认之后才会被调用。
    action_name = actions.detect(raw)
    if action_name:
        status, message, card, ask = actions.propose(
            action_name, raw, token, backend, context={"house_no": house_no})
        result = _action_result(action_name, status, message, card, ask,
                                {"house_no": house_no} if house_no else {},
                                shaped)
        # NOT_APPLICABLE 且没话可说时 result 为 None，继续往下走检索
        if result is not None:
            return result

    # ③.5 关于「这一套房」的提问：定向取房源详情，不走检索。
    #
    # 「讲解第一套的收费」这类问题，答案既不在 FAQ 里（每套房都不一样，
    # 也不该写进平台规则），也不是用户的个人数据。以前一路走到检索、
    # 被闸门拒掉，用户看到「这个我没查到相关规定」——可信息就在房源页上。
    aspect = house_tool.asks_about_house(raw, house_no)
    if aspect and backend is not None:
        message, house = house_tool.describe(backend, house_no, aspect)
        if message:
            return shaped("answered", message,
                          retrieval={"mode": "house", "house_no": house_no,
                                     "aspect": aspect,
                                     "note": "定向取房源详情，未使用知识检索"})

    # ③.6 「帮我租下来」——平台的签约由房东在看房通过后发起，
    # 代办不了。与其含糊答应，不如把流程讲清楚再把能做的递过去。
    if house_tool.wants_to_rent(raw):
        return shaped("answered", house_tool.rent_flow_message(house_no),
                      alternatives=["预约这周六下午两点看房", "怎么在线签约？",
                                    "签约要准备什么"],
                      retrieval={"mode": "flow", "note": "签约由房东发起，"
                                                        "智能体无此接口"})

    # ④ 事务型问题：大模型在白名单工具里选一个，查真实数据后照着数据回答。
    # 返回 knowledge 表示「这不是个人数据问题」，继续往下走检索。
    status, message, asked = toolcall.run(
        raw, token, backend, provider=provider)
    if status != "knowledge":
        return shaped(
            status, message,
            account=asked,
            retrieval={"mode": "account", "tool": asked.get("tool"),
                       "select_source": asked.get("select_source"),
                       "answer_source": asked.get("answer_source"),
                       "note": "事务型问题：查后端真实数据，未使用知识检索"})

    kb = knowledge_base(backend=backend, settings=settings)
    index = kb.refresh()
    if index is None or not len(index):
        return shaped("refused", "知识库还没准备好，稍后再问我一次。")

    # ④ 多轮指代消解
    query, carried = rewrite(raw, history or [])

    # ⑤⑥ 检索 + 精排
    expanded = corpus.expand_query(query)
    ranked, detail = index.search(query, expanded=expanded, top_k=CANDIDATE_K)
    reranked = index.rerank(query, ranked, detail, top_k=CONTEXT_K)
    candidates = [cid for cid, _ in reranked]

    # ⑦ 可答性闸门
    verdict = gate.evaluate(index, query, candidates, provider=provider)

    retrieval = {
        "mode": "vector+lexical" if index.vector_enabled else "lexical",
        "channels": detail.get("channels", []),
        "query": query,
        "carried_over": carried,
        "expanded": bool(expanded),
        "candidates": [{"id": cid, "score": score,
                        "title": (index.get(cid) or {}).get("title", "")}
                       for cid, score in reranked],
        "gate": {"cover": verdict["cover"], "unseen": verdict["unseen"],
                 "llm": verdict["llm"], "reason": verdict["reason"],
                 "passed": verdict["ok"]},
        "corpus": dict(kb.sources, origin=kb.origin),
    }

    if not verdict["ok"]:
        alternatives = suggest(index, query, candidates, verdict["cover"])
        retrieval["latency_ms"] = int((time.time() - started) * 1000)
        return shaped(
            "refused",
            "暂时没有查到相关规定，这个我不敢乱说。",
            alternatives=alternatives,
            retrieval=retrieval,
            handoff={"question": raw, "reason": verdict["reason"]})

    # ⑧ 生成 + 校验
    message, citations, source, note = answer_tool.generate(
        provider, index, query, candidates)

    if source == "refused" or not message:
        retrieval["latency_ms"] = int((time.time() - started) * 1000)
        retrieval["answer_source"] = source
        retrieval["answer_note"] = note
        return shaped("refused", "暂时没有查到相关规定，这个我不敢乱说。",
                      alternatives=suggest(index, query, candidates,
                                           verdict["cover"]),
                      retrieval=retrieval,
                      handoff={"question": raw, "reason": note})

    retrieval["answer_source"] = source
    retrieval["answer_note"] = note
    retrieval["latency_ms"] = int((time.time() - started) * 1000)

    cards = [c for c in (_citation(index, cid) for cid in citations) if c]
    return shaped("answered", message, citations=cards, retrieval=retrieval)


def record(result, backend, token="", session_id=""):
    """把一次问答写进后端日志，返回 log_id（失败为 None）。

    单独抽出来而不是塞进 `run()`，是为了让 `run()` 保持纯粹：
    它只负责「问题进、答案出」，不产生副作用，测试因此不需要任何桩后端。
    副作用留给调用方（`main.py` 的接口层）显式触发。

    日志的用途是运营而不是排错——拒答率、反复问却答不上来的问题清单、
    哪几条 FAQ 最常被引用。没有这些，语料该往哪儿补全靠猜。
    """
    if backend is None:
        return None
    retrieval = result.get("retrieval") or {}
    gate_info = retrieval.get("gate") or {}
    return backend.log_support({
        "question": (result.get("question") or "")[:500],
        "status": result.get("status", ""),
        "mode": retrieval.get("mode", ""),
        "session_id": session_id,
        "cited": [c["id"] for c in result.get("citations", [])],
        "top_id": (retrieval.get("candidates") or [{}])[0].get("id", ""),
        "cover": float(gate_info.get("cover") or 0.0),
        "answer_source": retrieval.get("answer_source", ""),
        "gate_reason": (gate_info.get("reason") or retrieval.get("note") or "")[:200],
        "latency_ms": int(retrieval.get("latency_ms") or 0),
    }, token=token)
