"""维修数据测试（F-09：维修物品、次数、人员的登记与统计）。

两条最容易出错、也最伤数据的规则在这里被钉死：

* **一张工单只能结案一次**——重复提交会把物料重复扣一遍库存、
  成本也重复计一遍，统计从此不可信。
* **单价是领用当时的快照**——物品调价之后，历史维修的成本不能跟着变，
  那等于把已经发生的账改了。
"""

from tests.base import ApiTestCase, auth_header, make_admin


class RepairDataCase(ApiTestCase):

    def setUp(self):
        super().setUp()
        self.admin = make_admin(self.client)
        self.landlord = auth_header(self.client, "landlord1", "test123")
        self.tenant = auth_header(self.client, "tenant1", "test123")

    def new_worker(self, name="张师傅", skill="水电"):
        return self.client.post("/api/repair-workers", headers=self.admin, json={
            "name": name, "phone": "13700001111", "skill": skill,
        }).json()["worker"]

    def new_item(self, name="水龙头", price=45, stock=10):
        return self.client.post("/api/repair-items", headers=self.admin, json={
            "name": name, "unit": "个", "unit_price": price, "stock": stock,
        }).json()["item"]

    def new_repair(self, house_no="DL0001", content="厨房水龙头一直滴水"):
        return self.client.post("/api/repairs", headers=self.tenant, json={
            "house_no": house_no, "content": content,
            "contact_name": "租客甲", "contact_phone": "13900005678",
            "category": "水电", "urgency": "一般",
        }).json()["repair"]

    def close(self, repair_id, **body):
        return self.client.post("/api/repairs/%d/record" % repair_id,
                                headers=self.admin, json=body)


class TestWorkersAndItems(RepairDataCase):

    def test_create_and_list_worker(self):
        self.new_worker()
        body = self.client.get("/api/repair-workers", headers=self.admin).json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["list"][0]["name"], "张师傅")

    def test_worker_phone_is_masked(self):
        self.new_worker()
        body = self.client.get("/api/repair-workers", headers=self.admin).json()
        self.assertEqual(body["list"][0]["phone"], "137****1111")

    def test_worker_status_validated(self):
        worker = self.new_worker()
        r = self.client.patch("/api/repair-workers/%d" % worker["id"],
                              headers=self.admin, json={"status": "离职了"})
        self.assertEqual(r.status_code, 400)

    def test_only_admin_maintains_workers(self):
        r = self.client.post("/api/repair-workers", headers=self.landlord,
                             json={"name": "李师傅"})
        self.assertEqual(r.status_code, 403)

    def test_landlord_can_read_workers(self):
        """房东要派单，得看得到人员清单。"""
        self.new_worker()
        r = self.client.get("/api/repair-workers", headers=self.landlord)
        self.assertEqual(r.status_code, 200)

    def test_tenant_cannot_read_workers(self):
        self.assertEqual(
            self.client.get("/api/repair-workers", headers=self.tenant).status_code,
            403)

    def test_create_item(self):
        item = self.new_item()
        self.assertEqual(item["stock"], 10)
        self.assertEqual(item["unit_price"], 45)

    def test_only_admin_maintains_items(self):
        r = self.client.post("/api/repair-items", headers=self.landlord,
                             json={"name": "灯泡"})
        self.assertEqual(r.status_code, 403)


