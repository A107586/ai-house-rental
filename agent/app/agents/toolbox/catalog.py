# -*- coding: utf-8 -*-
"""可调用工具的登记表（G1）。

## 为什么要有这一层

在这之前，「用户这句话该走哪条路」由**三层关键词表**决定：
`supervisor.classify` 一层、`support_router._rule_service` 一层、
各工具内部的 `ASPECTS` / `REFERS` 又一层。

这个结构的问题不是「偶尔答得不好」，是**漏掉的那一句会掉进另一条
完全无关的路**：

    「看看第六套」   → 序数表只到第五 → 当成新的找房 → 「没听懂你的找房需求」
    「评价怎么样」   → 命中讲解词      → 回一段户型朝向的推销词
    「再多看几套」   → 一个条件都没有  → 「没听懂你的找房需求」

这三句全是界面自己教用户说的。表总有漏的，而漏了的代价太大。

## 这一层做什么、不做什么

**做**：把「选哪条路、填哪些参数」交给模型。每个能力在这里登记一条，
带上一份参数 schema，模型只输出 `{tool, params}`。

**不做**：不执行、不算数、不组织最终话术。

- 读操作的**数据仍由代码去后端取**，模型碰不到金额、评分、房源字段；
- 写操作**一律不执行**，只转成已有的两阶段确认卡片
  （`actions.PROPOSERS` → 用户点确认 → `/agent/support/confirm`）。
  那条路径上的 `ALLOWED_PARAMS` 白名单是真正的安全边界，
  这一层无权绕过它。

所以模型多出来的权力只有一样：**决定调哪个工具**。参数要过 schema 校验，
结果由代码生成，副作用要人点头。模型选错的最坏后果是答非所问，
而不是改错数据。

## 为什么不用厂商的 function calling 接口

`LLMProvider.complete` 是团队自己的抽象层，接的是 DeepSeek（经中转站）。
不同厂商的 function calling 请求体、返回结构、甚至有没有这个能力都不一样，
一旦用上，`app/llm/base.py` 那句「换厂商只需新增一个实现类」就不成立了。

这里改成：把工具表渲染进 system prompt，让模型输出一份 JSON。
能力上等价（选工具 + 填参数），而且**任何一个能输出 JSON 的模型都能跑**，
包括本地部署的小模型。
"""

