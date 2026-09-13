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

from app.agents.booking.tools.parse_datetime import parse_datetime
from app.agents.support import corpus
from app.agents.support.tools import (actions, answer as answer_tool,
                                      classify_ask, forms, gate,
                                      house as house_tool,
                                      router, toolcall)
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
        # 长表单进度。非表单的轮次是 None，前端不画
        "form": None,
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
        # **填了一半的表单不能说丢就丢。**
        #
        # 否定词表里有「不要」「不用」，而表单答案里完全可能带这两个字
        # （「月租多少钱？」→「不要钱」）。更要紧的是：哪怕他真的想放弃，
        # 已经填的九项也值得再问一句——丢掉别人二十次输入是件大事，
        # 而再确认一次的成本只有一句话。
        slots_now = dict(pending.get("slots") or {})
        if forms.spec(name) and not pending.get("abandon_asked"):
            done, total = forms.progress(name, slots_now)
            if done >= 2:
                question = ("已经填了 %d/%d 项，确定不填了吗？"
                            "再说一次「不填了」我就丢掉，说「继续」就接着填。"
                            % (done, total))
                return shaped(
                    "need_slot", question,
                    options=[{"value": "继续", "label": "继续填"},
                             {"value": "不填了", "label": "丢掉，不填了"}],
                    pending=dict(pending, abandon_asked=True),
                    retrieval={"mode": "action", "action": name,
                               "note": "表单填了一半，放弃前再确认一次"})
        return shaped("cancelled", "好，那就先不弄了。")

    # 用户中途改口去做别的事，就别再往旧槽里塞
    if actions.detect(raw) and actions.detect(raw) != name:
        return None

    # 「继续」不是槽位值，是「回到刚才那一项」。不单独认出来的话，
    # 这两个字会被当成答案填进去——用户想接着填，反而把一项填坏了。
    if _wants_resume(raw):
        resumed = dict(pending)
        resumed.pop("abandon_asked", None)
        return shaped("need_slot", ask.get("question", "接着填吧。"),
                      options=ask.get("options", []),
                      pending=resumed,
                      retrieval={"mode": "action", "action": name,
                                 "note": "用户要求接着办，重问当前这一项"})

    # **中途问了个问题——去答他，但别把填过的东西丢掉。**
    #
    # 这是长表单最要命的一处。「发布房源」有十一项，填到第六项时用户问
    # 一句「租金一般填多少合适」，原先的做法是接不上就放弃整个待办，
    # 前六项白填。**代价是用户从此不敢在流程里问问题**——
    # 而问问题恰恰是他需要助手的原因。
    #
    # 现在返回 INTERRUPTED，由 run() 正常回答，答完把 pending 原样带回去，
    # 再补一句「刚才填到哪儿了」。
    # ……但**先确认这句话不是在回答当前这一项**。
    #
    # 实测：助手问「你想什么时候去看？」，用户答「本周六上午九点可以吗」，
    # 这句话结尾是「吗」，`_is_question` 判成提问，整句被丢去检索知识库，
    # 回了一句「暂时没有查到相关规定」——他明明**已经把时间说了**。
    # 中文里带商量语气的回答本来就长得像问句（「周六行吗」「三千二可以吗」），
    # 按问句处理等于惩罚有礼貌的说法。
    if _is_question(raw) and not _answers_slot(name, ask, raw, pending):
        return INTERRUPTED

    value = _fill_slot(ask, raw)
    if not value:
        return None

    slots = dict(pending.get("slots") or {})

    # 表单字段要当场校验。填错一项就是一条错数据，而用户不会逐字复核
    # 确认卡——所以在收下的这一刻就得挡住，并且**留在原地重问**，
    # 不能往下走，也不能把前面填好的丢掉。
    if forms.spec(name):
        good, cleaned = forms.validate(name, ask["slot"], value, slots)
        if not good:
            retry = dict(ask)
            retry["question"] = "%s%s" % (cleaned, ask["question"])
            return shaped("need_slot", retry["question"],
                          options=retry.get("options", []),
                          pending={"action": name, "slots": slots,
                                   "ask": retry},
                          retrieval={"mode": "action", "action": name,
                                     "note": "填的内容没通过校验，原地重问"})
        value = cleaned

    slots[ask["slot"]] = value
    context = dict(slots)
    context.setdefault("house_no", house_no)

    status, message, new_card, new_ask = actions.propose(
        name, raw, token, backend, context=context)
    return _action_result(name, status, message, new_card, new_ask, slots, shaped)


# `_resume` 的第三种结果：既不是「接住了」也不是「接不上」，
# 而是「用户插了个问题，答完还要回来」。用一个哨兵对象区分，
# 因为 None 已经表示放手了。
INTERRUPTED = object()


def _pending_label(pending):
    """待办的中文名。表单有自己的名字，普通动作用动作名兜底。"""
    name = (pending or {}).get("action") or ""
    form = forms.spec(name)
    if form:
        return form["label"]
    return actions.LABELS.get(name, name)


