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
    # 只写事情本身：报修的 request 会原样进工单，实测带进去的是
    # 「……房东消息也不回，帮我报修处理一下」——师傅要看的是故障，不是情绪
    "request": ("string", "用用户自己的口吻（第一人称）一句话说要办的事，带上全部细节："
                          "哪套房、哪天几点、哪笔账单、故障在哪个位置什么现象、要改成多少"
                          "（写确定的数，不写「约」「左右」）。只写事情本身，不带情绪、抱怨和催促。"
                          "例：「厨房水龙头一直滴水」「交 9 月房租」「把周六的看房改到下周一上午」"),
    "ordinal": ("number", "用户说的第几套（列表里的序号），没说就不填"),
    "house_no": ("string", "房源编号，只能抄工具结果或当前情况里出现过的"),
}

# 目录里没有、但提议器支持的写操作。只收语义清楚、权限简单的几个。
# `identities`：哪些身份用得上（见 catalog.IDENTITIES），不写就是都能用
_EXTRA_WRITES = [
    {"name": "publish_house", "kind": "write", "action": "publish_house",
     "identities": ("landlord",),
     "desc": "房东发布一套新房源（会逐项追问小区、户型、面积、租金等）。"},
    # 原先助手里没有签约：房东说「给 tenant1 签 DL0007」，模型只能让他去工作台点
    {"name": "sign_contract", "kind": "write", "action": "sign_contract",
     "identities": ("landlord",),
     "desc": "房东给自己的某套房源和某位租客发起签约（生成待确认合同，租客确认后生效）。"
             "会逐项追问房源、租客账号、起租日、租期。",
     "params": {
         "tenant_username": ("string", "租客的平台账号（用户名），用户说了才填"),
         "start_date": ("string", "起租日，写成 2026-11-01 这样；用户没说就不填"),
         "months": ("number", "租期几个月，用户没说就不填"),
         "rent": ("number", "谈定的月租（元），和挂牌价不同时才填"),
     }},
    {"name": "submit_review", "kind": "write", "action": "submit_review",
     "identities": ("tenant",),
     "desc": "给住过或看过的房源写评价、打分。"},
    {"name": "read_all_messages", "kind": "write", "action": "read_all_messages",
     "desc": "把站内消息全部标为已读。"},
    {"name": "clear_history", "kind": "write", "action": "clear_history",
     "identities": ("tenant",),
     "desc": "清空浏览记录。"},
    {"name": "update_profile", "kind": "write", "action": "update_profile",
     "desc": "修改自己的资料：姓名、手机号、邮箱等。"},
]

# 一轮里多次找房合并上屏时，屏幕上最多摆几套
_SCREEN_MAX = 20

# 合同审阅（AI-02）。工具箱目录里没有它：原先用户发来合同照片问「这几条合理吗」，
# 模型只能拿知识库的通用规则去比，把「押金不退、另付一个月违约金」说成「和平台规则
# 方向一致」——其实是双重罚；还建议他「让 AI 合同审阅帮你判断」，自己却调不到
_REVIEW_TOOL = {
    "name": "review_contract", "kind": "read",
    "desc": "审阅合同条款：用户发来合同照片、截图或贴了条款原文，问合不合理、有没有坑、"
            "能不能签时用它。会标出高风险条款、缺失的条款和修改建议。"
            "回答时可再调 knowledge 对照平台规则。",
    "params": {"text": ("string", "合同条款原文：照片里的条款**逐字抄下来**，不要概括、不要改写")},
}

# 转人工。原先工具表里没有它：用户说「转人工」，模型只能答「我这边没有转人工的工具，
# 你看看页面上有没有人工入口」——可回复下面明明就摆着一个「转人工」按钮。
# 和写工具一样**只出按钮不建工单**：用户点了，前端才把问题和最近几句对话交给管理员
_HANDOFF_TOOL = {
    "name": "handoff", "kind": "stop",
    "desc": "转人工：用户要找人工客服 / 真人，或者你查不到依据、答不上来、用户不满意时用。"
            "会在回复下面放一个「转人工」按钮，用户点了才把问题交给管理员，回复发到他的站内消息。"
            "不是用户主动要的（是你查不到）时，同一个 JSON 里加 say：先说清楚哪件事没查到、"
            "查到的部分是什么，按钮接在后面。",
    "params": {"question": ("string", "交给管理员的问题，用户口吻一句话写清楚；他只说了「转人工」"
                                      "就根据前面的对话概括他想问什么")},
}

