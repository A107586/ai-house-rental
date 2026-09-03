"""通知触达（待完善清单 C-2）。

原先系统只有站内信。**租金到期不发短信等于没催；看房前一小时不提醒，
大概率放鸽子。** 真实系统里触达是运营命脉，而站内信只有用户主动登录
才看得到。

这里把通知抽象成「通道」，站内信是其中一条，邮件是新加的：

* ``InAppNotifier``  —— 写 messages 表，已有
* ``EmailNotifier``  —— 标准库 smtplib，零新增依赖
* 短信 —— **没做**。需要实名认证的服务商账号，学生拿不到；
  但通道接口已经留好，接上只是加一个类

**为什么邮件失败不能影响主流程**：合同生效、账单生成这些动作已经落库了，
邮件发不出去不该让整个请求回滚——用户会以为操作失败又点一遍。
所以 send 一律吞掉异常，只返回成功与否。

未配置 SMTP 时 EmailNotifier 直接跳过，不报错也不重试。这是默认状态：
项目开箱即用不该要求先去申请一个邮箱。
"""

import os
import smtplib
import ssl
from email.message import EmailMessage

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT") or 465)
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("SMTP_FROM") or SMTP_USER
# 超时必须给，否则 SMTP 连不通时会一直挂着，把整个线程占死
SMTP_TIMEOUT = float(os.environ.get("SMTP_TIMEOUT") or 8)


def email_configured():
    return bool(SMTP_HOST and SMTP_USER and SMTP_PASSWORD)


class Notifier:
    """通道接口。"""

    name = "base"

    def available(self):
        raise NotImplementedError

    def send(self, user, title, content):
        raise NotImplementedError


class EmailNotifier(Notifier):
    """邮件通道。"""

    name = "email"

    def available(self):
        return email_configured()

    def send(self, user, title, content):
        """发一封邮件。**任何失败都只返回 False，不抛异常。**"""
        if not self.available():
            return False
        address = (getattr(user, "email", "") or "").strip()
        if "@" not in address:
            return False

        message = EmailMessage()
        message["Subject"] = title
        message["From"] = SMTP_FROM
        message["To"] = address
        message.set_content(
            "%s\n\n%s\n\n——\n本邮件由系统自动发送，请勿直接回复。"
            % (title, content))

        try:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT,
                                  context=context) as server:
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.send_message(message)
            return True
        except Exception:
            # 通道故障不该拖垮业务：合同已经生效、账单已经生成，
            # 邮件没发出去不能让整个请求回滚
            return False


_email = EmailNotifier()


def notify_external(user, title, content):
    """把一条通知推到所有可用的外部通道。

    站内信不在这里——它由 leasing.notify 直接写库，是**必达**的那一条；
    外部通道是尽力而为的补充。两者的失败语义不同，混在一起会让
    「通知发出去了吗」这个问题变得没法回答。
    """
    sent = []
    if _email.available() and _email.send(user, title, content):
        sent.append(_email.name)
    return sent
