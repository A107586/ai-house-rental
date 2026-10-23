"""助手的身份切换（2026-10-22）。

原先工具表按账号角色给：房东账号拿到的是租客和房东两套工具混在一起，
「确认预约」和「约看房」、「我的租约」和「我租出去的合同」并排摆着，模型常挑错；
中介、管理员也拿得到租客的工具；助手里还没有「发起签约」——房东签约用不了助手。

现在按**身份**给：房东账号在助手顶部切「房东 / 租客」，中介、管理员只有本职身份。
"""

import unittest
from types import SimpleNamespace

from app.agents.assistant.toolkit import Toolkit
from app.agents.support.tools import actions, greeting
from app.agents.toolbox import catalog
from tests.test_actions import RecordingBackend


def names(identity, logged_in=True):
    return {t["name"] for t in catalog.visible_tools(logged_in, identity)}


def kit(role, identity=""):
    return Toolkit(SimpleNamespace(logged_in=True, role=role, identity=identity,
                                   runner_ctx={}))


class TestResolveIdentity(unittest.TestCase):

    def test_landlord_account_can_switch(self):
        self.assertEqual(catalog.resolve_identity("landlord"), "landlord")
        self.assertEqual(catalog.resolve_identity("landlord", "tenant"), "tenant")

    def test_asking_for_an_identity_you_do_not_hold_falls_back(self):
        # 前端传什么都不作数，账号角色才作数
        self.assertEqual(catalog.resolve_identity("tenant", "landlord"), "tenant")
        self.assertEqual(catalog.resolve_identity("agent", "tenant"), "agent")
        self.assertEqual(catalog.resolve_identity("admin", "tenant"), "admin")
        self.assertEqual(catalog.resolve_identity("", "admin"), "tenant")


class TestToolsPerIdentity(unittest.TestCase):

    def test_landlord_identity_has_no_tenant_errands(self):
        got = names("landlord")
        self.assertIn("handle_appointment", got)
        self.assertIn("landlord_houses", got)
        for tenant_tool in ("book_viewing", "pay_bill", "my_contracts", "favorite_house"):
            self.assertNotIn(tenant_tool, got)

    def test_tenant_identity_has_no_landlord_tools(self):
        got = names("tenant")
        self.assertIn("book_viewing", got)
        self.assertNotIn("landlord_houses", got)
        self.assertNotIn("handle_appointment", got)

    def test_agent_and_admin_do_not_act_as_tenants(self):
        for identity in ("agent", "admin"):
            got = names(identity)
            for tenant_tool in ("book_viewing", "pay_bill", "my_bills", "recommend_houses"):
                self.assertNotIn(tenant_tool, got, identity)
            self.assertIn("search_houses", got, identity)
        self.assertIn("agent_commissions", names("agent"))
        self.assertIn("admin_todos", names("admin"))
        self.assertNotIn("landlord_houses", names("admin"))

    def test_logged_out_still_browses(self):
        got = names("", logged_in=False)
        self.assertIn("search_houses", got)
        self.assertNotIn("pay_bill", got)


class TestToolkitByIdentity(unittest.TestCase):

    def test_sign_contract_only_in_landlord_identity(self):
        landlord = {t["name"]: t for t in kit("landlord", "landlord").tools()}
        self.assertIn("sign_contract", landlord)
        self.assertIn("tenant_username", landlord["sign_contract"]["params"])
        self.assertIn("request", landlord["sign_contract"]["params"])
        self.assertNotIn("sign_contract", {t["name"] for t in kit("landlord", "tenant").tools()})
        self.assertNotIn("sign_contract", {t["name"] for t in kit("tenant").tools()})

    def test_missing_identity_defaults_from_role(self):
        got = {t["name"] for t in kit("landlord").tools()}
        self.assertIn("sign_contract", got)
        self.assertNotIn("pay_bill", got)

    def test_landlord_is_told_where_to_switch(self):
        hint = kit("landlord", "landlord").why_unavailable("book_viewing")
        self.assertIn("切到「租客」", hint)
        hint = kit("landlord", "tenant").why_unavailable("sign_contract")
        self.assertIn("切到「房东」", hint)

    def test_agent_is_told_it_is_not_their_job(self):
        hint = kit("agent").why_unavailable("pay_bill")
        self.assertIn("中介只做经纪业务", hint)
        self.assertNotIn("切到", hint)


class TestSignContractForm(unittest.TestCase):
    """模型直接给的槽位值也要校验——原先「十二个月」原样进卡片，点确认才炸。"""

    HOUSES = {"/api/auth/me": {"id": 2, "role": "landlord"},
              "/api/landlord/houses": {"list": [
                  {"house_no": "DL0101", "title": "中山区人民路 三室", "status": "available"}]}}

    def propose(self, **context):
        return actions.propose("sign_contract", "帮我发起签约", "tok",
                               RecordingBackend(self.HOUSES), context=context)

    def test_bad_value_is_asked_again(self):
        status, _, _, ask = self.propose(house_no="DL0101", tenant_username="tenant1",
                                         start_date="2026-11-01", months="十二个月")
        self.assertEqual(status, actions.NEED_SLOT)
        self.assertEqual(ask["slot"], "months")

    def test_agreed_rent_rides_along_without_being_asked(self):
        status, _, card, _ = self.propose(house_no="DL0101", tenant_username="tenant1",
                                          start_date="2026/11/1", months="12", rent="2800")
        self.assertEqual(status, actions.CONFIRM)
        self.assertEqual(card["params"]["start_date"], "2026-11-01")
        self.assertEqual(card["params"]["rent"], 2800)
        self.assertIn("月租 2800", card["summary"])

    def test_rent_reaches_the_backend(self):
        backend = RecordingBackend()
        actions.EXECUTORS["sign_contract"](
            {"house_no": "DL0101", "tenant_username": "tenant1",
             "start_date": "2026-11-01", "months": 12, "rent": 2800}, "tok", backend)
        self.assertEqual(backend.writes[0][2]["rent"], 2800)


class TestGreetingByIdentity(unittest.TestCase):
    """开场白跟着身份换。原先房东点开助手看到的是「交最近一笔房租」。"""

    def test_landlord_opening_lists_landlord_todos(self):
        backend = RecordingBackend({
            "/api/appointments/mine": {"list": [{"status": "pending"}, {"status": "confirmed"}]},
            "/api/messages": {"unread": 0}})
        got = greeting.build(backend, "tok", identity="landlord")
        self.assertIn("房东身份", got["message"])
        self.assertIn("1 条看房预约等你确认", got["message"])
        self.assertIn("帮我发起签约", got["chips"])
        self.assertNotIn("交最近一笔房租", got["chips"])

    def test_agent_and_admin_have_their_own_opening(self):
        self.assertIn("我的佣金", greeting.build(RecordingBackend(), "tok", identity="agent")["chips"])
        self.assertIn("平台有哪些待办",
                      greeting.build(RecordingBackend(), "tok", identity="admin")["chips"])

    def test_tenant_opening_unchanged(self):
        backend = RecordingBackend({"/api/messages": {"unread": 2}})
        got = greeting.build(backend, "tok")
        self.assertIn("2 条未读消息", got["message"])
        self.assertNotIn("房东身份", got["message"])


if __name__ == "__main__":
    unittest.main()