# 观察里每条记录最多带几个字段、每个字段最长多少
_ROW_FIELDS = 12
_FIELD_CHARS = 80


class Toolkit:
    """一轮对话的工具集。`deps` 由 `app/main.py` 注入，见 `_agent_turn`。

    deps 需要提供：
      logged_in, role（账号角色）, identity（这一轮的身份，见 catalog.resolve_identity）,
      runner_ctx (dict),
      run_read(name, params, ctx) -> dict | None
      knowledge(question) -> dict        # 客服检索问答的结果
      recommend() -> dict                 # 推荐结果（带 items）
      review(text) -> dict                # 合同审阅（AI-02）；不提供就没有 review_contract 这个工具
      propose(action, params, text, images) -> dict   # 提议器包装后的结果
    """

    def __init__(self, deps):
        self.deps = deps
        self._tools = None
        # 这一轮已经摆上屏幕的房源（多次找房时合并），以及它们的总数
        self._screen = []
        self._screen_total = 0
        # 这一轮查过平台规则却没查到。转人工时模型没写 say，就由代码补一句「没查到」——
        # 实测「电梯多久维保一次」回复只有一句「需要的话点下面的转人工」，没说为什么
        self._missed = False

    # ------------------------------------------------------------------ 工具表
    def tools(self):
        if self._tools is not None:
            return self._tools
        visible = catalog.visible_tools(self.deps.logged_in, self._identity())
        out = []
        for tool in visible:
            if tool["name"] in _SKIP:
                continue
            if tool.get("kind") == "write":
                tool = dict(tool, params=dict(_WRITE_PARAMS, **(tool.get("params") or {})))
            out.append(tool)
        if getattr(self.deps, "review", None):
            out.append(_REVIEW_TOOL)
        out.append(_HANDOFF_TOOL)
        if self.deps.logged_in:
            for tool in _EXTRA_WRITES:
                allowed = tool.get("identities")
                if allowed and self._identity() not in allowed:
                    continue
                out.append(dict(tool, params=dict(_WRITE_PARAMS, **(tool.get("params") or {}))))
        self._tools = out
        return out

    def _identity(self):
        if not self.deps.logged_in:
            return ""
        return getattr(self.deps, "identity", "") or catalog.resolve_identity(self.deps.role)

    def why_unavailable(self, name):
        """模型要的工具不在这个人的工具表里时，告诉它**为什么**。

        未登录用户的工具表里没有写工具。实测他说「帮我约周六上午去看」，
        模型照样去调预约（还自己编了个名字），拿回「没有这个工具」，
        就对用户说「我这边没有预约看房的入口，你去 App 里约吧」——
        平台明明能约，也没有 App，只是要先登录。
        """
        if self.deps.logged_in:
            known = {t["name"] for t in catalog.TOOLS} | {t["name"] for t in _EXTRA_WRITES}
            if name not in known:
                return None
            identity = self._identity()
            others = [i for i in catalog.IDENTITIES.get(self.deps.role, ()) if i != identity]
            if others:
                # 房东账号：另一个身份就能办，告诉他点哪里切
                return ("「%s」不是%s身份的功能。告诉用户：在助手顶部把身份切到「%s」再说一次就能办；"
                        "不要说平台没有这个功能。"
                        % (name, catalog.IDENTITY_LABEL[identity],
                           "」或「".join(catalog.IDENTITY_LABEL[i] for i in others)))
            return ("「%s」不是%s账号能用的功能（中介只做经纪业务、管理员只做平台管理，"
                    "不以租客身份预约、缴费、签约）。照实告诉用户，不要说平台没有这个功能。"
                    % (name, catalog.IDENTITY_LABEL.get(identity, "当前")))
        return ("没有叫「%s」的工具——用户**还没登录**，所以预约看房、收藏、缴费、报修这类办事的工具"
                "都还没给你。平台能办这些事：请他先点页面右上角「登录 / 注册」，登录后在对话里"
                "再说一声就能办。不要说做不了，也不要提 App、电话这类不存在的入口。" % name)

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
        if name == "review_contract":
            return self._review(params.get("text") or text)
        if name == "handoff":
            return self._handoff(params.get("question") or text)
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
        result = self._onto_screen(result)
        artifacts = {k: result[k] for k in _UI_KEYS if result.get(k) not in (None, "", [], {})}
        if result.get("_screen"):
            artifacts["items"] = result["_screen"]
            artifacts["total"] = self._screen_total
        return Outcome(_compact(result), artifacts)

    def _onto_screen(self, result):
        """一轮里第二次找房：结果**接在**上一批后面上屏，序号接着数。

        实测「甘井子和金州的也可以」，模型分两次查了两个区——前端只留最后一次的
        结果（金州 4 套），回答里却在讲甘井子那 5 套；下一句「按房租排一下」
        排的又是它记得的那几套，屏幕上的另外 3 套没了。屏幕、回答、「第几套」
        三者对不上。合并之后，模型看到的序号就是用户屏幕上的序号。
        """
        items = result.get("items") or []
        if not items or result.get("append"):
            return result
        if not self._screen:
            self._screen = list(items)
            self._screen_total = int(result.get("total") or len(items))
            return result
        known = {h.get("house_no") for h in self._screen}
        fresh = [h for h in items if h.get("house_no") not in known]
        start = len(self._screen)
        fresh = fresh[:max(0, _SCREEN_MAX - start)]
        self._screen = self._screen + fresh
        self._screen_total += int(result.get("total") or len(items))
        merged = dict(result, items=fresh, offset=start, _screen=list(self._screen))
        merged["message"] = ("这一批接在本轮上一次查询的结果后面显示，序号接着数。"
                             + (result.get("message") or ""))
        return merged

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
            self._missed = True
            near = result.get("related") or []
            if near:
                # 检索闸门没把握，但有沾边的条目。原文给模型，由它判断对不对得上——
                # 实测「这套有中介带看房吗」被拒，「看房是谁带看？」就在相近条目里
                observation["相近条目原文"] = [{"标题": r.get("title"), "原文": r.get("body")}
                                         for r in near if isinstance(r, dict)]
                observation["说明"] = ("检索没能确定命中。先看「相近条目原文」：确实回答了用户问的事，"
                                     "就按原文回答，并说明依据是平台的「某某」说明；对不上就照实说没查到，"
                                     "不要凭常识补，可以调 handoff 让用户转人工。")
            else:
                observation["说明"] = "知识库里没有这个问题的依据，不要凭常识回答，可以调 handoff 让用户转人工。"
            titles = [a.get("question") if isinstance(a, dict) else a
                      for a in (result.get("alternatives") or [])][:3]
            if titles:
                observation["相近的问题"] = titles
        artifacts = {"citations": result.get("citations") or [] if grounded else [],
                     "alternatives": result.get("alternatives") or [],
                     "handoff": None if grounded else result.get("handoff")}
        return Outcome(observation, artifacts)

    def _handoff(self, question):
        if not self.deps.logged_in:
            # 工单要回给某个人，没登录就没有「谁」。按钮摆出来点了也只会报错，不如直说
            return Outcome({"status": "need_login"}, stop=True,
                           message="转人工要先登录，管理员才知道回复给谁。点页面右上角「登录 / 注册」，"
                                   "登录后再说一声「转人工」就行。")
        return Outcome({"status": "button_shown"},
                       {"handoff": {"question": question, "reason": "用户要求转人工"}},
                       stop=True,
                       lead="这个问题平台规则里没有查到明确说明，我不敢乱说。" if self._missed else "",
                       message="需要的话点下面的「转人工」，我把这个问题和刚才的对话一起交给管理员，"
                               "回复会发到你的站内消息。")

    def _review(self, text):
        result = self.deps.review(text) or {}
        if not result.get("ok"):
            return Outcome({"说明": result.get("message") or "这段文字认不出合同条款，请用户把条款原文发来。"})
        risks = [{k: r.get(k) for k in ("level", "title", "why", "advice", "quote") if r.get(k)}
                 for r in (result.get("risks") or [])][:8]
        observation = {
            "摘要": (result.get("message") or "")[:400],
            "风险条款": risks,
            "缺少的条款": result.get("missing") or [],
            "对租客有利的条款": [f.get("title") if isinstance(f, dict) else f
                         for f in (result.get("favourable") or [])][:5],
        }
        return Outcome(observation)

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
        obs["房源"] = [dict(序号=offset + i + 1, **_house_row(item))
                     for i, item in enumerate(items[:10])]
        if result.get("total"):
            obs["符合条件的总数"] = result["total"]
    if isinstance(result.get("house"), dict):
        obs["房源资料"] = _house_row(result["house"], full=True)
    account = result.get("account")
    if isinstance(account, dict) and account.get("data") is not None:
        obs.update(_account_obs(account["data"]))
    if isinstance(result.get("compare"), dict):
        obs["对比"] = _compare_obs(result["compare"])
    if isinstance(result.get("pitch"), dict):
        obs["讲解要点"] = _pitch_obs(result["pitch"])
    return obs or "工具执行了，但没有返回内容。"


