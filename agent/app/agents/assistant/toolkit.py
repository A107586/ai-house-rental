"""agent 循环能用的工具，以及它们和现有实现的接线。

**工具本身几乎都不是新写的**：读操作走 `toolbox/runner.run_read`（取数、序号还原、
字段映射都在那里），平台规则走客服的检索问答（带可答性闸门，没依据就拒答），
写操作走 `support/tools/actions.propose`（只读后端、只生成确认卡片）。
这里做三件事：

1. **挑出这个人能用的工具**，并给写工具统一加上 `request` 参数——提议器是从一句
   自然语言里解析要办的事的（「房租」不给押金、「第二笔」「周六上午十点」），
   这套解析器处理过大量边界情况，让模型复述一句带全细节的话交给它，
   比让模型直接填一堆结构化字段稳。
2. **把工具结果压缩成模型看得完的观察**，同时把结构化结果留给前端渲染。
3. **守住写操作的边界**：出了确认卡片或需要追问，这一轮就停在这里，
   话术用提议器生成的（卡片上的事实不让模型改写）；执行永远要用户点确认。
"""

import json

from app.agents.assistant.loop import Outcome
from app.agents.toolbox import catalog

# agent 自己就能直接回答，不需要「兜底对话」和「不处理」这两个选项
_SKIP = {"none", "converse"}

# 给前端的字段：读工具的结果里有这些就原样带出去
_UI_KEYS = ("items", "total", "recommended", "append", "page", "account", "pitch",
            "compare", "house_no", "notes")

# 写工具统一的参数。request 必填，其余是提议器认得的槽位，填了就当已知
_WRITE_PARAMS = {
    # 用用户的口吻写：提议器会把这句话原样写进工单、卡片（报修的故障描述就是它），
    # 模型写成「租客报修：……」时，工单内容就成了「租客：厨房水龙头……」
    "request": ("string", "用用户自己的口吻（第一人称）一句话说要办的事，带上全部细节："
                          "哪套房、哪天几点、哪笔账单、故障现象、要改成什么。"
                          "例：「厨房水龙头一直滴水」「交 9 月房租」「把周六的看房改到下周一上午」"),
    "ordinal": ("number", "用户说的第几套（列表里的序号），没说就不填"),
    "house_no": ("string", "房源编号，只能抄工具结果或当前情况里出现过的"),
}

# 目录里没有、但提议器支持的写操作。只收语义清楚、权限简单的几个
_EXTRA_WRITES = [
    {"name": "publish_house", "kind": "write", "action": "publish_house", "roles": ("landlord",),
     "desc": "房东发布一套新房源（会逐项追问小区、户型、面积、租金等）。"},
    {"name": "submit_review", "kind": "write", "action": "submit_review",
     "desc": "给住过或看过的房源写评价、打分。"},
    {"name": "read_all_messages", "kind": "write", "action": "read_all_messages",
     "desc": "把站内消息全部标为已读。"},
    {"name": "clear_history", "kind": "write", "action": "clear_history",
     "desc": "清空浏览记录。"},
    {"name": "update_profile", "kind": "write", "action": "update_profile",
     "desc": "修改自己的资料：姓名、手机号、邮箱等。"},
]

# 观察里每条记录最多带几个字段、每个字段最长多少
_ROW_FIELDS = 12
_FIELD_CHARS = 80


class Toolkit:
    """一轮对话的工具集。`deps` 由 `app/main.py` 注入，见 `_agent_turn`。

    deps 需要提供：
      logged_in, role, runner_ctx (dict),
      run_read(name, params, ctx) -> dict | None
      knowledge(question) -> dict        # 客服检索问答的结果
      recommend() -> dict                 # 推荐结果（带 items）
      propose(action, params, text, images) -> dict   # 提议器包装后的结果
    """

    def __init__(self, deps):
        self.deps = deps
        self._tools = None

    # ------------------------------------------------------------------ 工具表
    def tools(self):
        if self._tools is not None:
            return self._tools
        visible = catalog.visible_tools(self.deps.logged_in, self.deps.role)
        out = []
        for tool in visible:
            if tool["name"] in _SKIP:
                continue
            if tool.get("kind") == "write":
                tool = dict(tool, params=dict(_WRITE_PARAMS, **(tool.get("params") or {})))
            out.append(tool)
        if self.deps.logged_in:
            for tool in _EXTRA_WRITES:
                roles = tool.get("roles")
                if roles and self.deps.role not in roles + ("admin",):
                    continue
                out.append(dict(tool, params=dict(_WRITE_PARAMS)))
        self._tools = out
        return out

    def _tool(self, name):
        for tool in self.tools():
            if tool["name"] == name:
                return tool
        return None

    # ------------------------------------------------------------------ 调用
    def call(self, name, params, text="", images=()):
        tool = self._tool(name)
        if tool is None:
            return Outcome("没有这个工具。")
        params = _whitelist(params, tool)
        if tool.get("kind") == "write":
            return self._write(tool, params, text, images)
        if name == "knowledge":
            return self._knowledge(params.get("question") or text)
        if name == "recommend_houses":
            return self._read_result(self.deps.recommend())
        result = self.deps.run_read(name, params, self.deps.runner_ctx)
        return self._read_result(result)

    def _read_result(self, result):
        if not result:
            return Outcome("没有查到结果。")
        if result.get("_out_of_range"):
            count = len(self.deps.runner_ctx.get("last_house_nos") or [])
            return Outcome("用户说的序号超出了列表范围：屏幕上一共只有 %d 套。" % count)
        artifacts = {k: result[k] for k in _UI_KEYS if result.get(k) not in (None, "", [], {})}
        return Outcome(_compact(result), artifacts)

    def _knowledge(self, question):
        result = self.deps.knowledge(question) or {}
        grounded = result.get("status") in ("answered", "account")
        sources = [c.get("title") or c.get("question") or ""
                   for c in (result.get("citations") or []) if isinstance(c, dict)]
        observation = {
            "有依据": grounded,
            "依据给出的回答": (result.get("message") or "")[:900] if grounded else "",
            "出处": [s for s in sources if s][:3],
        }
        if not grounded:
            observation["说明"] = "知识库里没有这个问题的依据，不要凭常识回答，可以建议转人工。"
            related = [a.get("question") if isinstance(a, dict) else a
                       for a in (result.get("alternatives") or [])][:3]
            if related:
                observation["相近的问题"] = related
        artifacts = {"citations": result.get("citations") or [] if grounded else [],
                     "alternatives": result.get("alternatives") or [],
                     "handoff": None if grounded else result.get("handoff")}
        return Outcome(observation, artifacts)

    def _write(self, tool, params, text, images):
        result = self.deps.propose(tool["action"], params, text, images) or {}
        status = result.get("status")
        artifacts = {k: result.get(k) for k in ("action", "pending", "options", "form")
                     if result.get(k)}
        if status in ("confirm", "need_slot"):
            # 卡片或追问：这一轮到此为止，话术用提议器的
            return Outcome({"status": status}, artifacts, stop=True,
                           message=result.get("message") or "")
        # 办不了（没登录、没有可缴的账单、后端没连上……）：交回给模型，由它向用户解释
        return Outcome({"status": status or "failed", "说明": result.get("message") or "没办成"},
                       artifacts)


