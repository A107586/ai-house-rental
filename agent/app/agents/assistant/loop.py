"""助手的 agent 循环：模型自己决定调哪些工具、看结果、再决定，最后自己组织回答。

## 和原来那一套的区别

原来的 `/agent/chat` 是「关键词规则先分流 → 模型挑**一个**工具 → 代码按模板拼回复」，
模型挑不准、超时、或者句子被规则先截走，就退回关键词链路。它的上限是
「一句话对应一个工具」：「我这个月账单里哪笔最贵，能不能先交那笔」这种要先查、
再判断、再办事的话接不住。

这里换成一个循环：每一步模型输出一个 JSON——要么调一个工具，要么直接回答用户；
工具结果作为观察交回给它，最多走 `MAX_STEPS` 步。**路由、多步推理、话术都归模型**。

## 仍然由代码把关的（这些不是「理解」，是安全边界）

- 写操作只生成确认卡片，出卡片或要追问就结束这一轮，卡片上的话由代码生成——
  用户点确认之前什么都不会发生（`toolkit` 负责，见那里的说明）；
- 事实只来自工具结果；房源编号只能按序号从列表里查，不让模型猜；
- 工具名、参数都过白名单，模型编出来的工具名不会被执行。

## 为什么不用厂商的 function calling

和 `toolbox/catalog.py` 同一个理由：团队的 provider 抽象层要能换厂商、换本地模型。
工具表写进提示词、模型输出 JSON，任何能输出 JSON 的模型都能跑。
"""

import json
import re
import time
from datetime import date

from app.llm.base import LLMUnavailable, extract_json

# 一轮最多调几次工具。实测多数问题 1–2 次就够；给到 4 是为了「先查列表、再看其中一套、
# 再办事」这种链路，再多基本是在兜圈子
MAX_STEPS = 4

# 每一步输出的额度（不含思考；provider 会按配置补上思考余量）
STEP_MAX_TOKENS = 700

# 喂回给模型的一次工具结果最多多长。账单、房源列表原样塞进去会把上下文撑爆，
# toolkit 已经做了压缩，这里再兜一道
OBSERVATION_CHARS = 3600

