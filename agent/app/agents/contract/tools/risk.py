"""工具：识别对租客不利的合同条款（AI-02 的核心）。

这是整个功能真正有价值的部分——租客普遍缺乏合同审阅能力，
系统要替他们把坑指出来。

两类判断：

* **模式匹配**：条款里出现「不予退还」「有权单方面」这类措辞，
  直接命中风险规则。
* **数值核对**：违约金是否超过一个月租金、押金是否超过三个月，
  这类要跟抽取出的金额比对才能判断。

每条风险给出严重程度与**一句给租客的建议**，光说「有风险」没用，
要告诉他能怎么办。
"""

import re

# 严重程度：high 签前必须谈 / medium 需要注意 / low 提示
RULES = [
    {
        "id": "deposit_no_refund",
        "level": "high",
        "title": "押金不退条款",
        "patterns": [r"押金.{0,20}(?:不予退还|不退还|概不退还|不予返还)",
                     r"(?:不予退还|不退还).{0,10}押金"],
        "why": "合同写明押金在某些情形下不退还，且未限定具体条件。",
        "advice": "要求写清「仅在房屋损坏且经双方确认后按实际损失扣除」，"
                  "并约定退还期限。",
    },
    {
        "id": "unilateral_terminate",
        "level": "high",
        "title": "房东可单方面解约",
        "patterns": [r"(?:甲方|出租方).{0,30}(?:有权|可以).{0,20}(?:单方面|随时).{0,10}(?:解除|终止)",
                     r"(?:单方面|随时).{0,10}(?:解除|终止).{0,10}(?:合同|租赁)"],
        "why": "出租方保留了随时解约的权利，租客可能被要求短期搬离。",
        "advice": "要求改为「双方均需提前 30 日书面通知」，并约定违约补偿。",
    },
    {
        "id": "rent_adjust",
        "level": "high",
        "title": "租期内可调整租金",
        "patterns": [r"(?:甲方|出租方).{0,30}(?:有权|可).{0,20}(?:调整|上调|变更).{0,10}租金",
                     r"租金.{0,20}(?:随时|视情况).{0,10}(?:调整|上调)"],
        "why": "租期内租金可被单方面上调，实际支出不可预期。",
        "advice": "要求删除该条，或明确「租期内租金不变」。",
    },
    {
        "id": "tenant_all_repair",
        "level": "medium",
        "title": "维修责任全归租客",
        "patterns": [r"(?:一切|所有|全部).{0,10}维修.{0,20}(?:由乙方|由承租方|乙方承担)",
                     r"(?:乙方|承租方).{0,10}(?:承担|负责).{0,10}(?:一切|所有|全部).{0,10}(?:维修|损坏)"],
        "why": "房屋主体与原有设施的自然损耗本应由出租方负责。",
        "advice": "要求区分「自然损耗由甲方负责，人为损坏由乙方负责」。",
    },
    {
        "id": "no_early_exit",
        "level": "medium",
        "title": "不允许提前退租",
        "patterns": [r"(?:不得|不能|不允许).{0,10}提前.{0,10}(?:退租|解除|终止)",
                     r"提前(?:退租|解约).{0,20}(?:押金|租金).{0,10}(?:不予退还|不退)"],
        "why": "工作变动等情况下无法退租，或退租代价极高。",
        "advice": "争取约定「提前 30 日通知并支付一个月租金违约金后可退租」。",
    },
    {
        "id": "auto_renew",
        "level": "medium",
        "title": "自动续约条款",
        "patterns": [r"(?:自动|默认).{0,10}(?:续[约租租期]|延长)"],
        "why": "到期未主动提出即自动续约，可能被动承担新一期租金。",
        "advice": "确认续约需双方书面确认，或约定提前通知期限。",
    },
    {
        "id": "landlord_entry",
        "level": "medium",
        "title": "房东可随意进入",
        "patterns": [r"(?:甲方|出租方).{0,20}(?:有权|可).{0,20}(?:随时|任意).{0,10}(?:进入|查看)"],
        "why": "居住隐私缺乏保障。",
        "advice": "要求约定「需提前 24 小时通知并经乙方同意」。",
    },
    {
        "id": "no_sublet",
        "level": "low",
        "title": "禁止转租",
        "patterns": [r"不得.{0,10}(?:擅自)?转租"],
        "why": "这是常规条款，但意味着中途换人需与房东重新协商。",
        "advice": "属正常约定，了解即可。若有转租需求，签前先沟通。",
    },
]