# 参数类型只用三种：string / number / string[]。
#
# **刻意不支持嵌套对象。** 一个能力如果需要嵌套参数才说得清，
# 多半是它本身太大了，该拆成两个工具——而嵌套 schema 对小模型
# 的正确率影响很明显。
TOOLS = [
    # ------------------------------------------------ 找房与房源（不需登录）
    {
        "name": "search_houses",
        "kind": "read",
        "desc": "按条件找房。用户说了地段、价格、户型、面积、朝向、"
                "配套（地铁/电梯/家具/养宠物）等任意条件时用它。",
        "params": {
            "district": ("string", "行政区，如 中山区、沙河口区、金州区"),
            "rent_max": ("number", "租金上限，单位元/月"),
            "rent_min": ("number", "租金下限，单位元/月"),
            "layout": ("string", "户型，如 一室一厅、两室一厅"),
            "area_min": ("number", "面积下限，平方米"),
            "area_max": ("number", "面积上限，平方米"),
            "features": ("string[]", "配套要求，只能取：地铁、电梯、有家具、"
                                     "可养宠物、近学校、近医院、有阳台、采光好"),
            "keyword": ("string", "小区名或街道名，如 桂林街"),
        },
        "required": [],
    },
    {
        "name": "more_houses",
        "kind": "read",
        "desc": "接着上一批往下翻，给更多房源。用户说「再多看几套」"
                "「还有别的吗」「换一批」时用它。",
        "params": {},
        "required": [],
    },
    {
        "name": "recommend_houses",
        "kind": "read",
        "desc": "不给具体条件、让平台推荐。用户说「有什么合适的」"
                "「帮我挑几套」「你看着办」时用它。",
        "params": {},
        "required": [],
    },
    {
        "name": "house_detail",
        "kind": "read",
        "desc": "讲解某一套房：亮点、短板、适合谁。用户指着列表里的某一套"
                "问「介绍一下」「怎么样」「值不值」时用它。",
        "params": {
            "house_no": ("string", "房源编号，如 DL0041。用户说「第三套」"
                                   "时填 ordinal，不要猜编号"),
            "ordinal": ("number", "用户说的第几套，从 1 开始"),
        },
        "required": [],
    },
    {
        "name": "house_reviews",
        "kind": "read",
        "desc": "某一套房的租后评价、评分、口碑、有没有人住过。",
        "params": {
            "house_no": ("string", "房源编号"),
            "ordinal": ("number", "用户说的第几套，从 1 开始"),
        },
        "required": [],
    },
    {
        "name": "landlord_reputation",
        "kind": "read",
        "desc": "某一套房的房东怎么样：口碑、评分、报修响应快不快、"
                "靠不靠谱。注意和 house_reviews 区分——那个问的是房子。",
        "params": {
            "house_no": ("string", "房源编号"),
            "ordinal": ("number", "用户说的第几套，从 1 开始"),
        },
        "required": [],
    },
    {
        "name": "compare_houses",
        "kind": "read",
        "desc": "在已经列出来的两三套之间对比、问哪个好、哪个划算。",
        "params": {
            "ordinals": ("number[]", "要对比的是第几套，从 1 开始，两到三个"),
            "house_nos": ("string[]", "房源编号，用户直接报编号时用"),
        },
        "required": [],
    },

    # ------------------------------------------------ 查我自己的数据（需登录）
    {
        "name": "my_bills",
        "kind": "read",
        "desc": "查我的账单：欠多少房租、有没有逾期、什么时候到期。",
        "params": {},
        "required": [],
    },
    {
        "name": "my_contracts",
        "kind": "read",
        "desc": "查我的租约：租到什么时候、租金多少、还有几天到期。",
        "params": {},
        "required": [],
    },
    {
        "name": "my_appointments",
        "kind": "read",
        "desc": "查我约的看房：约了哪几套、什么时候、房东确认了没有。",
        "params": {},
        "required": [],
    },
    {
        "name": "my_repairs",
        "kind": "read",
        "desc": "查我报的修：处理到哪一步了、谁在修。",
        "params": {},
        "required": [],
    },
    {
        "name": "my_favorites",
        "kind": "read",
        "desc": "查我收藏的房源。",
        "params": {},
        "required": [],
    },

    # ------------------------------------------------ 房东 / 中介侧（需登录）
    {
        "name": "landlord_appointments",
        "kind": "read",
        "desc": "（房东或中介用）谁约了我的房、哪些还等着我确认。"
                "注意和 my_appointments 区分——那个是「我约了别人」。",
        "params": {},
        "required": [],
    },
    {
        "name": "landlord_houses",
        "kind": "read",
        "desc": "（房东或中介用）我名下/在托的房源，在架几套、租出去几套。",
        "params": {},
        "required": [],
    },
    {
        "name": "landlord_tenants",
        "kind": "read",
        "desc": "（房东用）我的租客有哪些、租到什么时候。",
        "params": {},
        "required": [],
    },

    # ------------------------------------------------ 写操作：只出确认卡片
    #
    # 这里的 `action` 是 actions.PROPOSERS 里的键。这一层**只负责把
    # 意图翻译成动作名**，卡片、追问、参数白名单、真正的执行，
    # 全在原来那条两阶段路径上，一点没动。
    {
        "name": "book_viewing", "kind": "write", "action": "book_viewing",
        "desc": "帮我预约看房。",
        "params": {}, "required": [],
    },
    {
        "name": "cancel_viewing", "kind": "write", "action": "cancel_viewing",
        "desc": "取消一次看房预约。",
        "params": {}, "required": [],
    },
    {
        "name": "reschedule_viewing", "kind": "write",
        "action": "reschedule_viewing",
        "desc": "改约看房时间。",
        "params": {}, "required": [],
    },
    {
        "name": "pay_bill", "kind": "write", "action": "pay_bill",
        "desc": "缴一笔账单、交房租。",
        "params": {}, "required": [],
    },
    {
        "name": "submit_repair", "kind": "write", "action": "submit_repair",
        "desc": "报修：家里什么东西坏了、漏水、不出热水、下水道堵了。",
        "params": {}, "required": [],
    },
    {
        "name": "favorite_house", "kind": "write", "action": "favorite_house",
        "desc": "收藏一套房。",
        "params": {}, "required": [],
    },
    {
        "name": "unfavorite_house", "kind": "write",
        "action": "unfavorite_house",
        "desc": "取消收藏。",
        "params": {}, "required": [],
    },
    {
        "name": "end_contract", "kind": "write", "action": "end_contract",
        "desc": "退租、结束租约。",
        "params": {}, "required": [],
    },
    {
        "name": "renew_contract", "kind": "write", "action": "renew_contract",
        "desc": "续租、把租约延长。",
        "params": {}, "required": [],
    },
    {
        "name": "handle_appointment", "kind": "write",
        "action": "handle_appointment",
        "desc": "（房东或中介用）确认或拒绝一条看房申请。",
        "params": {}, "required": [],
    },
    {
        "name": "toggle_house", "kind": "write", "action": "toggle_house",
        "desc": "（房东用）把房源上架或下架。",
        "params": {}, "required": [],
    },

    # ------------------------------------------------ 兜底
    {
        "name": "knowledge",
        "kind": "read",
        "desc": "平台规则类问题：押一付三是什么意思、押金怎么退、"
                "水电费谁交、能签几年、发票怎么开。**答案在知识库里，"
                "和某一套房无关时用它。**",
        "params": {
            "question": ("string", "原样转述用户的问题"),
        },
        "required": [],
    },
    {
        "name": "none",
        "kind": "read",
        "desc": "以上都不合适，或者你不确定。选它，会退回规则链路处理——"
                "**猜不准的时候选它，比硬挑一个更好。**",
        "params": {},
        "required": [],
    },
]