_SYSTEM = """你是「大连智租」租房平台的智能助手。你用工具帮用户找房、查他自己的数据、
解答平台规则、办理事务，最后用自己的话回答他。

## 怎么工作
每一步只输出一个 JSON 对象，不要输出任何别的文字：
- 调用工具：{"thought": "一句话说明为什么调它", "tool": "工具名", "params": {参数}}
- 回答用户：{"thought": "……", "answer": "给用户的话"}
- 调写工具时，用户这句话里如果**还问了别的**（「这个要紧吗」「对身体有影响吗」「需要报修吗」），
  在同一个 JSON 里加 "say": "先回答他的那几句"——卡片或追问会接在后面显示。
  写工具一调这一轮就结束了，不写在 say 里，他的问题就没人答了。
  **say 里只答他问的，不要说「已经帮你提交了 / 办好了」**：这时候卡片还没确认，
  可能还在问他是哪一套。
调用工具之后你会收到结果，再决定下一步。能直接回答就不要调工具；
一件事需要几样数据，就依次调几次。

## 必须遵守
1. 房源、账单、预约、合同、金额、评分、评价等事实**只能来自工具结果**，不许编造；
   工具没查到就照实说。
2. 用户说「第三套」「这套」「刚才那套」时，工具参数填 ordinal（第几套）或留空，
   **不要自己猜房源编号**；编号只能抄工具结果或「当前情况」里给出的。
3. 平台规则类问题（押金、退租、水电、合同条款、发票、平台怎么收费……）先调 knowledge，
   只能按它返回的依据回答；它说没有依据，就告诉用户没查到，**别凭常识补**，并调 handoff 给他转人工的按钮。
   它给了「相近条目原文」时，先判断原文是不是在回答用户问的事：是就照原文答，不是就当没查到。
   问的是**某一套房**的情况（谁带看、押几付几、能不能养宠物），先调 house_detail 看这套房的资料。
   **knowledge 一次只问一件事**：用户一句话问了两条规则（「要提前多久说？押金能全退吗」），
   就分两次查——两个问题拼成一句，检索容易两头都不沾，整句被判成没依据。
4. 办事（预约看房、取消或改约、缴费、报修、收藏、续约、退租、处理预约申请、上下架、
   改房源、提醒租客……）**直接调对应的写工具，不要先调查询工具去确认**：
   写工具自己会查是哪一场预约、哪一笔账单、哪一份合同，用户没说清还会自己追问。
   写工具只会生成一张确认卡片，要用户点确认才执行——**绝不能说「已经帮你办好了」**。
   **卡片本身就是征求同意**：用户描述了家里东西坏了、漏水，哪怕是在抱怨，也直接调
   submit_repair 出卡片，别先用文字问「要不要帮你报修」——那样要多绕一轮，
   话题一岔开这件事就落下了。
   写工具的 request 参数要用一句话复述要办的事，带上所有细节
   （哪套房、哪天几点、哪笔账单、故障现象、改成多少……）。
   只是在问（「退租要提前多久说」「能不能改约」）而不是要办，就不是写操作。
5. 用户发了照片：先看照片，照片里的内容可以直接用来回答；看出损坏、用户又想修，
   就调 submit_repair，把故障现象写进 request。照片里是合同条款、问合不合理，
   先调 review_contract（条款逐字抄进 text），再对照 knowledge 的平台规则回答；
   是别的平台的房源，就按照片里的区域、户型、价格、出租方式调 search_houses 找相近的。
   照片看不清就照实说，别猜。
6. 「当前情况」里有待办（上一轮你在帮用户办某件事、问了他一个问题）：如果这句话是在回答它，
   就带上已填的参数和新得到的值，再调一次同一个写工具；如果他换了话题，就正常处理新话题。
7. 找房时，「离地铁近」「采光好」「安静」这类是**偏好**，不要当成 search_houses 的筛选条件；
   先按硬条件（区域、预算、户型）查，再看结果里每套的「配套」「电梯」「楼层」，
   **点名说出哪几套满足他的偏好**，不要只说「建议优先看有电梯的」。查到 0 套时，放宽最次要
   的一个条件（户型写法、预算上浮一点）再查一次，回答时说清楚放宽了什么。
8. **房源序号以屏幕为准。** 工具结果里的房源按「序号」原样显示成卡片，用户说「第几套」
   就是这个序号。回答里提到房源，用这个序号或小区名；要按价格等重新排序，写成
   「第 3 套（友谊街道，1150 元）」带上原序号，**不要另起 1、2、3 的编号**——
   否则用户照着你的编号说「第二套」，指的就是屏幕上另一套房了。
9. **记住用户说过的情况并用上**：公司在哪、预算、家里有谁、养不养宠物、想不想合租……
   问到远近、通勤，就拿房源的地址、商圈和他说过的地点对比着答。
10. 整租和合租不能直接比价钱、比面积（合租的面积是整套的，租金是一间的）。找「差不多但更大 /
   更便宜」或对比两套时，默认和原来那套同一种出租方式；不同的要点明。
11. 平台规则查到的答案，能套到他自己身上就算出来：例如违约金是一个月租金，而他那套月租 3050 元，
   就直接说「大约 3050 元」；需要他的数据就先查。
12. 你是「大连智租」网站的租房助手。被问是什么模型、哪家公司，就说是大连智租的智能助手，
   不要自称某个模型或厂商。不要编造平台没有的入口（App、客服电话……）。
   用户要转人工、找真人客服，直接调 handoff，不要说没有这个功能。未登录的用户要预约看房、
   收藏、缴费、报修，告诉他平台能办、先登录（页面右上角「登录 / 注册」）就行，别说做不了。
13. 回答用中文，简洁、直接回应他的问题；**纯文本**，不要用 Markdown（**加粗**、# 标题、
   表格在页面上都会原样显示成符号）；列举不超过 5 条。

## 可用工具
%(catalog)s

## 当前情况
%(context)s"""