# ---------------------------------------------------------------- 有利条款

# **评分原先只减不加。**
#
# 于是四份不同的合同——带家具的、可养宠物的、集中供暖的、合租的——
# 审出来全是 87 分、同样两条风险。用户的原话是「所有的审阅条款长得都
# 一样，评分也是」，说的就是这个。
#
# 光让合同正文分化还不够：新加的那些条款一条规则都不命中，
# 审阅照旧只看得见主干里那两条坑。**一份把责任划清楚的合同，
# 和一份什么都没写的合同，不该得同一个分。**
#
# 所以补一侧：写明了、对租客有保障的条款要加回分。
#
# 权重按**这一条实际值多少钱**分层，不是一律给同一个数：
#
#   3 分 —— 直接决定钱能不能拿回来（多付的租金可退、没清单不能扣款）
#   2 分 —— 划清了责任（家具清单、取暖费归属、停梯减租、免押边界）
#   1 分 —— 把口头约定写进了合同（养宠物）
#
# 一律给同一个数的后果试过：四份条款数不同的合同全都撞到加分上限，
# 又变成同一个分了——**分层不是讲究，是让分数真的能分出高下的前提**。
#
# 加分幅度刻意小于扣分（最高 3 对 25 / 10 / 3）：
# **合同审阅的本职是提醒风险**，有利条款是锦上添花，
# 不能靠堆几条好条款把一条高风险盖过去。
FAVOURABLE = [
    {
        "id": "inventory_signed",
        "weight": 2,
        "title": "家具清单写进了合同",
        "patterns": [r"物品交接清单", r"共同清点"],
        "note": "家具家电有清单可依，退租时不容易为「东西是不是你弄坏的」扯皮。",
    },
    {
        "id": "no_claim_without_inventory",
        "weight": 3,
        "title": "没清单就不能扣款",
        "patterns": [r"未签署清单.{0,20}不得.{0,10}(?:扣款|主张)"],
        "note": "这一条对租客很实在：清单是甲方该准备的，没准备就不能反过来扣你钱。",
    },
    {
        "id": "pet_allowed",
        "weight": 1,
        "title": "养宠物写进了合同",
        "patterns": [r"同意乙方.{0,15}饲养宠物"],
        "note": "口头答应养宠物、事后反悔要求搬离的情况很常见，写进合同才作数。",
    },
    {
        "id": "prepaid_refundable",
        "weight": 3,
        "title": "多付的租金可退",
        "patterns": [r"退还已付但尚未居住月份的租金",
                     r"不得以.{0,10}已缴不退.{0,10}为由拒绝"],
        "note": "一次付三个月以上时，这一条决定了提前退租能不能把钱拿回来。",
    },
    {
        "id": "elevator_downtime",
        "weight": 2,
        "title": "停梯可减租金",
        "patterns": [r"停运.{0,20}减免租金"],
        "note": "高层住户遇到长时间停梯时，有据可依。",
    },
    {
        "id": "heating_on_landlord",
        "weight": 2,
        "title": "取暖费由房东承担",
        "patterns": [r"取暖费由甲方承担"],
        "note": "北方冬季取暖费是一笔不小的开支，写清由谁承担能省掉一次争执。",
    },
    {
        "id": "deposit_free_explained",
        "weight": 2,
        "title": "免押的边界写清楚了",
        "patterns": [r"免收押金不免除.{0,20}赔偿责任"],
        "note": "免押不等于不用赔，说清楚反而对双方都好。",
    },
]


def check_favourable(text):
    """找出对租客有保障的条款。"""
    found = []
    for rule in FAVOURABLE:
        for pattern in rule["patterns"]:
            if re.search(pattern, text, re.S):
                found.append({"id": rule["id"], "title": rule["title"],
                              "note": rule["note"], "weight": rule["weight"]})
                break
    return found