def _pending_where(pending):
    """一句「填到哪儿了」。长表单报进度，普通追问报在问什么。"""
    name = (pending or {}).get("action") or ""
    slots = (pending or {}).get("slots") or {}
    if forms.spec(name):
        done, total = forms.progress(name, slots)
        return "已经填了 %d/%d 项" % (done, total)
    ask = (pending or {}).get("ask") or {}
    question = (ask.get("question") or "").strip()
    return "还差一步：%s" % question if question else "还没做完"


# 判断这一句是在**回答**还是在**提问**。
#
# 宁可判成回答：判错成提问，用户的答案会被当成问题去检索，回一句
# 「没查到」然后重问，他再说一遍就是；判错成回答，错值直接进了槽，
# 而槽里的东西是要落库的。所以这里只认相当明确的疑问句式。
_QUESTION_MARKS = ("吗", "呢", "?", "？")
_QUESTION_WORDS = ("怎么", "如何", "为什么", "什么时候", "多少钱一般",
                   "一般填", "一般是", "能不能", "可不可以", "是不是",
                   "有没有", "什么意思", "哪些", "区别")


_RESUME_WORDS = ("继续", "接着填", "接着办", "回到刚才", "继续填",
                 "刚才那个", "接着来", "往下")


def _wants_resume(text):
    raw = (text or "").strip()
    return bool(raw) and len(raw) <= 8 and any(w in raw for w in _RESUME_WORDS)


def _is_question(text):
    raw = (text or "").strip()
    if not raw:
        return False
    if any(word in raw for word in _QUESTION_WORDS):
        return True
    # 「多少」太常见了——「面积多少平米」的答案里不会有它，
    # 但「押金一般多少」是提问。所以只在带疑问语气词时才算
    return raw.endswith(_QUESTION_MARKS) and len(raw) >= 4


def _answers_slot(name, ask, raw, pending):
    """这句话是不是在**回答当前这一项**，哪怕它长得像问句。

    判据按可靠程度排：

    1. 点中了助手给的可选项——那就是答案，没有别的解释；
    2. 这一项要时间，而这句话解析得出一个时间；
    3. 这一项是表单字段，而这句话能通过该字段的校验。

    第 3 条是最通用的一条：**「能不能填进这一格」本身就是最好的判据**，
    不需要再去猜他的语气。校验器本来就写好了，这里只是提前问它一次。
    """
    slot = (ask or {}).get("slot") or ""
    if not slot:
        return False

    options = (ask or {}).get("options") or []
    if options and actions.pick_option(raw, options):
        return True

    if "time" in slot or "date" in slot:
        # **`parse_datetime` 永远返回一个 dict**，没解析出来时里面全是
        # None。写成 `if parse_datetime(raw)` 会恒为真，于是「押金怎么退」
        # 也成了「在回答时间」，把插问功能整个废掉。要看的是 date 那一格。
        if (parse_datetime(raw) or {}).get("date"):
            return True

    form = forms.spec(name)
    if form and any(f.get("name") == slot for f in form.get("fields") or []):
        good, _ = forms.validate(name, slot, raw, (pending or {}).get("slots") or {})
        return bool(good)
    return False


_REFUSED = "暂时没有查到相关规定，这个我不敢乱说。"
_REFUSED_FOREIGN = ("我这边的资料都是中文的，英文问句检索不到，"
                    "所以查不出来——你用中文再问一遍，我就能查了。")


def _refusal(text):
    """拒答该说哪一句。

    整句英文提问时，检索必然什么也捞不到——语料一个英文词都没有。
    这时候仍旧回「暂时没有查到相关规定」是**在撒谎**：规定写得好好的，
    查不到的原因是语言不通。说错原因比说不知道更糟，用户会照着这句话
    以为平台没这条规矩。所以据实说是语言的问题，并给出下一步怎么办。

    判据只看汉字占比，不做语种识别：夹一两个英文词（「我的 deposit
    什么时候 refund」）照旧走正常路径，那句话是检索得到的。
    """
    raw = (text or "").strip()
    if not raw:
        return _REFUSED
    chinese = sum(1 for ch in raw if "一" <= ch <= "鿿")
    letters = sum(1 for ch in raw if ch.isascii() and ch.isalpha())
    if letters >= 6 and chinese == 0:
        return _REFUSED_FOREIGN
    return _REFUSED