class TestRecord(RepairDataCase):

    def test_close_ticket_with_materials(self):
        worker = self.new_worker()
        item = self.new_item(price=45, stock=10)
        repair = self.new_repair()

        r = self.close(repair["id"], worker_id=worker["id"], hours=1.5,
                       labor_cost=80, note="换了新水龙头",
                       items=[{"item_id": item["id"], "quantity": 2}])
        self.assertEqual(r.status_code, 200)
        record = r.json()["record"]
        self.assertEqual(record["item_cost"], 90)
        self.assertEqual(record["labor_cost"], 80)
        self.assertEqual(record["total_cost"], 170)
        self.assertEqual(record["worker_name"], "张师傅")

    def test_stock_is_deducted(self):
        item = self.new_item(stock=10)
        repair = self.new_repair()
        self.close(repair["id"], items=[{"item_id": item["id"], "quantity": 3}])
        body = self.client.get("/api/repair-items", headers=self.admin).json()
        self.assertEqual(body["list"][0]["stock"], 7)

    def test_ticket_becomes_done(self):
        repair = self.new_repair()
        self.close(repair["id"])
        body = self.client.get("/api/repairs?house_no=DL0001",
                               headers=self.admin).json()
        self.assertEqual(body["list"][0]["status"], "done")

    def test_cannot_close_twice(self):
        """重复结案会把物料重复扣一遍、成本重复计一遍，统计就废了。"""
        item = self.new_item(stock=10)
        repair = self.new_repair()
        self.close(repair["id"], items=[{"item_id": item["id"], "quantity": 2}])
        second = self.close(repair["id"],
                            items=[{"item_id": item["id"], "quantity": 2}])
        self.assertEqual(second.status_code, 409)
        body = self.client.get("/api/repair-items", headers=self.admin).json()
        self.assertEqual(body["list"][0]["stock"], 8, "库存不能被扣第二次")

    def test_insufficient_stock_is_rejected(self):
        item = self.new_item(stock=1)
        repair = self.new_repair()
        r = self.close(repair["id"],
                       items=[{"item_id": item["id"], "quantity": 5}])
        self.assertEqual(r.status_code, 409)
        self.assertIn("库存不足", r.json()["detail"])

    def test_partial_stock_failure_deducts_nothing(self):
        """库存不足要整单拒绝，不能扣了一半才发现最后一样不够。"""
        enough = self.new_item(name="灯泡", price=10, stock=10)
        scarce = self.new_item(name="角阀", price=20, stock=1)
        repair = self.new_repair()
        r = self.close(repair["id"], items=[
            {"item_id": enough["id"], "quantity": 2},
            {"item_id": scarce["id"], "quantity": 5},
        ])
        self.assertEqual(r.status_code, 409)
        body = self.client.get("/api/repair-items", headers=self.admin).json()
        stocks = {row["name"]: row["stock"] for row in body["list"]}
        self.assertEqual(stocks["灯泡"], 10, "整单拒绝时先前的物料也不能扣")
        self.assertEqual(stocks["角阀"], 1)

    def test_price_snapshot_survives_repricing(self):
        """物品调价之后，历史维修的成本不能跟着一起变。"""
        item = self.new_item(price=45, stock=10)
        repair = self.new_repair()
        self.close(repair["id"], items=[{"item_id": item["id"], "quantity": 2}])

        self.client.patch("/api/repair-items/%d" % item["id"],
                          headers=self.admin, json={"unit_price": 200})

        body = self.client.get("/api/repair-records", headers=self.admin).json()
        record = body["list"][0]
        self.assertEqual(record["item_cost"], 90, "历史成本被调价改写了")
        self.assertEqual(record["items"][0]["unit_price"], 45)

    def test_unknown_item_rejected(self):
        repair = self.new_repair()
        r = self.close(repair["id"], items=[{"item_id": 999, "quantity": 1}])
        self.assertEqual(r.status_code, 404)

    def test_unknown_worker_rejected(self):
        repair = self.new_repair()
        r = self.close(repair["id"], worker_id=999)
        self.assertEqual(r.status_code, 404)

    def test_landlord_can_close_own_house_ticket(self):
        repair = self.new_repair()
        r = self.client.post("/api/repairs/%d/record" % repair["id"],
                             headers=self.landlord, json={"labor_cost": 50})
        self.assertEqual(r.status_code, 200)

    def test_tenant_cannot_close(self):
        repair = self.new_repair()
        r = self.client.post("/api/repairs/%d/record" % repair["id"],
                             headers=self.tenant, json={"labor_cost": 50})
        self.assertEqual(r.status_code, 403)

    def test_landlord_only_sees_own_records(self):
        repair = self.new_repair()
        self.close(repair["id"], labor_cost=50)
        mine = self.client.get("/api/repair-records",
                               headers=self.landlord).json()
        self.assertEqual(mine["total"], 1)


