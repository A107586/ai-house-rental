# -*- coding: utf-8 -*-
"""把金额写成人看的样子。

单独成文件，是因为同一个函数原先抄了三份，而**其中一份是坏的**：

    "%g" % 1000000.0   ->  '1e+06'

房东的累计收入过百万不是什么稀奇事，那句「合计 1e+06 元」就这么发给
用户了。另有一处干脆没格式化，直接 `%s` 一个 float，写出来是
「合计 22000.0 元」。

这些数字是用户照着去交钱的依据，所以规矩定死：
**整数不带小数点，带角分的留两位，任何情况下都不用科学计数法。**
"""


def money_text(value):
    """1250 → 「1250」；1250.5 → 「1250.50」；1000000 → 「1000000」。"""
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        return str(value)
    if amount == int(amount):
        return "%.0f" % amount
    return "%.2f" % amount
