"""中介：委托、带看、佣金、权限与统计（J 系列）。

设计见 docs/中介功能设计方案.md。

**这份测试的重心是越权。** 方案第 12 节把权限改造标成了唯一的 🔴 风险，
理由是：现有代码把「房东」和「能管这套房的人」当成同一件事，
改动波及 30 多个接口，而**改错的表现不是功能不好用，是安全问题**——
中介能改到别人家的房源，页面上看不出来，只有测试能拦住。

所以 TestAgentCannotOverreach 那一组比功能用例更要紧。
"""

from datetime import datetime, timedelta

from tests.base import ApiTestCase, auth_header, make_admin
import app.db as db
from app.models import (AgentCommission, AgentProfile, Contract, House,
                        HouseDelegation, User, ViewingRecord)


def future(days=7):
    return (datetime.now() + timedelta(days=days)).replace(
        hour=0, minute=0, second=0, microsecond=0)


class BrokerCase(ApiTestCase):
    """一个已核验的中介 + 房东 + 租客 + 管理员。"""

    def setUp(self):
        super().setUp()
        self.landlord = auth_header(self.client, "landlord1", "test123")
        self.tenant = auth_header(self.client, "tenant1", "test123")
        self.admin = make_admin(self.client)
        self.agent = self.make_agent("agent1", approved=True)

    def make_agent(self, username, approved=True, districts="中山区"):
        # 姓名不能带数字（注册接口的校验规则），所以按序号取汉字
        names = "甲乙丙丁戊己庚辛壬癸"
        index = int(username[-1]) if username[-1].isdigit() else 0
        # 直接以 agent 身份注册，走真实入口而不是在库里改 role——
        # 「中介账号根本注册不出来」这种问题，只有测试也走同一条路才会暴露
        created = self.client.post("/api/auth/register", json={
            "username": username, "password": "test123",
            "real_name": "中介" + names[index % len(names)],
            "phone": "13700000001", "role": "agent"})
        assert created.status_code == 200, created.text
        assert created.json()["user"]["role"] == "agent", created.text
        session = db.SessionLocal()
        try:
            user = session.query(User).filter_by(username=username).one()
            profile = AgentProfile(
                user_id=user.id, service_districts=districts,
                license_no="JJ2026001",
                license_status="approved" if approved else "unsubmitted")
            session.add(profile)
            session.commit()
        finally:
            session.close()
        return auth_header(self.client, username, "test123")

    def delegate(self, house_no="DL0001", rate=50, accept=True):
        """房东邀请 → 中介确认，返回委托 id。"""
        created = self.client.post("/api/delegations", headers=self.landlord,
                                   json={"house_no": house_no,
                                         "agent_username": "agent1",
                                         "commission_rate": rate})
        assert created.status_code == 200, created.text
        did = created.json()["delegation"]["id"]
        if accept:
            self.client.patch("/api/delegations/%d" % did,
                              headers=self.agent, json={"accept": True})
        return did


# ------------------------------------------------------------------ 委托

