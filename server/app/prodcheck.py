# -*- coding: utf-8 -*-
"""上线前的自检：配置不对就**不许启动**。

## 为什么要有这个

项目里有两个「不设就用默认值」的密钥：

    AUTH_SECRET         = "dev-only-secret-change-me"     （app/auth.py）
    PAYMENT_SIGN_SECRET = "rental-pay-dev-secret"         （app/gateway.py）

本地开发这样最省事——拉下代码就能跑，不用先配环境。**但这份代码在
GitHub 上是公开的**，默认值谁都看得到。带着它上公网，后果不是「不太安全」，
而是：

- 拿 `AUTH_SECRET` 可以**自己签一个管理员令牌**，不需要密码；
- 拿 `PAYMENT_SIGN_SECRET` 可以**伪造一份支付回调**，把任意账单改成已付。

原先只在 `/health` 里返回一个 `auth_secret_is_default: true` 作为提醒。
**提醒是没用的**：它既拦不住启动，还顺便把「这台机器用的是公开默认密钥」
这件事告诉了每一个访问者。

所以改成硬检查：`APP_ENV=production` 时，配置不合格**直接拒绝启动**。
开发环境（默认）一切照旧，一行都不用配。

## 为什么是「拒绝启动」而不是「警告」

警告会被滚上去。部署那天终端里刷过几十行日志，没有人会回头看；
而**启动失败没法忽略**——它就停在那里，直到你把配置补上。

这条边界要卡在启动时，不能卡在第一个请求上：
服务已经起来、监听着公网端口，这时候再报错已经晚了。
"""

import os

DEV_AUTH_SECRET = "dev-only-secret-change-me"
DEV_PAYMENT_SECRET = "rental-pay-dev-secret"

# 密钥最短长度。32 字符约等于 128 位随机，HMAC 用这个量级足够；
# 再短就不如直接说「没设」。
MIN_SECRET_LEN = 32


def is_production(env=None):
    env = os.environ if env is None else env
    return (env.get("APP_ENV") or "").strip().lower() in ("production", "prod")


def problems(env=None):
    """返回一串「不合格的地方」。空列表 = 可以上线。

    每一条都写清楚**为什么不行**与**怎么改**——
    上线时看到一句「配置错误」而不知道改哪里，是最耽误事的。
    """
    env = os.environ if env is None else env
    found = []

    auth_secret = env.get("AUTH_SECRET") or ""
    if not auth_secret or auth_secret == DEV_AUTH_SECRET:
        found.append(
            "AUTH_SECRET 还是开发默认值。这个值在公开仓库里写着，"
            "拿到它就能自己签一个管理员令牌。"
            "生成一个：python -c \"import secrets;print(secrets.token_urlsafe(48))\"")
    elif len(auth_secret) < MIN_SECRET_LEN:
        found.append("AUTH_SECRET 太短（%d 字符），至少 %d 位。"
                     % (len(auth_secret), MIN_SECRET_LEN))

    pay_secret = env.get("PAYMENT_SIGN_SECRET") or ""
    if not pay_secret or pay_secret == DEV_PAYMENT_SECRET:
        found.append(
            "PAYMENT_SIGN_SECRET 还是开发默认值。支付回调接口无登录态、"
            "只认签名，拿到它就能把任意账单改成已付。")
    elif len(pay_secret) < MIN_SECRET_LEN:
        found.append("PAYMENT_SIGN_SECRET 太短（%d 字符），至少 %d 位。"
                     % (len(pay_secret), MIN_SECRET_LEN))

    database_url = env.get("DATABASE_URL") or ""
    mysql_hint = ("DATABASE_URL="
                  "mysql+pymysql://user:pwd@host:3306/rental?charset=utf8mb4")
    if not database_url:
        found.append(
            "没有设 DATABASE_URL，会落到本地 SQLite 文件。"
            "SQLite 是**库级写锁**，多进程一起写会互相等；"
            "上线请用 MySQL：" + mysql_hint)
    elif database_url.startswith("sqlite"):
        # 写成 sqlite 和不写一样糟，甚至更糟——它看起来像是特意选的。
        found.append(
            "DATABASE_URL 指向 SQLite。**多 worker 会互相抢写锁**"
            "（N-01 的提速正是靠多 worker），而且整个库是一个文件，"
            "备份与迁移都只能整体拷。上线请用 MySQL：" + mysql_hint)

    origins = env.get("CORS_ORIGINS") or ""
    if not origins:
        found.append(
            "没有设 CORS_ORIGINS，跨域白名单会停在 localhost 上。"
            "填你的站点域名：CORS_ORIGINS=https://example.com")
    elif "localhost" in origins or "127.0.0.1" in origins:
        found.append("CORS_ORIGINS 里还留着 localhost，线上不该放行本地来源。")

    if not (env.get("TRUSTED_PROXY_HOPS") or "").strip():
        found.append(
            "没有设 TRUSTED_PROXY_HOPS。上线一般会在 nginx 后面，"
            "设为 1 才能取到真实客户端 IP（限流与操作日志都按它算）；"
            "如果确实是裸跑不带代理，显式写 TRUSTED_PROXY_HOPS=0。")

    return found


def enforce(env=None):
    """非生产环境直接放行；生产环境不合格就抛异常，**不要启动**。"""
    if not is_production(env):
        return
    found = problems(env)
    if not found:
        return
    lines = ["上线自检没过，服务拒绝启动。逐条改完再来：", ""]
    for i, problem in enumerate(found, 1):
        lines.append("  %d) %s" % (i, problem))
    lines += ["",
              "本地开发不受这套检查影响（不设 APP_ENV=production 即可）。",
              "完整清单见 docs/上线部署手册.md。"]
    raise RuntimeError("\n".join(lines))