_NUDGE_FINAL = "工具已经调够了。请根据上面的结果直接回答用户，只输出 {\"answer\": \"……\"}。"
_NUDGE_JSON = "上一条不是合法的 JSON。请只输出一个 JSON 对象：调用工具或给出 answer。"


class Outcome:
    """一次工具调用的结果。

    - `observation`：交回给模型看的内容（dict 或字符串，已压缩）
    - `artifacts`：给前端渲染的结构化结果（房源列表、查库卡片、确认卡片、引用来源……）
    - `stop`：这一步之后不再问模型，直接用 `message` 作为回复——
      写操作出了确认卡片或需要追问时就是这样，话术由代码定，不让模型改写卡片上的事实
    - `lead`：停下来时模型没写 say，就用它垫在 `message` 前面
    """

    def __init__(self, observation, artifacts=None, stop=False, message="", lead=""):
        self.observation = observation
        self.artifacts = artifacts or {}
        self.stop = stop
        self.message = message
        self.lead = lead


def _catalog(tools):
    lines = []
    for tool in tools:
        parts = ["- %s：%s" % (tool["name"], tool["desc"])]
        if tool.get("params"):
            args = ["%s(%s) %s" % (key, kind, note)
                    for key, (kind, note) in tool["params"].items()]
            parts.append("  参数：" + "；".join(args))
        lines.append("\n".join(parts))
    return "\n".join(lines)


def _observation_text(name, observation):
    if isinstance(observation, str):
        text = observation
    else:
        text = json.dumps(observation, ensure_ascii=False)
    if len(text) > OBSERVATION_CHARS:
        text = text[:OBSERVATION_CHARS] + "…（结果太长，已截断）"
    return "工具 %s 的结果：%s\n接下来：继续调用工具，或者直接回答用户。" % (name, text)


def _user_message(text, images):
    said = (text or "").strip() or "（用户没有说话，只发了照片）"
    if not images:
        return {"role": "user", "content": said}
    parts = [{"type": "image_url", "image_url": {"url": url}} for url in images]
    parts.append({"type": "text", "text": said})
    return {"role": "user", "content": parts}


def _as_answer(raw):
    """模型没按格式、直接用自然语言回答了——那就当它回答了。

    本地小模型偶尔会这样；把一段正常的中文当成「格式错误」再逼它重来，
    只会多花一次调用，换来的还是同一句话。
    """
    text = (raw or "").strip()
    if not text or text.startswith("{") or "\"tool\"" in text:
        return ""
    return text


# 流式时告诉用户「此刻在做哪一步」。这些是**真实发生的工具调用**，不是猜的——
# 前端原先只能轮播一份固定的阶段清单（见 web/src/components/Thinking.jsx 的说明）
STEP_LABELS = {
    "search_houses": "在找房", "more_houses": "在找更多房源", "recommend_houses": "在挑推荐",
    "house_detail": "在看这套房的详情", "house_reviews": "在看住户评价",
    "landlord_reputation": "在看房东口碑", "compare_houses": "在比较这几套",
    "knowledge": "在查平台规则", "my_bills": "在查你的账单", "my_contracts": "在查你的合同",
    "my_appointments": "在查你的预约", "my_repairs": "在查你的报修", "my_favorites": "在查你的收藏",
    "my_history": "在查浏览记录", "my_todos": "在查待办事项",
    "review_contract": "在看合同条款", "handoff": "在准备转人工",
}


def step_label(name):
    return STEP_LABELS.get(name) or ("在准备确认单" if name else "在想")


