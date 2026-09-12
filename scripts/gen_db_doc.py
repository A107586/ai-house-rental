"""从 models.py 生成《数据库设计说明书》的表结构章节。

**手抄一份表结构必然会和代码脱节** —— 改了字段忘了改文档，是这类文档
最常见的死法。这里直接从 SQLAlchemy 的元数据读，生成的内容天然与代码
一致，改完模型重跑一次即可。
"""
import io
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, 'server'))

from app.console import force_utf8  # noqa: E402

force_utf8()
os.environ.setdefault("DATABASE_URL", "sqlite://")

from app.models import Base  # noqa: E402

# 表的中文名与用途。代码里没有这层信息，只能在这里维护。
TABLE_INFO = {
    "users": ("用户", "三种角色共用一张表，靠 role 区分租客、房东、管理员"),
    "houses": ("房源", "系统的核心实体，状态机为 pending→available→rented→offline"),
    "house_images": ("房源相册", "is_cover 标记封面，列表页只取封面，详情页取全部"),
    "appointments": ("看房预约", "时段冲突由接口层按配置的时间窗口判断"),
    "contracts": ("租赁合同", "状态机为 pending→active→ended，另有 cancelled 分支"),
    "bills": ("账单", "合同生效时一次排满整期：1 条押金 + N 条租金"),
    "payments": ("缴费流水", "一条账单对应一条流水，trade_no 唯一防重复入账"),
    "favorites": ("收藏", "租客与房源的多对多关系"),
    "browsing_history": ("浏览足迹", "AI-07 房源推荐的输入"),
    "reviews": ("租后评价", "一份合同只能评一次，评价人姓名对外只露姓氏"),
    "repairs": ("报修工单", "category/urgency/assignee 三项由 AI-04 分析填充"),
    "repair_images": ("报修照片", "单开一表而非给 repairs 加列，见下方说明"),
    "repair_workers": ("维修人员", "F-09 维修数据的人员档案"),
    "repair_items": ("维修物品", "F-09 的耗材档案，含库存"),
    "repair_records": ("维修记录", "工单结案时登记的人、物、工时、费用"),
    "repair_item_usages": ("物品领用明细", "单价是领用当时的快照，不跟随物品调价"),
    "messages": ("站内消息", "预约结果、账单到期、工单进度的通知"),
    "notices": ("平台公告", "同时是 AI-03 智能客服的知识语料"),
    "chat_messages": ("对话记录", "AI-09 多轮记忆的持久化，只存原文不存槽位"),
    "operation_logs": ("操作日志", "M-07，只记改变状态的操作，不记请求体"),
    "content_flags": ("内容复核队列", "M-08，只记机器拿不准的，确定违规的根本不入库"),
    "system_configs": ("系统配置", "M-09，值统一用字符串存 + 类型标记"),
    # ↓ K 系列与退款这六张表此前一直没登记，生成出来是
    #   「password_resets — password_resets」这种没有信息量的标题。
    #   9/24 数据库评审要读这份文档，补上。
    "refunds": (
        "押金退款流水",
        "退租结算的凭据。课程项目内记录模拟退款，不调真实支付渠道——"
        "与 D-1、J-06 同一条边界"),
    "conversations": (
        "咨询会话",
        "租客与房源负责人的一对一会话。**委托期间「对方」是受托中介**"
        "（J-03），但会话本身仍是两方，不因换人而断"),
    "chat_lines": ("会话消息", "conversations 里的一条消息"),
    "saved_searches": (
        "保存的搜索",
        "K-09。**匹配逻辑必须与列表页共用同一套条件判断**"
        "（`house_query.apply_filters`），另写一份判断迟早会两边不一致"),
    "house_reports": (
        "房源举报",
        "K-08。**刻意不复用 content_flags**：那张表是机器复核队列，"
        "这张是用户举报，发起人、处理流程、回执要求都不同。"
        "判定与处置分开——判「属实」不自动下架"),
    "password_resets": (
        "找回密码令牌",
        "K-11。**只存哈希不存明文令牌**，库被读走也无法直接冒用；"
        "重置成功即吊销该用户全部旧令牌与其余未用链接"),

    # ↓ 这三张表的说明原本是有人**直接改在生成结果里**的，
    #   于是任何人重跑一次脚本都会把它们静默抹掉（本次就抹了一回，
    #   靠 git diff 才发现）。补回到这里，生成器重新成为唯一的源。
    "faqs": (
        "客服知识条目",
        "AI-03 智能客服的**权威语料源**。与 notices 的分工：公告是面向"
        "全体用户的通告（有时效、会过期），FAQ 是平台规则的常驻问答，"
        "两者生命周期不同，混一张表会让「公告过期下架」和「FAQ 长期有效」"
        "互相打架。语料的编写源是 `agent/knowledge/faq.md`，由 seed.py "
        "导入本表；导入之后**数据库才是运行时的权威源**"),
    "support_logs": (
        "客服问答日志",
        "存它不是为了排错，是为了回答「这个客服到底行不行」：拒答率多少、"
        "哪些问题反复问却答不上来、命中的都是哪几条 FAQ。"
        "**没有这张表，语料该往哪儿补全靠拍脑袋。**"
        "高频机器写入，已在 `oplog.SKIP_PATHS` 里排除，不进操作日志"),
    "support_handoffs": (
        "转人工工单",
        "客服闭环里**最不该省**的一环：没有它，拒答就是死路一条，"
        "用户被告知「我不知道」之后无处可去。"
        "建单后给每位管理员发一条站内消息；管理员回复后，回复同样通过 "
        "messages 送达用户——复用现成的消息通道，不另造一套通知机制"),

    "agent_profiles": (
        "中介档案",
        "J-01。与 users 一对一。**表里没有任何统计列**——带看数、成交数、"
        "好评率全部由明细实时聚合（原则 1.4 / 需求 R-05）；"
        "存下来就意味着有人能改，也意味着迟早会和明细对不上"),
    "house_delegations": (
        "房源委托",
        "J-04，中介这条链的核心表，也是权限判断的唯一依据——"
        "`can_manage_house()` 每次鉴权都要问它。"
        "状态机 pending→active→ended，另有 rejected 分支；"
        "**双向确认**，单方发起只到 pending，须另一方点头才 active"),
    "viewing_records": (
        "带看记录",
        "J-05。不是考勤表，价值在 concerns——同一套房被三个人嫌厨房小，"
        "房东就该降价或改描述了。**委托解除后不删**，问责需要"),
    "agent_commissions": (
        "佣金",
        "J-06。**刻意不复用 bills / payments**：那两张表描述的是"
        "「租客付给房东」，佣金是「房东付给中介」，付款方、收款方、"
        "触发条件都不同，塞进去只会让两边字段互相为空。"
        "结算不接真实资金，与 D-1 同一条边界：状态机做全，钱线下结"),
}

