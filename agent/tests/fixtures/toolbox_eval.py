# -*- coding: utf-8 -*-
"""工具选择的评测集。

## 为什么要有这一份

项目的主线改动是「把选路从关键词表换成模型选工具，规则兜底」。
AI-01 解析、AI-02 条款、AI-03 客服、AI-04 报修分类、AI-06 时间
每一项都有验收数字，**唯独这条主线没有**——它有很完整的管道测试
（`test_toolbox.py` 覆盖回退、参数白名单、序数消解、字段映射），
但没有一个数字回答「模型到底挑得准不准」。

主线没有指标，答辩时说「换成模型选路之后更准了」就只能靠嘴说。

## 标注口径

每条是 `(用户这句话, 期望的工具名, 身份)`。身份影响可见工具集：
未登录看不到 `pay_bill`，租客看不到 `landlord_houses`，
房东看不到 `agent_commissions`——佣金是他付出去的钱，不是他能查的收入。

期望值可以是**一个元组**，表示这几个都算对——真有歧义的句子硬指定
一个答案，只会让这条永远红着，并且误导人去改提示词，
把一个本来合理的选择改坏。

`None` 表示**这一句不该交给工具层**——它要么是知识型提问（走客服检索）、
要么根本不是在办事。挑中任何工具都算错，因为工具层一旦认领，
规则链路就不再介入了。

## 这份集合是新写的，没有调参/验收之分

前面几项指标都严格分了调参集与验收集。这一份**第一次量，还没有
拿它调过任何东西**——所以现在它同时是「基线」。真要拿它调参数
（改提示词、改工具描述），得先按同样的规矩拆成两半，
否则第二次报出来的数字就不能信了。
"""

