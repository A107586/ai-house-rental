"""通知触达测试（待完善清单 C-2）。

原先系统只有站内信——租金到期不发邮件等于没催，而站内信只有用户主动
登录才看得到。

这批用例守的是**失败语义**：站内信必达，外部通道尽力而为。
两者混在一起会让「通知发出去了吗」这个问题变得没法回答。
"""

from unittest import mock

from app import leasing, notifier
from tests.base import ApiTestCase, auth_header


class TestEmailNotifier(ApiTestCase):

    def test_skipped_when_not_configured(self):
        """未配置 SMTP 时直接跳过——项目开箱即用不该要求先去申请邮箱。"""
        with mock.patch.object(notifier, "SMTP_HOST", ""):
            self.assertFalse(notifier.EmailNotifier().available())

    def test_available_when_configured(self):
        with mock.patch.multiple(notifier, SMTP_HOST="smtp.example.com",
                                 SMTP_USER="a@example.com",
                                 SMTP_PASSWORD="secret"):
            self.assertTrue(notifier.EmailNotifier().available())

    def test_no_address_no_send(self):
        with mock.patch.multiple(notifier, SMTP_HOST="smtp.example.com",
                                 SMTP_USER="a@example.com",
                                 SMTP_PASSWORD="secret"):
            class Anon:
                email = ""
            self.assertFalse(
                notifier.EmailNotifier().send(Anon(), "标题", "正文"))

    def test_smtp_failure_is_swallowed(self):
        """**通道故障不该拖垮业务。** 合同已经生效、账单已经生成，
        邮件发不出去不能让整个请求回滚。"""
        with mock.patch.multiple(notifier, SMTP_HOST="smtp.example.com",
                                 SMTP_USER="a@example.com",
                                 SMTP_PASSWORD="secret"):
            with mock.patch("smtplib.SMTP_SSL",
                            side_effect=OSError("连不上")):
                class Someone:
                    email = "who@example.com"
                # 不抛异常，只返回 False
                self.assertFalse(
                    notifier.EmailNotifier().send(Someone(), "标题", "正文"))


class TestNotifyIntegration(ApiTestCase):

    def setUp(self):
        super().setUp()
        self.tenant = auth_header(self.client, "tenant1", "test123")

    def test_inbox_message_is_always_written(self):
        """站内信是必达的那一条，与外部通道成不成功无关。"""
        import app.db as db
        from app.models import Message, User
        session = db.SessionLocal()
        try:
            user = session.query(User).filter_by(username="tenant1").one()
            with mock.patch.object(notifier, "notify_external",
                                   side_effect=RuntimeError("邮件炸了")):
                try:
                    leasing.notify(session, user.id, "bill", "账单到期",
                                   "请及时缴纳")
                except RuntimeError:
                    pass
            session.commit()
            rows = session.query(Message).filter_by(user_id=user.id).all()
            self.assertTrue(any(m.title == "账单到期" for m in rows))
        finally:
            session.close()

    def test_only_important_types_go_external(self):
        """**不是所有站内信都值得发邮件。** 每条都推，用户很快会把整个
        域名拉黑，真正要紧的那封反而看不见了。"""
        self.assertIn("bill", leasing.EXTERNAL_TYPES)
        self.assertIn("contract", leasing.EXTERNAL_TYPES)
        self.assertIn("repair", leasing.EXTERNAL_TYPES)
        self.assertNotIn("system", leasing.EXTERNAL_TYPES)

    def test_system_notice_does_not_trigger_email(self):
        import app.db as db
        from app.models import User
        session = db.SessionLocal()
        try:
            user = session.query(User).filter_by(username="tenant1").one()
            with mock.patch.object(notifier, "notify_external") as spy:
                leasing.notify(session, user.id, "system", "有人给你发消息",
                               "内容")
                session.commit()
                spy.assert_not_called()
        finally:
            session.close()

    def test_bill_notice_triggers_external(self):
        import app.db as db
        from app.models import User
        session = db.SessionLocal()
        try:
            user = session.query(User).filter_by(username="tenant1").one()
            with mock.patch.object(notifier, "notify_external") as spy:
                leasing.notify(session, user.id, "bill", "账单到期", "请缴纳")
                session.commit()
                spy.assert_called_once()
        finally:
            session.close()
