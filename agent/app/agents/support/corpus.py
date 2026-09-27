"""客服知识语料的装载与切片。

三类来源，切片粒度各不相同：

- **静态 FAQ**（`agent/knowledge/faq.md`）：一问一答天然就是一片，不用再切。
- **平台公告**（业务后端 `notices` 表）：标题 + 正文，公告本身就短，整条一片。
- **房源详情**：只在用户问题里带了房源编号时**定向**取，不进常驻索引——
  几百套房源灌进索引只会稀释 IDF，让「押金」这种词到处都是。

每一片的结构统一为 `dict`：

    {"id", "title", "body", "category", "aliases", "source", "link"}

`title` 与 `body` 分开保存不是为了显示，而是因为它们要**分别建索引**：
客服场景里「用户问句 vs FAQ 问句」的匹配信号比「问句 vs 答案正文」强得多。
"""

import os
import re

# ---------------------------------------------------------------- 同义词

# 同义词表。**这是本功能里性价比最高的一块投入。**
#
# 客服问答的召回失败，绝大多数不是「语义理解不到位」，而是用词对不上：
# 用户说「保证金」，语料里写的是「押金」；用户说「搬走」，语料里写的是「退租」。
# 与其指望嵌入模型泛化（还得联网、还得标定），不如把领域内几十组说法直接列出来——
# 可控、可测、可解释，出了问题能精确定位到是哪一组没写。
#
# 用法是**查询扩展**：命中任一说法，就把整组词并进扩展查询里，作为独立一路检索。
SYNONYM_GROUPS = [
    ["押金", "保证金", "押一付三", "押金退款", "押金退还"],
    ["退租", "解约", "搬走", "退房", "不租了", "提前走", "中途退租"],
    ["续租", "续签", "接着租", "延长租期"],
    ["水电费", "水费", "电费", "水电", "公摊费用"],
    ["物业费", "物业管理费", "物业"],
    ["取暖费", "暖气费", "供暖费"],
    ["宽带", "网费", "上网", "有线电视"],
    ["报修", "维修", "修理", "坏了", "故障", "修一下", "不好使"],
    ["紧急", "急", "危险", "安全隐患"],
    ["漏水", "渗水", "滴水", "跑水"],
    ["漏电", "触电", "跳闸", "断电", "停电"],
    ["燃气", "煤气", "天然气", "燃气泄漏"],
    ["看房", "带看", "去看房子", "约看", "看一下房子"],
    ["预约", "约时间", "预定", "订时间"],
    ["取消", "改约", "改期", "改时间", "退掉"],
    ["合同", "租约", "协议", "契约"],
    ["签约", "签合同", "签字", "确认合同"],
    ["违约金", "赔偿金", "罚金", "违约责任"],
    ["房租", "租金", "月租", "房钱"],
    ["账单", "费用单", "缴费单", "催缴"],
    ["缴费", "交钱", "付款", "支付", "交租", "付房租"],
    ["逾期", "晚交", "拖欠", "没按时交"],
    ["房东", "户主", "业主", "出租人"],
    ["租客", "承租人", "住户", "房客"],
    ["房源", "房子", "房屋", "出租房"],
    ["发布", "上架", "挂出去", "出租房源"],
    ["审核", "审批", "过审", "审查"],
    ["注册", "开户", "注册账号"],
    ["密码", "登录密码", "口令"],
    ["身份证", "实名", "证件", "个人信息"],
    ["隐私", "信息安全", "泄露"],
    ["消息", "通知", "站内信", "提醒"],
    ["投诉", "举报", "反映问题", "申诉"],
    ["虚假", "假房源", "骗子", "诈骗", "不实"],
    ["转租", "二房东", "转给别人"],
    ["验房", "交房", "房屋状态确认"],
    ["流水", "凭证", "记录", "收据"],
    ["工单", "维修单", "报修单"],
    ["合租", "室友", "分摊", "均摊"],
    ["整租", "独立租", "单独租"],
    ["定价", "价格建议", "租金定多少"],
    ["收租", "收房租", "收款"],
    ["扣款", "扣钱", "扣除"],

    # ---- 下面这批是按实测补的（C2）----
    #
    # 起因：检索的 Recall@3 是 100%——**正确答案每次都排在第一位**——
    # 而有依据正确率只有 80%。差的那些全卡在「问的那个词资料里没提到」，
    # 而它们全是很自然的换一种说法。
    #
    # 补的方向不是「再想几个近义词」，是**把用户的口语和文档的书面语
    # 对起来**：文档写「维修费用由谁承担」，用户说「修东西的钱谁出」；
    # 文档写「退租流程」，用户说「到期退租有哪些步骤」。
    # 两边说的是同一件事，只是一个在写规章、一个在说话。
    ["费用", "钱", "花费", "多少钱", "价钱", "收费"],
    ["谁出", "谁承担", "谁付", "谁掏", "算谁的", "由谁负责"],
    ["步骤", "流程", "手续", "要做什么", "需要做什么"],
    ["打扫", "清洁", "收拾", "打扫干净", "保洁"],
    ["到期", "期满", "租期结束", "住满"],
    ["东西", "物品", "设施", "家电", "设备", "家具"],
    ["坏", "损坏", "弄坏", "用坏", "自然损耗"],
    ["晚交", "迟交", "晚几天", "推迟", "缓几天"],
]

