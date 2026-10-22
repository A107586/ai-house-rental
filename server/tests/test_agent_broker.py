"""中介：委托、带看、佣金、权限与统计（J 系列）。

设计见 docs/中介功能设计方案.md。

**这份测试的重心是越权。** 方案第 12 节把权限改造标成了唯一的 🔴 风险，
理由是：现有代码把「房东」和「能管这套房的人」当成同一件事，
改动波及 30 多个接口，而**改错的表现不是功能不好用，是安全问题**——
中介能改到别人家的房源，页面上看不出来，只有测试能拦住。

所以 TestAgentCannotOverreach 那一组比功能用例更要紧。
"""

from datetime import datetime, timedelta

from tests.base import ApiTestCase, auth_header, make_admin, make_resident
import app.db as db
from app.models import (AgentCommission, AgentProfile, Appointment, Contract,
                        House, HouseDelegation, User, ViewingRecord)


def future(days=7):
    return (datetime.now() + timedelta(days=days)).replace(
        hour=0, minute=0, second=0, microsecond=0)


def slot(days=1):
    """几天后上午 10 点。看房只能约 9:00–18:00，用「现在 + N 天」的话
    傍晚和清早跑测试会落在时段外面，按时钟变脸。"""
    return (datetime.now() + timedelta(days=days)).replace(
        hour=10, minute=0, second=0, microsecond=0)


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

    def test_other_pending_applications_are_closed_when_one_is_accepted(self):
        """一套房只能有一条生效委托：别的中介还在等的申请原先永远「待确认」。"""
        other = self.make_agent("agent2")
        applied = self.client.post("/api/delegations", headers=other,
                                   json={"house_no": "DL0001", "commission_rate": 40})
        self.assertEqual(applied.status_code, 200, applied.text)
        self.delegate()
        row = next(d for d in self.client.get("/api/delegations", headers=other).json()["list"]
                   if d["id"] == applied.json()["delegation"]["id"])
        self.assertEqual(row["status"], "rejected")
        notes = self.client.get("/api/messages", headers=other).json()["list"]
        self.assertTrue(any(m["title"] == "委托未成" for m in notes))

    def test_house_detail_says_who_shows_the_flat(self):
        """租客问「这套有中介带看吗」，助手要从房源详情里拿到答案，
        而不是去翻平台规则答「没查到」。只有生效中的委托才算。"""
        house = self.client.get("/api/houses/DL0001").json()
        self.assertEqual((house["viewing_by"], house["viewing_agent"]), ("landlord", ""))
        did = self.delegate(accept=False)
        self.assertEqual(self.client.get("/api/houses/DL0001").json()["viewing_by"], "landlord")
        self.client.patch("/api/delegations/%d" % did, headers=self.agent, json={"accept": True})
        house = self.client.get("/api/houses/DL0001").json()
        self.assertEqual(house["viewing_by"], "agent")
        self.assertEqual(house["viewing_agent"], "中**")         # 「中介甲」只露姓

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

    def test_landlord_settles_and_the_agent_sees_it(self):
        """原先佣金记下之后再没有任何一处改它的状态：「已结算」永远是 0。"""
        self.delegate(rate=50)
        self.sign_and_activate()
        owed = self.client.get("/api/landlord/commissions", headers=self.landlord).json()
        self.assertEqual(owed["pending_amount"], 1400.0)
        cid = owed["list"][0]["id"]

        # 中介自己点「已结清」不算数
        self.assertEqual(self.client.post("/api/commissions/%d/settle" % cid,
                                          headers=self.agent).status_code, 403)
        done = self.client.post("/api/commissions/%d/settle" % cid, headers=self.landlord)
        self.assertEqual(done.status_code, 200, done.text)
        self.assertEqual(self.client.post("/api/commissions/%d/settle" % cid,
                                          headers=self.landlord).status_code, 409)
        mine = self.client.get("/api/agent/commissions", headers=self.agent).json()
        self.assertEqual((mine["pending_amount"], mine["settled_amount"]), (0, 1400.0))
        notes = self.client.get("/api/messages", headers=self.agent).json()["list"]
        self.assertTrue(any(m["title"] == "佣金已结清" for m in notes))

    def test_only_admin_cancels_and_needs_a_reason(self):
        self.delegate()
        self.sign_and_activate()
        cid = self.client.get("/api/landlord/commissions",
                              headers=self.landlord).json()["list"][0]["id"]
        self.assertEqual(self.client.post("/api/commissions/%d/cancel" % cid, headers=self.landlord,
                                          json={"reason": "不想给"}).status_code, 403)
        self.assertEqual(self.client.post("/api/commissions/%d/cancel" % cid, headers=self.admin,
                                          json={"reason": ""}).status_code, 400)
        done = self.client.post("/api/commissions/%d/cancel" % cid, headers=self.admin,
                                json={"reason": "成交后三天因中介隐瞒问题退租"})
        self.assertEqual(done.json()["commission"]["status"], "cancelled")

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

    def _appoint(self, house_no="DL0001", at_hour=12, confirm=False):
        """建一条预约，并把看房时间钉在**今天的某个整点**上。

        为什么要钉：接口要求 `viewing_time > now`，所以只能传一个未来时间；
        而「今日带看」判定的是「落在今天」。傍晚跑测试时 `now + 3h`
        就跨到明天去了，这条用例会在下午之前一直绿、入夜之后突然红——
        **这种按时钟变脸的用例比没有用例更糟**，它会让人以为是代码坏了。
        （本次就是在 23:38 跑全量时才炸出来的。）

        所以：先用一个必然合法的未来时间把记录建出来，
        再直接改库把时间挪到今天的整点。
        """
        when = slot(1)
        created = self.client.post("/api/appointments", headers=self.tenant,
                                   json={"house_no": house_no,
                                         "viewing_time": when.isoformat(),
                                         "contact_name": "租客甲",
                                         "contact_phone": "13800000001"})
        assert created.status_code == 200, created.text
        body = created.json()
        aid = body["appointment"]["id"]
        if confirm:
            # 先确认再挪：钉到今天的整点之后，下午跑的话那个点已经过了，不能再确认
            replied = self.client.patch(
                "/api/appointments/%d" % aid, headers=self.landlord,
                json={"status": "confirmed", "reply": "到时候见"})
            assert replied.status_code == 200, replied.text

        session = db.SessionLocal()
        try:
            row = session.get(Appointment, aid)
            row.viewing_time = datetime.now().replace(
                hour=at_hour, minute=0, second=0, microsecond=0)
            session.commit()
        finally:
            session.close()
        return body

    def test_today_viewings_only_counts_confirmed(self):
        """**未确认的预约不算「今日带看」。**

        算进去的话，中介照着一个虚高的数字安排一天行程，
        到了地方才发现这场约根本没成立。
        """
        self.delegate("DL0001")
        # 停在 pending，时间是**今天、还没到**：过了点的待确认已经确认不了，不算待办。
        # 钉在今天 12 点的话，下午跑就成了「已过点」——又一条按时钟变脸的用例
        soon = datetime.now() + timedelta(minutes=5)
        if soon.date() != datetime.now().date():
            self.skipTest("离午夜不到 5 分钟，凑不出「今天还没到」的时刻")
        aid = self._appoint("DL0001")["appointment"]["id"]
        session = db.SessionLocal()
        try:
            session.get(Appointment, aid).viewing_time = soon
            session.commit()
        finally:
            session.close()

        todo = self.client.get("/api/agent/dashboard",
                               headers=self.agent).json()["todo"]
        self.assertEqual(len(todo["today_viewings"]), 0)
        # 但它不能就此消失——受托期间这些预约本来就该由中介处理
        self.assertEqual(len(todo["pending_appointments"]), 1)

    def test_confirmed_appointment_shows_up(self):
        self.delegate("DL0001")
        self._appoint("DL0001", confirm=True)

        todo = self.client.get("/api/agent/dashboard",
                               headers=self.agent).json()["todo"]
        self.assertEqual(len(todo["today_viewings"]), 1)
        self.assertEqual(len(todo["pending_appointments"]), 0)