class TestRepairStats(RepairDataCase):

    def test_stats_aggregate(self):
        worker = self.new_worker()
        item = self.new_item(price=45, stock=20)
        for _ in range(2):
            repair = self.new_repair()
            self.close(repair["id"], worker_id=worker["id"], hours=1,
                       labor_cost=80,
                       items=[{"item_id": item["id"], "quantity": 2}])

        stats = self.client.get("/api/repair-stats", headers=self.admin).json()
        self.assertEqual(stats["record_total"], 2)
        self.assertEqual(stats["total_cost"], 340)
        self.assertEqual(stats["avg_cost"], 170)

        by_item = stats["by_item"][0]
        self.assertEqual(by_item["name"], "水龙头")
        self.assertEqual(by_item["quantity"], 4)
        self.assertEqual(by_item["amount"], 180)

        by_worker = stats["by_worker"][0]
        self.assertEqual(by_worker["name"], "张师傅")
        self.assertEqual(by_worker["count"], 2)
        self.assertEqual(by_worker["hours"], 2)

    def test_category_counts_all_tickets(self):
        """故障构成要含未结案的工单，否则看到的只是「已经修好的那部分」。"""
        self.new_repair()
        stats = self.client.get("/api/repair-stats", headers=self.admin).json()
        self.assertEqual(stats["by_category"][0]["category"], "水电")
        self.assertEqual(stats["by_category"][0]["count"], 1)
        self.assertEqual(stats["pending"], 1)

    def test_top_houses_surfaces_repeat_offenders(self):
        """同一套房反复报修，往往不是「又坏了一次」，
        而是上次没修好或者房子本身有问题。"""
        for _ in range(3):
            self.new_repair("DL0001")
        self.new_repair("DL0002")
        stats = self.client.get("/api/repair-stats", headers=self.admin).json()
        self.assertEqual(stats["top_houses"][0]["house_no"], "DL0001")
        self.assertEqual(stats["top_houses"][0]["count"], 3)

    def test_unassigned_records_are_counted(self):
        repair = self.new_repair()
        self.close(repair["id"], labor_cost=60)
        stats = self.client.get("/api/repair-stats", headers=self.admin).json()
        self.assertEqual(stats["by_worker"][0]["name"], "未指派")

    def test_empty_stats_do_not_divide_by_zero(self):
        stats = self.client.get("/api/repair-stats", headers=self.admin).json()
        self.assertEqual(stats["avg_cost"], 0)

    def test_stats_denied_for_landlord(self):
        self.assertEqual(
            self.client.get("/api/repair-stats", headers=self.landlord).status_code,
            403)

class TestTenantSeesOwnRepairs(RepairDataCase):
    """A-1：租客要能查到自己提交的工单与处理结果。

    原先租客提交报修之后就再也看不到它了——不知道有没有人接单、
    修没修好。后端本来就按角色过滤，缺的只是把结果暴露完整。
    """

    def test_tenant_lists_only_own_repairs(self):
        self.new_repair()
        mine = self.client.get("/api/repairs", headers=self.tenant).json()
        self.assertEqual(mine["total"], 1)

        # 另一个租客看不到
        self.client.post("/api/auth/register", json={
            "username": "other2", "password": "test123", "role": "tenant"})
        other = auth_header(self.client, "other2", "test123")
        self.assertEqual(
            self.client.get("/api/repairs", headers=other).json()["total"], 0)

    def test_result_appears_after_closing(self):
        worker = self.new_worker(name="张建国")
        repair = self.new_repair()

        before = self.client.get("/api/repairs", headers=self.tenant).json()
        self.assertIsNone(before["list"][0]["result"], "没结案时不该有处理结果")

        self.close(repair["id"], worker_id=worker["id"], labor_cost=80,
                   note="换了新水龙头")
        after = self.client.get("/api/repairs", headers=self.tenant).json()
        result = after["list"][0]["result"]
        self.assertEqual(result["worker_name"], "张建国")
        self.assertEqual(result["note"], "换了新水龙头")
        self.assertTrue(result["finished_at"])

    def test_result_hides_cost_from_tenant(self):
        """维修花了多少是房东的成本，不该露给租客。"""
        worker = self.new_worker()
        item = self.new_item(price=45, stock=10)
        repair = self.new_repair()
        self.close(repair["id"], worker_id=worker["id"], labor_cost=80,
                   items=[{"item_id": item["id"], "quantity": 2}])

        body = self.client.get("/api/repairs", headers=self.tenant).json()
        dump = str(body)
        self.assertNotIn("labor_cost", dump)
        self.assertNotIn("item_cost", dump)
        self.assertNotIn("total_cost", dump)

    def test_unassigned_worker_shows_placeholder(self):
        """没指派维修人员时不能显示成空白。"""
        repair = self.new_repair()
        self.close(repair["id"], labor_cost=50)
        body = self.client.get("/api/repairs", headers=self.tenant).json()
        self.assertEqual(body["list"][0]["result"]["worker_name"], "维修人员")


