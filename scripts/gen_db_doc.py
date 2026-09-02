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
}

# 字段级说明。只写那些「光看类型看不出来」的。
COLUMN_NOTES = {
    ("users", "password_hash"): "BCrypt 哈希；环境无 bcrypt 时退回 PBKDF2-HMAC-SHA256",
    ("users", "role"): "tenant 租客 / landlord 房东 / admin 管理员",
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
            "system_configs"]
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