# -------------------------------------------------- 会话与预约路由（J-03/J-05）

class TestWorkActuallyMoves(BrokerCase):
    """委托之后，**活有没有真的转过去**。

    这一组比权限那一组更贴近「这个功能好不好用」：权限对了但咨询和预约
    仍然只发给房东，中介就是个摆设，房东该干的事一件没少。
    """

    def _ask(self, house_no="DL0001", text="这套能养猫吗"):
        started = self.client.post("/api/conversations", headers=self.tenant,
                                   json={"house_no": house_no})
        assert started.status_code == 200, started.text
        cid = started.json()["conversation"]["id"]
        sent = self.client.post("/api/conversations/%d/lines" % cid,
                                headers=self.tenant, json={"content": text})
        assert sent.status_code == 200, sent.text
        return cid

    def test_agent_sees_conversation_of_delegated_house(self):
        self.delegate("DL0001")
        cid = self._ask()
        rows = self.client.get("/api/conversations",
                               headers=self.agent).json()["list"]
        self.assertEqual([c["id"] for c in rows], [cid])
        # 中介的「对方」是租客，不是房东
        self.assertEqual(rows[0]["peer_role"], "租客")

    def test_agent_cannot_read_conversation_of_other_house(self):
        """**这条是安全用例。** 会话对中介开放的是「他在托的那几套」，
        不是「所有房源」。"""
        self.delegate("DL0001")
        cid = self._ask("DL0002")
        blocked = self.client.get("/api/conversations/%d/lines" % cid,
                                  headers=self.agent)
        self.assertEqual(blocked.status_code, 403)

    def test_tenant_sees_agent_as_peer(self):
        """租客得知道自己在跟谁说话。

        一直显示「房东」而实际由中介在回，租客会以为在和房东直接谈价——
        那是会出纠纷的误导。
        """
        self.delegate("DL0001")
        self._ask()
        rows = self.client.get("/api/conversations",
                               headers=self.tenant).json()["list"]
        self.assertEqual(rows[0]["peer_role"], "经纪人")

    def test_landlord_still_sees_and_is_copied(self):
        """**房东委托出去的是带看，不是知情权。**"""
        self.delegate("DL0001")
        self._ask()
        rows = self.client.get("/api/conversations",
                               headers=self.landlord).json()["list"]
        self.assertEqual(len(rows), 1)

        titles = [m["title"] for m in self.client.get(
            "/api/messages", headers=self.landlord).json()["list"]]
        self.assertIn("租客咨询（由中介代答）", titles)

    def test_history_bubbles_keep_their_side(self):
        """房东先回过一句，中介进来看到的仍应是「自己这一侧」。

        按发送者算 `mine` 的话，房东那句会跑到租客一侧去，
        中介当场就读错是谁说的。
        """
        cid = self._ask()
        self.client.post("/api/conversations/%d/lines" % cid,
                         headers=self.landlord, json={"content": "可以养猫"})
        self.delegate("DL0001")

        lines = self.client.get("/api/conversations/%d/lines" % cid,
                                headers=self.agent).json()["list"]
        landlord_line = next(l for l in lines if l["content"] == "可以养猫")
        self.assertTrue(landlord_line["mine"])
        self.assertEqual(landlord_line["sender_label"], "房东")
        tenant_line = next(l for l in lines if l["content"] == "这套能养猫吗")
        self.assertFalse(tenant_line["mine"])

    def test_appointment_notifies_agent_and_copies_landlord(self):
        self.delegate("DL0001")
        when = slot(1).isoformat()
        made = self.client.post("/api/appointments", headers=self.tenant,
                                json={"house_no": "DL0001",
                                      "viewing_time": when,
                                      "contact_name": "租客甲",
                                      "contact_phone": "13800000001"})
        self.assertEqual(made.status_code, 200, made.text)

        agent_titles = [m["title"] for m in self.client.get(
            "/api/messages", headers=self.agent).json()["list"]]
        self.assertIn("新的看房预约", agent_titles)

        landlord_titles = [m["title"] for m in self.client.get(
            "/api/messages", headers=self.landlord).json()["list"]]
        self.assertIn("新的看房预约（已转交中介）", landlord_titles)

    def test_agent_can_confirm_appointment(self):
        """委托他去带看，却不让他确认预约，等于只转了权没转活。"""
        self.delegate("DL0001")
        when = slot(1).isoformat()
        made = self.client.post("/api/appointments", headers=self.tenant,
                                json={"house_no": "DL0001",
                                      "viewing_time": when,
                                      "contact_name": "租客甲",
                                      "contact_phone": "13800000001"}).json()
        aid = made["appointment"]["id"]
        ok = self.client.patch("/api/appointments/%d" % aid,
                               headers=self.agent,
                               json={"status": "confirmed", "reply": "到时候见"})
        self.assertEqual(ok.status_code, 200, ok.text)

    def test_agent_cannot_confirm_appointment_elsewhere(self):
        self.delegate("DL0001")
        when = slot(1).isoformat()
        made = self.client.post("/api/appointments", headers=self.tenant,
                                json={"house_no": "DL0002",
                                      "viewing_time": when,
                                      "contact_name": "租客甲",
                                      "contact_phone": "13800000001"}).json()
        aid = made["appointment"]["id"]
        blocked = self.client.patch("/api/appointments/%d" % aid,
                                    headers=self.agent,
                                    json={"status": "confirmed"})
        self.assertEqual(blocked.status_code, 403)

    def test_permission_gone_after_delegation_ends(self):
        """解除委托后会话立刻不可见，但**消息不删**——问责需要。"""
        did = self.delegate("DL0001")
        cid = self._ask()
        self.client.post("/api/delegations/%d/end" % did, headers=self.landlord,
                         json={"reason": "自己出租"})

        blocked = self.client.get("/api/conversations/%d/lines" % cid,
                                  headers=self.agent)
        self.assertEqual(blocked.status_code, 403)
        # 房东仍然读得到全部记录
        kept = self.client.get("/api/conversations/%d/lines" % cid,
                               headers=self.landlord).json()
        self.assertEqual(len(kept["list"]), 1)


