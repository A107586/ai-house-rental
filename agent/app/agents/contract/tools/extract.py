"""工具：从合同正文抽取关键条款（AI-02 的第一步）。

平台自己生成的合同是模板化的，正则抽取又快又准，不消耗 Token。
只有抽不全时才交给大模型——比如用户上传的第三方合同。

抽取的六项对应《需求分析说明书》AI-02 的验收标准：
租期、租金、押金、付款方式、违约责任、维修责任。
"""

import re

from app.cn_number import CN_NUM_CHARS, _token_to_number

_NUM = r"(?:\d+(?:[,\d]*)(?:\.\d+)?|[" + CN_NUM_CHARS + r"]+)"

# ---------------------------------------------------------------- 各条款

_PATTERNS = {
    "租期": [
        r"租赁期(?:限)?[自为]\s*(.{4,40}?)\s*[起至到]\s*(.{4,40}?)\s*[止。\n]",
        r"租期\s*[为：:]\s*(" + _NUM + r")\s*(?:个)?月",
    ],
    "租金": [
        r"月租金(?:为)?\s*(?:人民币)?\s*(" + _NUM + r")\s*元",
        r"租金\s*[为：:]\s*(?:人民币)?\s*(" + _NUM + r")\s*元",
    ],
    "押金": [
        r"押金(?:为)?\s*(?:人民币)?\s*(" + _NUM + r")\s*元",
        r"保证金(?:为)?\s*(?:人民币)?\s*(" + _NUM + r")\s*元",
    ],
    # 原先只认「按每 N 个月支付一次」——那恰好是**本平台自己生成的合同**
    # 的写法。拿外面的合同来审，这一条几乎必然抽不到。真实合同里
    # 「按月支付」「季付」「押一付三」都很常见，逐个补上。
    "付款方式": [
        r"按每\s*(" + _NUM + r")\s*个月支付一次",
        r"([一二三四五六七八九十\d]+)\s*月[一]?付",
        r"押([一二三四五六七八九十\d]+)付([一二三四五六七八九十\d]+)",
        r"(租金按(?:月|季度?|半年|年)(?:度)?支付)",
        r"(按(?:月|季度?|半年|年)(?:度)?(?:结算|支付|缴纳))",
        r"(每(?:月|季度?|半年|年)\s*\d{0,2}\s*[日号]?前?支付)",
        r"((?:月|季|年)结)",
    ],
    "违约责任": [
        r"(第[一二三四五六七八九十]+条\s*违约责任[^第]{10,300})",
        r"(违约[^。]{5,200}。)",
    ],
    "维修责任": [
        r"(第[一二三四五六七八九十]+条\s*维修责任[^第]{10,300})",
        r"(维修[^。]{5,200}。)",
    ],
}

CLAUSE_KEYS = list(_PATTERNS)


def _clean(text):
    return re.sub(r"\s+", " ", (text or "").strip())


def extract_clauses(text):
    """抽取关键条款。

    返回 ``{"clauses": {...}, "missing": [...], "coverage": 0.0-1.0}``。
    `coverage` 低于阈值说明这份合同不是平台模板，应交给大模型重新解析。
    """
    empty = {"clauses": {}, "missing": CLAUSE_KEYS[:], "coverage": 0.0}
    if not text or not text.strip():
        return empty

    clauses = {}
    for key, patterns in _PATTERNS.items():
        for pattern in patterns:
            match = re.search(pattern, text, re.S)
            if not match:
                continue
            groups = [g for g in match.groups() if g]
            if key in ("租金", "押金"):
                value = _token_to_number(groups[0].replace(",", ""))
                if value:
                    clauses[key] = int(value)
                    break
            elif key == "租期":
                clauses[key] = (_clean(" 至 ".join(groups)) if len(groups) > 1
                                else _clean(groups[0]) + " 个月")
                break
            elif key == "付款方式":
                months = _token_to_number(groups[-1])
                clauses[key] = ("每 %d 个月一付" % int(months) if months
                                else _clean(groups[0]))
                break
            else:
                clauses[key] = _clean(groups[0])[:200]
                break

    missing = [k for k in CLAUSE_KEYS if k not in clauses]
    return {
        "clauses": clauses,
        "missing": missing,
        "coverage": round(len(clauses) / len(CLAUSE_KEYS), 2),
    }