# ---------------------------------------------------------------- 说法组
#
# **这一类只参与检索扩展，不参与可答性闸门。**
#
# 它们是语气和句式，不是话题：「能不能」「多久」「怎么办」谁都会说，
# 说了也没告诉你他在问什么。
#
# 分开是被咬了一次才分的。第一版把它们和话题词混在一张表里，
# 闸门拿同义组去比对材料时，「房子能不能做短租」靠「能不能」
# 在资料里命中了——**于是一条本该拒答的问题被放行**，
# 拒答率从 100% 掉到 90%，正好卡在需求下限上。
#
# 检索侧用它们是有益的：多一路召回，排序会更稳。
# 闸门侧用它们是有害的：闸门问的是「你说的那个东西我们讲过没有」，
# 而这些词指向的东西是空的。
PHRASING_GROUPS = [
    ["多久", "多长时间", "几天", "什么时候", "何时", "多快"],
    ["能不能", "可不可以", "行不行", "可以吗", "允许吗"],
    ["怎么办", "怎么走", "如何", "怎么弄"],
    ["确认", "回复", "答复", "回话"],
    ["自己", "本人", "我自己", "亲自"],
    ["提前", "提早", "早点"],
    ["交给", "交由", "归", "属于"],
]

# term -> 同组的全部词。构造成扁平表，查询时逐词查表即可。
# 话题词的索引：检索与闸门都用。
_TOPIC_INDEX = {}
for _group in SYNONYM_GROUPS:
    for _word in _group:
        _TOPIC_INDEX.setdefault(_word, set()).update(_group)

# 检索扩展用的全量索引 = 话题词 + 说法词
_SYNONYM_INDEX = {}
for _group in SYNONYM_GROUPS + PHRASING_GROUPS:
    for _word in _group:
        _SYNONYM_INDEX.setdefault(_word, set()).update(_group)


# ---------------------------------------------------------------- 口语话题补全
#
# 有些问法**整句都是虚词和单字**，话题藏在句式里：「最短能租几个月」问的是租期，
# 「客户想签」问的是签约。分词后挑不出任何名词或动词，闸门 G0 判「没有话题词」
# 直接拒答——而答案就排在检索第一（实测 0.87 分）。
#
# G0 本身不动（它挡的是「怎么办」「随便看看」这类真的没内容的话，标定过）。
# 这里只做一件窄的事：**认出几种固定句式，把它指的那个书面词补在句尾**，
# 让检索和闸门看到同一个话题。只收句式，不收单字，免得把空话也补出话题。
_TOPIC_HINTS = (
    (re.compile(r"租(几个月|多久|多长时间|多长)|最(短|长).{0,3}租|租期"), "租期"),
    (re.compile(r"(客户|租客|他|她|对方).{0,4}想签|想签(约|合同)?$"), "签约"),
)


def add_topic_hints(text):
    """句式里藏着话题时，把书面词补到句尾。没有就原样返回。"""
    raw = text or ""
    extra = [word for pattern, word in _TOPIC_HINTS
             if pattern.search(raw) and word not in raw]
    return raw + ("（%s）" % "、".join(extra) if extra else "")


def expand_query(text):
    """把查询里命中的同义组整组并进来，返回扩展后的文本。

    返回的是**追加**而非替换：原问句仍然是主通道，
    扩展只作为独立一路参与融合，避免扩展词把原意冲淡。
    """
    if not text:
        return ""
    extra = set()
    for word, group in _SYNONYM_INDEX.items():
        if word in text:
            extra.update(group)
    if not extra:
        return ""
    return " ".join(sorted(extra))


# ---------------------------------------------------------------- FAQ 解析

# 元数据行：`id: xxx` / `category: xxx` / `alias: a / b / c`
_META = re.compile(r"^(id|category|alias)\s*:\s*(.+)$")

KNOWLEDGE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))),
    "knowledge")