class TestAppointmentReceipt(BrokerCase):
    """预约的处理结果要回给租客。

    这个缺口不属于中介功能，是做 J-05 时顺带照出来的：约看房的人要据此
    安排出门，把结果压在库里不告诉他，等于让他白等。
    """

    def _appoint(self, house_no="DL0001"):
        when = slot(1)
        created = self.client.post("/api/appointments", headers=self.tenant,
                                   json={"house_no": house_no,
                                         "viewing_time": when.isoformat(),
                                         "contact_name": "租客甲",
                                         "contact_phone": "13800000001"})
        assert created.status_code == 200, created.text
        return created.json()["appointment"]["id"]

    def _titles(self, headers):
        return [m["title"] for m in self.client.get(
            "/api/messages", headers=headers).json()["list"]]

    def test_confirm_notifies_tenant(self):
        aid = self._appoint()
        self.client.patch("/api/appointments/%d" % aid, headers=self.landlord,
                          json={"status": "confirmed", "reply": "到时候见"})
        self.assertIn("看房预约已确认", self._titles(self.tenant))

    def test_reject_carries_the_reason(self):
        """只说「未通过」，租客不知道是时间不合适还是房子已经租出去了，
        只能再约一次碰运气——与 K-08 同一条理由。"""
        aid = self._appoint()
        self.client.patch("/api/appointments/%d" % aid, headers=self.landlord,
                          json={"status": "rejected", "reply": "那天我不在大连"})
        body = [m for m in self.client.get(
            "/api/messages", headers=self.tenant).json()["list"]
            if m["title"] == "看房预约未通过"]
        self.assertTrue(body)
        self.assertIn("那天我不在大连", body[0]["content"])

    def test_agent_can_confirm_and_tenant_is_told(self):
        """受托中介确认，租客照样收到回执——通知的是结果，不是谁点的。"""
        self.delegate("DL0001")
        aid = self._appoint()
        ok = self.client.patch("/api/appointments/%d" % aid, headers=self.agent,
                               json={"status": "confirmed", "reply": "我带你看"})
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertIn("看房预约已确认", self._titles(self.tenant))

    def test_self_cancel_is_not_echoed_back(self):
        """租客自己点的取消，再给他发一条「你的预约已取消」是噪音。"""
        aid = self._appoint()
        self.client.patch("/api/appointments/%d" % aid, headers=self.tenant,
                          json={"status": "cancelled"})
        self.assertNotIn("看房预约已取消", self._titles(self.tenant))

    def test_landlord_cancel_is_notified(self):
        aid = self._appoint()
        self.client.patch("/api/appointments/%d" % aid, headers=self.landlord,
                          json={"status": "cancelled", "reply": "房子已出租"})
        self.assertIn("看房预约已取消", self._titles(self.tenant))


