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

# 打开一条消息、点进一个会话时前端自动发的「标为已读」。不是人做的决定，
# 每点一下就是一条，记进来会把真正的操作冲没
SKIP_PATTERNS = tuple(re.compile(p) for p in (
    r"^/api/messages/\d+/read$", r"^/api/conversations/\d+/read$"))

WATCHED_METHODS = ("POST", "PATCH", "PUT", "DELETE")

# 路径 → 业务动作名。日志要给人看，「PATCH /api/admin/houses/12」
# 谁也读不出发生了什么，「审核房源」才是。
# 元组为 (方法, 路径正则, 动作名)，按顺序匹配，先匹配到的胜出。
ACTION_RULES = (
    # 账号
    ("POST", r"^/api/auth/register$", "注册账号"),
    ("PUT", r"^/api/auth/profile$", "修改个人资料"),
    ("POST", r"^/api/auth/password$", "修改密码"),
    ("POST", r"^/api/auth/logout-all$", "退出所有设备"),
    ("POST", r"^/api/auth/forgot$", "申请找回密码"),
    ("POST", r"^/api/auth/reset$", "重置密码"),
    ("POST", r"^/api/auth/become-landlord$", "升级为房东"),
    ("POST", r"^/api/auth/revert-to-tenant$", "改回租客身份"),
    ("POST", r"^/api/auth/deactivate$", "注销账号"),

    # 房源。图片、核验这些带后缀的要排在「修改房源」前面——虽然那条锚了结尾，
    # 顺序写清楚，以后有人去掉 $ 也不会把上传图片记成修改房源
    ("POST", r"^/api/houses$", "发布房源"),
    ("PATCH", r"^/api/houses/[^/]+/status$", "上下架房源"),
    ("PATCH", r"^/api/houses/[^/]+/review$", "审核房源"),
    ("POST", r"^/api/houses/[^/]+/verify-request$", "申请房源核验"),
    ("PATCH", r"^/api/houses/[^/]+/verify$", "核验房源"),
    ("POST", r"^/api/houses/[^/]+/images/\d+/cover$", "设置房源封面"),
    ("POST", r"^/api/houses/[^/]+/images/\d+/caption$", "修改图片说明"),
    ("POST", r"^/api/houses/[^/]+/images$", "上传房源图片"),
    ("DELETE", r"^/api/houses/[^/]+/images/\d+$", "删除房源图片"),
    ("PUT", r"^/api/houses/[^/]+$", "修改房源"),
    ("POST", r"^/api/upload$", "上传图片"),

    ("POST", r"^/api/favorites", "收藏房源"),
    ("DELETE", r"^/api/favorites", "取消收藏"),
    ("DELETE", r"^/api/history$", "清空浏览足迹"),
    ("POST", r"^/api/saved-searches$", "保存搜索条件"),
    ("PATCH", r"^/api/saved-searches/", "修改保存的搜索"),
    ("DELETE", r"^/api/saved-searches/", "删除保存的搜索"),

    ("POST", r"^/api/appointments$", "预约看房"),
    ("PATCH", r"^/api/appointments/", "处理看房预约"),

    # 合同与账单
    ("POST", r"^/api/contracts$", "创建合同"),
    ("PATCH", r"^/api/contracts/[^/]+$", "修改合同条款"),
    ("POST", r"^/api/contracts/[^/]+/activate$", "确认合同生效"),
    ("POST", r"^/api/contracts/[^/]+/cancel$", "撤回或拒签待确认合同"),
    ("POST", r"^/api/contracts/[^/]+/bills$", "加收水电等费用"),
    ("POST", r"^/api/contracts/[^/]+/end$", "退租（租客为申请）"),
    ("POST", r"^/api/contracts/[^/]+/renew$", "续租申请或确认"),
    ("DELETE", r"^/api/contracts/[^/]+/request$", "撤回或拒绝退租续租申请"),
    ("POST", r"^/api/contracts/[^/]+/review-result$", "保存合同审阅结果"),

    ("POST", r"^/api/bills/[^/]+/pay$", "缴纳账单"),
    ("POST", r"^/api/bills/[^/]+/offline$", "确认线下收款"),
    ("POST", r"^/api/bills/[^/]+/waive$", "减免账单"),
    ("POST", r"^/api/admin/reminders/run$", "手动跑一轮到期提醒"),
    ("POST", r"^/api/payments/notify$", "支付结果回调"),
    ("POST", r"^/api/payments/[^/]+/mock-pay$", "模拟支付"),
    ("POST", r"^/api/landlord/tenants/\d+/remind$", "催缴提醒租客"),

    # 报修
    ("POST", r"^/api/repairs$", "提交报修"),
    ("POST", r"^/api/repairs/\d+/images$", "上传报修图片"),
    ("POST", r"^/api/repairs/\d+/record$", "登记维修结果"),
    ("PATCH", r"^/api/repairs/", "处理报修工单"),
    ("POST", r"^/api/repair-workers$", "新增维修师傅"),
    ("PATCH", r"^/api/repair-workers/", "修改维修师傅"),
    ("POST", r"^/api/repair-items$", "新增维修物料"),
    ("PATCH", r"^/api/repair-items/", "修改维修物料"),

    ("POST", r"^/api/reviews$", "发表评价"),

    # 消息与会话
    ("POST", r"^/api/messages/read-all$", "全部消息标为已读"),
    # 下面两条现在不记日志（见 SKIP_PATTERNS），名字留给改规则之前写下的旧记录用
    ("PATCH", r"^/api/messages/\d+/read$", "标记消息已读"),
    ("POST", r"^/api/conversations/\d+/read$", "标记会话已读"),
    ("POST", r"^/api/conversations$", "发起会话"),
    ("POST", r"^/api/conversations/\d+/lines$", "发送会话消息"),

    ("POST", r"^/api/notices$", "发布公告"),
    ("PATCH", r"^/api/notices/", "上下架公告"),

    # 中介
    ("PUT", r"^/api/agent/profile$", "修改中介资料"),
    ("POST", r"^/api/agent/license$", "提交中介资质"),
    ("POST", r"^/api/delegations$", "发起房源委托"),
    ("POST", r"^/api/delegations/\d+/end$", "解除房源委托"),
    ("PATCH", r"^/api/delegations/", "处理委托邀请"),
    ("POST", r"^/api/commissions/\d+/settle$", "确认佣金结清"),
    ("POST", r"^/api/commissions/\d+/cancel$", "取消佣金"),
    ("POST", r"^/api/viewings$", "登记带看记录"),

    # 管理员
    ("PATCH", r"^/api/admin/users/", "修改用户状态或角色"),
    # 签发重置码等于临时把账号交出去，必须留痕（B1）
    ("POST", r"^/api/admin/users/\d+/reset-code$", "签发密码重置码"),
    ("POST", r"^/api/admin/role-requests/", "审核身份升级申请"),
    ("POST", r"^/api/admin/agents$", "新建中介账号"),
    ("PATCH", r"^/api/admin/agents/", "审核中介资质"),
    ("PATCH", r"^/api/admin/flags/", "处理待审内容"),
    ("PATCH", r"^/api/admin/config$", "修改系统配置"),
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
    if any(path.startswith(skip) for skip in SKIP_PATHS):
        return False
    return not any(p.match(path) for p in SKIP_PATTERNS)


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