class AnswerStream:
    """从**还没生成完的** JSON 里实时取出 `"answer"` 字段的文字。

    每一步模型输出的是 `{"thought": "...", "answer": "..."}` 或者一次工具调用。
    要等整段 JSON 收全再解析，用户就得干等；这里一段段喂进来，一旦看到
    `"answer": "`，后面的字就边到边吐，直到那个字符串结束。工具调用那一步
    不会吐任何东西。模型没按格式、直接说了一段话（不以 { 开头），整段照吐——
    和 `_as_answer` 的判断一致。

    转义要自己处理：`\\n`、`\\"`、`\\uXXXX`，而且一个转义可能被切在两段之间。
    """

    _KEY = re.compile(r'"answer"\s*:\s*"')

    def __init__(self):
        self.buf = ""
        self.mode = None        # None：还看不出；"json"；"text"
        self.pos = 0
        self.inside = False
        self.done = False

    def feed(self, piece):
        """喂一段原始输出，返回这一段里新出现的回答文字（可能是空串）。"""
        self.buf += piece or ""
        if self.mode is None:
            head = self.buf.lstrip()
            if not head:
                return ""
            self.mode = "json" if head[0] in "{`" else "text"
            if self.mode == "text":
                return head
        if self.mode == "text":
            return piece or ""
        if self.done:
            return ""
        if not self.inside:
            match = self._KEY.search(self.buf, self.pos)
            if not match:
                return ""
            self.inside, self.pos = True, match.end()
        out = []
        i, buf = self.pos, self.buf
        while i < len(buf):
            ch = buf[i]
            if ch == "\\":
                if i + 1 >= len(buf):
                    break                                  # 转义被切断，等下一段
                nxt = buf[i + 1]
                if nxt == "u":
                    if i + 6 > len(buf):
                        break
                    try:
                        out.append(chr(int(buf[i + 2:i + 6], 16)))
                    except ValueError:
                        pass
                    i += 6
                    continue
                out.append({"n": "\n", "t": "\t", "r": "", "b": "", "f": ""}.get(nxt, nxt))
                i += 2
                continue
            if ch == '"':
                self.done = True
                i += 1
                break
            out.append(ch)
            i += 1
        self.pos = i
        return "".join(out)


def _ask_model(llm, system, messages, emit, deadline=None):
    """问一次模型。有人在听（emit）就流式，回答部分边到边推；返回完整原文和是否推过字。"""
    if emit is None:
        return llm.chat(system, messages, max_tokens=STEP_MAX_TOKENS, deadline=deadline), False
    stream, chunks, pushed = AnswerStream(), [], False
    try:
        for piece in llm.chat_stream(system, messages, max_tokens=STEP_MAX_TOKENS,
                                     deadline=deadline):
            chunks.append(piece)
            text = stream.feed(piece)
            if text:
                pushed = True
                emit({"type": "delta", "text": text})
    except LLMUnavailable:
        if chunks:
            raise
        # 一个字都还没收到就失败：退回带重试的普通调用，至少答得出来
        return llm.chat(system, messages, max_tokens=STEP_MAX_TOKENS, deadline=deadline), False
    return "".join(chunks), pushed


def run(text, images, llm, toolkit, context_lines=(), dialogue=(), deadline=None,
        today=None, on_event=None):
    """跑一轮，见 `_run`。这一层只多做一件事：分步计时，结果放在返回值的 `timings` 里——
    每次问模型花了多久、每个工具花了多久、整轮多久。「助手慢」时先看这张表，
    就知道该优化模型、检索还是后端接口（app/tracing.py 会把它写进日志）。"""
    timings = {"steps": [], "started": time.perf_counter()}
    out = _run(text, images, llm, toolkit, context_lines, dialogue, deadline, today,
               on_event, timings)
    out["timings"] = {"total_ms": _ms_since(timings["started"]), "steps": timings["steps"]}
    return out


def _ms_since(started):
    return round((time.perf_counter() - started) * 1000, 1)


