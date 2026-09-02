"""AI-02 审阅结论回写测试。

`contracts.review_result` 这个字段建了很久却一直没人写，注释里还留着
「暂未接入」。字段建了不用，比没建更糟——读代码的人会以为它有数据。

存的是**结论摘要**而不是完整明细：明细每次审阅都能重新算出来，
存下来只会在规则升级后变成一份过期的旧结论。
"""

from datetime import datetime, timedelta

from tests.base import ApiTestCase, auth_header, make_admin


def future(days=1):
    return (datetime.now() + timedelta(days=days)).replace(
        hour=0, minute=0, second=0, microsecond=0)


class ReviewResultCase(ApiTestCase):

    def setUp(self):
        super().setUp()
        self.admin = make_admin(self.client)
        self.landlord = auth_header(self.client, "landlord1", "test123")
        self.tenant = auth_header(self.client, "tenant1", "test123")
        self.no = self.client.post("/api/contracts", headers=self.landlord, json={
            "house_no": "DL0001", "tenant_username": "tenant1",
            "start_date": future().isoformat(), "months": 12,
        }).json()["contract"]["contract_no"]

    def save(self, headers=None, **body):
        payload = {"score": 92, "summary": "条款完整，无高风险",
                   "risk_count": 1, "source": "rules", **body}
        return self.client.post("/api/contracts/%s/review-result" % self.no,
                                headers=headers or self.tenant, json=payload)


class TestSaveReviewResult(ReviewResultCase):

    def test_saved_and_read_back(self):
        self.assertEqual(self.save().status_code, 200)
        body = self.client.get("/api/contracts/%s" % self.no,
                               headers=self.tenant).json()
        summary = body["review_result"]
        self.assertEqual(summary["score"], 92)
        self.assertEqual(summary["risk_count"], 1)
        self.assertEqual(summary["source"], "rules")

    def test_records_when_it_was_reviewed(self):
        """没有时间戳的话，看到一个 92 分也不知道是哪一版规则算的。"""
        self.save()
        body = self.client.get("/api/contracts/%s" % self.no,
                               headers=self.tenant).json()
        self.assertTrue(body["review_result"]["reviewed_at"])

    def test_absent_before_review(self):
        body = self.client.get("/api/contracts/%s" % self.no,
                               headers=self.tenant).json()
        self.assertIsNone(body["review_result"])

    def test_landlord_can_save_too(self):
        """出租方也会想看一眼自己拟的合同有没有问题。"""
        self.assertEqual(self.save(headers=self.landlord).status_code, 200)

    def test_rewriting_overwrites(self):
        self.save(score=92)
        self.save(score=60, summary="发现两条高风险", risk_count=2)
        body = self.client.get("/api/contracts/%s" % self.no,
                               headers=self.tenant).json()
        self.assertEqual(body["review_result"]["score"], 60)

    def test_score_range_validated(self):
        self.assertEqual(self.save(score=200).status_code, 422)
        self.assertEqual(self.save(score=-1).status_code, 422)

    def test_outsider_denied(self):
        self.client.post("/api/auth/register", json={
            "username": "nosy", "password": "test123", "role": "tenant"})
        nosy = auth_header(self.client, "nosy", "test123")
        self.assertEqual(self.save(headers=nosy).status_code, 403)

    def test_anonymous_denied(self):
        r = self.client.post("/api/contracts/%s/review-result" % self.no,
                             json={"score": 90})
        self.assertEqual(r.status_code, 401)

    def test_broken_json_does_not_break_the_api(self):
        """一个坏掉的字段不该让整个合同接口跟着报错。"""
        import app.db as db
        from app.models import Contract
        session = db.SessionLocal()
        try:
            row = session.query(Contract).filter_by(contract_no=self.no).one()
            row.review_result = "{这不是合法的 JSON"
            session.commit()
        finally:
            session.close()
        body = self.client.get("/api/contracts/%s" % self.no,
                               headers=self.tenant)
        self.assertEqual(body.status_code, 200)
        self.assertIsNone(body.json()["review_result"])