class TestDelegation(BrokerCase):

    def test_landlord_invites_agent_accepts(self):
        did = self.delegate()
        body = self.client.get("/api/delegations",
                               headers=self.agent).json()
        row = next(d for d in body["list"] if d["id"] == did)
        self.assertEqual(row["status"], "active")

    def test_invitation_starts_pending(self):
        """**发起只到 pending。** 单方面就能建立委托，
        等于允许中介给自己派活。"""
        created = self.client.post("/api/delegations", headers=self.landlord,
                                   json={"house_no": "DL0001",
                                         "agent_username": "agent1"})
        self.assertEqual(created.json()["delegation"]["status"], "pending")

    def test_initiator_cannot_confirm_own_invitation(self):
        """发起人自己点同意就是单方面成立，正是双向确认要防的事。"""
        created = self.client.post("/api/delegations", headers=self.landlord,
                                   json={"house_no": "DL0001",
                                         "agent_username": "agent1"})
        did = created.json()["delegation"]["id"]
        response = self.client.patch("/api/delegations/%d" % did,
                                     headers=self.landlord,
                                     json={"accept": True})
        self.assertEqual(response.status_code, 403)

    def test_agent_can_apply_and_landlord_confirms(self):
        """反方向也走得通：中介申请接单，房东点头。"""
        created = self.client.post("/api/delegations", headers=self.agent,
                                   json={"house_no": "DL0002"})
        self.assertEqual(created.status_code, 200, created.text)
        did = created.json()["delegation"]["id"]
        self.assertEqual(created.json()["delegation"]["initiated_by"], "agent")

        ok = self.client.patch("/api/delegations/%d" % did,
                               headers=self.landlord, json={"accept": True})
        self.assertEqual(ok.json()["delegation"]["status"], "active")

    def test_only_one_active_delegation_per_house(self):
        """两个中介同时在托，带看归谁、佣金归谁就说不清了。"""
        self.delegate()
        self.make_agent("agent2")
        again = self.client.post("/api/delegations", headers=self.landlord,
                                 json={"house_no": "DL0001",
                                       "agent_username": "agent2"})
        self.assertEqual(again.status_code, 409)

    def test_unverified_agent_cannot_take_orders(self):
        """未核验一律不许接单。

        一个没验过资质的人代表平台去见租客，风险比一条假房源大得多。
        """
        raw = self.make_agent("agent3", approved=False)
        response = self.client.post("/api/delegations", headers=raw,
                                    json={"house_no": "DL0002"})
        self.assertEqual(response.status_code, 403)

    def test_landlord_cannot_delegate_someone_elses_house(self):
        session = db.SessionLocal()
        try:
            other = User(username="ld2", password_hash="x",
                         real_name="房东乙", role="landlord")
            session.add(other)
            session.commit()
        finally:
            session.close()
        # tenant1 不是任何房源的房东
        response = self.client.post("/api/delegations", headers=self.tenant,
                                    json={"house_no": "DL0001",
                                          "agent_username": "agent1"})
        self.assertEqual(response.status_code, 403)

    def test_ending_requires_a_reason(self):
        did = self.delegate()
        blank = self.client.post("/api/delegations/%d/end" % did,
                                 headers=self.landlord, json={"reason": ""})
        self.assertEqual(blank.status_code, 400)

    def test_either_party_can_end(self):
        did = self.delegate()
        response = self.client.post("/api/delegations/%d/end" % did,
                                    headers=self.agent,
                                    json={"reason": "手上房源太多"})
        self.assertEqual(response.json()["delegation"]["status"], "ended")


# ------------------------------------------------------------------ 权限

class TestAgentCanManageDelegatedHouse(BrokerCase):
    """受托中介管得了那套房——这是委托的意义所在。"""

    def test_can_edit_delegated_house(self):
        self.delegate()
        response = self.client.put("/api/houses/DL0001", headers=self.agent,
                                   json={"description": "中介补充的描述"})
        self.assertEqual(response.status_code, 200, response.text)

    def test_can_change_status(self):
        self.delegate()
        response = self.client.patch("/api/houses/DL0001/status?status=offline",
                                     headers=self.agent)
        self.assertEqual(response.status_code, 200, response.text)

    def test_can_request_verification(self):
        """房东可以把「提交核验材料」也一并委托出去。"""
        self.delegate()
        response = self.client.post("/api/houses/DL0001/verify-request",
                                    headers=self.agent)
        self.assertIn(response.status_code, (200, 409))