def parse_faq(text):
    """解析 `faq.md`，返回片列表。

    格式约定见 `knowledge/faq.md` 开头。解析器刻意写得严格：
    **缺 id 的段落直接跳过并不报错**——文件开头的说明性内容也是二级标题之外的
    普通文本，宽松跳过比抛异常更合适；但 id 重复会抛，那是真正的错误，
    重复 id 会让引用校验指向错误的材料。
    """
    chunks = []
    seen = set()
    # 按二级标题切段。`(?m)` 让 ^ 匹配每一行开头
    parts = re.split(r"(?m)^##\s+", text)
    for part in parts[1:]:
        lines = part.splitlines()
        title = lines[0].strip()
        meta = {}
        body_start = 1
        for i in range(1, len(lines)):
            line = lines[i].strip()
            if not line:
                # 元数据块与正文之间的空行；元数据已读完
                if meta:
                    body_start = i + 1
                    break
                continue
            matched = _META.match(line)
            if not matched:
                body_start = i
                break
            meta[matched.group(1)] = matched.group(2).strip()

        chunk_id = meta.get("id")
        if not chunk_id:
            continue
        if chunk_id in seen:
            raise ValueError("FAQ 语料存在重复 id：%s" % chunk_id)
        seen.add(chunk_id)

        body = "\n".join(lines[body_start:]).strip()
        # 段落分隔用的 `---` 会被切进上一片的正文尾部，去掉
        body = re.sub(r"\n*-{3,}\s*$", "", body).strip()

        aliases = [a.strip() for a in meta.get("alias", "").split("/")
                   if a.strip()]
        chunks.append({
            "id": chunk_id,
            "title": title,
            "body": body,
            "category": meta.get("category", "其他"),
            "aliases": aliases,
            "source": "faq",
            "link": None,
        })
    return chunks


def load_faq(path=None):
    """从磁盘装载 FAQ。文件不存在时返回空列表而不是抛异常。

    智能体服务少了 FAQ 应当降级为「只有公告可检索」，
    而不是整个服务起不来——与 N-07 的降级原则一致。
    """
    path = path or os.path.join(KNOWLEDGE_DIR, "faq.md")
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as handle:
        return parse_faq(handle.read())


# ---------------------------------------------------------------- 公告

def notice_to_chunk(notice):
    """把后端返回的一条公告转成片。

    公告正文本身就只有一两百字，不再二次切分：切了反而会把
    「7 个工作日内原路退还」这样的完整规定拆到两片里，
    引用时只能给出半句话。
    """
    notice_id = notice.get("id")
    body = (notice.get("content") or "").strip()
    if not body:
        return None
    return {
        "id": "notice-%s" % notice_id,
        "title": (notice.get("title") or "").strip(),
        "body": body,
        "category": notice.get("category") or "公告",
        "aliases": [],
        "source": "notice",
        # 前端据此跳转到公告详情
        "link": {"type": "notice", "id": notice_id},
    }


def house_to_chunk(house):
    """把一套房源转成片，供「这套房子押付方式是什么」这类定向提问使用。

    只在问题里带了房源编号时才构造，不进常驻索引。
    """
    house_no = house.get("house_no")
    if not house_no:
        return None
    fields = [
        ("小区", house.get("community")),
        ("区域", house.get("district")),
        ("户型", house.get("layout")),
        ("面积", "%s 平方米" % house.get("area") if house.get("area") else None),
        ("租金", "每月 %s 元" % house.get("rent_price")
         if house.get("rent_price") else None),
        ("租赁方式", house.get("rent_type")),
        ("楼层", house.get("floor")),
        ("朝向", house.get("orientation")),
    ]
    lines = ["%s：%s" % (name, value) for name, value in fields if value]
    description = (house.get("description") or "").strip()
    if description:
        lines.append(description)
    return {
        "id": "house-%s" % house_no,
        "title": "房源 %s %s" % (house_no, house.get("community") or ""),
        "body": "\n".join(lines),
        "category": "房源",
        "aliases": [house_no],
        "source": "house",
        "link": {"type": "house", "house_no": house_no},
    }


def faq_to_chunk(row):
    """把后端 `faqs` 表返回的一行转成片。

    与 `parse_faq` 产出的结构完全一致，检索层因此不需要知道这条 FAQ
    是管理员在后台写的还是从 Markdown 里读的。
    """
    code = row.get("code") or ("faq-%s" % row.get("id"))
    question = (row.get("question") or "").strip()
    answer = (row.get("answer") or "").strip()
    if not question or not answer:
        return None
    aliases = row.get("aliases") or ""
    if isinstance(aliases, str):
        aliases = [a.strip() for a in aliases.split("/") if a.strip()]
    return {
        "id": code,
        "title": question,
        "body": answer,
        "category": row.get("category") or "其他",
        "aliases": list(aliases),
        "source": "faq",
        "link": None,
    }


def synonyms_of(word):
    """这个词的同义组（含它自己）。查不到就只返回它自己。

    给可答性闸门用。**闸门原先只拿原始查询里的词去比**，
    同义词表只作用在检索那一侧——于是出现了一个很别扭的局面：
    检索靠同义词把正确的那一片排到了第一位，闸门却因为
    「你问的『修东西的钱』这片资料里没提到」把它拒了，
    而那片资料写的正是「维修费用由谁承担」。

    **同义词表本来就是为了跨越这种用词差异，没有理由只用在一半路径上。**
    """
    if not word:
        return set()
    # **只查话题词表**，不查说法词表。理由见 PHRASING_GROUPS 的注释：
    # 闸门问的是「你说的那个东西我们讲过没有」，而「能不能」「多久」
    # 这类词指向的东西是空的，拿它们去比对材料等于放空门。
    return set(_TOPIC_INDEX.get(word) or {word})
