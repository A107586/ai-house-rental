# -*- coding: utf-8 -*-
"""客服问答反馈的归属校验。

`/api/support/feedback` 原先只按 log_id 找行就改，不看是谁在改。
log_id 是自增连号，挨个点一遍「没用」，管理端那张
「哪些回答被点了没用」的清单就全是假的——而那张清单是拿来决定
下一批写哪些 FAQ 的，被污染之后是在指挥人去修没坏的东西。
"""

from tests.base import ApiTestCase, auth_header, make_admin


class TestFeedbackOwnership(ApiTestCase):

    def _log(self, headers=None, session_id=""):
        res = self.client.post("/api/support/logs", json={
            "question": "押金怎么退", "status": "answered",
            "session_id": session_id, "cited": []},
            headers=headers or {})
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()["log_id"]

    # ---------------------------------------------------- 本人可以打分

    def test_owner_can_rate(self):
        headers = auth_header(self.client)
        log_id = self._log(headers)
        res = self.client.post("/api/support/feedback",
                               json={"log_id": log_id, "value": -1},
                               headers=headers)
        self.assertEqual(res.status_code, 200, res.text)

    def test_anonymous_owner_rates_by_session(self):
        """未登录也要能给自己的回答打分，凭会话号认人。"""
        log_id = self._log(session_id="sess-abc")
        res = self.client.post("/api/support/feedback",
                               json={"log_id": log_id, "value": 1,
                                     "session_id": "sess-abc"})
        self.assertEqual(res.status_code, 200, res.text)

    # ---------------------------------------------------- 别人不可以

    def test_anonymous_cannot_rate_someone_elses(self):
        headers = auth_header(self.client)
        log_id = self._log(headers)
        res = self.client.post("/api/support/feedback",
                               json={"log_id": log_id, "value": -1})
        self.assertEqual(res.status_code, 404, res.text)

    def test_other_user_cannot_rate(self):
        mine = self._log(auth_header(self.client))
        other = make_admin(self.client)
        res = self.client.post("/api/support/feedback",
                               json={"log_id": mine, "value": -1},
                               headers=other)
        self.assertEqual(res.status_code, 404, res.text)

    def test_wrong_session_id_rejected(self):
        log_id = self._log(session_id="sess-abc")
        res = self.client.post("/api/support/feedback",
                               json={"log_id": log_id, "value": -1,
                                     "session_id": "sess-xyz"})
        self.assertEqual(res.status_code, 404, res.text)

    def test_enumeration_says_not_found_either_way(self):
        """不存在的号和别人的号回一样的话。

        回「这条不是你的」等于确认这个号存在，逐个试一遍
        能问出总量和分布。
        """
        headers = auth_header(self.client)
        log_id = self._log(headers)
        theirs = self.client.post("/api/support/feedback",
                                  json={"log_id": log_id, "value": -1})
        missing = self.client.post("/api/support/feedback",
                                   json={"log_id": 999999, "value": -1})
        self.assertEqual(theirs.status_code, missing.status_code)
        self.assertEqual(theirs.json(), missing.json())

    def test_ownerless_row_cannot_be_rated(self):
        """既没登录也没带会话号的老数据，一律不让改。"""
        log_id = self._log()
        res = self.client.post("/api/support/feedback",
                               json={"log_id": log_id, "value": -1})
        self.assertEqual(res.status_code, 404, res.text)


class TestFeedbackReachesAnalytics(ApiTestCase):
    """打了「没用」之后，管理端的清单里要真的出现它。"""

    def test_disliked_shows_up(self):
        headers = auth_header(self.client)
        res = self.client.post("/api/support/logs", json={
            "question": "到期退租有哪些步骤", "status": "answered",
            "session_id": "s1", "cited": []}, headers=headers)
        log_id = res.json()["log_id"]
        self.client.post("/api/support/feedback",
                         json={"log_id": log_id, "value": -1}, headers=headers)

        admin = make_admin(self.client)
        data = self.client.get("/api/admin/support/analytics",
                               headers=admin).json()
        questions = [r["question"] for r in data["disliked"]]
        self.assertIn("到期退租有哪些步骤", questions)


class TestHandoffIsNotAFloodGate(ApiTestCase):
    """转人工每一条都给全体管理员发站内信。原先不设限：同一句话点十次就是十张工单。"""

    def _handoff(self, headers, question="押金一直没退怎么办"):
        return self.client.post("/api/support/handoff", headers=headers,
                                json={"question": question, "reason": "助手答不上"})

    def test_same_question_is_not_queued_twice(self):
        headers = auth_header(self.client)
        admin = make_admin(self.client)
        first = self._handoff(headers).json()["handoff"]["handoff_no"]
        again = self._handoff(headers).json()
        self.assertEqual(again["handoff"]["handoff_no"], first)
        self.assertTrue(again["duplicate"])
        queued = self.client.get("/api/admin/handoffs", headers=admin).json()
        self.assertEqual(queued["total"], 1)

    def test_open_questions_are_capped(self):
        headers = auth_header(self.client)
        for i in range(5):
            self.assertEqual(self._handoff(headers, "第 %d 个问题" % i).status_code, 200)
        self.assertEqual(self._handoff(headers, "第 6 个问题").status_code, 429)
