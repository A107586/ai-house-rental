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
            # 「高新园区」「软件园」不是行政区，填进来一套也筛不出，
            # 而且会卡在上下文里——实测之后整轮对话都是 0 套
            "district": ("string", "行政区，只能填大连的区县名：中山区、西岗区、"
                                   "沙河口区、甘井子区、旅顺口区、金州区、普兰店区、"
                                   "瓦房店市、庄河市、长海县。园区、商圈、公司所在地"
                                   "不是行政区，不要填这里"),
            "rent_max": ("number", "租金上限，单位元/月"),
            "rent_min": ("number", "租金下限，单位元/月"),
            "layout": ("string", "户型，如 一室一厅、两室一厅"),
            "area_min": ("number", "面积下限，平方米"),
            "area_max": ("number", "面积上限，平方米"),
            "features": ("string[]", "配套要求，只能取：地铁、电梯、有家具、"
                                     "可养宠物、近学校、近医院、有阳台、采光好"),
            "keyword": ("string", "小区名或街道名，如 桂林街"),
            # 没有这一项时，「差不多价格但大一点的」查回来的是三居室里的合租单间——
            # 整套的面积配一间的租金，看着又大又便宜。模型只能拿户型去凑整租，
            # 实测连查三次 0 套
            "rent_type": ("string", "出租方式，只能取：整租、合租、公寓。用户要一个人住整套、"
                                    "或要找和某套「差不多」的房时，填和那套一样的方式"),
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
                "问「介绍一下」「怎么样」「值不值」时用它。"
                "**问这套房的某个具体条件也用它**——能不能养宠物、有没有"
                "电梯、带不带家具、能不能做饭、周边有什么、朝向如何，"
                "这些都是房源自己的属性，查这一套就知道，"
                "不是平台规则（别选 knowledge），也不是住户评价（别选 house_reviews）。",
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
        # **「报修响应快不快」这句话原样写在描述里，把规则问题引过来了。**
        # 实测「报修多久能上门」被选成了这个工具——用户问的是平台时限
        # （知识库里写着「紧急 24 小时、一般 48 小时」），不是某个房东的表现。
        # 描述里出现的词，模型就会拿它去对；**含糊的描述比缺失的描述更坏**。
        "desc": "**这个房东**本人怎么样：口碑评分、他回消息快不快、"
                "他处理报修积不积极。必须是问某个具体房东才用它——"
                "问平台规定多久上门用 knowledge，问房子好不好用 house_reviews。",
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
        "params": {
            # 「哪几笔逾期了」原先拿到的是和「我有哪些待缴费」一字不差的回答
            "overdue_only": ("boolean", "只看已经逾期的，用户问「哪几笔逾期了」时填 true"),
        },
        "required": [],
    },
    {
        "name": "my_money",
        "kind": "read",
        # 2026-10-22 起退租会退回押金与已付未住的房租，每一笔都有流水。
        # 「押金退了吗」「退了多少」「我一共交过多少钱」原先助手一句也答不上
        "desc": "查我的收支记录：交过哪些钱、退回了多少（押金退款、提前退租退回的房租）。",
        "params": {},
        "required": [],
    },
    {
        "name": "my_contracts",
        "kind": "read",
        # 「我租了哪些房」「在租的有哪些」都是它。原说明只写了「租约」，
        # 模型实测会答「没有查询已租房源的工具」然后挑 none。
        "desc": "查我的租约 / 我租的房：租了哪几套、哪些还在租、租到什么时候、"
                "租金多少、还有几天到期。",
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
    {
        "name": "converse",
        "kind": "read",
        # 没有它的时候，「总结下呗」「我上一条问了什么」只能挑 none，
        # 然后被规则链路硬塞进某个查询——实测「总结下呗」被当成又问了一遍评价。
        # 不收寒暄与跑题闲聊：寒暄有规则秒回，跑题的落到对话兜底也能接住，
        # 犯不着为它们多等一次选择。
        # 抱怨是后补的：「房东一直不回我消息，我很生气，这平台到底管不管」
        # 原先被规则当成「查站内消息」，回了一句「我这边没有看到处理记录」。
        "desc": "不查任何数据，只针对刚才这段对话回话：问上一条问了什么、"
                "要你总结或解释刚才说的内容、嫌刚才的回答草率要讲细一点；"
                "用户在抱怨、生气、投诉（房东不回消息、服务太差）时也用它。"
                "问平台规则的不是它（那是 knowledge），"
                "要查房源、查自己账单预约的也不是它。",
        "params": {},
        "required": [],
    },
    {
        "name": "my_history",
        "kind": "read",
        "desc": "查我最近浏览过的房源（浏览记录、足迹、看过哪些房）。",
        "params": {},
        "required": [],
    },
    {
        "name": "my_todos",
        "kind": "read",
        # 「待办」常被打成「代办」。和「你能帮我代办什么」（问功能）区分：
        # 带「我的」、问「有哪些 / 还有什么没处理」的是它。
        "desc": "汇总我还没处理完的事（我的待办 / 待处理事项 / 还有什么要办的 / "
                "我现在该处理哪些事）。租客：待缴账单、逾期、待确认的看房预约、"
                "待签的租约、进行中的报修、待评价、未读消息；房东：别人约我的看房"
                "等我确认、欠租的租客、名下房源的报修。"
                "问「你能帮我办什么」的不是它，那是 knowledge。",
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
        "params": {
            # 「哪几套还空着」原先拿到的是全部 9 套，和「我有几套房」同一句话
            "status": ("string", "只看某一种：available=在架还空着，"
                                 "rented=已租出，offline=已下架"),
            # 「哪套房评价最差」原先答成了某一套房的评价详情，还说「很好」
            "by_rating": ("string", "按评价排：worst=问哪套评价最差，best=问哪套最好"),
        },
        "required": [],
    },
    {
        "name": "landlord_tenants",
        "kind": "read",
        # 「有没有租客拖欠房租的」原先被挑成了 my_bills——查的是房东
        # **自己**欠别人的账单。说明里得把「欠租的租客」写出来。
        "desc": "（房东用）我的租客有哪些、租到什么时候、哪个租客拖欠房租/有逾期账单。",
        "params": {
            "overdue_only": ("boolean", "只看欠租、有逾期账单的租客，"
                                        "问「有没有租客拖欠」「谁欠租」时填 true"),
        },
        "required": [],
    },
    # 这两条是模拟房东实测漏的：「有租客给我报修吗」「下个月有哪些合同到期」
    # 只能挑 my_repairs / my_contracts——查的是他**自己作为租客**的那份，
    # 回一句「你提交过 0 张报修单」，答非所问。后端早就有 side=landlord。
    {
        "name": "landlord_repairs",
        "kind": "read",
        "desc": "（房东用）租客给我名下房源报的修：有没有人报修、哪些还没处理。"
                "注意和 my_repairs 区分——那个是「我自己报的修」。",
        "params": {},
        "required": [],
    },
    {
        "name": "landlord_contracts",
        "kind": "read",
        "desc": "（房东用）我出租出去的合同：哪些快到期、哪些等租客确认、各租多少钱。"
                "注意和 my_contracts 区分——那个是「我自己租的房」。",
        "params": {
            "expiring_days": ("number", "只看多少天内到期的，问「下个月有哪些到期」"
                                        "填 31，「快到期的」填 60"),
        },
        "required": [],
    },
    # 「西岗区两室一厅70平一般租多少」原先被当成找房，筛出 0 套，
    # 回「把面积要求降低一些」——房东问的是定价，后端早有 /api/pricing/suggest。
    {
        "name": "pricing_suggest",
        "kind": "read",
        "desc": "（房东用）定价参考：某个区、某种户型、多大面积一般租多少，"
                "我要发布/调价的房子定多少合适。",
        "params": {
            "district": ("string", "行政区，如 西岗区"),
            "layout": ("string", "户型，如 两室一厅"),
            "area": ("number", "面积（平米）"),
        },
        "required": [],
    },
    {
        "name": "landlord_income",
        "kind": "read",
        # 没有它的时候，「这个月我收了多少租金」被规则层当成「问这套房的租金」，
        # 答了一段房源介绍。
        "desc": "（房东用）经营数据：这个月收了多少租金、还有多少待收、出租率、"
                "预期收入。",
        "params": {},
        "required": [],
    },

    # 管理员原先一个工具都没有：「平台现在有多少房源待审核」被当成找房，
    # 「今天有多少转人工」更糟——当场给他自己建了一张转人工工单。
    {
        "name": "admin_todos",
        "kind": "read",
        "desc": "（管理员用）平台待办：多少房源待审核/待核验、多少转人工没回复"
                "（含今天新增）、举报、内容复核、角色与中介资质申请。",
        "params": {},
        "required": [],
    },

    # ------------------------------------------------ 中介侧（需登录）
    #
    # 这三条是长对话测试里发现漏的：系统有三种角色，
    # 中介账号问「我这个月佣金多少」「我带看过几次」「我代理了哪些房」，
    # 助手一条都答不上来——**而后端这三个接口一直都在**。
    # 中介只好退回去翻工作台页面，等于对他这个角色，助手不存在。
    {
        "name": "landlord_commissions",
        "kind": "read",
        # 房东付给中介的佣金。和 agent_commissions 方向相反——那个是中介自己的收入
        "desc": "（房东用）我应付给中介的佣金：还有多少没付、已经付清多少。",
        "params": {},
        "required": [],
    },
    {
        "name": "agent_commissions",
        "kind": "read",
        "desc": "（中介用）我的佣金：待结多少、已结多少、共几笔。",
        "params": {},
        "required": [],
    },
    {
        "name": "agent_delegations",
        "kind": "read",
        "desc": "（中介用）我受托代理的房源有哪些、还有几个在生效。"
                "注意和 landlord_houses 区分——那个是「我自己名下的房」。",
        "params": {},
        "required": [],
    },
    {
        "name": "agent_viewings",
        "kind": "read",
        "desc": "（中介用）我的带看记录：带看过几次、都是哪几套。"
                "注意和 landlord_appointments 区分——那个是「谁约了我的房」。",
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
    # 没有它的时候，「我想把空着的那套租金降两百」模型只能答「没有修改租金的工具」
    # 挑 none，退回规则链路后被「租金」二字带去查了**房东自己的账单**。
    # 编号、改哪项、改成多少由表单自己从原话里认，这里不收参数。
    # 「帮我提醒一下欠租的那个人」原先只能答一句 FAQ——平台根本没有这个动作
    {
        "name": "remind_tenant", "kind": "write", "action": "remind_tenant",
        "desc": "（房东用）给欠租、有逾期账单的租客发一条催缴提醒。",
        "params": {}, "required": [],
    },
    {
        "name": "edit_house", "kind": "write", "action": "edit_house",
        # 「空着的那套租金降两百，怎么操作」实测被挑成了 landlord_houses，
        # 列了一遍空房却没说怎么改。问怎么改也归它：写操作护栏见到问句会转去知识库答流程
        "desc": "（房东用）修改自己房源的租金、押金、标题、地址或描述，包括降价、涨价；"
                "问「怎么改价」「降价怎么操作」也选它。",
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
    "my_favorites", "my_history", "my_todos", "my_money", "landlord_commissions",
    "landlord_appointments", "landlord_houses",
    "landlord_tenants", "landlord_income", "landlord_repairs",
    "landlord_contracts", "pricing_suggest", "admin_todos", "agent_commissions",
    "agent_delegations", "agent_viewings", "book_viewing", "cancel_viewing",
    "reschedule_viewing", "pay_bill", "submit_repair", "favorite_house",
    "unfavorite_house", "end_contract", "renew_contract",
    "handle_appointment", "toggle_house", "edit_house", "remind_tenant",
}

# 只有房东/中介用得上的。租客账号看到它们没有坏处，但会增加误选——
# 「我的预约」和「谁约了我的房」这两条对模型来说长得很像
STAFF_ONLY = {"landlord_appointments", "landlord_houses", "landlord_tenants",
              "landlord_income", "landlord_repairs", "landlord_contracts",
              "landlord_commissions", "pricing_suggest",
              "handle_appointment", "toggle_house", "edit_house",
              "remind_tenant"}

# 只有中介用得上的。房东看到「我的佣金」只会更容易误选——
# 佣金是房东**付出去**的钱，不是他能查的收入
AGENT_ONLY = {"agent_commissions", "agent_delegations", "agent_viewings"}

# 只有管理员用得上的
ADMIN_ONLY = {"admin_todos"}

# 只有**以租客身份**才用得上的：自己的租约、账单、预约、报修、收藏，
# 以及预约看房、缴费、续租退租这些租客办的事。
# 原先房东账号拿到的是租客和房东两套工具混在一起——「确认预约」和「约看房」、
# 「我的租约」和「我租出去的合同」并排摆着，模型常常挑错；
# 中介、管理员也拿得到租客的工具（2026-10-22 起身份分开，见 IDENTITIES）
TENANT_ONLY = {
    "recommend_houses", "my_bills", "my_money", "my_contracts", "my_appointments",
    "my_repairs", "my_favorites", "my_history", "my_todos",
    "book_viewing", "cancel_viewing", "reschedule_viewing", "pay_bill",
    "submit_repair", "favorite_house", "unfavorite_house", "end_contract",
    "renew_contract",
}

# 每种账号能切到哪些身份，第一个是默认。
#
# 房东账号是单账号双身份：管自己的房用「房东」，租别人的房用「租客」，
# 在助手顶部切换。中介只做经纪、管理员只做平台管理，都只有本职身份——
# 和业务后端 tenant_side_block 是同一条规则（2026-10-22 定）。
IDENTITIES = {
    "tenant": ("tenant",),
    "landlord": ("landlord", "tenant"),
    "agent": ("agent",),
    "admin": ("admin",),
}

IDENTITY_LABEL = {"tenant": "租客", "landlord": "房东", "agent": "中介",
                  "admin": "管理员"}


def resolve_identity(role, asked=""):
    """这一轮按什么身份服务。要的身份这个账号没有，就用账号的默认身份——
    前端传什么都不作数，账号角色才作数（角色由后端 /api/auth/me 给）。"""
    allowed = IDENTITIES.get(role) or ("tenant",)
    return asked if asked in allowed else allowed[0]


def visible_tools(logged_in, identity):
    """当前这个身份能用的工具。`identity` 见 `resolve_identity`；没登录传空串。

    过滤不是权限控制——**权限在后端**，令牌带不带得动那个接口，
    不取决于提示词里写了什么。这里过滤只是为了让模型少几个选项，
    选得更准，也让「我现在是哪个身份在办事」没有歧义。
    """
    out = []
    for tool in TOOLS:
        name = tool["name"]
        if not logged_in and name in NEEDS_LOGIN:
            continue
        if name in TENANT_ONLY and identity not in ("tenant", ""):
            continue
        if name in STAFF_ONLY and identity not in ("landlord", "agent"):
            continue
        if name in AGENT_ONLY and identity != "agent":
            continue
        if name in ADMIN_ONLY and identity != "admin":
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