class TestReportAttribution(BrokerCase):
    """被举报次数怎么算到中介头上。

    **按举报发生时这套房是不是他在托**，不按当前受托人——否则委托一解除，
    历史举报就全都不算数了，中介档案页也就失去了意义。
    """

    def _report(self, house_no, headers=None):
        return self.client.post(
            "/api/reports", headers=headers or self.tenant,
            json={"house_no": house_no, "kind": "fake",
                  "description": "打电话过去说这套早就租出去了"})

    def _count(self):
        roster = self.client.get("/api/admin/agents",
                                 headers=self.admin).json()
        row = next(r for r in roster["list"] if r["username"] == "agent1")
        return row["reported"]

    def test_report_during_delegation_counts(self):
        self.delegate("DL0001")
        self.assertEqual(self._report("DL0001").status_code, 200)
        self.assertEqual(self._count()["total"], 1)

    def test_report_on_a_house_he_never_held_does_not_count(self):
        """**这条守的是这次的性能改法。**

        为了不整表扫描，实现改成「先用他托过的房源把举报缩小，再比时间」。
        写错成「先取全部举报」或者漏掉房源过滤，这条就会红。
        """
        self.delegate("DL0001")
        self.assertEqual(self._report("DL0002").status_code, 200)
        self.assertEqual(self._count()["total"], 0)

    def test_report_after_delegation_ended_does_not_count(self):
        did = self.delegate("DL0001")
        self.client.post("/api/delegations/%d/end" % did,
                         headers=self.landlord, json={"reason": "自己出租"})
        self.assertEqual(self._report("DL0001").status_code, 200)
        self.assertEqual(self._count()["total"], 0)


class TestPartyNotRole(BrokerCase):
    """取数按「是不是当事一方」，不按角色分支。

    **K-13 的教训在这里又犯了一次。** 那次是四个 `/mine` 接口按角色
    二选一取数，租客升级为房东后升级前签的租约当场从页面上消失；
    这次是带看流水写成「role 是 agent 就只看他带的，否则看他名下房源的」。

    构造一个**名下有房、同时又是中介**的账号来守这条——管理员改过角色
    的账号就是这样，房源不会跟着角色走。
    """

    def _agent_with_houses(self):
        """把 landlord1 的角色改成 agent，他名下房源原样保留。"""
        header = self.make_agent("agent7")
        session = db.SessionLocal()
        try:
            agent = session.query(User).filter_by(username="agent7").one()
            house = session.query(House).filter_by(house_no="DL0003").one()
            house.landlord_id = agent.id
            session.commit()
        finally:
            session.close()
        return header

    def test_agent_still_sees_viewings_on_his_own_houses(self):
        owner = self._agent_with_houses()

        # 另一个中介受托 DL0003 并带看了一次
        invited = self.client.post("/api/delegations", headers=owner,
                                   json={"house_no": "DL0003",
                                         "agent_username": "agent1"})
        self.assertEqual(invited.status_code, 200, invited.text)
        did = invited.json()["delegation"]["id"]
        self.client.patch("/api/delegations/%d" % did, headers=self.agent,
                          json={"accept": True})
        made = self.client.post("/api/viewings", headers=self.agent,
                                json={"house_no": "DL0003",
                                      "tenant_username": "tenant1",
                                      "showed_up": True, "intention": "high"})
        self.assertEqual(made.status_code, 200, made.text)

        # 房主本人是中介身份，但这条带看发生在**他的房子**上，他必须看得到
        rows = self.client.get("/api/viewings", headers=owner).json()["list"]
        self.assertEqual([r["house_no"] for r in rows], ["DL0003"])

    def test_agent_sees_conversations_on_his_own_houses(self):
        owner = self._agent_with_houses()
        started = self.client.post("/api/conversations", headers=self.tenant,
                                   json={"house_no": "DL0003"})
        self.assertEqual(started.status_code, 200, started.text)

        rows = self.client.get("/api/conversations",
                               headers=owner).json()["list"]
        self.assertEqual([c["house_no"] for c in rows], ["DL0003"])

    def test_owner_who_is_an_agent_invites_instead_of_self_delegating(self):
        """**房主对自己的房子永远是委托方，与他的 role 无关。**

        顺序反过来（先看角色）时，这里会建出一条 `agent_id == landlord_id`
        的记录——自己委托自己，而且返回 200，看起来一切正常。
        """
        owner = self._agent_with_houses()
        created = self.client.post("/api/delegations", headers=owner,
                                   json={"house_no": "DL0003",
                                         "agent_username": "agent1"})
        self.assertEqual(created.status_code, 200, created.text)
        row = created.json()["delegation"]
        self.assertEqual(row["initiated_by"], "landlord")
        self.assertNotEqual(row["agent_id"], row["landlord_id"])

    def test_cannot_delegate_a_house_to_its_own_owner(self):
        owner = self._agent_with_houses()
        blocked = self.client.post("/api/delegations", headers=owner,
                                   json={"house_no": "DL0003",
                                         "agent_username": "agent7"})
        self.assertEqual(blocked.status_code, 400)
        self.assertIn("房主自己", blocked.json()["detail"])


class TestRepairListIsPartyBased(BrokerCase):
    """工单列表也按当事方取（K-13 那次漏掉的第五个接口）。

    房东自己也可能是别处的租客。他报的那张工单，在他升级成房东的那一刻
    就从这个列表里消失了——**数据一直在库里，是查询把它挡住的。**
    """

    def test_landlord_still_sees_the_repair_he_filed_as_a_tenant(self):
        # 先以租客身份报一张工单，再把这个账号升级成房东
        make_resident("tenant1", "DL0002")   # 报修只收在住租客
        filed = self.client.post("/api/repairs", headers=self.tenant, json={
            "house_no": "DL0002", "content": "厨房水管漏水",
            "contact_name": "租客甲", "contact_phone": "13800000001"})
        self.assertEqual(filed.status_code, 200, filed.text)
        repair_no = filed.json()["repair"].get("repair_no")

        session = db.SessionLocal()
        try:
            me = session.query(User).filter_by(username="tenant1").one()
            me.role = "landlord"
            session.commit()
        finally:
            session.close()

        rows = self.client.get("/api/repairs", headers=self.tenant).json()["list"]
        self.assertTrue(rows, "升级成房东后，自己报的工单不该消失")
        if repair_no:
            self.assertIn(repair_no, [r.get("repair_no") for r in rows])