class TestAgentCannotOverreach(BrokerCase):
    """**越权用例。这一组比功能用例更要紧。**

    权限改造把 30 多个接口的角色门放宽到了中介，如果房源级的第二道门
    漏了一处，中介就能改到别人家的房源——而页面上看不出来。
    """

    def test_cannot_touch_a_house_not_delegated(self):
        self.delegate("DL0001")
        response = self.client.put("/api/houses/DL0002", headers=self.agent,
                                   json={"description": "不该改得动"})
        self.assertEqual(response.status_code, 403)
        self.assertIn("受托范围", response.json()["detail"])

    def test_permission_is_revoked_the_moment_delegation_ends(self):
        did = self.delegate()
        self.client.post("/api/delegations/%d/end" % did,
                         headers=self.landlord, json={"reason": "自己带看"})
        response = self.client.put("/api/houses/DL0001", headers=self.agent,
                                   json={"description": "解除之后不该改得动"})
        self.assertEqual(response.status_code, 403)

    def test_pending_delegation_grants_nothing(self):
        """没确认的委托不给任何权限。"""
        self.client.post("/api/delegations", headers=self.landlord,
                         json={"house_no": "DL0001",
                               "agent_username": "agent1"})
        response = self.client.put("/api/houses/DL0001", headers=self.agent,
                                   json={"description": "还没确认"})
        self.assertEqual(response.status_code, 403)

    def test_agent_cannot_publish_a_house(self):
        """中介不能凭空发房源——房源归属必须有唯一源头。"""
        response = self.client.post("/api/houses", headers=self.agent, json={
            "title": "中介发的房源", "district": "中山区",
            "rent_price": 2600, "area": 70})
        self.assertEqual(response.status_code, 403)

    def test_agent_cannot_sign_a_contract(self):
        """签约是钱和法律责任，必须房东本人点。"""
        self.delegate()
        response = self.client.post("/api/contracts", headers=self.agent, json={
            "house_no": "DL0001", "tenant_username": "tenant1",
            "start_date": future().isoformat(), "months": 12})
        self.assertEqual(response.status_code, 403)

    def test_landlord_keeps_control_during_delegation(self):
        """**房东的权限不因委托而丧失。**

        委托是「多一个人能管」，不是「换人管」。反过来做是把工具
        做成了枷锁。
        """
        self.delegate()
        response = self.client.put("/api/houses/DL0001",
                                   headers=self.landlord,
                                   json={"description": "房东自己还能改"})
        self.assertEqual(response.status_code, 200, response.text)

    def test_tenant_still_blocked(self):
        """放宽角色门之后，租客不能跟着溜进来。"""
        response = self.client.put("/api/houses/DL0001", headers=self.tenant,
                                   json={"description": "租客不该改得动"})
        self.assertEqual(response.status_code, 403)


# ------------------------------------------------------------------ 带看

class TestViewingRecords(BrokerCase):

    def record(self, headers=None, **extra):
        body = {"house_no": "DL0001", "tenant_username": "tenant1",
                "showed_up": True, "duration_minutes": 25,
                "feedback": "嫌厨房小，问能不能养猫",
                "concerns": ["厨房面积"], "intention": "medium"}
        body.update(extra)
        return self.client.post("/api/viewings", headers=headers or self.agent,
                                json=body)

    def test_agent_records_a_viewing(self):
        self.delegate()
        response = self.record()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["viewing"]["concerns"], ["厨房面积"])

    def test_cannot_record_for_a_house_not_delegated(self):
        response = self.record()
        self.assertEqual(response.status_code, 403)

    def test_landlord_gets_the_summary(self):
        """带看纪要回流给房东——这是他委托出去之后唯一想知道的事。"""
        self.delegate()
        self.record(intention="high")
        messages = self.client.get("/api/messages",
                                   headers=self.landlord).json()
        titles = [m["title"] for m in messages["list"]]
        self.assertTrue(any("有一次带看" in t for t in titles), titles)

    def test_landlord_sees_records_for_own_house(self):
        self.delegate()
        self.record()
        body = self.client.get("/api/viewings", headers=self.landlord).json()
        self.assertEqual(body["total"], 1)

    def test_tenant_name_is_masked(self):
        """带看记录会被房东看到，租客姓名只露姓氏。"""
        self.delegate()
        self.record()
        body = self.client.get("/api/viewings", headers=self.agent).json()
        self.assertNotIn("租客甲", body["list"][0]["tenant_name"])
        self.assertTrue(body["list"][0]["tenant_name"].startswith("租"))

    def test_bad_intention_rejected(self):
        self.delegate()
        self.assertEqual(self.record(intention="随便").status_code, 422)


# ------------------------------------------------------------------ 佣金

