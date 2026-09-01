"""浏览足迹与租后评价测试。

评价的准入条件是重点：不限制的话任何人都能刷分，评分就没有参考价值。
"""

import unittest
from datetime import datetime, timedelta

from tests.base import ApiTestCase, auth_header


def future(days=7):
    return (datetime.now() + timedelta(days=days)).replace(
        hour=0, minute=0, second=0, microsecond=0)


class ReviewCase(ApiTestCase):

    def setUp(self):
        super().setUp()
        self.landlord = auth_header(self.client, "landlord1", "test123")
        self.tenant = auth_header(self.client, "tenant1", "test123")

    def ended_contract(self, house_no="DL0001"):
        """签一份合同并让它走完，得到可评价状态。"""
        no = self.client.post("/api/contracts", headers=self.landlord, json={
            "house_no": house_no, "tenant_username": "tenant1",
            "start_date": future().isoformat()}).json()["contract"]["contract_no"]
        self.client.post("/api/contracts/%s/activate" % no, headers=self.tenant)
        self.client.post("/api/contracts/%s/end" % no, headers=self.landlord,
                         json={"reason": "到期"})
        return no

    def active_contract(self, house_no="DL0001"):
        no = self.client.post("/api/contracts", headers=self.landlord, json={
            "house_no": house_no, "tenant_username": "tenant1",
            "start_date": future().isoformat()}).json()["contract"]["contract_no"]
        self.client.post("/api/contracts/%s/activate" % no, headers=self.tenant)
        return no


class TestBrowsingHistory(ApiTestCase):

    def setUp(self):
        super().setUp()
        self.tenant = auth_header(self.client, "tenant1", "test123")

    def test_records_on_detail_view(self):
        self.client.get("/api/houses/DL0001", headers=self.tenant)
        data = self.client.get("/api/history", headers=self.tenant).json()
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["list"][0]["house_no"], "DL0001")

    def test_repeat_view_updates_not_duplicates(self):
        """刷十次详情页不能把足迹列表刷满。"""
        for _ in range(3):
            self.client.get("/api/houses/DL0001", headers=self.tenant)
        data = self.client.get("/api/history", headers=self.tenant).json()
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["list"][0]["my_view_count"], 3)

    def test_anonymous_view_not_recorded(self):
        self.client.get("/api/houses/DL0001")
        data = self.client.get("/api/history", headers=self.tenant).json()
        self.assertEqual(data["total"], 0)

    def test_multiple_houses_ordered_by_recency(self):
        self.client.get("/api/houses/DL0001", headers=self.tenant)
        self.client.get("/api/houses/DL0002", headers=self.tenant)
        data = self.client.get("/api/history", headers=self.tenant).json()
        self.assertEqual(data["total"], 2)
        self.assertEqual(data["list"][0]["house_no"], "DL0002", "最近浏览的排最前")

    def test_clear(self):
        self.client.get("/api/houses/DL0001", headers=self.tenant)
        self.client.delete("/api/history", headers=self.tenant)
        self.assertEqual(
            self.client.get("/api/history", headers=self.tenant).json()["total"], 0)

    def test_requires_login(self):
        self.assertEqual(self.client.get("/api/history").status_code, 401)

    def test_view_count_still_increments(self):
        """足迹逻辑不能影响原有的房源浏览量统计。"""
        before = self.client.get("/api/houses/DL0001").json()["view_count"]
        after = self.client.get("/api/houses/DL0001", headers=self.tenant).json()
        self.assertEqual(after["view_count"], before + 1)


