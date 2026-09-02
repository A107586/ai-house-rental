"""AI-03 智能客服问答（RAG）。

一次问答的完整链路：

    ① 转人工请求？    → 直接走转人工，不浪费一次检索
    ② 事务型还是知识型？ → 事务型查后端真实数据，知识型才走检索
    ③ 多轮指代消解     → 「那它多久到账」补成「押金多久到账」
    ④ 同义扩展 + 多路检索 + RRF 融合
    ⑤ 确定性精排，取 top-3
    ⑥ 可答性闸门      → 不过就拒答，给「猜你想问」和转人工出口
    ⑦ 受约束生成 + 引用校验 → 不过就退回抽取式

每一步的中间结果都塞进返回值的 `retrieval` 字段里。这不是调试残留：
前端要用它渲染「正在理解问题 → 正在检索知识库（命中 3 条）→ 正在核对依据」
的思考态，答辩时也要用它当场说清楚「为什么是这条、为什么拒答」。
"""

import time

from app.agents.support import corpus
from app.agents.support.tools import (answer as answer_tool, classify_ask,
                                      gate, toolcall)
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
        "retrieval": {},
    }
    payload.update(extra)
    return payload


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


def run(text, token="", history=None, provider=None, backend=None,
        settings=None, house_no=""):
    """回答一个客服问题。"""
    started = time.time()
    raw = (text or "").strip()
    if not raw:
        return _shape("unknown", "你想问什么？比如「押金怎么退」「水电费怎么算」。")

    def shaped(status, message, **extra):
        # 每条返回都带上原问题，日志与前端反馈都要用它定位这一轮
        extra.setdefault("question", raw)
        return _shape(status, message, **extra)

    # ① 明确要转人工，不必先检索一遍
    if classify_ask.wants_handoff(raw):
        return shaped("escalate",
                      "好的，我把你的问题转给管理员，稍后会通过站内消息回复你。",
                      handoff={"question": raw, "reason": "用户主动要求转人工"})

    # ② 事务型问题：大模型在白名单工具里选一个，查真实数据后照着数据回答。
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

    # ③ 多轮指代消解
    query, carried = rewrite(raw, history or [])

    # ④⑤ 检索 + 精排
    expanded = corpus.expand_query(query)
    ranked, detail = index.search(query, expanded=expanded, top_k=CANDIDATE_K)
    reranked = index.rerank(query, ranked, detail, top_k=CONTEXT_K)
    candidates = [cid for cid, _ in reranked]

    # ⑥ 可答性闸门
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
        # 拒答也要给出口：检索的 top-3 标题就是「猜你想问」，
        # 一次检索的结果用两次，不额外花成本。
        alternatives = [(index.get(cid) or {}).get("title", "")
                        for cid in candidates[:3]]
        alternatives = [a for a in alternatives if a]
        retrieval["latency_ms"] = int((time.time() - started) * 1000)
        return shaped(
            "refused",
            "暂时没有查到相关规定，这个我不敢乱说。",
            alternatives=alternatives,
            retrieval=retrieval,
            handoff={"question": raw, "reason": verdict["reason"]})

    # ⑦ 生成 + 校验
    message, citations, source, note = answer_tool.generate(
        provider, index, query, candidates)

    if source == "refused" or not message:
        retrieval["latency_ms"] = int((time.time() - started) * 1000)
        retrieval["answer_source"] = source
        retrieval["answer_note"] = note
        return shaped("refused", "暂时没有查到相关规定，这个我不敢乱说。",
                      alternatives=[(index.get(cid) or {}).get("title", "")
                                    for cid in candidates[:3]],
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