def _compare_obs(data):
    """对比表 → 按房源列出的各项数值。

    原先走 `_row`：rows、houses 两个列表被各自转成 JSON 再截到 80 个字，
    模型看到的是半张表。实测「第一套和我刚才看的那套比」，工具算对了
    （66 平对 123 平），模型却说「123 平对 121 平」——它从屏幕列表里
    拿了另一套的面积来补那半张表。
    """
    houses = [h.get("house_no") or "第 %d 套" % (i + 1)
              for i, h in enumerate(data.get("houses") or [])]
    table = {no: {} for no in houses}
    for row in data.get("rows") or []:
        values = row.get("values") or []
        for no, value in zip(houses, values):
            table[no][row.get("label") or ""] = value
        best = row.get("best")
        if isinstance(best, int) and 0 <= best < len(houses):
            table[houses[best]].setdefault("占优的项", []).append(row.get("label"))
    return table


def _pitch_obs(data):
    """讲解一套房的要点：亮点、短板、价格对比，原样给全（不截成 80 个字）。"""
    out = {}
    for key, value in data.items():
        if value in (None, "", [], {}) or key in ("house", "houses", "images"):
            continue
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        out[key] = text[:400]
    return out


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
    stated = data.get("total") if isinstance(data.get("total"), int) else None
    for key, value in data.items():
        if isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
            # **列表只带了一部分时，别拿这部分去算合计。**
            # 账单卡片只摆前 5 条（共 36 条）。原先照样算「合计」「最大一笔」，
            # 模型就说「工具列出的未缴账单是 5 笔，合计 8850 元」——把截断当成了全部
            partial = stated is not None and stated > len(value)
            summary[key + "_条数"] = ("这里列了 %d 条，共 %d 条" % (len(value), stated)
                                      if partial else len(value))
            money = next((k for k in _MONEY_KEYS if any(k in r for r in value)), None)
            if money and not partial:
                amounts = [(float(r.get(money) or 0), r) for r in value]
                top = max(amounts, key=lambda pair: pair[0])[1]
                summary[key + "_最大一笔"] = _row(top, limit=9)
                summary[key + "_合计"] = round(sum(a for a, _ in amounts), 2)
            if len(value) > _ROWS_SHOWN:
                summary[key + "_说明"] = "明细只列了前 %d 条" % _ROWS_SHOWN
            # 收藏、足迹、房东名下的房：是房源就按房源挑字段，否则按出现顺序截
            rows[key] = [_house_row(r) if _is_house(r) else _row(r, limit=9)
                         for r in value[:_ROWS_SHOWN]]
        elif isinstance(value, dict):
            # 上游按全量算好的汇总（如账单的「待缴拆分」），原样交给模型
            summary[_KEY_LABELS.get(key, key)] = value
        elif not isinstance(value, list):
            summary[_KEY_LABELS.get(key, key)] = value
    summary.update(rows)
    return summary


