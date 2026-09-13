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
未登录看不到 `pay_bill`，租客看不到 `landlord_houses`。

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

# (问句, 期望工具, 身份)  身份取值：""(未登录) / "tenant" / "landlord"
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
    ("有人约我的房吗", "landlord_appointments", "landlord"),
    ("我名下有几套房", "landlord_houses", "landlord"),
    ("我的租客都有谁", "landlord_tenants", "landlord"),

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

    # ---------------------------------------------------------- 不该交给工具层
    ("你好", None, ""),
    ("谢谢", None, ""),
    ("今天天气怎么样", None, ""),
    ("帮我写首诗", None, ""),
]

# 未登录时这些工具不该出现在候选里——挑中了就是身份过滤没生效
LOGIN_ONLY = ("my_bills", "my_contracts", "my_appointments", "my_repairs",
              "my_favorites", "pay_bill", "book_viewing", "submit_repair",
              "end_contract", "renew_contract", "favorite_house",
              "unfavorite_house", "cancel_viewing", "reschedule_viewing")
