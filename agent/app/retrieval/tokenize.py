"""中文检索分词。

**默认走字符 n-gram（unigram + bigram），不依赖分词器。**
这是本模块最重要的一个决定，理由有三条：

1. **零依赖**。`agent/requirements.txt` 只有 fastapi / uvicorn / pydantic，
   全部自动化测试都能在任何装了 Python 的机器上跑。为了检索去引入
   jieba 的词典（几十 MB）不划算——语料只有几十条。
2. **没有 OOV 问题**。「押一付三」「二房东」「押金退款」这类说法，
   分词器切错就彻底检不到；字符 bigram 一定能对上「押金」「金退」。
3. **对错别字宽容**。客服场景用户会打「压金」「水电废」，
   bigram 仍有部分重合，词级分词则完全丢失。

装了 jieba 就自动多一路词级分词（`WORD_LEVEL` 为真），检索侧多一个通道；
没装就只用字符级，**检索本身照常工作**——上面三条理由至今成立。

> ⚠️ **但 jieba 后来从「可选」变成了 `requirements.txt` 里的必装项。**
> 不是检索需要它，是**可答性闸门**需要：闸门要挑出「用户问的那个东西」，
> 而字符 bigram 分不出「公积金」和「扫干」谁是词。详见
> `index.RetrievalIndex._key_term_by_word`。没装它一切仍然能跑，
> 只是 AI-03 的有依据正确率会从 85% 掉回 80%——**这种「装没装结果不一样」
> 的差异恰恰是不能留给队友的**，所以写进了依赖清单而不是继续当彩蛋。
"""

import re
import unicodedata

try:  # pragma: no cover - 取决于运行环境是否装了 jieba
    import jieba

    WORD_LEVEL = True
except ImportError:  # pragma: no cover
    jieba = None
    WORD_LEVEL = False


# 中日韩统一表意文字。检索只关心「是不是汉字」，不需要更精细的分区。
_CJK = re.compile(r"[一-鿿]")
# 连续的英文字母或数字算一个词：「wifi」「7」「24」不该被拆开
_ASCII_WORD = re.compile(r"[a-z0-9]+")

# 停用字。**只去掉纯功能性的字**，「怎么」「如何」这类问句词保留——
# 客服语料里 FAQ 的标题本身就是问句，问句词是有效的匹配信号。
_STOP_CHARS = set("的了着吗呢吧啊呀哦嗯么呗")


def normalize(text):
    """全角转半角、小写化、去除标点与空白。

    `unicodedata.normalize("NFKC")` 一次性解决全角字母数字、
    全角空格与部分兼容字符；中文标点不在 NFKC 覆盖范围内，另行剔除。
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", str(text)).lower()
    # 保留汉字、字母、数字，其余一律当作分隔符
    return re.sub(r"[^一-鿿a-z0-9]+", " ", text).strip()


def char_tokens(text):
    """字符级分词：汉字取 unigram + bigram，英文数字取整词。

    bigram 承担「词」的作用，unigram 保证单字查询也能召回。
    两者混在同一个词袋里，BM25 的 IDF 会自动把区分度低的单字压下去，
    不需要人为调权重。
    """
    text = normalize(text)
    if not text:
        return []

    tokens = []
    for segment in text.split():
        # 先把段落里的**英文数字串整体切出来**。
        #
        # 不能只判断「整段是不是纯 ASCII」——中英混排是常态，
        # 「有wifi吗」整段不是纯 ASCII，于是曾被逐字拆成 w/i/f/i，
        # 结果任何含这些字母的词都能召回它。
        for word in _ASCII_WORD.findall(segment):
            tokens.append(word)
        # 剩下的中文部分照常做 unigram + bigram。英文串在这里被剔掉，
        # 免得「wifi」的字母混进中文的 bigram 里造出无意义的组合。
        chars = [c for c in segment if _CJK.match(c)]
        # unigram：跳过停用字，它们出现在几乎每句话里，只会拉低信噪比
        tokens.extend(c for c in chars if c not in _STOP_CHARS)
        # bigram：**不跳停用字**。「押金的退还」里的「金的」「的退」确实是噪声，
        # 但「什么」「怎么」这类词一旦被拆断，问句相似度就没了。
        # 交给 IDF 去压，比在这里硬删安全。
        tokens.extend(chars[i] + chars[i + 1] for i in range(len(chars) - 1))
    return tokens


def word_tokens(text):
    """词级分词。未安装 jieba 时返回空列表，调用方据此跳过这一路。"""
    if not WORD_LEVEL:
        return []
    text = normalize(text)
    if not text:
        return []
    return [t for t in jieba.lcut(text) if t.strip() and t not in _STOP_CHARS]


# 「问的那个东西」只可能是名词或动词。
#
# jieba 的词性标记里：n* 名词各类、v* 动词各类、j 简称、l 习用语、i 成语、
# s 处所词。**形容词 a 与副词 d 刻意不收**——「一般」「干净」「合适」
# 描述的是程度和状态，不是话题本身。实测「退租一般要提前多久说」
# 曾把「一般」当成话题，于是拒答了一个知识库里写得清清楚楚的问题。
_TOPIC_POS = frozenset((
    "n", "nr", "ns", "nt", "nz", "nl", "ng", "v", "vn", "vg",
    "j", "l", "i", "s",
))


def topic_words(text):
    """按词性挑出可能充当「话题」的词，长的在前不排序，交给调用方比区分度。

    未安装 jieba 时返回空列表——调用方据此退回字符级的近似做法。
    """
    if not WORD_LEVEL:
        return []
    text = normalize(text)
    if not text:
        return []
    import jieba.posseg as posseg              # 只有这一处用到，就地引入

    return [word for word, flag in posseg.lcut(text)
            if flag in _TOPIC_POS and word.strip()
            and word not in _STOP_CHARS]


def tokenize(text):
    """检索用的默认分词：字符级为主，装了 jieba 则并上词级。

    两路合并而不是二选一——词级给出「押金」这样的完整词，
    字符级给出容错，词袋模型下直接相加即可。
    """
    tokens = char_tokens(text)
    tokens.extend(word_tokens(text))
    return tokens


def content_chars(text):
    """提取实义字符集合，供可答性闸门计算覆盖率用。

    与 `char_tokens` 的区别：这里**只要 unigram、且去掉停用字与纯数字**。
    闸门问的是「用户问的这些字，材料里到底有没有」，
    bigram 会让覆盖率虚高（一个字对上就带出两个 bigram）。
    """
    text = normalize(text)
    return {c for c in text
            if (_CJK.match(c) or c.isalpha()) and c not in _STOP_CHARS}