# 房源交给模型时**按名字挑字段**，不按出现顺序截前几个。
#
# 原先房源行和账单行一样走 `_row`：取前 12 个非空字段。房源接口的字段顺序是
# 编号、标题、区、小区、地址、租金、押金、面积、户型、方式、楼层、朝向……
# 第 13 个起是 tags、评分、电梯、装修——**正好是用户追问最多的那几样**。
# 实测「一家四口、老人腿脚不好、孩子要上学」，模型只能说「建议优先看有电梯、
# 近学校的」，却说不出列表里哪套有电梯：它压根没看到这两个字段。
_HOUSE_FIELDS = (
    ("house_no", "编号"), ("title", "标题"), ("rent_price", "租金"), ("area", "面积"),
    ("layout", "户型"), ("rent_type", "方式"), ("floor", "楼层"), ("elevator", "电梯"),
    ("orientation", "朝向"), ("tags", "配套"), ("decoration", "装修"),
    ("rating", "评分"), ("review_count", "评价数"), ("deposit", "押金"),
    ("address", "地址"),
    # 房东看自己名下的房时才有：判断「挂了很久没租出去」靠这三个
    ("status", "状态"), ("published_at", "上架日期"), ("view_count", "浏览量"),
)
# 讲单独一套时再多给的：描述、商圈、付款方式、起租、费用……
_HOUSE_DETAIL_FIELDS = (
    ("business_area", "商圈"), ("description", "描述"), ("pay_plan", "付款方式"),
    ("min_months", "最短租期（月）"), ("available_from", "可入住"),
    ("heating", "供暖"), ("fee_detail", "费用说明"), ("landlord_name", "房东"),
    ("verify_status", "核验"), ("viewing_by", "带看"),
)

