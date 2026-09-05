"""工具：从中介口述的带看记录里抽出结构化信息（J-09-c 的规则层）。

中介带完看，写的是这种话：

    「小两口，觉得朝向可以，嫌厨房小，问了能不能养猫，说要和家里商量下」

要从里面抽出：到没到场、意向强弱、顾虑、满意点、提出的问题。

**为什么规则层要写得像样，而不是随便糊一个等大模型救。**
带看记录会汇进两个统计：「放鸽子率」和房源的「常见异议」聚合。
统计要求口径稳定——同一句话今天判成「意向强」、明天判成「一般」，
那两个数字就没法看了。规则层判出来的东西至少是**可复现**的，
大模型只在它拿不准的地方补充。没有大模型时（需求 N-07）纪要照样能存，
只是关键词少一点。

顺带说一句为什么 `showed_up` 单独拎出来：它是唯一一个**会影响别人**
的字段——放鸽子率高的是租客，不是中介。判错了是在冤枉人，
所以只认明确的说法，含糊的一律按「到场」算（默认值也是到场）。
"""

# 顾虑 / 异议。键是归一化之后的标签，值是说法。
#
# **标签必须归一化**，不能原样存中介写的词。同一件事有人写「厨房小」、
# 有人写「厨房太窄」、有人写「灶台不够用」，原样存进去，
# 房源的「常见异议」聚合就永远聚不到一起——三个人嫌厨房小会显示成
# 三条各出现一次的异议，等于没统计。
CONCERN_KEYWORDS = [
    ("厨房面积", ["厨房小", "厨房太小", "厨房窄", "厨房不够", "灶台小"]),
    ("采光", ["采光差", "光线暗", "太暗", "不见光", "背阴", "没有阳光"]),
    ("楼层", ["楼层高", "楼层低", "爬楼", "没电梯", "一楼", "顶楼"]),
    ("噪音", ["吵", "噪音", "临街", "隔音差", "隔音不好"]),
    ("价格", ["贵", "超预算", "价格高", "租金高", "太贵"]),
    ("装修", ["装修旧", "老旧", "破旧", "墙皮", "发霉", "潮"]),
    ("卫生间", ["卫生间小", "厕所小", "干湿不分", "没有窗"]),
    ("面积", ["房间小", "太小", "空间小", "面积小", "局促"]),
    ("交通", ["离地铁远", "不方便", "公交少", "太偏"]),
    ("朝向", ["朝向不好", "朝北", "西晒"]),
]

# 满意点。与顾虑分开统计：一套房「三个人夸朝向、三个人嫌厨房」
# 和「六个人什么都没说」，对房东是完全不同的两条信息
POSITIVE_KEYWORDS = [
    ("采光", ["采光好", "亮堂", "光线好", "阳光好"]),
    ("朝向", ["朝向好", "朝南", "朝向可以", "南北通透"]),
    ("面积", ["宽敞", "空间大", "够大", "面积合适"]),
    ("装修", ["装修好", "干净", "新装", "整洁", "家电全", "家具全"]),
    ("交通", ["离地铁近", "交通方便", "出门就是", "通勤近"]),
    ("价格", ["便宜", "性价比", "价格合适", "不贵"]),
    ("楼层", ["楼层好", "有电梯", "楼层合适"]),
    ("小区", ["小区好", "环境好", "安静", "绿化好"]),
]

# 租客当场问的问题。这些是**房东该在房源描述里写清楚的东西**——
# 同一个问题被问三次，说明描述里缺了它
QUESTION_KEYWORDS = [
    ("是否可养宠物", ["养猫", "养狗", "养宠物", "能不能养"]),
    ("押付方式", ["押一付一", "押二", "押付", "能不能月付", "付款方式"]),
    ("可入住时间", ["什么时候能住", "几号能搬", "入住时间", "多久能住"]),
    ("是否可短租", ["短租", "住半年", "住三个月", "租期能不能短"]),
    ("费用包含", ["水电", "物业费", "取暖费", "网费", "包不包"]),
    ("是否可做饭", ["做饭", "开火", "明火"]),
    ("能否议价", ["能不能便宜", "还价", "少点", "降点"]),
]

# 没到场。**只认明确的说法**——理由见模块开头
NO_SHOW_KEYWORDS = ["没来", "没到", "未到场", "放鸽子", "爽约", "临时有事",
                    "改期", "没露面", "人没来", "取消了"]