class TestAppointmentSlotsAreNotAnAddressBook(BrokerCase):
    """`GET /api/appointments` 不需要登录，所以它只能返回时段。

    在此之前它返回完整的 `to_dict()`——**任何人不登录就能翻出全站
    「谁、什么时候、看哪套房」，还附带姓名和号码后四位。**
    姓名 + 号码后四位 + 具体地址 + 具体时间，凑在一起已经足够定位到
    一个真人。掩码不是免死金牌：R-02 要求的是脱敏，不是「脱敏了就可以
    随便公开」。
    """

    def setUp(self):
        super().setUp()
        when = slot(1)
        made = self.client.post("/api/appointments", headers=self.tenant,
                                json={"house_no": "DL0001",
                                      "viewing_time": when.isoformat(),
                                      "contact_name": "林小雨",
                                      "contact_phone": "13800000001"})
        assert made.status_code == 200, made.text

    def test_public_list_carries_no_identity(self):
        body = self.client.get("/api/appointments?house_no=DL0001")
        self.assertEqual(body.status_code, 200)
        row = body.json()["list"][0]
        self.assertEqual(set(row), {"id", "viewing_time", "status"})
        self.assertNotIn("林小雨", body.text)
        # 明文与掩码后的号码都不该出现。用完整串比对而不是「0001」——
        # 那四位随时可能撞上某个自增 id，用例会假红
        self.assertNotIn("13800000001", body.text)
        self.assertNotIn("138****0001", body.text)

    def test_cannot_dump_the_whole_platform(self):
        """不带房源编号拉全表只有「翻遍全平台」一种意义。"""
        self.assertEqual(self.client.get("/api/appointments").status_code, 400)

    def test_the_landlord_still_gets_the_contact(self):
        """收窄的是公开接口，房东该看到的一样看得到——走要登录的 /mine。"""
        rows = self.client.get("/api/appointments/mine",
                               headers=self.landlord).json()["list"]
        mine = [r for r in rows if r["house_no"] == "DL0001"]
        self.assertTrue(mine)
        self.assertEqual(mine[0]["contact_name"], "林小雨")
        self.assertEqual(mine[0]["contact_phone"], "138****0001")

    def test_delegated_agent_sees_it_in_mine(self):
        self.delegate("DL0001")
        rows = self.client.get("/api/appointments/mine",
                               headers=self.agent).json()["list"]
        self.assertTrue([r for r in rows if r["house_no"] == "DL0001"])


class TestStatsIsTiered(BrokerCase):
    """`/api/stats` 分两档。

    这个接口原先完全不需要登录，却返回 `income_received`、
    `monthly_income`、逾期笔数——**平台收了多少钱、还有多少没收上来，
    全网可见**。这不是个人隐私问题，是经营数据外泄。

    公开的那一档必须留住：首页的地图找房未登录也能用，靠的正是
    `by_district`。
    """

    PRIVATE = ("income_received", "income_pending", "monthly_income",
               "overdue_count", "contract_total", "repair_pending")

    def test_anonymous_gets_facts_but_no_money(self):
        body = self.client.get("/api/stats")
        self.assertEqual(body.status_code, 200)
        data = body.json()
        # 地图要用的那几项还在
        self.assertIn("by_district", data)
        self.assertIn("avg_rent", data)
        self.assertIn("house_total", data)
        for key in self.PRIVATE:
            self.assertNotIn(key, data, "未登录不该看到 %s" % key)

    def test_tenant_is_not_special(self):
        """登录了也不行——这一档是管理员的，不是「登录用户」的。"""
        data = self.client.get("/api/stats", headers=self.tenant).json()
        for key in self.PRIVATE:
            self.assertNotIn(key, data)

    def test_admin_gets_everything(self):
        data = self.client.get("/api/stats", headers=self.admin).json()
        for key in self.PRIVATE:
            self.assertIn(key, data, "管理端仪表盘要用 %s" % key)


class TestRepairHistoryHidesTheReporter(BrokerCase):
    """公开的房源维修历史里不带报修人。

    「这套房修过什么、修好没有」是对看房人透明的信息；
    **「谁报的、他的号码后四位」不是。** 原来两者一起返回，于是任何人
    打开房源页，就能看到上一任租客的名字连同「他家卧室门锁坏过」。
    """

    def setUp(self):
        super().setUp()
        make_resident("tenant1", "DL0001")   # 报修只收在住租客
        filed = self.client.post("/api/repairs", headers=self.tenant, json={
            "house_no": "DL0001", "content": "卧室门锁打不开",
            "contact_name": "林小雨", "contact_phone": "13800000001"})
        assert filed.status_code == 200, filed.text

    def _public(self):
        return self.client.get("/api/repairs?house_no=DL0001")

    def test_anonymous_sees_the_fault_not_the_person(self):
        body = self._public()
        row = body.json()["list"][0]
        self.assertEqual(row["content"], "卧室门锁打不开")   # 故障要留着
        self.assertNotIn("contact_name", row)
        self.assertNotIn("contact_phone", row)
        self.assertNotIn("林小雨", body.text)
        self.assertNotIn("138****0001", body.text)

    def test_a_stranger_who_is_logged_in_is_no_different(self):
        other = self.make_agent("agent8")
        row = self.client.get("/api/repairs?house_no=DL0001",
                              headers=other).json()["list"][0]
        self.assertNotIn("contact_name", row)

    def test_landlord_still_sees_who_to_call(self):
        row = self.client.get("/api/repairs?house_no=DL0001",
                              headers=self.landlord).json()["list"][0]
        self.assertEqual(row["contact_name"], "林小雨")

    def test_delegated_agent_sees_it_too(self):
        self.delegate("DL0001")
        row = self.client.get("/api/repairs?house_no=DL0001",
                              headers=self.agent).json()["list"][0]
        self.assertEqual(row["contact_name"], "林小雨")

    def test_reporter_sees_his_own(self):
        row = self.client.get("/api/repairs?house_no=DL0001",
                              headers=self.tenant).json()["list"][0]
        self.assertEqual(row["contact_name"], "林小雨")