# (问句, 期望工具, 身份)  身份取值：""(未登录) / "tenant" / "landlord" / "agent"
CASES = [
    # ---------------------------------------------------------- 找房与房源
    ("帮我找中山区两室一厅，三千以内", "search_houses", ""),
    ("沙河口区有没有带电梯的一室", "search_houses", ""),
    ("金州区 2000 到 3000 的房子", "search_houses", ""),
    ("再多看几套", "more_houses", ""),
    ("还有别的房源吗", "more_houses", ""),
    # **这一条允许两个答案。** 屏幕上已经列着三套时，「你推荐哪套」
    # 既可以理解成「在这几套里挑」（compare_houses 的描述正是
    # 「问哪个好」），也可以理解成「你帮我推荐」。实测模型选了
    # compare_houses，我原先标的是 recommend_houses——
    # **回头看是我标错了，不是模型错了**。
    #
    # 评测集遇到真有歧义的句子，写成「这几个都算对」比硬指定一个
    # 更诚实；硬指定的话，这条永远红着，而且会误导人去改提示词，
    # 把一个本来合理的选择改坏。
    ("你推荐哪套", ("recommend_houses", "compare_houses"), ""),
    ("帮我挑一个", "recommend_houses", ""),
    ("第二套详细说说", "house_detail", ""),
    ("DL0007 这套什么情况", "house_detail", ""),
    ("这套房的评价怎么样", "house_reviews", ""),
    ("住过的人怎么说", "house_reviews", ""),
    ("这个房东靠谱吗", "landlord_reputation", ""),
    ("第一套和第三套比一下", "compare_houses", ""),

    # ---------------------------------------------------------- 查自己的数据
    ("我还欠多少房租", "my_bills", "tenant"),
    ("这个月的账单交了吗", "my_bills", "tenant"),
    ("我的合同什么时候到期", "my_contracts", "tenant"),
    ("我约了哪几场看房", "my_appointments", "tenant"),
    ("我报的修处理得怎么样了", "my_repairs", "tenant"),
    ("我收藏了哪些房子", "my_favorites", "tenant"),
    # 下面几条来自一次真实会话里全部掉进兜底的问法
    ("我租了哪些房", "my_contracts", "tenant"),
    ("在租的有哪些", "my_contracts", "tenant"),
    ("我的历史浏览记录", "my_history", "tenant"),
    ("我最近看过哪些房子", "my_history", "tenant"),
    ("我的待处理事项有哪些", "my_todos", "tenant"),
    # 「代办」是「待办」的常见错字，但「你能帮我代办什么」问的是功能
    ("我的代办有哪些", "my_todos", "tenant"),
    ("你能帮我代办哪些事", "knowledge", "tenant"),
    ("有人约我的房吗", "landlord_appointments", "landlord"),
    ("我名下有几套房", "landlord_houses", "landlord"),
    ("我的租客都有谁", "landlord_tenants", "landlord"),

    # 中介侧。这三条是长对话测试补上工具之后加的——**加工具的时候
    # 忘了加评测**，`test_suite_hygiene` 里那条覆盖率用例就是为此写的。
    # 三句都刻意贴近中介真会说的话，而不是照着工具名念一遍。
    ("我这个月的佣金有多少", "agent_commissions", "agent"),
    ("我手上代理了哪些房源", "agent_delegations", "agent"),
    ("我带看过几次了", "agent_viewings", "agent"),

    # ---------------------------------------------------------- 办事（写操作）
    ("这周六下午两点去看这套", "book_viewing", "tenant"),
    ("帮我约明天上午看房", "book_viewing", "tenant"),
    ("把明天那场看房取消", "cancel_viewing", "tenant"),
    ("看房改到周日行吗", "reschedule_viewing", "tenant"),
    ("帮我把这个月房租交了", "pay_bill", "tenant"),
    ("厨房水管漏水，帮我报修", "submit_repair", "tenant"),
    ("把这套收藏起来", "favorite_house", "tenant"),
    ("取消收藏", "unfavorite_house", "tenant"),
    ("我想退租", "end_contract", "tenant"),
    ("帮我续租一年", "renew_contract", "tenant"),
    ("同意那个看房申请", "handle_appointment", "landlord"),
    ("把 DL0007 下架", "toggle_house", "landlord"),

    # ---------------------------------------------------------- 知识型
    ("押金一般收几个月", "knowledge", ""),
    ("水电费怎么算", "knowledge", ""),
    ("提前退租要赔钱吗", "knowledge", ""),
    ("报修多久能上门", "knowledge", ""),

    # ---------------------------------------------------------- 针对对话本身
    ("我上一条问了什么", "converse", "tenant"),
    ("你这太草率了，总结下呗", "converse", "tenant"),
    ("房东一直不回我消息，我很生气，这平台到底管不管", "converse", "tenant"),

    # 房东的经营与欠租——原先分别被答成房源介绍、房东自己的账单
    ("这个月我收了多少租金", "landlord_income", "landlord"),
    ("有没有租客拖欠房租的", "landlord_tenants", "landlord"),
    # 房东问的是名下房源的报修与合同，原先查成了他自己作为租客的那份
    ("有租客给我报修吗", "landlord_repairs", "landlord"),
    ("下个月有哪些合同到期", "landlord_contracts", "landlord"),
    ("帮我把DL0023的租金改成2300", "edit_house", "landlord"),
    ("我哪套房评价最差", "landlord_houses", "landlord"),
    ("帮我提醒一下欠租的那个人", "remind_tenant", "landlord"),
    ("西岗区两室一厅70平一般租多少", "pricing_suggest", "landlord"),
    ("平台现在有多少房源待审核", "admin_todos", "admin"),

    # ---------------------------------------------------------- 不该交给工具层
    ("你好", None, ""),
    ("谢谢", None, ""),
    ("今天天气怎么样", None, ""),
    ("帮我写首诗", None, ""),
]

# 未登录时这些工具不该出现在候选里——挑中了就是身份过滤没生效
LOGIN_ONLY = ("my_bills", "my_contracts", "my_appointments", "my_repairs",
              "my_favorites", "my_history", "my_todos",
              "pay_bill", "book_viewing", "submit_repair",
              "end_contract", "renew_contract", "favorite_house",
              "unfavorite_house", "cancel_viewing", "reschedule_viewing")