# 带看是谁：后端给的是 agent / landlord，换成模型能直接说给用户的话
_VIEWING_BY = {"agent": "委托中介带看", "landlord": "房东自己带看"}


_UNITS = {"rent_price": "%g 元/月", "area": "%g 平", "deposit": "%g 元"}

_HOUSE_STATUS = {"available": "在架", "rented": "已出租", "offline": "已下架",
                 "pending": "待审核", "rejected": "审核未通过"}


def _is_house(row):
    # 看房预约的行也带 house_no、layout、rent_price——当成房源会把看房时间、
    # 状态挑掉。房源记录才有 title 和 area
    return (isinstance(row, dict)
            and all(k in row for k in ("house_no", "title", "area", "rent_price")))


def _house_row(house, full=False):
    fields = _HOUSE_FIELDS + (_HOUSE_DETAIL_FIELDS if full else ())
    out = {}
    for key, label in fields:
        value = house.get(key)
        if value in (None, "", [], {}):
            continue
        if key == "elevator":
            value = "有" if value in (True, 1, "1", "true", "True") else "无"
        elif key in _UNITS:
            try:
                value = _UNITS[key] % float(value)
            except (TypeError, ValueError):
                pass
        elif key == "review_count" and not value:
            continue
        elif key == "status":
            value = _HOUSE_STATUS.get(value, value)
        elif key == "viewing_by":
            value = _VIEWING_BY.get(value, value)
            if house.get("viewing_agent") and house.get("viewing_by") == "agent":
                value += "（中介 %s）" % house["viewing_agent"]
        elif isinstance(value, list):
            value = "、".join(str(v) for v in value)
        elif isinstance(value, dict):
            value = json.dumps(value, ensure_ascii=False)
        out[label] = str(value)[:200 if key == "description" else _FIELD_CHARS]
    return out


# 观察里用不上的字段：内部 id、空备注
_DROP_KEYS = ("id", "contract_id", "house_id", "user_id", "tenant_id", "landlord_id")

# 容易读错的英文字段换成带解释的中文名。
# 「待缴 / 待收」里大半是合同里排好、还没到期的账单，不是拖欠——
# 原样给 unpaid_total 时，助手对房东说「林小雨一共欠 39650 元」，实际逾期只有 2 笔
_KEY_LABELS = {
    "unpaid_total": "待缴合计（含还没到期的）",
    "unpaid_amount": "待缴合计（含还没到期的）",
    "unpaid_count": "待缴笔数（含还没到期的）",
    "income_pending": "待收（含还没到期的）",
    "overdue_total": "逾期未缴合计",
    "overdue_count": "逾期笔数",
    "paid_total": "已缴合计",
    "month_income": "本月已收",
    "overdue": "已逾期",
    "due_date": "到期日",
    # 收支记录：方向是从当前这一方看的
    "total_out": "交出去的合计",
    "total_in": "退回 / 收到的合计",
    "pending_amount": "待结合计",
    "settled_amount": "已结合计",
}


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
        out[_KEY_LABELS.get(key, key)] = value[:_FIELD_CHARS]
    return out