class TestRepairSla(RepairDataCase):
    """工单超时判定（C-4）。

    报修判定为「紧急」之后原先就没有下文了——没人处理也不会催。
    时限做成可配置（M-09），因为不同规模的团队标准不一样。

    **只标记不自动升级**：自动改优先级会让「紧急」这一档失去意义
    ——挂久了的都变紧急，等于没有紧急。该被催的是人，不是数据。
    """

    def set_sla(self, urgent=4, normal=48):
        return self.client.patch("/api/admin/config", headers=self.admin, json={
            "items": {"repair_sla_urgent_hours": str(urgent),
                      "repair_sla_normal_hours": str(normal)}})

    def age(self, repair_id, hours):
        """把工单的创建时间往前挪，等价于「过了这么久」。"""
        import app.db as db
        from datetime import datetime, timedelta
        from app.models import Repair
        session = db.SessionLocal()
        try:
            row = session.get(Repair, repair_id)
            row.created_at = datetime.now() - timedelta(hours=hours)
            session.commit()
        finally:
            session.close()

    def test_fresh_ticket_is_not_overdue(self):
        self.new_repair()
        body = self.client.get("/api/repairs", headers=self.admin).json()
        self.assertFalse(body["list"][0]["overdue"])
        self.assertEqual(body["overdue_count"], 0)

    def test_normal_ticket_overdue_after_limit(self):
        self.set_sla(normal=48)
        repair = self.new_repair()
        self.age(repair["id"], 50)
        body = self.client.get("/api/repairs", headers=self.admin).json()
        self.assertTrue(body["list"][0]["overdue"])
        self.assertEqual(body["overdue_count"], 1)

    def test_urgent_has_a_tighter_limit(self):
        """同样挂了 6 小时，紧急的算超时，一般的不算。"""
        self.set_sla(urgent=4, normal=48)
        urgent = self.client.post("/api/repairs", headers=self.tenant, json={
            "house_no": "DL0001", "content": "厨房漏水",
            "contact_name": "租客甲", "contact_phone": "13900005678",
            "category": "水暖", "urgency": "紧急"}).json()["repair"]
        normal = self.new_repair()
        self.age(urgent["id"], 6)
        self.age(normal["id"], 6)

        body = self.client.get("/api/repairs", headers=self.admin).json()
        by_id = {r["id"]: r for r in body["list"]}
        self.assertTrue(by_id[urgent["id"]]["overdue"])
        self.assertFalse(by_id[normal["id"]]["overdue"])

    def test_done_ticket_is_never_overdue(self):
        """已经处理完的不该再被算作超时——它已经不需要人管了。"""
        repair = self.new_repair()
        self.age(repair["id"], 999)
        self.close(repair["id"], labor_cost=50)
        body = self.client.get("/api/repairs", headers=self.admin).json()
        self.assertFalse(body["list"][0]["overdue"])
        self.assertEqual(body["overdue_count"], 0)

    def test_config_change_takes_effect(self):
        """时限是可配置的，改了立刻生效。"""
        repair = self.new_repair()
        self.age(repair["id"], 10)

        self.set_sla(normal=48)
        self.assertFalse(self.client.get(
            "/api/repairs", headers=self.admin).json()["list"][0]["overdue"])

        self.set_sla(normal=8)
        self.assertTrue(self.client.get(
            "/api/repairs", headers=self.admin).json()["list"][0]["overdue"])

    def test_urgency_is_not_auto_escalated(self):
        """超时只标记，不改优先级——挂久了都变紧急，等于没有紧急。"""
        self.set_sla(normal=1)
        repair = self.new_repair()
        self.age(repair["id"], 100)
        body = self.client.get("/api/repairs", headers=self.admin).json()
        self.assertTrue(body["list"][0]["overdue"])
        self.assertNotEqual(body["list"][0]["urgency"], "紧急")