# 字段级说明。只写那些「光看类型看不出来」的。
COLUMN_NOTES = {
    ("users", "password_hash"): "BCrypt 哈希；环境无 bcrypt 时退回 PBKDF2-HMAC-SHA256",
    ("users", "role"):
        "tenant 租客 / landlord 房东 / agent 经纪人 / admin 管理员。"
        "**agent 本身不给任何权限**，接单须资质过审（J-01）",
    ("users", "status"): "active 正常 / disabled 已禁用",
    ("houses", "rent_price"): "统一 decimal(10,2)，不用 int 或 varchar",
    ("houses", "status"): "pending 待审核 / available 可租 / rented 已租 / offline 已下架",
    ("houses", "tags"): "逗号分隔的标签串，读取时拆成数组",
    ("appointments", "viewing_time"): "**必须 datetime**，date 表达不了「下午两点」",
    ("contracts", "review_result"): "AI-02 合同审阅的结论快照",
    ("contracts", "deposit_refund"): "退租时实退的押金，扣款不得超过押金且不得为负",
    ("bills", "type"): "deposit 押金 / rent 租金",
    ("bills", "status"): "unpaid 未缴 / paid 已缴 / cancelled 已作废",
    ("payments", "trade_no"): "唯一，防止同一笔缴费被重复入账",
    ("repairs", "urgency"): "紧急 / 一般 / 可延后，由 AI-04 判定",
    ("repair_records", "repair_id"): "**唯一约束**：一张工单只能结案一次",
    ("repair_item_usages", "unit_price"): "领用当时的单价快照，物品调价不影响历史",
    ("chat_messages", "session_id"): "会话编号，查询时必须与 user_id 一起过滤",
    ("operation_logs", "username"): "冗余存储，用户改名或被删后仍查得到当时是谁",
    ("content_flags", "excerpt"): "内容摘要，冗余存储，原内容可能已被作者改掉",
    ("system_configs", "value"): "统一字符串，类型由 value_type 描述",
    ("system_configs", "is_public"): "为真才会出现在公开配置接口里",

    ("faqs", "code"):
        "检索侧标识，如 `faq-deposit-refund`。回答的引用用它，"
        "可读性直接影响排查效率",
    ("faqs", "question"):
        "也是检索的一路独立索引：客服场景「问句 vs 问句」信号最强",
    ("faqs", "answer"):
        "**首句必须是一句话结论**，大模型不可用时抽取式回答只取首句",
    ("faqs", "category"): "押金／费用／看房／报修／退租／合同／缴费／账号／房东",
    ("faqs", "aliases"):
        "同义问法，`/` 分隔。**每条别名单独建索引并取最大值**，"
        "不能拼成一篇长文档——BM25 会按长度惩罚，别名写得越多排得越靠后",
    ("faqs", "is_published"): "下架后智能体下一次刷新即不再检索",
    ("faqs", "hit_count"):
        "被智能体引用的次数，管理端据此看哪些条目写了没人用",

    ("support_logs", "user_id"):
        "允许为空：未登录用户的提问同样是「哪些问题答不上来」的证据",
    ("support_logs", "status"):
        "answered／refused／escalate／account／need_login",
    ("support_logs", "mode"): "lexical／vector+lexical／account",
    ("support_logs", "cited"): "引用的片 id，逗号分隔；拒答时为空",
    ("support_logs", "top_id"):
        "检索首位，用于分析「排对了但被闸门拦下」的情况",
    ("support_logs", "cover"): "可答性闸门的覆盖率判据",
    ("support_logs", "answer_source"): "llm／extractive／rules",
    ("support_logs", "gate_reason"): "拒答原因，直接显示在管理端",
    ("support_logs", "feedback"):
        "1 有用／−1 没用／0 未评价。**−1 比 1 值钱**，它指出哪条写得不对",

    ("support_handoffs", "handoff_no"):
        "`HD` + 时间戳，与合同、账单编号同一套生成规则",
    ("support_handoffs", "user_id"):
        "必填：管理员回复时得知道回给谁，所以转人工要求登录",
    ("support_handoffs", "reason"):
        "智能体为什么没答上来，供管理员判断是语料缺失还是问题超范围",
    ("support_handoffs", "context"): "最近几轮对话",
    ("support_handoffs", "status"): "pending／replied／closed",

    ("password_resets", "token_hash"):
        "**只存哈希**。存明文等于把一份可直接改密码的钥匙留在库里",
    ("house_reports", "status"):
        "pending 待处理 / accepted 属实 / rejected 不属实。"
        "**判定与处置分开**——判「属实」不自动下架，下架是另一个动作",
    ("saved_searches", "conditions"):
        "筛选条件的 JSON 快照。匹配时交给 `house_query` 执行，"
        "不在这里另写一份判断",


    ("contracts", "agent_id"):
        "促成这单的中介（J-06）。**甲方仍是房东**，中介不是合同一方，"
        "此列只用于算佣金与「累计成交」，不进合同正文；房东自管的合同为空",

    ("agent_profiles", "license_status"):
        "unsubmitted / pending / approved / rejected —— 沿用 K-07 的核验"
        "四态，不另发明一套",
    ("agent_profiles", "reject_reason"):
        "**驳回必填**。只回「未通过」三个字，被驳回的人不知道该补什么，"
        "只会反复提交同一份材料",
    ("agent_profiles", "reviewed_by"): "审核人。审计需要，不能只留结论",
    ("agent_profiles", "service_districts"): "主营区域，逗号分隔；空表示不限",
    ("agent_profiles", "accepting"):
        "是否接受新委托。**关掉只挡新邀请，已在托房源不受影响**——"
        "休假不等于甩手",

    ("house_delegations", "landlord_id"):
        "冗余存储（原则 1.6）：房源日后转手也不影响这条委托当时是谁签的",
    ("house_delegations", "status"): "pending / active / rejected / ended",
    ("house_delegations", "initiated_by"):
        "landlord / agent。决定该由哪一方来确认",
    ("house_delegations", "commission_rate"): "佣金比例，按月租金的百分比",
    ("house_delegations", "end_reason"): "**解除必填**，任一方解除都要说明",

    ("viewing_records", "appointment_id"):
        "允许为空：临时到访的带看没有对应预约",
    ("viewing_records", "showed_up"):
        "租客是否到场。**放鸽子率单列统计**——那个数高通常说明预约确认"
        "流程有问题，不是中介的服务问题",
    ("viewing_records", "concerns"): "结构化异议，逗号分隔",
    ("viewing_records", "intention"): "high 意向强 / medium 一般 / low 意向弱",

    ("agent_commissions", "delegation_id"):
        "允许为空：委托记录若被清理，佣金本身仍应留存",
    ("agent_commissions", "base_amount"): "计佣基数（月租金）",
    ("agent_commissions", "amount"):
        "**基数与比例分开存**：只存最终金额，事后想核对「这笔是怎么算"
        "出来的」就查不了了",
    ("agent_commissions", "status"): "pending 待结算 / settled 已结算 / cancelled 已取消",
}