def check_risks(text, clauses=None):
    """检查合同风险。

    返回 ``{"risks": [...], "levels": {...}, "score": 0-100}``。
    `score` 越高越安全，供前端用颜色表达整体印象。
    """
    if not text or not text.strip():
        return {"risks": [], "favourable": [],
                "levels": {"high": 0, "medium": 0, "low": 0},
                "score": 0}

    clauses = clauses or {}
    risks = []

    for rule in RULES:
        hit = None
        for pattern in rule["patterns"]:
            match = re.search(pattern, text, re.S)
            if match:
                hit = match.group(0)[:60]
                break
        if hit:
            risks.append({
                "id": rule["id"], "level": rule["level"], "title": rule["title"],
                "why": rule["why"], "advice": rule["advice"], "quote": hit,
            })

    risks.extend(_numeric_risks(text, clauses))

    levels = {"high": 0, "medium": 0, "low": 0}
    for risk in risks:
        levels[risk["level"]] = levels.get(risk["level"], 0) + 1

    # 高风险扣 25 分，中风险 10 分，低风险 3 分
    score = 100 - levels["high"] * 25 - levels["medium"] * 10 - levels["low"] * 3

    # 写明了、对租客有保障的条款加回一些分。
    #
    # **加分封顶在 100，也封顶在「不能把高风险盖过去」**：
    # 单条最多 8 分，而一条高风险扣 25——堆三条好条款也翻不过来。
    # 合同审阅的本职是提醒风险，不是给合同评好评。
    good = check_favourable(text)
    # **加分总额封顶。**
    #
    # 不封的话，一份条款写得细的合同能靠六条有利条款把 87 分顶到 100——
    # 而那两条真实存在的风险（不允许提前退租、禁止转租）还在那儿。
    # 满分会让人以为这份合同没问题，恰恰是审阅最不该传达的印象。
    #
    # 12 分的上限，比一条中风险（10 分）多一点、比一条高风险（25 分）
    # 少得多：条款写得好能把分数抬上去一档，但抬不过一条硬伤。
    score += min(sum(item["weight"] for item in good), 12)

    return {"risks": risks, "favourable": good, "levels": levels,
            "score": max(0, min(score, 100))}


def _numeric_risks(text, clauses):
    """数值类风险：要跟抽取出的金额比对才能判断。"""
    found = []
    rent = clauses.get("租金")
    deposit = clauses.get("押金")

    # 押金超过三个月租金
    if rent and deposit and deposit > rent * 3:
        found.append({
            "id": "deposit_too_high", "level": "medium", "title": "押金偏高",
            "why": "押金 %d 元相当于 %.1f 个月租金，高于常见的一到三个月。"
                   % (deposit, deposit / rent),
            "advice": "可与房东协商降低押金，或要求在合同中写明退还条件与期限。",
            "quote": "押金 %d 元 / 月租 %d 元" % (deposit, rent),
        })

    # 违约金超过一个月租金
    if rent:
        for match in re.finditer(r"违约金[^。]{0,40}?(\d[\d,]*)\s*元", text):
            amount = int(match.group(1).replace(",", ""))
            if amount > rent:
                found.append({
                    "id": "penalty_too_high", "level": "high", "title": "违约金过高",
                    "why": "违约金 %d 元超过一个月租金 %d 元。" % (amount, rent),
                    "advice": "常见约定为一个月租金，可要求下调至该水平。",
                    "quote": match.group(0)[:60],
                })
                break
        for match in re.finditer(r"违约金[^。]{0,30}?([一二三四五六七八九十\d]+)\s*个月",
                                 text):
            raw = match.group(1)
            months = int(raw) if raw.isdigit() else "一二三四五六七八九十".find(raw) + 1
            if months > 1:
                found.append({
                    "id": "penalty_months", "level": "high", "title": "违约金过高",
                    "why": "违约金相当于 %d 个月租金，高于常见的一个月。" % months,
                    "advice": "可要求下调至一个月租金。",
                    "quote": match.group(0)[:60],
                })
                break
    return found


def summarize(result, extracted):
    """生成一句面向租客的整体结论。"""
    levels = result["levels"]
    if not result["risks"]:
        return "未发现明显不利条款，合同条款整体常规。签约前仍建议逐条通读。"

    parts = []
    if levels["high"]:
        parts.append("%d 条需要签前重点协商" % levels["high"])
    if levels["medium"]:
        parts.append("%d 条需要注意" % levels["medium"])
    if levels["low"]:
        parts.append("%d 条提示" % levels["low"])

    text = "共识别出 %d 处需关注的条款，其中%s。" % (len(result["risks"]), "、".join(parts))
    if extracted.get("missing"):
        text += "另外合同中未找到：%s，建议要求补充。" % "、".join(extracted["missing"])
    return text
