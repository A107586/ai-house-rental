"""操作日志（M-07）。

用中间件统一采集，而不是在每个接口里手写一行 `log(...)`。手写的问题不是
麻烦，是**会漏**：新增接口时忘记加日志，事后从日志里根本看不出来少了什么，
而审计日志一旦有洞就失去了意义。中间件是「默认全记，显式排除」。

记录的口径有三条：

* 只记改变状态的请求（POST / PATCH / PUT / DELETE），GET 一律不记。
  全量记访问会让日志以每天几万条的速度涨，真要追溯时反而翻不动。
* 只记 `/api/` 下的业务请求，静态资源与前端页面不记。
* **不记录请求体**。改密码、注册这类接口的请求体里有明文密码，
  日志是给管理员看的，不该成为一个新的密码泄露面。
"""

import re

from app.auth import decode_token

# 不记录的路径。登录失败的爆破痕迹有价值，但成功登录每天几百条纯噪音；
# 对话记录接口一轮对话写两条，会把日志淹掉。
# `/api/support/logs` 与 `/api/support/feedback` 是智能体与前端的高频机器写入，
# 每问一句话就写一条。记进操作日志只会把真正需要审计的人工动作冲没。
SKIP_PATHS = ("/api/auth/login", "/api/chat-messages",
              "/api/support/logs", "/api/support/feedback")

WATCHED_METHODS = ("POST", "PATCH", "PUT", "DELETE")

# 路径 → 业务动作名。日志要给人看，「PATCH /api/admin/houses/12」
# 谁也读不出发生了什么，「审核房源」才是。
# 元组为 (方法, 路径正则, 动作名)，按顺序匹配，先匹配到的胜出。
ACTION_RULES = (
    ("POST", r"^/api/auth/register$", "注册账号"),
    ("PATCH", r"^/api/profile$", "修改个人资料"),
    ("POST", r"^/api/password$", "修改密码"),

    ("POST", r"^/api/houses$", "发布房源"),
    ("PATCH", r"^/api/houses/[^/]+/status$", "上下架房源"),
    ("PATCH", r"^/api/houses/[^/]+$", "修改房源"),
    ("DELETE", r"^/api/houses/[^/]+$", "删除房源"),

    ("POST", r"^/api/favorites", "收藏房源"),
    ("DELETE", r"^/api/favorites", "取消收藏"),
    ("DELETE", r"^/api/history$", "清空浏览足迹"),

    ("POST", r"^/api/appointments$", "预约看房"),
    ("PATCH", r"^/api/appointments/", "处理看房预约"),

    ("POST", r"^/api/contracts$", "创建合同"),
    ("PATCH", r"^/api/contracts/[^/]+$", "修改合同条款"),
    ("POST", r"^/api/contracts/[^/]+/activate$", "确认合同生效"),
    ("POST", r"^/api/contracts/[^/]+/end$", "办理退租"),
    ("POST", r"^/api/contracts/[^/]+/renew$", "办理续租"),

    ("POST", r"^/api/bills/[^/]+/pay$", "缴纳账单"),

    ("POST", r"^/api/repairs$", "提交报修"),
    ("PATCH", r"^/api/repairs/", "处理报修工单"),

    ("POST", r"^/api/reviews$", "发表评价"),

    ("POST", r"^/api/notices$", "发布公告"),
    ("PATCH", r"^/api/notices/", "上下架公告"),

    ("PATCH", r"^/api/admin/houses/", "审核房源"),
    ("PATCH", r"^/api/admin/users/", "修改用户状态或角色"),
    # 签发重置码等于临时把账号交出去，必须留痕（B1）
    ("POST", r"^/api/admin/users/\d+/reset-code$", "签发密码重置码"),
    # AI-03 智能客服
    ("POST", r"^/api/faqs$", "新增知识条目"),
    ("PUT", r"^/api/faqs/", "修改知识条目"),
    ("PATCH", r"^/api/faqs/", "上下架知识条目"),
    ("DELETE", r"^/api/faqs/", "删除知识条目"),
    ("POST", r"^/api/support/handoff$", "提交转人工"),
    ("POST", r"^/api/admin/handoffs/", "回复转人工工单"),
    # K-08 房源举报
    ("POST", r"^/api/reports$", "提交房源举报"),
    ("PATCH", r"^/api/admin/reports/", "处理房源举报"),
)

_COMPILED = tuple((method, re.compile(pattern), name)
                  for method, pattern, name in ACTION_RULES)


def action_of(method, path):
    """把方法与路径翻译成业务动作名。"""
    for want_method, pattern, name in _COMPILED:
        if method == want_method and pattern.match(path):
            return name
    return "%s %s" % (method, path)


def should_log(method, path):
    if method not in WATCHED_METHODS:
        return False
    if not path.startswith("/api/"):
        return False
    return not any(path.startswith(skip) for skip in SKIP_PATHS)


def identify(authorization):
    """从请求头解出操作者，返回 ``(user_id, username, role)``。

    这里只解令牌、不查库：中间件跑在请求链路上，为写一条日志再开一次
    数据库会话不划算，而令牌里本来就带着用户名与角色。
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        return None, "", ""
    payload = decode_token(authorization[7:].strip())
    if not payload:
        return None, "", ""
    return payload.get("uid"), payload.get("username", ""), payload.get("role", "")


# 客户端地址的取法只有一处实现，见 `app/netaddr.py`。
#
# 日志里这一栏原本也能被伪造：取的是 `X-Forwarded-For` 的第一段，
# 而那一段由请求方自己填。**操作日志是用来追溯「谁在哪做了什么」的**，
# 这一栏可以随便写，它就失去了作为证据的意义。
from app.netaddr import client_ip  # noqa: E402,F401