class TestReview(ReviewCase):

    def test_review_after_contract_ends(self):
        no = self.ended_contract()
        response = self.client.post("/api/reviews", headers=self.tenant, json={
            "contract_no": no, "rating": 5, "content": "房东很好说话"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["review"]["rating"], 5)

    def test_cannot_review_active_contract(self):
        """还在租就能评价的话，评价就成了要挟房东的筹码。"""
        no = self.active_contract()
        response = self.client.post("/api/reviews", headers=self.tenant, json={
            "contract_no": no, "rating": 1})
        self.assertEqual(response.status_code, 409)

    def test_cannot_review_twice(self):
        no = self.ended_contract()
        self.client.post("/api/reviews", headers=self.tenant,
                         json={"contract_no": no, "rating": 5})
        again = self.client.post("/api/reviews", headers=self.tenant,
                                 json={"contract_no": no, "rating": 1})
        self.assertEqual(again.status_code, 409)

    def test_landlord_cannot_review_own_house(self):
        no = self.ended_contract()
        response = self.client.post("/api/reviews", headers=self.landlord,
                                    json={"contract_no": no, "rating": 5})
        self.assertEqual(response.status_code, 403)

    def test_rating_out_of_range(self):
        no = self.ended_contract()
        for bad in (0, 6):
            response = self.client.post("/api/reviews", headers=self.tenant,
                                        json={"contract_no": no, "rating": bad})
            self.assertEqual(response.status_code, 422, str(bad))

    def test_unknown_contract(self):
        response = self.client.post("/api/reviews", headers=self.tenant,
                                    json={"contract_no": "NOPE", "rating": 5})
        self.assertEqual(response.status_code, 404)

    def test_tenant_name_is_masked(self):
        """评价对所有人可见，不能露出租客全名。"""
        no = self.ended_contract()
        self.client.post("/api/reviews", headers=self.tenant,
                         json={"contract_no": no, "rating": 5})
        response = self.client.get("/api/houses/DL0001/reviews")
        self.assertNotIn("租客甲", response.text)
        self.assertIn("租*", response.text)

    def test_landlord_notified(self):
        no = self.ended_contract()
        self.client.post("/api/reviews", headers=self.tenant,
                         json={"contract_no": no, "rating": 4})
        messages = self.client.get("/api/messages", headers=self.landlord).json()
        self.assertIn("收到新评价", [m["title"] for m in messages["list"]])


class TestHouseRating(ReviewCase):

    def test_average_appears_on_house(self):
        no = self.ended_contract()
        self.client.post("/api/reviews", headers=self.tenant,
                         json={"contract_no": no, "rating": 4})
        house = self.client.get("/api/houses/DL0001").json()
        self.assertEqual(house["rating"], 4)
        self.assertEqual(house["review_count"], 1)

    def test_no_review_means_zero(self):
        house = self.client.get("/api/houses/DL0002").json()
        self.assertEqual(house["rating"], 0)
        self.assertEqual(house["review_count"], 0)

    def test_reviews_endpoint(self):
        no = self.ended_contract()
        self.client.post("/api/reviews", headers=self.tenant, json={
            "contract_no": no, "rating": 5, "content": "很满意"})
        data = self.client.get("/api/houses/DL0001/reviews").json()
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["average"], 5)
        self.assertEqual(data["list"][0]["content"], "很满意")

    def test_unknown_house(self):
        self.assertEqual(
            self.client.get("/api/houses/NOPE/reviews").status_code, 404)


class TestPendingReviews(ReviewCase):

    def test_ended_contract_shows_up(self):
        self.ended_contract()
        data = self.client.get("/api/reviews/pending", headers=self.tenant).json()
        self.assertEqual(data["total"], 1)

    def test_disappears_after_review(self):
        no = self.ended_contract()
        self.client.post("/api/reviews", headers=self.tenant,
                         json={"contract_no": no, "rating": 5})
        data = self.client.get("/api/reviews/pending", headers=self.tenant).json()
        self.assertEqual(data["total"], 0)

    def test_active_contract_not_pending(self):
        self.active_contract()
        data = self.client.get("/api/reviews/pending", headers=self.tenant).json()
        self.assertEqual(data["total"], 0)


if __name__ == "__main__":
    unittest.main()