# ------------------------------------------------------------ J-07 隐私

class TestContactWindow(BrokerCase):
    """租客的联系方式对中介只在窗口内可见。

    **窗口只卡中介，不卡房东**——不是给中介搞特殊，恰恰相反：
    方案 J-07 整节讲的就是「租客与中介」这一侧，理由是中介比房东更有
    动机把交易引到站外；而房东是潜在合同的对方当事人，本来就要和租客
    建立直接联系，卡住他只会逼双方另找渠道，反而更不可控。
    """

    def _appoint(self, days=0, hour=12, confirm=True):
        when = slot(3)
        made = self.client.post("/api/appointments", headers=self.tenant,
                                json={"house_no": "DL0001",
                                      "viewing_time": when.isoformat(),
                                      "contact_name": "林小雨",
                                      "contact_phone": "13800000001"})
        assert made.status_code == 200, made.text
        aid = made.json()["appointment"]["id"]
        if confirm:
            self.client.patch("/api/appointments/%d" % aid,
                              headers=self.landlord,
                              json={"status": "confirmed"})
        session = db.SessionLocal()
        try:
            row = session.get(Appointment, aid)
            row.viewing_time = (datetime.now() + timedelta(days=days)).replace(
                hour=hour, minute=0, second=0, microsecond=0)
            session.commit()
        finally:
            session.close()
        return aid

    def _agent_row(self, aid):
        rows = self.client.get("/api/appointments/mine",
                               headers=self.agent).json()["list"]
        return next(r for r in rows if r["id"] == aid)

    def test_agent_sees_contact_on_the_viewing_day(self):
        self.delegate("DL0001")
        aid = self._appoint(days=0)
        row = self._agent_row(aid)
        self.assertTrue(row["contact_visible"])
        self.assertEqual(row["contact_phone"], "138****0001")

    def test_agent_cannot_see_it_days_ahead(self):
        """约在下周三的看房，中介今天不需要那个号码。"""
        self.delegate("DL0001")
        aid = self._appoint(days=5)
        row = self._agent_row(aid)
        self.assertFalse(row["contact_visible"])
        self.assertEqual(row["contact_phone"], "")
        self.assertEqual(row["contact_name"], "")

    def test_unconfirmed_stays_closed_even_today(self):
        self.delegate("DL0001")
        aid = self._appoint(days=0, confirm=False)
        self.assertFalse(self._agent_row(aid)["contact_visible"])

    def test_landlord_is_not_windowed(self):
        self.delegate("DL0001")
        aid = self._appoint(days=5)
        rows = self.client.get("/api/appointments/mine",
                               headers=self.landlord).json()["list"]
        row = next(r for r in rows if r["id"] == aid)
        self.assertTrue(row["contact_visible"])
        self.assertEqual(row["contact_phone"], "138****0001")

    def test_tenant_always_sees_his_own(self):
        aid = self._appoint(days=5)
        rows = self.client.get("/api/appointments/mine",
                               headers=self.tenant).json()["list"]
        row = next(r for r in rows if r["id"] == aid)
        self.assertTrue(row["contact_visible"])


class TestDoNotDisturb(BrokerCase):
    """免打扰（J-07 第三条）。"""

    def _appoint(self):
        when = slot(2)
        made = self.client.post("/api/appointments", headers=self.tenant,
                                json={"house_no": "DL0001",
                                      "viewing_time": when.isoformat(),
                                      "contact_name": "林小雨",
                                      "contact_phone": "13800000001"})
        assert made.status_code == 200, made.text

    def _set(self, value):
        r = self.client.put("/api/auth/profile", headers=self.tenant,
                            json={"accept_contact": value})
        assert r.status_code == 200, r.text
        return r.json()

    def test_default_is_on(self):
        """默认关等于功能上线当天所有联系按钮一起置灰。"""
        me = self.client.get("/api/auth/me", headers=self.tenant).json()
        self.assertTrue(me["accept_contact"])

    def test_agent_can_reach_a_related_tenant(self):
        self.delegate("DL0001")
        self._appoint()
        started = self.client.post("/api/conversations", headers=self.agent,
                                   json={"house_no": "DL0001",
                                         "tenant_username": "tenant1"})
        self.assertEqual(started.status_code, 200, started.text)

    def test_dnd_blocks_it(self):
        self.delegate("DL0001")
        self._appoint()
        self._set(False)
        blocked = self.client.post("/api/conversations", headers=self.agent,
                                   json={"house_no": "DL0001",
                                         "tenant_username": "tenant1"})
        self.assertEqual(blocked.status_code, 403)
        self.assertIn("不接受主动联系", blocked.json()["detail"])

    def test_dnd_does_not_block_the_tenant_himself(self):
        """免打扰挡的是别人主动来，不是把自己也关在外面。"""
        self.delegate("DL0001")
        self._set(False)
        started = self.client.post("/api/conversations", headers=self.tenant,
                                   json={"house_no": "DL0001"})
        self.assertEqual(started.status_code, 200, started.text)

    def test_cannot_cold_message_an_unrelated_tenant(self):
        """**这条是安全用例。**

        中介手上有全站房源，若能对任意人发起会话，「主动联系」就成了
        一个站内群发入口。必须是这套房上已经有往来的人。
        """
        self.delegate("DL0001")
        # 一个和这套房毫无往来的租客（种子库里只有 tenant1，现注册一个）
        created = self.client.post("/api/auth/register", json={
            "username": "passerby", "password": "test123",
            "real_name": "路人甲", "phone": "13700009999", "role": "tenant"})
        self.assertEqual(created.status_code, 200, created.text)

        blocked = self.client.post("/api/conversations", headers=self.agent,
                                   json={"house_no": "DL0001",
                                         "tenant_username": "passerby"})
        self.assertEqual(blocked.status_code, 403)
        self.assertIn("约过看房", blocked.json()["detail"])

    def test_agent_cannot_reach_into_a_house_he_does_not_hold(self):
        self._appoint()
        blocked = self.client.post("/api/conversations", headers=self.agent,
                                   json={"house_no": "DL0001",
                                         "tenant_username": "tenant1"})
        self.assertEqual(blocked.status_code, 403)