# 意向。强弱各自的说法，都没命中就是「一般」
HIGH_INTENT = ["很满意", "非常满意", "当场", "想定", "要定", "签", "交定金",
               "催我", "问什么时候能签", "特别喜欢", "就要这套"]
LOW_INTENT = ["不合适", "不考虑", "算了", "再看看别的", "没兴趣", "不喜欢",
              "差太远", "不满意", "看不上"]

# 「要和家里商量」这类是典型的中等意向——既没拒绝也没定，
# 单独列出来是为了不让它被 LOW_INTENT 里的「再看看」误伤
MEDIUM_INTENT = ["商量", "考虑一下", "回去想想", "再想想", "和家里说",
                 "对比一下", "再比较"]

INTENTIONS = ("high", "medium", "low")


def _hits(text, table):
    """按表匹配，返回命中的标签（去重、保持表里的顺序）。

    **同一段文字只算一次。** 这不是洁癖：表里有具体标签也有笼统标签，
    而具体说法往往包含笼统说法——「厨房太小」里就含着「太小」，
    于是一句话同时命中「厨房面积」和「面积」两个标签，
    房源的异议聚合里这一条被数了两遍。

    做法是记下每个命中占掉的字符区间，后面的词若落在已被占掉的区间里
    就跳过。表的顺序因此有意义：**具体的排前面，笼统的排后面**。
    """
    found, taken = [], []

    def overlaps(start, end):
        return any(start < t_end and t_start < end for t_start, t_end in taken)

    for label, words in table:
        for word in words:
            index = text.find(word)
            while index != -1:
                if not overlaps(index, index + len(word)):
                    taken.append((index, index + len(word)))
                    if label not in found:
                        found.append(label)
                    break
                index = text.find(word, index + 1)
            if label in found:
                break
    return found


def extract(text):
    """从一段口述里抽出结构化字段。

    返回的形状与 `ViewingRecord` 的字段对齐，前端拿到就能直接填表。
    """
    text = (text or "").strip()
    if not text:
        return {
            "showed_up": True, "intention": "medium",
            "concerns": [], "positives": [], "questions": [],
            "summary": "", "matched": [],
        }

    showed_up = not any(word in text for word in NO_SHOW_KEYWORDS)

    concerns = _hits(text, CONCERN_KEYWORDS)
    positives = _hits(text, POSITIVE_KEYWORDS)
    questions = _hits(text, QUESTION_KEYWORDS)

    # 意向的判定顺序：先看有没有明确要定，再看有没有明确否掉，
    # 最后才是「要商量」那一档。
    #
    # **顺序不能反。** 「很满意，回去和家里商量一下」两个信号都有，
    # 先判 medium 就把「很满意」这个更强的信号丢了。
    if not showed_up:
        intention = "low"          # 人都没来，谈不上意向
    elif any(word in text for word in HIGH_INTENT):
        intention = "high"
    elif any(word in text for word in LOW_INTENT):
        intention = "low"
    elif any(word in text for word in MEDIUM_INTENT):
        intention = "medium"
    else:
        intention = "medium"

    return {
        "showed_up": showed_up,
        "intention": intention,
        "concerns": concerns,
        "positives": positives,
        "questions": questions,
        "summary": compose(showed_up, intention, concerns, positives,
                           questions, text),
        # 命中了哪些标签，答辩时用来说明「这个判断是怎么来的」
        "matched": concerns + positives + questions,
    }


INTENTION_TEXT = {"high": "意向强", "medium": "意向一般", "low": "意向弱"}


def compose(showed_up, intention, concerns, positives, questions, raw=""):
    """把结构化字段拼成一句人话纪要。

    **没有大模型时这就是最终输出**，所以它必须自己能读通，
    不能是「concerns: 厨房面积」这种半成品。
    """
    if not showed_up:
        return "租客未到场。" + (("原话：" + raw[:60]) if raw else "")

    parts = []
    if positives:
        parts.append("对%s比较满意" % "、".join(positives))
    if concerns:
        parts.append("主要顾虑是%s" % "、".join(concerns))
    if questions:
        parts.append("现场问到%s" % "、".join(questions))
    parts.append(INTENTION_TEXT.get(intention, "意向一般"))

    return "；".join(parts) + "。"
