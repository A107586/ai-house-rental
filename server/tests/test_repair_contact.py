"""从工单联系到人：房东联系报修人、管理员就工单联系房东或报修人（2026-10-22）。

原先工单详情只能看：房东要回问租客现场情况，得在这套房上有过看房预约
才开得了会话——线下谈好直接签约的租客报修之后，房东从工单上联系不到他；
管理员看到紧急工单挂了一天，想催房东也只能自己去翻用户列表找人。
"""

from app import db
from app.models import User
from tests.base import ApiTestCase, auth_header, make_admin


class RepairContactCase(ApiTestCase):

    def setUp(self):
        super().setUp()
        self.landlord = auth_header(self.client, "landlord1", "test123")
        self.tenant = auth_header(self.client, "tenant1", "test123")
        self.admin = make_admin(self.client)
        signed = self.client.post("/api/contracts", headers=self.landlord, json={
            "house_no": "DL0001", "tenant_username": "tenant1",
            "start_date": "2026-11-01T00:00:00", "months": 12})
        assert signed.status_code == 200, signed.text
        no = signed.json()["contract"]["contract_no"]
        self.client.post("/api/contracts/%s/activate" % no, headers=self.tenant)
        made = self.client.post("/api/repairs", headers=self.tenant, json={
            "house_no": "DL0001", "content": "卫生间水龙头一直滴水",
            "contact_name": "租客甲", "contact_phone": "13800000001"})
        assert made.status_code == 200, made.text
        self.repair_id = made.json()["repair"]["id"] if "repair" in made.json() \
            else made.json()["id"]

    def titles(self, headers):
        return [m["title"] for m in self.client.get(
            "/api/messages", headers=headers).json()["list"]]


class TestTicketCarriesWhoAndWhere(RepairContactCase):

    def test_landlord_sees_reporter_and_house(self):
        rows = self.client.get("/api/repairs?side=landlord",
                               headers=self.landlord).json()["list"]
        row = next(r for r in rows if r["id"] == self.repair_id)
        self.assertEqual(row["tenant_username"], "tenant1")
        self.assertTrue(row["house_title"])

    def test_public_history_still_hides_the_reporter(self):
        rows = self.client.get("/api/repairs?house_no=DL0001").json()["list"]
        row = next(r for r in rows if r["id"] == self.repair_id)
        self.assertNotIn("tenant_username", row)
        self.assertNotIn("contact_name", row)


class TestLandlordContactsReporter(RepairContactCase):

    def contact(self):
        return self.client.post("/api/conversations", headers=self.landlord, json={
            "house_no": "DL0001", "tenant_username": "tenant1"})

    def test_resident_without_viewing_can_be_contacted(self):
        # tenant1 在 DL0001 上没约过看房，只签过约、报过修
        r = self.contact()
        self.assertEqual(r.status_code, 200, r.text)

    def test_do_not_disturb_does_not_block_the_landlord_of_a_resident(self):
        session = db.SessionLocal()
        try:
            session.query(User).filter_by(username="tenant1").one().accept_contact = False
            session.commit()
        finally:
            session.close()
        self.assertEqual(self.contact().status_code, 200)

    def test_strangers_are_still_off_limits(self):
        r = self.client.post("/api/conversations", headers=self.landlord, json={
            "house_no": "DL0002", "tenant_username": "tenant1"})
        self.assertEqual(r.status_code, 403)


class TestAdminMessagesAboutTicket(RepairContactCase):

    def send(self, to, headers=None):
        return self.client.post(
            "/api/admin/repairs/%d/message" % self.repair_id,
            headers=headers or self.admin,
            json={"to": to, "content": "这张工单已超时，请尽快安排师傅"})

    def test_admin_reaches_landlord_and_reporter(self):
        self.assertEqual(self.send("landlord").status_code, 200)
        self.assertEqual(self.send("tenant").status_code, 200)
        title = "平台就工单 GD%06d 联系你" % self.repair_id
        self.assertIn(title, self.titles(self.landlord))
        self.assertIn(title, self.titles(self.tenant))

    def test_only_admin(self):
        self.assertEqual(self.send("tenant", self.landlord).status_code, 403)

    def test_unknown_ticket(self):
        r = self.client.post("/api/admin/repairs/99999/message", headers=self.admin,
                             json={"to": "landlord", "content": "在吗"})
        self.assertEqual(r.status_code, 404)
