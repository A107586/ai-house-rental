"""内容审核（M-08）。

审核的对象是用户产生的公开文本：房源标题与描述、租客评价、平台公告。

两条设计原则：

**一、先归一化再匹配。** 想绕过关键词过滤的人不会老老实实打「微信」，
他会写「微 信」「微❤信」「Ｗｅｉｘｉｎ」。不做归一化的词表形同虚设。
所以匹配前统一做：全角转半角、转小写、去掉空白与常见的分隔／装饰字符。

**二、拦截与标记要分开。** 全部硬拦会把正常内容误伤到没法用，全部放行
又等于没做。所以只有确定性高的才拦（私下联系方式、明确的违法违规词），
模糊的一律放行但记一条待人工复核——审核的价值在于让人能复查，
不在于让机器替人下判断。

私下联系方式为什么要拦：租房平台上把交易引到站外，是骗押金最常见的
第一步，平台也因此失去纠纷时的凭证。这条规则对房源、评价、公告生效，
但**不对报修生效**——报修本来就有独立的联系人字段，租客在描述里写
自己的电话既正常也无害。
"""

import re
import unicodedata

# 归一化时要去掉的干扰字符。绕过词表最常用的手法就是往中间插东西。
NOISE = set(" \t\r\n·．.-_*+~^|/\\、，,;；:：'\"“”‘’()（）[]【】{}<>《》!！?？❤♥★☆")

# 确定性高、直接拦截的词。分类是为了让管理员知道拦的是哪一类。
BLOCK_WORDS = {
    "违法违规": ("代开发票", "办证刻章", "枪支弹药", "赌博网站", "洗钱"),
    "诈骗话术": ("先交定金再看房", "不看房直接签", "押金秒退保证",
                 "内部房源特批"),
}

# 需要人工复核但不直接拦的词
FLAG_WORDS = {
    "辱骂攻击": ("傻逼", "垃圾房东", "滚蛋", "废物"),
    "夸大宣传": ("全网最低", "绝对最便宜", "百分百满意", "稳赚不赔"),
    "歧视性表述": ("不租给外地人", "限本地人", "不租女生", "不租男生"),
}

# 联系方式。手机号按国内号段匹配；社交账号靠「关键词 + 后面跟着账号」判断，
# 只出现「微信」两个字不算——「微信联系管家」这种平台内的正常表述不该被拦。
PHONE_RE = re.compile(r"1[3-9]\d{9}")
QQ_RE = re.compile(r"(?:qq|扣扣|企鹅)[号:]?\s*(\d{5,12})")
WECHAT_RE = re.compile(r"(?:微信|weixin|wx|vx|v信)[号:]?\s*([a-z0-9_-]{5,20})")

# 对哪些场景做联系方式拦截。报修不在其中，理由见模块文档。
#
# chat 是租客与房东的私聊（C-1）。**这才是这类内容真正高发的地方**——
# 平台上线后第一件事就是有人在聊天里发微信号绕过平台，或者骗子发链接。
# 只审房源和评价而不审私聊，等于把门锁在了没人走的那道。
# profile 是中介的公开简介（`PUT /api/agent/profile` 的 intro），
# 它会原样出现在「找中介」名录里——和房源描述一样是**面向所有人**的
# 自由文案，只是入口不同。原先只审房源不审这里，等于把同一句
# 「加微信 wxid_xxx」换个地方就发出去了。
CONTACT_SCENES = ("house", "review", "notice", "chat", "profile")

LEVEL_PASS = "pass"
LEVEL_FLAG = "flag"
LEVEL_BLOCK = "block"


def normalize(text):
    """归一化：全角转半角、转小写、去掉空白与装饰字符。

    `NFKC` 把「Ｗｅｉｘｉｎ」「①」这类兼容字符折叠成常规形式，
    剩下的分隔符再逐个滤掉。
    """
    if not text:
        return ""
    folded = unicodedata.normalize("NFKC", text).lower()
    return "".join(ch for ch in folded if ch not in NOISE)


def find_contacts(text):
    """找出文本里的联系方式，返回 ``[(类型, 命中内容), ...]``。"""
    flat = normalize(text)
    hits = []
    for number in PHONE_RE.findall(flat):
        hits.append(("手机号", number))
    for account in QQ_RE.findall(flat):
        hits.append(("QQ号", account))
    for account in WECHAT_RE.findall(flat):
        hits.append(("微信号", account))
    return hits


def check(text, scene="house"):
    """审核一段文本。

    返回 ``{"level", "reasons", "categories"}``：

    * ``block`` —— 拒绝提交，reasons 里说明原因
    * ``flag``  —— 允许提交，但记一条待人工复核
    * ``pass``  —— 无异常
    """
    result = {"level": LEVEL_PASS, "reasons": [], "categories": []}
    if not text or not text.strip():
        return result

    flat = normalize(text)

    for category, words in BLOCK_WORDS.items():
        for word in words:
            if normalize(word) in flat:
                result["level"] = LEVEL_BLOCK
                result["categories"].append(category)
                result["reasons"].append("包含%s内容：%s" % (category, word))

    if scene in CONTACT_SCENES:
        for kind, value in find_contacts(text):
            result["level"] = LEVEL_BLOCK
            result["categories"].append("私下联系方式")
            # 只回显前三位，既让用户知道拦的是哪一处，又不把号码再抄一遍
            result["reasons"].append(
                "不能留%s（%s…）。平台外交易无法在纠纷时提供凭证，"
                "也是骗押金最常见的第一步。" % (kind, value[:3]))

    if result["level"] == LEVEL_BLOCK:
        # 已经要拦了就不必再列举需复核的词，报错信息越短越有人看
        result["categories"] = sorted(set(result["categories"]))
        return result

    for category, words in FLAG_WORDS.items():
        for word in words:
            if normalize(word) in flat:
                result["level"] = LEVEL_FLAG
                result["categories"].append(category)
                result["reasons"].append("疑似%s：%s" % (category, word))

    result["categories"] = sorted(set(result["categories"]))
    return result


def check_all(fields, scene="house"):
    """审核多个字段，返回合并后的结论。

    任意一段被拦就整体拦下——房源标题干净、描述里留了微信号，
    显然不能算通过。
    """
    merged = {"level": LEVEL_PASS, "reasons": [], "categories": []}
    for name, text in fields.items():
        one = check(text, scene=scene)
        if one["level"] == LEVEL_PASS:
            continue
        merged["reasons"].extend("%s：%s" % (name, r) for r in one["reasons"])
        merged["categories"].extend(one["categories"])
        if one["level"] == LEVEL_BLOCK:
            merged["level"] = LEVEL_BLOCK
        elif merged["level"] != LEVEL_BLOCK:
            merged["level"] = LEVEL_FLAG
    merged["categories"] = sorted(set(merged["categories"]))
    return merged