class TestCommission(BrokerCase):

    def sign_and_activate(self):
        signed = self.client.post("/api/contracts", headers=self.landlord,
                                  json={"house_no": "DL0001",
                                        "tenant_username": "tenant1",
                                        "start_date": future().isoformat(),
                                        "months": 12})
        no = signed.json()["contract"]["contract_no"]
        self.client.post("/api/contracts/%s/activate" % no,
                         headers=self.tenant)
        return no

    def test_commission_created_on_activation(self):
        self.delegate(rate=50)
        self.sign_and_activate()
        body = self.client.get("/api/agent/commissions",
                               headers=self.agent).json()
        self.assertEqual(body["total"], 1)
        row = body["list"][0]
        # DL0001 月租 2800，比例 50% → 1400
        self.assertEqual(row["amount"], 1400.0)
        self.assertEqual(row["status"], "pending")

    def test_delegation_ends_when_the_house_is_let(self):
        did = self.delegate()
        self.sign_and_activate()
        body = self.client.get("/api/delegations", headers=self.agent).json()
        row = next(d for d in body["list"] if d["id"] == did)
        self.assertEqual(row["status"], "ended")

    def test_no_commission_without_delegation(self):
        """房东自管的房源签约不产生佣金。"""
        self.sign_and_activate()
        body = self.client.get("/api/agent/commissions",
                               headers=self.agent).json()
        self.assertEqual(body["total"], 0)

    def test_base_and_rate_are_kept(self):
        """基数和比例分开存——只留最终金额，事后就没法核对怎么算的。"""
        self.delegate(rate=30)
        self.sign_and_activate()
        row = self.client.get("/api/agent/commissions",
                              headers=self.agent).json()["list"][0]
        self.assertEqual(row["base_amount"], 2800.0)
        self.assertEqual(row["rate"], 30.0)
        self.assertEqual(row["amount"], 840.0)


# ------------------------------------------------------------------ 统计

class TestStats(BrokerCase):

    def test_dashboard_returns_everything_at_once(self):
        """一个页面一个接口。十个卡片打十次请求会把首屏拖慢。"""
        body = self.client.get("/api/agent/dashboard",
                               headers=self.agent).json()
        for key in ("profile", "stats", "todo", "delegations"):
            self.assertIn(key, body)

    def test_ratios_carry_their_denominator(self):
        """**比率必须带分母。**

        「成交率 25%」在样本只有 4 次带看时是误导。
        """
        stats = self.client.get("/api/agent/dashboard",
                                headers=self.agent).json()["stats"]
        for key in ("conversion", "no_show", "praise"):
            self.assertIn("rate", stats[key])
            self.assertIn("hit", stats[key])
            self.assertIn("total", stats[key])

    def test_no_show_is_separate_from_conversion(self):
        """放鸽子率要能和成交率分开看——前者高通常说明预约确认
        流程有问题，不是中介的服务质量问题。"""
        self.delegate()
        self.client.post("/api/viewings", headers=self.agent, json={
            "house_no": "DL0001", "tenant_username": "tenant1",
            "showed_up": False, "intention": "low"})
        stats = self.client.get("/api/agent/dashboard",
                                headers=self.agent).json()["stats"]
        self.assertEqual(stats["no_show"]["hit"], 1)
        self.assertEqual(stats["no_show"]["total"], 1)

    def test_stats_are_aggregated_not_stored(self):
        """**一个统计字段都不存**（R-05）。

        中介档案里没有任何可写的统计列，想改也无从改起。
        """
        response = self.client.put("/api/agent/profile", headers=self.agent,
                                   json={"years": 5,
                                         "deal_total": 999,
                                         "recent_viewings": 999})
        self.assertEqual(response.status_code, 200)
        stats = self.client.get("/api/agent/dashboard",
                                headers=self.agent).json()["stats"]
        self.assertEqual(stats["deal_total"], 0)
        self.assertEqual(stats["recent_viewings"], 0)

    def test_active_count_tracks_delegations(self):
        self.delegate()
        stats = self.client.get("/api/agent/dashboard",
                                headers=self.agent).json()["stats"]
        self.assertEqual(stats["active_delegations"], 1)

    def test_dashboard_lists_invitations_to_confirm(self):
        self.client.post("/api/delegations", headers=self.landlord,
                         json={"house_no": "DL0001",
                               "agent_username": "agent1"})
        todo = self.client.get("/api/agent/dashboard",
                               headers=self.agent).json()["todo"]
        self.assertEqual(len(todo["invitations"]), 1)