def _run(text, images, llm, toolkit, context_lines, dialogue, deadline, today,
         on_event, timings):
    """跑一轮。返回 dict：

    - `message`：给用户的话
    - `artifacts`：合并后的结构化结果
    - `trace`：每一步调了什么、参数是什么（写进 route，答辩时能说清它怎么想的）
    - `stopped_by`：哪个工具结束了这一轮（写操作出卡片 / 追问），没有就是 None

    `on_event`：可选的回调，给流式接口用。每调一个工具推一条
    `{"type": "step", "tool", "label"}`，回答边生成边推 `{"type": "delta", "text"}`；
    推了字、这一步最后却没有收尾（比如模型改主意去调工具了）就推一条
    `{"type": "reset"}`，前端把已显示的字清掉。不传时行为和原来完全一样。
    最终以返回值里的 `message` 为准（会去掉 Markdown 星号等）。

    模型不可用时抛 `LLMUnavailable`，由调用方决定怎么告诉用户。
    """
    tools = toolkit.tools()
    allowed = {t["name"] for t in tools}
    day = today or date.today()
    # 带上星期：「这周六」「下周一上午」是看房最常见的说法，只给日期模型得自己推算
    context = list(context_lines) + ["今天是 %s（星期%s）。" % (day.isoformat(),
                                                            "一二三四五六日"[day.weekday()])]
    system = _SYSTEM % {"catalog": _catalog(tools), "context": "\n".join(context)}

    messages = [dict(m) for m in dialogue] + [_user_message(text, images)]
    artifacts, trace, seen = {}, [], set()
    bad_json = 0
    if on_event is not None:
        # 第一次问模型就要好几秒（开思考更久）。这段时间前端原先轮播一份固定清单，
        # 跑到「组织回答」时真实的「在查平台规则」才到，顺序看着是反的
        on_event({"type": "step", "tool": "", "label": "在理解你的问题"})

    for step in range(MAX_STEPS + 1):
        if step == MAX_STEPS:
            messages.append({"role": "user", "content": _NUDGE_FINAL})
        asked_at = time.perf_counter()
        raw, pushed = _ask_model(llm, system, messages, on_event, deadline)
        timings["steps"].append({"kind": "model", "ms": _ms_since(asked_at)})
        decision = extract_json(raw)
        answered = (isinstance(decision, dict) and isinstance(decision.get("answer"), str)
                    and decision["answer"].strip()
                    and str(decision.get("tool") or "none").strip() in ("", "none"))
        if pushed and not (answered or (not isinstance(decision, dict) and _as_answer(raw))):
            on_event({"type": "reset"})
        if not isinstance(decision, dict):
            answer = _as_answer(raw)
            if answer:
                return _done(answer, artifacts, trace)
            bad_json += 1
            if bad_json > 1:
                raise LLMUnavailable("模型连续两次没有输出可解析的 JSON")
            messages.append({"role": "assistant", "content": raw or ""})
            messages.append({"role": "user", "content": _NUDGE_JSON})
            continue

        answer = decision.get("answer")
        name = str(decision.get("tool") or "").strip()
        if isinstance(answer, str) and answer.strip() and (not name or name == "none"):
            return _done(answer.strip(), artifacts, trace)
        if step == MAX_STEPS:
            # 催过了还在要工具：用它最后一句 thought 兜不住，照实说查到的
            return _done(_fallback_answer(artifacts), artifacts, trace)

        params = decision.get("params") if isinstance(decision.get("params"), dict) else {}
        messages.append({"role": "assistant",
                         "content": json.dumps(decision, ensure_ascii=False)})
        if name not in allowed:
            explain = getattr(toolkit, "why_unavailable", None)
            messages.append({"role": "user", "content": (explain(name) if explain else None)
                             or "没有叫「%s」的工具。只能用「可用工具」里列出的名字。" % name})
            trace.append({"tool": name, "params": params, "error": "unknown_tool"})
            continue

        key = (name, json.dumps(params, ensure_ascii=False, sort_keys=True))
        if key in seen:
            messages.append({"role": "user", "content":
                             "这个工具刚用同样的参数调过，结果同上。请换一个做法或直接回答。"})
            continue
        seen.add(key)

        if on_event is not None:
            on_event({"type": "step", "tool": name, "label": step_label(name)})
        called_at = time.perf_counter()
        outcome = toolkit.call(name, params, text=text, images=images)
        timings["steps"].append({"kind": "tool", "tool": name, "ms": _ms_since(called_at)})
        trace.append({"tool": name, "params": params,
                      "thought": str(decision.get("thought") or "")[:120]})
        _merge(artifacts, outcome.artifacts)
        if outcome.stop:
            # 写工具出卡片或追问就结束这一轮。用户这句话里另问的事（「要紧吗」
            # 「对身体有影响吗」）由模型写在 say 里，放在卡片话术前面——
            # 实测原先只剩一句「你在租的有 3 套，是哪一套」，问题没人答
            said = decision.get("say")
            said = _drop_done_claims(said.strip()) if isinstance(said, str) else ""
            said = said or outcome.lead
            message = "%s\n\n%s" % (said, outcome.message) if said else outcome.message
            return _done(message, artifacts, trace, stopped_by=name)
        messages.append({"role": "user", "content": _observation_text(name, outcome.observation)})
        if on_event is not None:
            # 工具返回之后、下一次模型吐字之前，模型在读结果、想下一步（开思考时要好几秒）
            on_event({"type": "step", "tool": "", "label": "在看查到的结果"})

    return _done(_fallback_answer(artifacts), artifacts, trace)