def type_name(col):
    t = str(col.type)
    return {
        "INTEGER": "int",
        "TEXT": "text",
        "DATETIME": "datetime",
        "BOOLEAN": "bool",
        "FLOAT": "float",
    }.get(t, t.lower())


lines = []
add = lines.append

add("## 三、表结构")
add("")
add("> 本章由 `scripts/gen_db_doc.py` 从 `server/app/models.py` 的模型定义")
add("> 直接生成。**手抄一份表结构必然会和代码脱节**——改了字段忘了改文档，")
add("> 是这类文档最常见的死法。改完模型重跑一次脚本即可。")
add("")

order = [t.name for t in Base.metadata.sorted_tables]
# 按业务重要性重排，让读者先看到主干
priority = ["users", "houses", "house_images", "appointments", "contracts",
            "bills", "payments", "favorites", "browsing_history", "reviews",
            "repairs", "repair_images", "repair_workers", "repair_items",
            "repair_records", "repair_item_usages", "messages", "notices",
            "chat_messages", "operation_logs", "content_flags",
            "system_configs",
            # AI-03 客服
            "faqs", "support_logs", "support_handoffs",
            # J 系列中介。四张表必须挨着出现——它们是一条链，
            # 散落在末尾会让人以为彼此无关
            "agent_profiles", "house_delegations", "viewing_records",
            "agent_commissions",
            # K 系列与退款
            "refunds", "conversations", "chat_lines", "saved_searches",
            "house_reports", "password_resets"]
