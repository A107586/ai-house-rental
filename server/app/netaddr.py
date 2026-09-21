# -*- coding: utf-8 -*-
"""取客户端 IP —— 只有这一处实现，限流与操作日志都用它。

## 为什么要单独拎出来

原先 `ratelimit.py` 与 `oplog.py` **各写了一份**，两份都是
「取 `X-Forwarded-For` 的第一段」。两份一样的代码，也就一起错了。

## 「第一段」错在哪

`X-Forwarded-For` 是一条**从左往右累加**的链：最左边是最初的客户端，
每经过一个代理就往右追加一个。问题在于**最左边那一段是客户端自己发的**，
谁都可以随手写一个。

nginx 的标准写法恰恰会把它原样带上：

    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    # 展开后 = "$http_x_forwarded_for, $remote_addr"
    #           ↑ 客户端发来的，可以是任何值   ↑ nginx 亲眼看见的地址

所以取第一段 = **取攻击者自己填的那个值**。

实测过：登录连错 8 次会被限流（429），但只要把 `X-Forwarded-For`
换一个值，计数就从零开始——

    同一个伪造 IP 连打 10 次: [401×8, 429, 429]
    换成 8.8.8.8: 401      ← 又放行了
    换成 7.7.7.7: 401

**登录限流等于不存在**，撞库脚本每次换个头就行。同一个洞还会污染操作日志
（M-07）：日志里记下的「谁在哪个 IP 上做了什么」也是攻击者写的。

## 现在的规则

- **默认不信任 `X-Forwarded-For`**，直接用 TCP 对端地址。
  没有反向代理时这是唯一可信的来源。
- 部署在反向代理后面时，设 `TRUSTED_PROXY_HOPS=1`（几层代理就写几）。
  这时从**右边**数：右边那几段是各层代理自己写的，客户端够不着。

**从右边数是关键。** 左边可以无限伪造，右边由代理逐跳追加，
伪造不了——攻击者塞进来的内容只会被挤到更左边去。
"""

import os

# 代理层数。0 = 直接暴露（不信任任何转发头），1 = 前面有一层 nginx，以此类推。
TRUSTED_PROXY_HOPS = int(os.environ.get("TRUSTED_PROXY_HOPS") or 0)

_MAX_LEN = 45          # IPv6 最长 45 字符，够了


def client_ip(request, hops=None):
    """客户端地址。取不到时返回空串——**不要编一个默认值**。

    `hops` 只给测试用，正常由环境变量决定。
    """
    hops = TRUSTED_PROXY_HOPS if hops is None else hops
    if hops > 0:
        raw = request.headers.get("x-forwarded-for") or ""
        chain = [part.strip() for part in raw.split(",") if part.strip()]
        if chain:
            # 从右往左数第 hops 个；链比代理层数短就取最左边那个
            index = min(hops, len(chain))
            return chain[-index][:_MAX_LEN]
    return (request.client.host if request.client else "")[:_MAX_LEN]