# ------------------------------------------------------------------ 管理端

class TestAdminGovernance(BrokerCase):

    def test_license_queue_and_review(self):
        raw = self.make_agent("agent9", approved=False)
        self.client.post("/api/agent/license", headers=raw,
                         json={"license_no": "JJ2026999"})

        queue = self.client.get("/api/admin/agent-queue",
                                headers=self.admin).json()
        self.assertEqual(queue["total"], 1)

        session = db.SessionLocal()
        try:
            uid = session.query(User).filter_by(username="agent9").one().id
        finally:
            session.close()

        ok = self.client.patch("/api/admin/agents/%d" % uid,
                               headers=self.admin,
                               json={"approved": True})
        self.assertEqual(ok.json()["profile"]["license_status"], "approved")

    def test_rejection_requires_a_reason(self):
        """「未通过」三个字对被驳回的人毫无意义，他不知道该补什么。"""
        raw = self.make_agent("agent8", approved=False)
        self.client.post("/api/agent/license", headers=raw,
                         json={"license_no": "JJ2026888"})
        session = db.SessionLocal()
        try:
            uid = session.query(User).filter_by(username="agent8").one().id
        finally:
            session.close()
        response = self.client.patch("/api/admin/agents/%d" % uid,
                                     headers=self.admin,
                                     json={"approved": False, "reason": ""})
        self.assertEqual(response.status_code, 400)

    def test_agent_roster_carries_the_same_stats(self):
        """管理端和中介自己看到的是同一份计算。

        各算各的话，被问「工作台说 12 次、后台说 9 次，哪个对」
        就没法回答了。
        """
        self.delegate()
        mine = self.client.get("/api/agent/dashboard",
                               headers=self.agent).json()["stats"]
        roster = self.client.get("/api/admin/agents",
                                 headers=self.admin).json()
        row = next(r for r in roster["list"] if r["username"] == "agent1")
        self.assertEqual(row["stats"]["active_delegations"],
                         mine["active_delegations"])

    def test_roster_shows_reports(self):
        """这一页要回答「这个中介靠不靠谱」，被举报记录必须在这里。"""
        roster = self.client.get("/api/admin/agents",
                                 headers=self.admin).json()
        row = next(r for r in roster["list"] if r["username"] == "agent1")
        self.assertIn("reported", row)

    def test_platform_stats_compare_delegated_against_self_managed(self):
        """**唯一能回答「中介到底有没有用」的数字。**

        如果委托房源的成交率反而更低，就该如实显示——看板不是
        用来证明功能有价值的，是用来发现它没价值的。
        """
        body = self.client.get("/api/admin/agent-stats",
                               headers=self.admin).json()
        self.assertIn("deals_with_agent", body)
        self.assertIn("deals_self_managed", body)
        self.assertIn("delegated_share", body)

    def test_only_admin(self):
        for path in ("/api/admin/agent-queue", "/api/admin/agents",
                     "/api/admin/agent-stats"):
            self.assertEqual(
                self.client.get(path, headers=self.agent).status_code, 403)


# ---------------------------------------------------------- 角色入口（J-01）