BY_NAME = {t["name"]: t for t in TOOLS}

# 需要登录才有意义的工具。未登录时从提示词里整段删掉——
# 让模型挑一个必然要求登录的工具，只会换来一句「请先登录」，
# 而用户问的可能根本不是这件事
NEEDS_LOGIN = {
    "my_bills", "my_contracts", "my_appointments", "my_repairs",
    "my_favorites", "landlord_appointments", "landlord_houses",
    "landlord_tenants", "book_viewing", "cancel_viewing",
    "reschedule_viewing", "pay_bill", "submit_repair", "favorite_house",
    "unfavorite_house", "end_contract", "renew_contract",
    "handle_appointment", "toggle_house",
}

# 只有房东/中介用得上的。租客账号看到它们没有坏处，但会增加误选——
# 「我的预约」和「谁约了我的房」这两条对模型来说长得很像
STAFF_ONLY = {"landlord_appointments", "landlord_houses", "landlord_tenants",
              "handle_appointment", "toggle_house"}


def visible_tools(logged_in, role):
    """当前这个人能用的工具。

    过滤不是权限控制——**权限在后端**，令牌带不带得动那个接口，
    不取决于提示词里写了什么。这里过滤只是为了让模型少几个选项，
    选得更准。
    """
    out = []
    for tool in TOOLS:
        if not logged_in and tool["name"] in NEEDS_LOGIN:
            continue
        if tool["name"] in STAFF_ONLY and role not in ("landlord", "agent",
                                                       "admin"):
            continue
        out.append(tool)
    return out


def render_catalog(tools):
    """把工具表渲染成提示词里的一段。

    写成紧凑的一行一个工具，不用 JSON Schema 的完整写法：
    实测小模型看到大段嵌套 JSON 更容易把 schema 本身抄进答案里，
    而这种「名字 — 说明 — 参数」的行式表格它反而读得准。
    """
    lines = []
    for tool in tools:
        parts = ["- %s：%s" % (tool["name"], tool["desc"])]
        if tool["params"]:
            args = ["%s(%s) %s" % (key, kind, note)
                    for key, (kind, note) in tool["params"].items()]
            parts.append("  参数：" + "；".join(args))
        lines.append("\n".join(parts))
    return "\n".join(lines)