# ------------------------------------------------------------ J-09-b 智能派单

class TestSuggestAgents(BrokerCase):
    """给一套房推荐中介。

    **全部由规则算，不调大模型。** 派单结果直接决定谁能接到这单生意，
    它必须是可复算、可申诉的：中介问「为什么不派给我」，
    要能拿出一行一行的算式，而不是「模型觉得他更合适」。
    """

    def _suggest(self, house_no="DL0001", headers=None):
        return self.client.get("/api/delegations/suggest?house_no=" + house_no,
                               headers=headers or self.landlord)

    def test_only_the_owner_can_ask(self):
        """这是「我该把房子委托给谁」的答案，不是一份中介通讯录。"""
        self.assertEqual(self._suggest(headers=self.tenant).status_code, 403)
        self.assertEqual(self._suggest(headers=self.agent).status_code, 403)
        self.assertEqual(self._suggest(headers=self.admin).status_code, 200)

    def test_ranked_by_score_with_reasons(self):
        body = self._suggest().json()
        self.assertTrue(body["list"])
        row = body["list"][0]
        self.assertIn("score", row)
        self.assertTrue(row["reasons"], "排序要能解释，不能是黑箱")
        scores = [r["score"] for r in body["list"]]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_weights_are_returned(self):
        """中介问「为什么不派给我」，答案得在同一个页面上找得到。"""
        self.assertEqual(set(self._suggest().json()["weights"]),
                         {"activity", "praise", "load", "deals"})

    def test_region_is_a_hard_filter(self):
        """主营区域不覆盖的，分数再高也不该出现。"""
        # agent1 主营中山区；DL0001 若不在中山区，他就不该被推荐
        session = db.SessionLocal()
        try:
            house = session.query(House).filter_by(house_no="DL0001").one()
            district = house.district
        finally:
            session.close()
        names = [r["username"] for r in self._suggest().json()["list"]]
        if district == "中山区":
            self.assertIn("agent1", names)
        else:
            self.assertNotIn("agent1", names)

    def test_not_accepting_is_excluded(self):
        self.client.put("/api/agent/profile", headers=self.agent,
                        json={"accepting": False, "service_districts": []})
        names = [r["username"] for r in self._suggest().json()["list"]]
        self.assertNotIn("agent1", names)

    def test_unapproved_is_excluded(self):
        self.make_agent("agent6", approved=False)
        names = [r["username"] for r in self._suggest().json()["list"]]
        self.assertNotIn("agent6", names)

    def test_a_busy_agent_scores_lower_than_an_idle_one(self):
        """**负载均衡**——手上二十套的中介接不过来。"""
        self.make_agent("agent5", districts="")     # 不限区域，一定进候选
        self.client.put("/api/agent/profile", headers=self.agent,
                        json={"service_districts": []})
        self.delegate("DL0002")                     # agent1 手上多一套

        rows = {r["username"]: r["score"]
                for r in self._suggest().json()["list"]}
        self.assertIn("agent5", rows)
        self.assertIn("agent1", rows)
        self.assertGreater(rows["agent5"], rows["agent1"],
                           "在托更多的中介不该排在空闲的前面")

    def test_thin_praise_sample_is_neutral_not_zero(self):
        """没人给过差评和大家都给差评是两回事。

        按 0 分算等于新中介永远排最后——那不是排序，是封杀。
        """
        body = self._suggest().json()
        row = next(r for r in body["list"] if r["username"] == "agent1")
        self.assertTrue(any("样本不足" in x for x in row["reasons"]))
        self.assertGreater(row["score"], 0)

    def test_refuses_when_already_delegated(self):
        self.delegate("DL0001")
        self.assertEqual(self._suggest().status_code, 409)


