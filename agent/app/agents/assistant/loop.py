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
调用工具之后你会收到结果，再决定下一步。能直接回答就不要调工具；
一件事需要几样数据，就依次调几次。

## 必须遵守
1. 房源、账单、预约、合同、金额、评分、评价等事实**只能来自工具结果**，不许编造；
   工具没查到就照实说。
2. 用户说「第三套」「这套」「刚才那套」时，工具参数填 ordinal（第几套）或留空，
   **不要自己猜房源编号**；编号只能抄工具结果或「当前情况」里给出的。
3. 平台规则类问题（押金、退租、水电、合同条款、发票、平台怎么收费……）先调 knowledge，
   只能按它返回的依据回答；它说没有依据，就告诉用户没查到，可以转人工，**别凭常识补**。
4. 办事（预约看房、取消或改约、缴费、报修、收藏、续约、退租、处理预约申请、上下架、
   改房源、提醒租客……）**直接调对应的写工具，不要先调查询工具去确认**：
   写工具自己会查是哪一场预约、哪一笔账单、哪一份合同，用户没说清还会自己追问。
   写工具只会生成一张确认卡片，要用户点确认才执行——**绝不能说「已经帮你办好了」**。
   写工具的 request 参数要用一句话复述要办的事，带上所有细节
   （哪套房、哪天几点、哪笔账单、故障现象、改成多少……）。
   只是在问（「退租要提前多久说」「能不能改约」）而不是要办，就不是写操作。
5. 用户发了照片：先看照片，照片里的内容可以直接用来回答；看出损坏、用户又想修，
   就调 submit_repair，把故障现象写进 request。
6. 「当前情况」里有待办（上一轮你在帮用户办某件事、问了他一个问题）：如果这句话是在回答它，
   就带上已填的参数和新得到的值，再调一次同一个写工具；如果他换了话题，就正常处理新话题。
7. 找房时，「离地铁近」「采光好」「安静」这类是**偏好**，不要当成 search_houses 的筛选条件；
   先按硬条件（区域、预算、户型）查，再结合结果回答偏好。查到 0 套时，放宽最次要的一个条件
   （户型写法、预算上浮一点）再查一次，回答时说清楚放宽了什么。
8. 回答用中文，简洁、直接回应他的问题，不要用 Markdown 标题；列举不超过 5 条。

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
    """

    def __init__(self, observation, artifacts=None, stop=False, message=""):
        self.observation = observation
        self.artifacts = artifacts or {}
        self.stop = stop
        self.message = message


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


def run(text, images, llm, toolkit, context_lines=(), dialogue=(), deadline=None,
        today=None):
    """跑一轮。返回 dict：

    - `message`：给用户的话
    - `artifacts`：合并后的结构化结果
    - `trace`：每一步调了什么、参数是什么（写进 route，答辩时能说清它怎么想的）
    - `stopped_by`：哪个工具结束了这一轮（写操作出卡片 / 追问），没有就是 None

    模型不可用时抛 `LLMUnavailable`，由调用方决定怎么告诉用户。
    """
    tools = toolkit.tools()
    allowed = {t["name"] for t in tools}
    context = list(context_lines) + ["今天是 %s。" % (today or date.today()).isoformat()]
    system = _SYSTEM % {"catalog": _catalog(tools), "context": "\n".join(context)}

    messages = [dict(m) for m in dialogue] + [_user_message(text, images)]
    artifacts, trace, seen = {}, [], set()
    bad_json = 0

    for step in range(MAX_STEPS + 1):
        if step == MAX_STEPS:
            messages.append({"role": "user", "content": _NUDGE_FINAL})
        raw = llm.chat(system, messages, max_tokens=STEP_MAX_TOKENS, deadline=deadline)
        decision = extract_json(raw)
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
            messages.append({"role": "user", "content":
                             "没有叫「%s」的工具。只能用「可用工具」里列出的名字。" % name})
            trace.append({"tool": name, "params": params, "error": "unknown_tool"})
            continue

        key = (name, json.dumps(params, ensure_ascii=False, sort_keys=True))
        if key in seen:
            messages.append({"role": "user", "content":
                             "这个工具刚用同样的参数调过，结果同上。请换一个做法或直接回答。"})
            continue
        seen.add(key)

        outcome = toolkit.call(name, params, text=text, images=images)
        trace.append({"tool": name, "params": params,
                      "thought": str(decision.get("thought") or "")[:120]})
        _merge(artifacts, outcome.artifacts)
        if outcome.stop:
            return _done(outcome.message, artifacts, trace, stopped_by=name)
        messages.append({"role": "user", "content": _observation_text(name, outcome.observation)})

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


def _done(message, artifacts, trace, stopped_by=None):
    return {"message": message, "artifacts": artifacts, "trace": trace,
            "stopped_by": stopped_by}