def _form_progress(name, slots):
    """长表单的进度。不是表单就返回 None，前端据此决定画不画。"""
    form = forms.spec(name)
    if not form:
        return None
    done, total = forms.progress(name, slots)
    filled = []
    for field in form["fields"]:
        value = slots.get(field["name"])
        if value in (None, ""):
            continue
        filled.append({"label": forms.label_of(field), "value": str(value)})
    return {"label": form["label"], "done": done, "total": total,
            "filled": filled}


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
        # 提议器这一轮算出来、下一轮还要用的东西（比如报修的故障描述）
        # 通过 ask["carry"] 带过来，并进槽位一起存住。
        slots = dict(slots or {})
        slots.update((ask or {}).get("carry") or {})
        return shaped("need_slot", message,
                      options=(ask or {}).get("options", []),
                      pending={"action": name, "slots": slots, "ask": ask},
                      # 长表单额外给一份进度，前端拿它画进度条与已填清单。
                      # 「第 6/11 项」写在文字里也能看，但十一项填下来，
                      # 用户想知道的是**已经填了什么**——那份清单只有画出来才看得清。
                      form=_form_progress(name, slots),
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
        settings=None, house_no="", house_nos=None, pending=None, routing=None):
    """回答一个客服问题。

    `pending` 是上一轮没做完的代办：问了哪个槽、已经填了什么、
    或者已经摆出的确认卡片。它**不存在服务端**——随响应发给前端、
    下一轮再带回来，与确认卡片同一个理由：无状态就没有
    「接的是上一条还是上上条」的竞态。

    `routing` 是调用方已经算好的路由结论（`tools/router.route()` 的返回）。
    统一入口 `/agent/chat` 会先路由一次来决定要不要交给客服处理，
    传进来可以省掉第二次大模型调用。
    """
    started = time.time()
    raw = (text or "").strip()
    if not raw:
        return _shape("unknown", "你想问什么？比如「押金怎么退」「水电费怎么算」。")

    def shaped(status, message, **extra):
        # 每条返回都带上原问题，日志与前端反馈都要用它定位这一轮
        extra.setdefault("question", raw)
        payload = _shape(status, message, **extra)

        # **打断之后要回得来。** 用户在长表单里插了个问题，答完就得把
        # 待办原样交回前端，并且明说「刚才填到哪儿了」——
        # 不说这一句，他不知道流程还在，多半会重新说一遍「发布房源」，
        # 前面填的又白填了。
        if carry_pending and not payload.get("pending"):
            payload["pending"] = carry_pending
            payload["options"] = (payload.get("options") or []) + [
                {"value": "继续", "label": "继续刚才那件事"}]
            hint = ("（刚才我们在办「%s」，%s。回一句「继续」就接着填。）"
                    % (_pending_label(carry_pending),
                       _pending_where(carry_pending)))
            payload["message"] = (payload.get("message", "")
                                  + chr(10) * 2 + hint)
        return payload

    # ⓪ 先看这一句是不是在接上一轮没做完的事：回答追问，或者对着
    # 确认卡片说「好的」。接得上就继续，接不上就当新话题往下走。
    # 用户在流程里插了个问题时，`_resume` 返回 INTERRUPTED：正常去答，
    # 答完再把待办原样交回前端。这个变量一路带到函数末尾。
    carry_pending = None
    if pending:
        resumed = _resume(raw, pending, token, backend, house_no, shaped)
        if resumed is INTERRUPTED:
            carry_pending = pending
        elif resumed is not None:
            return resumed

    if carry_pending:
        # 打断期间不再让路由重新判一次意图。用户明摆着是在问问题，
        # 而不是要开一件新的事——这时候弹一张新卡片是最讨嫌的。
        routing = {"kind": router.KIND_KNOWLEDGE, "name": "none",
                   "source": "interrupted",
                   "rule": {"kind": router.KIND_KNOWLEDGE, "name": "none"}}

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

    # ③ 统一路由：这句话是要**做事**、**查自己的数据**、还是**问规则**。
    #
    # 以前这里是两段独立的关键词判断（先 actions.detect、再 classify_ask），
    # 表写得再全也追不上口语——「我要看房第一个」「预约明天上午看房第三套」
    # 一条都没命中，用户被丢回旧的子智能体，得到一句
    # 「请到具体房源页面点预约看房」。在一个号称能代办的助手里，
    # 这是最伤人的一种回答。
    #
    # 现在把能力清单交给大模型去选，规则退居参考答案与护栏。
    # 大模型不可用时结论就是规则的结论，整条链路照常工作（N-07）。
    decision = routing or router.route(raw, provider=provider,
                                       history=history, pending=pending)

    if decision["kind"] == router.KIND_ACTION:
        status, message, card, ask = actions.propose(
            decision["name"], raw, token, backend,
            context={"house_no": house_no, "house_nos": house_nos or []})
        result = _action_result(decision["name"], status, message, card, ask,
                                {"house_no": house_no} if house_no else {},
                                shaped)
        if result is not None:
            result.setdefault("retrieval", {})["route_source"] = decision["source"]
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

    # ④ 查用户自己的数据：调后端受控接口取真实数据，再由大模型照着数据回答。
    if decision["kind"] == router.KIND_QUERY:
        status, message, detail = toolcall.run(
            raw, token, backend, provider=provider, tool=decision["name"])
        if status != "knowledge":
            return shaped(
                status, message, account=detail,
                retrieval={"mode": "account", "tool": detail.get("tool"),
                           "select_source": detail.get("select_source"),
                           "route_source": decision["source"],
                           "answer_source": detail.get("answer_source"),
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
    # 扩展句一并给精排：召回用它、精排不用的话，同一次排序里
    # 两个环节看的是两份不同的查询（见 index.rerank 的说明）
    reranked = index.rerank(query, ranked, detail, top_k=CONTEXT_K,
                            expanded=expanded)
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
            _refusal(raw),
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
        return shaped("refused", _refusal(raw),
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