def _whitelist(params, tool):
    """只留工具声明过的参数；类型不对的丢掉，不替模型猜。"""
    out = {}
    for key, (kind, _note) in (tool.get("params") or {}).items():
        if key not in (params or {}):
            continue
        value = params[key]
        if kind == "number":
            try:
                value = float(value)
                value = int(value) if value.is_integer() else value
            except (TypeError, ValueError):
                continue
        elif kind == "string[]":
            if isinstance(value, str):
                value = [value]
            if not isinstance(value, list):
                continue
            value = [str(v) for v in value if str(v).strip()]
        else:
            if isinstance(value, (dict, list)):
                continue
            value = str(value).strip()
        if value in ("", [], None):
            continue
        out[key] = value
    return out


def _compact(result):
    """读工具结果 → 给模型看的观察。模板话术留着当摘要，数据只挑要紧的。"""
    obs = {}
    message = (result.get("message") or "").strip()
    if message:
        obs["摘要"] = message[:600]
    items = result.get("items") or []
    if items:
        offset = int(result.get("offset") or 0)
        obs["房源"] = [dict(序号=offset + i + 1, **_row(item)) for i, item in enumerate(items[:10])]
        if result.get("total"):
            obs["符合条件的总数"] = result["total"]
    account = result.get("account")
    if isinstance(account, dict) and account.get("data") is not None:
        obs.update(_account_obs(account["data"]))
    for key in ("pitch", "compare"):
        if isinstance(result.get(key), dict):
            obs[key] = _row(result[key], limit=20)
    return obs or "工具执行了，但没有返回内容。"


# 金额类字段：记录多的时候（实测一个租客 36 笔待缴账单）不可能全塞给模型，
# 就替它把「最大的一笔」和合计算好——「哪笔最贵」「一共欠多少」是最常见的追问
_MONEY_KEYS = ("amount", "rent_price", "price", "commission", "total_amount")
_ROWS_SHOWN = 12


def _account_obs(data):
    """查库结果 → 观察。记录列表可能挂在任意字段名下（unpaid、list、items……），都认。"""
    if isinstance(data, list):
        data = {"记录": data}
    if not isinstance(data, dict):
        return {"数据": str(data)[:600]}
    # **汇总在前、明细在后。** 观察有长度上限，明细一多就会被截断——
    # 第一版把「最大一笔」放在明细后面，36 笔账单时正好被截掉，
    # 模型回「没列出每笔金额，没法判断哪笔最贵」
    summary, rows = {}, {}
    for key, value in data.items():
        if isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
            summary[key + "_条数"] = len(value)
            money = next((k for k in _MONEY_KEYS if any(k in r for r in value)), None)
            if money:
                amounts = [(float(r.get(money) or 0), r) for r in value]
                top = max(amounts, key=lambda pair: pair[0])[1]
                summary[key + "_最大一笔"] = _row(top, limit=9)
                summary[key + "_合计"] = round(sum(a for a, _ in amounts), 2)
            if len(value) > _ROWS_SHOWN:
                summary[key + "_说明"] = "明细只列了前 %d 条" % _ROWS_SHOWN
            rows[key] = [_row(r, limit=9) for r in value[:_ROWS_SHOWN]]
        elif not isinstance(value, (list, dict)):
            summary[key] = value
    summary.update(rows)
    return summary


# 观察里用不上的字段：内部 id、空备注
_DROP_KEYS = ("id", "contract_id", "house_id", "user_id", "tenant_id", "landlord_id")


def _row(row, limit=_ROW_FIELDS):
    if not isinstance(row, dict):
        return {"值": str(row)[:_FIELD_CHARS]}
    out = {}
    for key, value in row.items():
        if len(out) >= limit:
            break
        if value in (None, "", [], {}) or key in _DROP_KEYS:
            continue
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)
        value = str(value)
        if key.endswith(("url", "image", "images", "pic", "photo")) or value.startswith(("http", "/static/", "data:")):
            continue
        out[key] = value[:_FIELD_CHARS]
    return out
