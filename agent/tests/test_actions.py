# -*- coding: utf-8 -*-
"""代办操作层的测试。

**这个文件里最重要的不是「功能对不对」，是「不该发生的事有没有发生」。**

一个能替用户花钱、退租、清数据的助手，出错的代价与答错一道 FAQ 完全不是
一个量级。所以这里的用例分两类：

- **安全性**（占大头）：提议阶段绝不写后端、执行只认白名单参数、
  问句永远不会被当成指令、未登录不进任何执行路径。
- 功能性：卡片上的数字对不对、槽位缺失时会不会追问。

用假后端记录每一次调用，因此「有没有写」是可以被断言的，
不是靠读代码确认。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agents.support import agent as support  # noqa: E402
from app.agents.support.tools import actions  # noqa: E402
from app.backend import BackendError  # noqa: E402


class RecordingBackend(object):
    """记录每一次调用的假后端。

    `writes` 只要非空，就说明有人在不该写的时候写了——
    这是本文件里最重要的一个断言对象。
    """

    def __init__(self, payloads=None):
        self.payloads = payloads or {}
        self.reads = []
        self.writes = []

    def get(self, path, params=None, token=None):
        self.reads.append(path)
        if path in self.payloads:
            return self.payloads[path]
        return {"list": []}

    def post(self, path, payload=None, token=None):
        self.writes.append(("POST", path, payload))
        return {"ok": True}

    def _request(self, method, path, params=None, payload=None, token=None):
        self.writes.append((method, path, payload or params))
        return {"ok": True}


UNPAID = {"/api/bills/mine": {"list": [
    {"bill_no": "ZD001", "amount": 2200, "status": "unpaid",
     "bill_type": "租金", "due_date": "2026-09-01"},
    {"bill_no": "ZD002", "amount": 2200, "status": "unpaid",
     "bill_type": "租金", "due_date": "2026-10-01"},
]}}

ACTIVE_CONTRACT = {"/api/contracts/mine": {"list": [
    {"contract_no": "HT001", "status": "active", "house_no": "DL0001",
     "start_date": "2026-01-01", "end_date": "2026-12-31"},
]}}


class TestDetection(unittest.TestCase):
    """把「问」和「做」分开——这一步错了，后面全错。"""

    def test_questions_never_become_actions(self):
        """**咨询流程绝不能触发操作。**

        用户问「怎么退租」是想了解流程。如果这句话弹出一张退租确认卡，
        轻则吓人，重则手滑点了确认——而退租不可撤销。
        """
        for question in ["怎么退租", "退租流程是什么", "押金怎么退",
                         "房租什么时候交", "报修多久上门", "能不能提前退租",
                         "续租需要什么手续"]:
            self.assertEqual(actions.detect(question), "",
                             "「%s」是提问，不该被当成指令" % question)

    def test_queries_never_become_actions(self):
        """查询也不是指令。「我还欠多少房租」问的是余额，不是要交钱。"""
        for question in ["我还欠多少房租", "账单还有几笔没交",
                         "我的租约什么时候到期", "我的报修到哪一步了"]:
            self.assertEqual(actions.detect(question), "",
                             "「%s」是查询，不该被当成指令" % question)

    def test_imperative_requests_are_detected(self):
        cases = [("帮我把这个月房租交了", "pay_bill"),
                 ("帮我交一下房租", "pay_bill"),
                 ("帮我约周六下午看房", "book_viewing"),
                 ("厨房水管漏水了帮我报修", "submit_repair"),
                 ("我要退租", "end_contract"),
                 ("帮我续租一年", "renew_contract"),
                 ("取消预约", "cancel_viewing"),
                 ("消息全部已读", "read_all_messages"),
                 ("帮我把房源下架", "toggle_house")]
        for question, want in cases:
            self.assertEqual(actions.detect(question), want,
                             "「%s」应识别为 %s" % (question, want))

    def test_word_order_does_not_matter(self):
        """中文语序自由，动词和宾语之间常常隔着几个字。

        「帮我把**这个月**房租交了」——写死的短语对不上，
        所以用「动词 × 对象」共现来兜。
        """
        for question in ["帮我把这个月房租交了", "帮我把上个月的房租交一下",
                         "帮我把房租交了"]:
            self.assertEqual(actions.detect(question), "pay_bill")

    def test_soft_veto_blocks_questions_that_contain_action_verbs(self):
        """**带业务动词的疑问句不能被当成指令。**

        这一层是被验收集逼出来的：三条知识型问题因为句子里有「报修」
        「退租」「付房租」这些词，被动作层拦下当成了指令。
        它们的共同点是有疑问语气（「吗」「什么」）却没有祈使词。
        """
        for question in ["哪种情况算紧急报修", "退租需要打扫吗",
                         "可以刷信用卡付房租吗", "续租要重新签合同吗",
                         "什么样的房子可以下架"]:
            self.assertEqual(actions.detect(question), "",
                             "「%s」是提问，不该被当成指令" % question)

    def test_soft_veto_does_not_block_polite_requests(self):
        """「吗」不能无条件否决——客气的请求也带「吗」。

        判断标准是有没有祈使词：有「帮我」就是请求，没有才是提问。
        否则用户得把话说得生硬才能使唤动这个助手。
        """
        self.assertEqual(actions.detect("能帮我交下房租吗"), "pay_bill")
        self.assertEqual(actions.detect("可以帮我约看房吗"), "book_viewing")


class TestProposeIsSideEffectFree(unittest.TestCase):
    """**提议阶段一次写操作都不许发生。**

    整个两阶段设计的安全性全押在这一条上：只要提议会写，
    「先给你看看」就成了「已经做完了」。
    """

    def test_no_writes_for_any_action(self):
        payloads = {}
        payloads.update(UNPAID)
        payloads.update(ACTIVE_CONTRACT)
        payloads["/api/messages"] = {"list": [{"title": "x"}], "unread": 3}
        payloads["/api/appointments/mine"] = {"list": [
            {"id": 1, "status": "pending", "viewing_time": "2026-09-05 14:00",
             "house_no": "DL0001"}]}
        payloads["/api/reviews/pending"] = {"list": [{"contract_no": "HT001"}]}
        payloads["/api/landlord/houses"] = {"list": [{"house_no": "DL0001"}]}
        payloads["/api/appointments"] = {"list": [
            {"id": 2, "status": "pending", "viewing_time": "2026-09-06 10:00",
             "house_no": "DL0002"}]}
        payloads["/api/repairs"] = {"list": [
            {"id": 3, "status": "pending", "content": "空调不制冷"}]}

        for name in actions.PROPOSERS:
            backend = RecordingBackend(payloads)
            actions.propose(name, "帮我处理一下 厨房漏水", "tok", backend,
                            context={"house_no": "DL0001"})
            self.assertEqual(
                backend.writes, [],
                "提议 %s 时写了后端：%s" % (name, backend.writes))

    def test_propose_without_token_never_touches_backend(self):
        backend = RecordingBackend()
        status, message, card = actions.propose(
            "pay_bill", "帮我交房租", "", backend)
        self.assertEqual(status, actions.NEED_LOGIN)
        self.assertIsNone(card)
        self.assertEqual(backend.reads, [])
        self.assertEqual(backend.writes, [])

    def test_backend_failure_degrades_quietly(self):
        class Broken(object):
            def get(self, path, params=None, token=None):
                raise BackendError("连不上")

        status, message, card = actions.propose(
            "pay_bill", "帮我交房租", "tok", Broken())
        self.assertEqual(status, actions.FAILED)
        self.assertIsNone(card)
        self.assertTrue(message)


class TestCardContents(unittest.TestCase):
    """卡片上写的就是用户点确认时看到的全部信息，必须准确。"""

    def test_pay_bill_card_states_amount_and_due_date(self):
        backend = RecordingBackend(UNPAID)
        status, _, card = actions.propose("pay_bill", "帮我交房租", "tok", backend)
        self.assertEqual(status, actions.CONFIRM)
        self.assertIn("2200", card["summary"])
        self.assertIn("2026-09-01", card["summary"])
        self.assertEqual(card["params"]["bill_no"], "ZD001",
                         "多笔待缴时应选最早到期的那一笔")
        self.assertIn("另外 1 笔", card["warning"],
                      "有其它待缴账单时必须说明这次只交一笔")

    def test_money_and_irreversible_actions_are_high_risk(self):
        backend = RecordingBackend(dict(UNPAID, **ACTIVE_CONTRACT))
        for name, text in [("pay_bill", "帮我交房租"),
                           ("end_contract", "我要退租"),
                           ("renew_contract", "帮我续租")]:
            _, _, card = actions.propose(name, text, "tok", backend)
            self.assertEqual(card["risk"], actions.RISK_HIGH,
                             "%s 涉及钱或不可逆，必须是高风险" % name)

    def test_end_contract_warns_about_penalty(self):
        backend = RecordingBackend(ACTIVE_CONTRACT)
        _, _, card = actions.propose("end_contract", "我要退租", "tok", backend)
        self.assertIn("不可撤销", card["warning"])
        self.assertIn("违约金", card["warning"])

    def test_repair_content_strips_the_imperative(self):
        """工单内容不该带上「帮我报修」这几个字——它会跟着进故障分类，
        也会显示在房东的工单列表里。"""
        backend = RecordingBackend(ACTIVE_CONTRACT)
        _, _, card = actions.propose(
            "submit_repair", "厨房水管漏水了帮我报修", "tok", backend)
        self.assertEqual(card["params"]["content"], "厨房水管漏水了")

    def test_missing_slot_asks_instead_of_guessing(self):
        backend = RecordingBackend()
        status, message, card = actions.propose(
            "book_viewing", "帮我约看房", "tok", backend)
        self.assertEqual(status, actions.NEED_SLOT)
        self.assertIsNone(card)
        self.assertTrue(message)

    def test_ambiguous_target_lists_options(self):
        """有多条可取消的预约时要问是哪一条，不能替用户挑。"""
        backend = RecordingBackend({"/api/appointments/mine": {"list": [
            {"id": 1, "status": "pending", "viewing_time": "9-05 14:00",
             "house_no": "A"},
            {"id": 2, "status": "pending", "viewing_time": "9-06 10:00",
             "house_no": "B"}]}})
        status, message, card = actions.propose(
            "cancel_viewing", "取消预约", "tok", backend)
        self.assertEqual(status, actions.NEED_SLOT)
        self.assertIn("A", message)
        self.assertIn("B", message)


class TestExecuteGuards(unittest.TestCase):
    """执行阶段的护栏。卡片是从前端回传的，必须当作不可信输入。"""

    def test_unknown_action_is_refused(self):
        backend = RecordingBackend()
        ok, message, _ = actions.execute("删库跑路", {}, "tok", backend)
        self.assertFalse(ok)
        self.assertEqual(backend.writes, [])

    def test_unexpected_param_is_refused(self):
        """多一个没见过的字段就拒绝，不原样透传给后端。"""
        backend = RecordingBackend()
        ok, message, _ = actions.execute(
            "pay_bill", {"bill_no": "ZD001", "amount": 1}, "tok", backend)
        self.assertFalse(ok)
        self.assertIn("amount", message)
        self.assertEqual(backend.writes, [])

    def test_missing_required_param_is_refused(self):
        backend = RecordingBackend()
        ok, _, _ = actions.execute("pay_bill", {}, "tok", backend)
        self.assertFalse(ok)
        self.assertEqual(backend.writes, [])

    def test_execute_without_token_does_nothing(self):
        backend = RecordingBackend()
        ok, _, _ = actions.execute("pay_bill", {"bill_no": "ZD001"}, "", backend)
        self.assertFalse(ok)
        self.assertEqual(backend.writes, [])

    def test_backend_rejection_is_reported_verbatim(self):
        """后端说「该房源当前不可预约」，就要原话转述。

        含糊成「操作失败」会让用户反复重试同一件根本做不成的事。
        """
        class Denied(object):
            def post(self, path, payload=None, token=None):
                raise BackendError("该房源当前不可预约")

        ok, message, _ = actions.execute(
            "book_viewing", {"house_no": "A", "viewing_time": "2026-09-05 14:00"},
            "tok", Denied())
        self.assertFalse(ok)
        self.assertIn("不可预约", message)

    def test_successful_execution_calls_the_right_endpoint(self):
        backend = RecordingBackend()
        ok, message, _ = actions.execute(
            "pay_bill", {"bill_no": "ZD001", "method": "alipay"}, "tok", backend)
        self.assertTrue(ok)
        self.assertEqual(len(backend.writes), 1)
        method, path, payload = backend.writes[0]
        self.assertEqual(method, "POST")
        self.assertIn("ZD001", path)

    def test_three_registries_stay_in_sync(self):
        """加一个操作要在三处各加一行，漏掉任何一处都是静默失效。"""
        self.assertEqual(set(actions.PROPOSERS), set(actions.EXECUTORS))
        self.assertEqual(set(actions.EXECUTORS), set(actions.ALLOWED_PARAMS))


class TestAgentNeverExecutes(unittest.TestCase):
    """**对话主流程永远走不到执行。** 这条边界是架构级的，不是约定。"""

    def setUp(self):
        support.reset_knowledge_base()

    def test_conversation_only_proposes(self):
        backend = RecordingBackend(dict(UNPAID, **ACTIVE_CONTRACT))
        for text in ["帮我把这个月房租交了", "我要退租", "帮我续租一年"]:
            backend.writes = []
            result = support.run(text, token="tok", backend=backend)
            self.assertEqual(result["status"], "confirm")
            self.assertIn("action", result)
            self.assertEqual(
                backend.writes, [],
                "「%s」在对话里就写了后端：%s" % (text, backend.writes))

    def test_confirm_card_is_returned_for_the_ui(self):
        backend = RecordingBackend(UNPAID)
        result = support.run("帮我交房租", token="tok", backend=backend)
        card = result["action"]
        for key in ("name", "label", "summary", "risk", "params"):
            self.assertIn(key, card)
        self.assertEqual(result["retrieval"]["mode"], "action")

    def test_action_without_login_asks_to_login(self):
        backend = RecordingBackend()
        result = support.run("帮我交房租", backend=backend)
        self.assertEqual(result["status"], "need_login")
        self.assertEqual(backend.writes, [])

    def test_knowledge_question_still_answered(self):
        """接了操作层之后，知识问答不能受影响。"""
        result = support.run("押金怎么退")
        self.assertEqual(result["status"], "answered")
        self.assertTrue(result["citations"])


if __name__ == "__main__":
    unittest.main()