class TestDemandSummaryHandoff(BrokerCase):
    """需求摘要随咨询一并交给中介（J-09-a 的落地那一半）。

    摘要**做成会话的第一条消息，而不是给会话加一列**：
    加列要改表、要在每一处会话展示里加渲染分支；做成消息则天然带时间、
    天然进历史、天然在中介手机上也看得到。
    """

    SUMMARY = "【需求摘要】硬条件：中山区 · 两室一厅 · 租金上限≤3000元"

    def test_summary_lands_as_the_first_line(self):
        self.delegate("DL0001")
        started = self.client.post("/api/conversations", headers=self.tenant,
                                   json={"house_no": "DL0001",
                                         "demand_summary": self.SUMMARY})
        self.assertEqual(started.status_code, 200, started.text)
        cid = started.json()["conversation"]["id"]

        lines = self.client.get("/api/conversations/%d/lines" % cid,
                                headers=self.agent).json()["list"]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["kind"], "summary")
        self.assertIn("中山区", lines[0]["content"])

    def test_agent_sees_it_before_the_tenant_says_anything(self):
        """**这就是这个功能的全部价值**——中介不必让租客再说一遍。"""
        self.delegate("DL0001")
        self.client.post("/api/conversations", headers=self.tenant,
                         json={"house_no": "DL0001",
                               "demand_summary": self.SUMMARY})
        rows = self.client.get("/api/conversations",
                               headers=self.agent).json()
        self.assertEqual(rows["unread"], 1)
        self.assertIn("需求摘要", rows["list"][0]["last_message"])

    def test_a_manager_cannot_forge_a_demand_summary(self):
        """摘要是租客的需求。中介发起会话时带一段，不该被当成租客说的。"""
        self.delegate("DL0001")
        when = slot(2).isoformat()
        self.client.post("/api/appointments", headers=self.tenant,
                         json={"house_no": "DL0001", "viewing_time": when,
                               "contact_name": "林小雨",
                               "contact_phone": "13800000001"})
        started = self.client.post("/api/conversations", headers=self.agent,
                                   json={"house_no": "DL0001",
                                         "tenant_username": "tenant1",
                                         "demand_summary": "他想要五千的三室"})
        self.assertEqual(started.status_code, 200, started.text)
        cid = started.json()["conversation"]["id"]
        lines = self.client.get("/api/conversations/%d/lines" % cid,
                                headers=self.tenant).json()["list"]
        self.assertEqual(lines, [])

    def test_no_summary_means_an_empty_conversation(self):
        started = self.client.post("/api/conversations", headers=self.tenant,
                                   json={"house_no": "DL0001"})
        cid = started.json()["conversation"]["id"]
        lines = self.client.get("/api/conversations/%d/lines" % cid,
                                headers=self.tenant).json()["list"]
        self.assertEqual(lines, [])


class TestDelegationEndsWithTheParty(BrokerCase):
    """房东不再是房东、中介不再能接待时，委托跟着收尾（2026-10-22）。

    原先委托一直挂着「生效中」：中介能替一个已经退回租客的人把房子重新上架，
    看房预约也照样派给一个已经被停用的中介。
    """

    def _status(self, did):
        session = db.SessionLocal()
        try:
            return session.get(HouseDelegation, did).status
        finally:
            session.close()

    def _owner_with_an_offline_house(self):
        """一个名下只有一套已下架房源的房东——这样才退得回租客。"""
        self.client.post("/api/auth/register", json={
            "username": "owner2", "password": "test123",
            "real_name": "房东乙", "phone": "13800002222"})
        session = db.SessionLocal()
        try:
            owner = session.query(User).filter_by(username="owner2").one()
            owner.role = "landlord"
            session.add(House(house_no="DL0099", title="下架的一套", landlord_id=owner.id,
                              district="中山区", community="港湾", rent_price=2500, area=60,
                              layout="一室一厅", rent_type="整租", status="offline",
                              approved_at=datetime.now()))
            session.commit()
        finally:
            session.close()
        owner = auth_header(self.client, "owner2", "test123")
        created = self.client.post("/api/delegations", headers=owner, json={
            "house_no": "DL0099", "agent_username": "agent1"})
        did = created.json()["delegation"]["id"]
        self.client.patch("/api/delegations/%d" % did, headers=self.agent,
                          json={"accept": True})
        return owner, did

    def test_reverting_to_tenant_ends_the_delegation(self):
        owner, did = self._owner_with_an_offline_house()
        self.assertEqual(self._status(did), "active")

        self.assertEqual(self.client.post("/api/auth/revert-to-tenant",
                                          headers=owner).status_code, 200)
        self.assertEqual(self._status(did), "ended")
        relist = self.client.patch("/api/houses/DL0099/status?status=available",
                                   headers=self.agent)
        self.assertEqual(relist.status_code, 403)

    def test_disabling_the_agent_ends_his_delegations(self):
        did = self.delegate("DL0001")
        pending = self.delegate("DL0002", accept=False)
        self.client.patch("/api/admin/users/%d" % self._uid("agent1"),
                          headers=self.admin, json={"status": "disabled"})
        self.assertEqual(self._status(did), "ended")
        self.assertEqual(self._status(pending), "rejected")
        titles = [m["title"] for m in self.client.get(
            "/api/messages", headers=self.landlord).json()["list"]]
        self.assertIn("委托已结束", titles)

    def test_demoting_the_agent_ends_his_delegations(self):
        did = self.delegate("DL0001")
        self.client.patch("/api/admin/users/%d" % self._uid("agent1"),
                          headers=self.admin, json={"role": "tenant"})
        self.assertEqual(self._status(did), "ended")

    def test_disabling_the_landlord_ends_delegations_on_his_houses(self):
        did = self.delegate("DL0001")
        self.client.patch("/api/admin/users/%d" % self._uid("landlord1"),
                          headers=self.admin, json={"status": "disabled"})
        self.assertEqual(self._status(did), "ended")

    def _uid(self, username):
        session = db.SessionLocal()
        try:
            return session.query(User).filter_by(username=username).one().id
        finally:
            session.close()


class TestCannotInviteADisabledAgent(BrokerCase):
    """名录里不列停用的中介，但手填用户名原先照样能邀请——对方登不上来，邀请永远没人确认。"""

    def test_invite_is_refused(self):
        session = db.SessionLocal()
        try:
            session.query(User).filter_by(username="agent1").one().status = "disabled"
            session.commit()
        finally:
            session.close()
        response = self.client.post("/api/delegations", headers=self.landlord,
                                    json={"house_no": "DL0001", "agent_username": "agent1"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("停用", response.json()["detail"])