ordered = [n for n in priority if n in order] + [n for n in order if n not in priority]

tables = {t.name: t for t in Base.metadata.sorted_tables}

for i, name in enumerate(ordered, 1):
    t = tables[name]
    cn, purpose = TABLE_INFO.get(name, (name, ""))
    add("### 3.%d `%s` — %s" % (i, name, cn))
    add("")
    if purpose:
        add("> %s" % purpose)
        add("")
    add("| 字段 | 类型 | 空 | 键 | 说明 |")
    add("|---|---|:---:|:---:|---|")
    for c in t.columns:
        keys = []
        if c.primary_key:
            keys.append("PK")
        if c.foreign_keys:
            keys.append("FK→" + str(list(c.foreign_keys)[0].target_fullname))
        if c.unique:
            keys.append("UQ")
        if c.index:
            keys.append("IDX")
        note = COLUMN_NOTES.get((name, c.name), "")
        add("| `%s` | %s | %s | %s | %s |" % (
            c.name, type_name(c), "否" if not c.nullable else "是",
            " ".join(keys) or "—", note))
    add("")

out = "\n".join(lines)
# 只重写《数据库设计说明书》的第三章，前后各章是手写的分析内容，不动
doc_path = os.path.join(_ROOT, 'docs', '数据库设计说明书.md')
whole = io.open(doc_path, encoding='utf-8').read()
start = whole.index('## 三、表结构')
end = whole.index('## 四、索引设计')
io.open(doc_path, 'w', encoding='utf-8', newline='').write(
    whole[:start] + out.rstrip() + "\n\n---\n\n" + whole[end:])
print("生成 %d 张表，%d 行" % (len(ordered), len(lines)))
