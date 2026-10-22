# -*- coding: utf-8 -*-
"""注销账号（个人信息保护法第 47 条）。

原先只有管理员能「停用」，用户自己没有任何办法让平台忘掉他。
重心：没了结的合同、账单、佣金不让注销；注销之后个人信息清掉、用户名释放、
所有设备登出；合同与收支流水作为交易凭证保留。
"""

import unittest
from datetime import datetime, timedelta

import app.db as db
from app.models import Contract, Favorite, User
from tests.base import ApiTestCase, auth_header, end_lease, make_admin


class DeletionCase(ApiTestCase):

    def setUp(self):
        super().setUp()
        self.landlord = auth_header(self.client, "landlord1", "test123")
        self.tenant = auth_header(self.client, "tenant1", "test123")

    def deactivate(self, headers=None, password="test123"):
        return self.client.post("/api/auth/deactivate", headers=headers or self.tenant,
                                json={"password": password})


class TestDeactivate(DeletionCase):

    def test_wrong_password_is_refused(self):
        self.assertEqual(self.deactivate(password="nope123").status_code, 403)

    def test_open_contract_blocks_and_says_what_is_left(self):
        start = (datetime.now() + timedelta(days=3)).replace(hour=0, minute=0, second=0,
                                                            microsecond=0)
        self.client.post("/api/contracts", headers=self.landlord, json={
            "house_no": "DL0001", "tenant_username": "tenant1",
            "start_date": start.isoformat(), "months": 12})
        r = self.deactivate()
        self.assertEqual(r.status_code, 409)
        self.assertIn("合同还没结束", r.json()["detail"])

    def test_landlord_with_listings_is_blocked(self):
        r = self.deactivate(headers=self.landlord)
        self.assertEqual(r.status_code, 409)
        self.assertIn("房源还在架", r.json()["detail"])

    def test_admin_cannot_self_deactivate(self):
        admin = make_admin(self.client)
        self.assertEqual(self.deactivate(headers=admin, password="admin123").status_code, 400)

    def test_deactivation_forgets_the_person_and_frees_the_name(self):
        self.client.post("/api/favorites/DL0001", headers=self.tenant)
        r = self.deactivate()
        self.assertEqual(r.status_code, 200, r.text)

        # 所有设备登出、原用户名登不上
        self.assertEqual(self.client.get("/api/auth/me", headers=self.tenant).status_code, 401)
        login = self.client.post("/api/auth/login",
                                 json={"username": "tenant1", "password": "test123"})
        self.assertNotEqual(login.status_code, 200)

        session = db.SessionLocal()
        try:
            gone = session.query(User).filter(User.username.like("deleted_%")).one()
            self.assertEqual((gone.status, gone.phone, gone.email, gone.real_name),
                             ("deleted", "", "", "已注销用户"))
            self.assertEqual(session.query(Favorite).filter_by(user_id=gone.id).count(), 0)
        finally:
            session.close()

        # 原来那个用户名可以被重新注册
        again = self.client.post("/api/auth/register", json={
            "username": "tenant1", "password": "test456", "real_name": "新租客"})
        self.assertEqual(again.status_code, 200, again.text)

    def test_contracts_are_kept_as_records(self):
        start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        no = self.client.post("/api/contracts", headers=self.landlord, json={
            "house_no": "DL0001", "tenant_username": "tenant1",
            "start_date": start.isoformat(), "months": 12}).json()["contract"]["contract_no"]
        self.client.post("/api/contracts/%s/activate" % no, headers=self.tenant)
        # 交清、退租，才能注销
        from tests.base import settle
        for bill in self.client.get("/api/bills/mine", headers=self.tenant,
                                    params={"side": "tenant"}).json()["list"]:
            if bill["status"] == "unpaid" and bill.get("payable"):
                settle(self.client, bill["bill_no"], self.tenant)
        end_lease(self.client, no, self.landlord, self.tenant)
        r = self.deactivate()
        self.assertEqual(r.status_code, 200, r.text)
        session = db.SessionLocal()
        try:
            self.assertEqual(session.query(Contract).filter_by(contract_no=no).one().status,
                             "ended")
        finally:
            session.close()
        # 房东那边的合同还在
        mine = self.client.get("/api/contracts/mine", headers=self.landlord,
                               params={"side": "landlord"}).json()["list"]
        self.assertIn(no, [c["contract_no"] for c in mine])

    def test_admin_cannot_revive_a_deleted_account(self):
        self.deactivate()
        admin = make_admin(self.client)
        session = db.SessionLocal()
        try:
            uid = session.query(User).filter(User.username.like("deleted_%")).one().id
        finally:
            session.close()
        r = self.client.patch("/api/admin/users/%d" % uid, headers=admin,
                              json={"status": "active"})
        self.assertEqual(r.status_code, 409)


if __name__ == "__main__":
    unittest.main()