class TestAgentRole(BrokerCase):
    """中介这个角色本身的出入口。

    这一组测的不是功能，是**功能够不够得着**。后端做得再全，
    如果没有任何途径让一个账号变成 agent，整套中介功能在系统里
    就是一段谁也走不到的代码。
    """

    def test_can_register_as_agent(self):
        """注册时能直接选经纪人。"""
        body = self.client.post("/api/auth/register", json={
            "username": "broker9", "password": "test123",
            "real_name": "中介玖", "phone": "13700000009",
            "role": "agent"}).json()
        self.assertEqual(body["user"]["role"], "agent")

    def test_fresh_agent_can_do_nothing_until_approved(self):
        """**注册出来的中介是个空壳。**

        自助注册之所以安全，全靠这一条：role 本身不给任何权限，
        接单要等资质审核通过。如果哪天有人放松了这里，
        这条用例应当立刻变红。
        """
        self.client.post("/api/auth/register", json={
            "username": "broker8", "password": "test123",
            "real_name": "中介捌", "phone": "13700000008", "role": "agent"})
        fresh = auth_header(self.client, "broker8", "test123")

        # 能看自己的工作台（要在上面提交资质），但接不了单
        dash = self.client.get("/api/agent/dashboard", headers=fresh)
        self.assertEqual(dash.status_code, 200)
        self.assertFalse(dash.json()["profile"]["can_take_orders"])

        applied = self.client.post("/api/delegations", headers=fresh,
                                   json={"house_no": "DL0001"})
        self.assertEqual(applied.status_code, 403)

    def test_agent_cannot_flip_to_landlord(self):
        """**中介不能一键转房东。**

        角色一旦变成 landlord，他手上在托房源的管理权立刻全部消失
        （`can_manage_house` 要求 role == agent），而页面上不会有任何提示，
        房东那边也不知道自己的委托已经失效。这是一次需要本人确认的迁移，
        不该由「升级为房东」按钮顺手完成。
        """
        self.delegate("DL0001")
        blocked = self.client.post("/api/auth/become-landlord",
                                   headers=self.agent)
        self.assertEqual(blocked.status_code, 400)
        self.assertIn("解除", blocked.json()["detail"])

        # 委托仍然生效，权限没被这次尝试改坏
        rows = self.client.get("/api/delegations?role=agent",
                               headers=self.agent).json()
        self.assertEqual(rows["list"][0]["status"], "active")

    def test_eligibility_marks_agent(self):
        """前端据此把「成为房东」入口整个藏掉，而不是给一个点了会报错的按钮。"""
        body = self.client.get("/api/auth/landlord-eligibility",
                               headers=self.agent).json()
        self.assertTrue(body["is_agent"])
        self.assertFalse(body["can_upgrade"])


class TestDashboardTodo(BrokerCase):
    """工作台首屏那几个数字的口径。"""

    def _appoint(self, house_no="DL0001", days=0, hours=3):
        when = (datetime.now() + timedelta(days=days, hours=hours))
        created = self.client.post("/api/appointments", headers=self.tenant,
                                   json={"house_no": house_no,
                                         "viewing_time": when.isoformat(),
                                         "contact_name": "租客甲",
                                         "contact_phone": "13800000001"})
        assert created.status_code == 200, created.text
        return created.json()

    def test_today_viewings_only_counts_confirmed(self):
        """**未确认的预约不算「今日带看」。**

        算进去的话，中介照着一个虚高的数字安排一天行程，
        到了地方才发现这场约根本没成立。
        """
        self.delegate("DL0001")
        self._appoint("DL0001")          # 停在 pending

        todo = self.client.get("/api/agent/dashboard",
                               headers=self.agent).json()["todo"]
        self.assertEqual(len(todo["today_viewings"]), 0)
        # 但它不能就此消失——受托期间这些预约本来就该由中介处理
        self.assertEqual(len(todo["pending_appointments"]), 1)

    def test_confirmed_appointment_shows_up(self):
        self.delegate("DL0001")
        appointment = self._appoint("DL0001")
        aid = appointment["appointment"]["id"] if "appointment" in appointment \
            else appointment["id"]
        replied = self.client.patch(
            "/api/appointments/%d" % aid, headers=self.landlord,
            json={"status": "confirmed", "reply": "到时候见"})
        self.assertEqual(replied.status_code, 200, replied.text)

        todo = self.client.get("/api/agent/dashboard",
                               headers=self.agent).json()["todo"]
        self.assertEqual(len(todo["today_viewings"]), 1)
        self.assertEqual(len(todo["pending_appointments"]), 0)