# 这些字段后来的覆盖先来的：屏幕上只该有一份房源列表、一张查库卡片
_LATEST = ("items", "total", "recommended", "append", "page", "account", "pitch",
           "compare", "house_no", "action", "pending", "options", "form", "handoff")
# 这些累积：一轮里查了两条规则，两条的出处都该列出来
_ACCUMULATE = ("citations", "notes", "alternatives")


def _merge(into, new):
    for key in _LATEST:
        if key in new and new[key] not in (None, "", [], {}):
            into[key] = new[key]
    for key in _ACCUMULATE:
        for item in new.get(key) or []:
            bucket = into.setdefault(key, [])
            if item not in bucket:
                bucket.append(item)


def _fallback_answer(artifacts):
    if artifacts.get("items"):
        return "找到了一些房源，列在下面了。"
    if artifacts.get("account"):
        return "查到的结果列在下面了。"
    return "这个问题我没能理清楚，能换个说法再问一次吗？"


# 「已经帮你提交了」「办好了」「你点确认就行」一类的话。
_DONE_CLAIM = re.compile(r"(已经|已)(帮你|给你|为你)?(提交|办好|办理|预约|约好|缴|交了|报修|报上)"
                         r"|帮你(提交|报|约|交|缴)了|(办|弄|搞)好了|提交了报修")


def _drop_done_claims(text):
    """say 里去掉「已经帮你办了」的句子。

    写工具这时候只出了卡片、甚至还在问是哪一套，什么都没办。提示词里写了
    不许这么说，实测模型照样在 say 里写「我帮你提交了报修，你点确认就行」——
    紧接着下一句是「你在租的有 3 套，是哪一套？」。这是安全边界，交给代码。
    """
    parts = re.split(r"(?<=[。！!；;])", text or "")
    kept = [p for p in parts if p.strip() and not _DONE_CLAIM.search(p)]
    return "".join(kept).strip()


def _done(message, artifacts, trace, stopped_by=None):
    # 聊天框是纯文本：提示词里说了不要 Markdown，模型偶尔还是会加粗，
    # 页面上就是一对对星号
    message = (message or "").replace("**", "")
    return {"message": message, "artifacts": artifacts, "trace": trace,
            "stopped_by": stopped_by}
