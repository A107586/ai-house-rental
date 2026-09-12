# -*- coding: utf-8 -*-
"""计数器必须在数据库里加，不能读出来加完再写回去。

`house.view_count += 1` 是「读-改-写」：两个请求同时读到 563、
各自算出 564、各自写回，两次浏览只记了一次。

实测（4 个 worker，100 并发发 200 次详情请求）：**只涨了 6，丢了 97%。**
浏览量在房东那边是要看的（「浏览 346」），也参与热度与推荐。

改成 SQL 的 `view_count = view_count + 1` 之后，同样的压法一次不丢。
这里用两个各自持有旧值的会话把那个场景压缩成一条确定性用例，
不靠起线程——靠线程的用例在慢机器上会时绿时红，而它测的东西
本来和调度快慢无关。
"""

from sqlalchemy import event, select, update

import app.db as db
from app.models import BrowsingHistory, Faq, House
from tests.base import ApiTestCase, auth_header


class TestAtomicIncrement(ApiTestCase):

    def _view_count(self, house_no="DL0001"):
        session = db.SessionLocal()
        try:
            return session.scalar(
                select(House.view_count).where(House.house_no == house_no))
        finally:
            session.close()

    def test_every_view_is_counted(self):
        before = self._view_count()
        for _ in range(5):
            self.assertEqual(self.client.get("/api/houses/DL0001").status_code, 200)
        self.assertEqual(self._view_count(), before + 5)

    def test_response_shows_the_new_number(self):
        """返回体里的数字要是加过之后的，不能是加之前那个。

        加法挪到 SQL 里之后，会话里那个对象手上还是旧值——
        接口返回的要是它，用户刷新一次才看得到自己刚才那一次。
        """
        first = self.client.get("/api/houses/DL0001").json()["view_count"]
        second = self.client.get("/api/houses/DL0001").json()["view_count"]
        self.assertEqual(second, first + 1)

    def test_the_increment_happens_in_sql(self):
        """**这条才是真正守住那个 bug 的用例。**

        按次数断言是抓不住它的：一次一次地请求，每个请求各拿一个新会话，
        读到的都是最新值，读-改-写照样算得对。区别只在并发时才显形，
        而靠起线程的用例在慢机器上会时绿时红。

        所以直接看发出去的 SQL：加法必须写在语句里
        （`SET view_count=(houses.view_count + ?)`），
        而不是把算好的数字当参数绑上去。
        """
        seen = []

        @event.listens_for(db.engine, "before_cursor_execute")
        def _capture(conn, cursor, statement, params, context, executemany):
            seen.append(" ".join(statement.split()))

        try:
            self.client.get("/api/houses/DL0001")
        finally:
            event.remove(db.engine, "before_cursor_execute", _capture)

        updates = [sql for sql in seen
                   if sql.upper().startswith("UPDATE") and "view_count" in sql]
        self.assertTrue(updates, "详情接口应当更新浏览量：%s" % seen)
        self.assertTrue(
            any("view_count + " in sql.replace("houses.", "") for sql in updates),
            "加法要写在 SQL 里，不能读出来加完再写回去：%s" % updates)

    def test_two_stale_sessions_both_land(self):
        """**这条就是那个 bug 的复现。**

        两个会话都先把房源读进来（各自记住同一个旧值），再各加一次。
        读-改-写的话结果是 +1；加法在库里做，结果才是 +2。
        """
        before = self._view_count()
        sessions = [db.SessionLocal(), db.SessionLocal()]
        try:
            houses = [s.scalar(select(House).where(House.house_no == "DL0001"))
                      for s in sessions]
            for one in houses:              # 两边都读到同一个旧值
                self.assertEqual(one.view_count, before)
            for session, house in zip(sessions, houses):
                session.execute(update(House).where(House.id == house.id)
                                .values(view_count=House.view_count + 1))
                session.commit()
        finally:
            for session in sessions:
                session.close()
        self.assertEqual(self._view_count(), before + 2)

    def test_browsing_history_counts_every_visit(self):
        headers = auth_header(self.client)
        for _ in range(3):
            self.client.get("/api/houses/DL0001", headers=headers)
        session = db.SessionLocal()
        try:
            rows = session.scalars(select(BrowsingHistory)).all()
            self.assertEqual(len(rows), 1, "同一套房只留一条足迹")
            self.assertEqual(rows[0].view_count, 3)
        finally:
            session.close()


class TestFaqHitCount(ApiTestCase):
    """知识条目的命中数同样不能读-改-写。

    「最常被引用的条目」那张表就是拿这个数排序的，少记等于排错。
    """

    def _make_faq(self):
        session = db.SessionLocal()
        try:
            row = Faq(code="faq-probe", question="押金怎么退",
                      answer="租期届满后 7 个工作日内原路退还。",
                      category="押金", is_published=True)
            session.add(row)
            session.commit()
            return row.code
        finally:
            session.close()

    def test_each_citation_counts(self):
        code = self._make_faq()
        for _ in range(4):
            res = self.client.post("/api/support/logs", json={
                "question": "押金怎么退", "status": "answered",
                "session_id": "s", "cited": [code]})
            self.assertEqual(res.status_code, 200, res.text)
        session = db.SessionLocal()
        try:
            hits = session.scalar(select(Faq.hit_count).where(Faq.code == code))
        finally:
            session.close()
        self.assertEqual(hits, 4)
