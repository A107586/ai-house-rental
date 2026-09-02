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
        status, message, card, _ask = actions.propose(
            "pay_bill", "帮我交房租", "", backend)
        self.assertEqual(status, actions.NEED_LOGIN)
        self.assertIsNone(card)
        self.assertEqual(backend.reads, [])
        self.assertEqual(backend.writes, [])

    def test_backend_failure_degrades_quietly(self):
        class Broken(object):
            def get(self, path, params=None, token=None):
                raise BackendError("连不上")

        status, message, card, _ask = actions.propose(
            "pay_bill", "帮我交房租", "tok", Broken())
        self.assertEqual(status, actions.FAILED)
        self.assertIsNone(card)
        self.assertTrue(message)


class TestCardContents(unittest.TestCase):
    """卡片上写的就是用户点确认时看到的全部信息，必须准确。"""

    def test_pay_bill_card_states_amount_and_due_date(self):
        backend = RecordingBackend(UNPAID)
        status, _, card, _ = actions.propose("pay_bill", "帮我交房租", "tok", backend)
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
            _, _, card, _ = actions.propose(name, text, "tok", backend)
            self.assertEqual(card["risk"], actions.RISK_HIGH,
                             "%s 涉及钱或不可逆，必须是高风险" % name)

    def test_end_contract_warns_about_penalty(self):
        backend = RecordingBackend(ACTIVE_CONTRACT)
        _, _, card, _ = actions.propose("end_contract", "我要退租", "tok", backend)
        self.assertIn("不可撤销", card["warning"])
        self.assertIn("违约金", card["warning"])

    def test_repair_content_strips_the_imperative(self):
        """工单内容不该带上「帮我报修」这几个字——它会跟着进故障分类，
        也会显示在房东的工单列表里。"""
        backend = RecordingBackend(ACTIVE_CONTRACT)
        _, _, card, _ = actions.propose(
            "submit_repair", "厨房水管漏水了帮我报修", "tok", backend)
        self.assertEqual(card["params"]["content"], "厨房水管漏水了")

    def test_missing_slot_asks_instead_of_guessing(self):
        backend = RecordingBackend()
        status, message, card, _ask = actions.propose(
            "book_viewing", "帮我约看房", "tok", backend)
        self.assertEqual(status, actions.NEED_SLOT)
        self.assertIsNone(card)
        self.assertTrue(message)

    def test_ambiguous_target_offers_clickable_options(self):
        """有多条可取消的预约时要问是哪一条，不能替用户挑。

        **选项必须是结构化的，不能塞进一句话里。** 原先是
        「你有 6 条预约，要取消哪一条？2026-09-09 15:00 DL0041；…」——
        用户只能猜该怎么回，实测回一句「第一个」直接被当成新问题拒答了。
        改成结构化选项后，前端渲染成按钮，点一下就等于把值填进槽位。
        """
        backend = RecordingBackend({"/api/appointments/mine": {"list": [
            {"id": 1, "status": "pending", "viewing_time": "9-05 14:00",
             "house_no": "A"},
            {"id": 2, "status": "pending", "viewing_time": "9-06 10:00",
             "house_no": "B"}]}})
        status, message, card, ask = actions.propose(
            "cancel_viewing", "取消预约", "tok", backend)
        self.assertEqual(status, actions.NEED_SLOT)
        self.assertIsNone(card)
        self.assertEqual(ask["slot"], "appointment_id")
        labels = " ".join(o["label"] for o in ask["options"])
        self.assertIn("A", labels)
        self.assertIn("B", labels)
        self.assertEqual([o["value"] for o in ask["options"]], ["1", "2"])


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


class TestMultiTurn(unittest.TestCase):
    """多轮代办。**这一组直接对应用户报的「不好用」。**

    上线后收到的反馈是「预约看房还是不行」。复现出来是同一个毛病：
    每一轮都是孤立的，助手问了问题却接不住答案——

        用户：帮我约看房
        助手：想看哪一套？
        用户：周六下午两点        ← 上一轮整个丢了，跑去查 FAQ

    参考拼多多、美团那类客服补了三样：待办状态、可点选项、口头确认。
    下面每个用例都是当时手工复现过的一条真实对话。
    """

    def setUp(self):
        support.reset_knowledge_base()
        self.backend = RecordingBackend({
            "/api/appointments/mine": {"list": [
                {"id": 1, "status": "pending", "viewing_time": "2026-09-05 14:00",
                 "house_no": "A"},
                {"id": 2, "status": "pending", "viewing_time": "2026-09-06 10:00",
                 "house_no": "B"}]},
            "/api/favorites": {"list": [
                {"house_no": "DL0008", "title": "中山区桂林街"},
                {"house_no": "DL0007", "title": "中山区青泥洼桥"}]},
            "/api/messages": {"list": [{"title": "x"}], "unread": 3},
        })
        self.backend.payloads.update(UNPAID)

    def converse(self, turns):
        """跑一段对话，返回每一轮的结果。pending 由调用方带着走，
        与前端的做法一致——服务端不存会话态。"""
        pending = None
        results = []
        for text in turns:
            result = support.run(text, token="tok", backend=self.backend,
                                 pending=pending)
            pending = result.get("pending")
            results.append(result)
        return results

    def test_booking_fills_two_slots_across_turns(self):
        """约看房要两个槽：哪一套、什么时候。分三轮说完也得接得住。"""
        turns = self.converse(["帮我约看房", "第一个", "周六下午两点"])
        self.assertEqual(turns[0]["status"], "need_slot")
        self.assertTrue(turns[0]["options"], "问哪一套时要给可点选项")
        self.assertEqual(turns[1]["status"], "need_slot")
        self.assertTrue(turns[1]["options"], "问什么时候也要给可点选项")
        self.assertEqual(turns[2]["status"], "confirm")
        self.assertIn("DL0008", turns[2]["action"]["summary"])

    def test_ordinal_reply_picks_the_right_option(self):
        """「第一个」是最自然的回答，必须能对应到第一个选项。"""
        turns = self.converse(["取消预约", "第一个"])
        self.assertEqual(turns[0]["status"], "need_slot")
        self.assertEqual(turns[1]["status"], "confirm")
        self.assertEqual(turns[1]["action"]["params"]["appointment_id"], 1)

    def test_saying_ok_confirms_a_low_risk_action(self):
        """卡片出来以后说「好的」就是确认。只认按钮不认人话，
        等于把对话做回了表单。"""
        turns = self.converse(["消息全部已读", "好的"])
        self.assertEqual(turns[0]["status"], "confirm")
        self.assertEqual(turns[1]["status"], "confirm")
        self.assertTrue(turns[1].get("auto_confirm"),
                        "低风险操作口头确认即可执行")

    def test_high_risk_still_requires_the_button(self):
        """**涉及钱或不可逆的，口头确认不算数。**

        「好的」两个字太容易脱口而出，而这一类做错了收不回来。
        多点一下按钮不算麻烦。
        """
        turns = self.converse(["帮我交房租", "好的"])
        self.assertEqual(turns[0]["status"], "confirm")
        self.assertEqual(turns[0]["action"]["risk"], actions.RISK_HIGH)
        self.assertFalse(turns[1].get("auto_confirm"),
                         "高风险操作不能凭一句「好的」就执行")
        self.assertIn("点", turns[1]["message"])

    def test_denial_during_slot_filling_stops_everything(self):
        """**追问途中说「算了」必须停下，不能把它当成槽位值填进去。**

        这是实测撞到的最危险的一个 bug：「取消预约」→「要取消哪一条？」
        → 用户说「算了」，「算了」被当成了选项值，反而生成了一张
        取消预约的确认卡片——用户在喊停，系统却把事情往前推了一步。
        """
        turns = self.converse(["取消预约", "算了"])
        self.assertEqual(turns[1]["status"], "cancelled")
        self.assertIsNone(turns[1].get("action"),
                          "喊停之后不该还有确认卡片")
        self.assertEqual(self.backend.writes, [])

    def test_denial_after_card_cancels(self):
        turns = self.converse(["帮我交房租", "不用了"])
        self.assertEqual(turns[1]["status"], "cancelled")
        self.assertEqual(self.backend.writes, [])

    def test_changing_topic_drops_the_pending_action(self):
        """用户问到一半改主意去问别的，不能硬把新话往旧槽里塞。"""
        turns = self.converse(["取消预约", "算了", "押金怎么退"])
        self.assertEqual(turns[2]["status"], "answered")
        self.assertTrue(turns[2]["citations"])

    def test_bare_acknowledgement_without_pending_is_graceful(self):
        """没有待办时单说一句「好的」，不该撞上「暂时没有查到相关规定」——
        用户只是应了一声，却被告知知识库里查不到，莫名其妙。"""
        turns = self.converse(["好的"])
        self.assertEqual(turns[0]["status"], "answered")
        self.assertNotIn("没有查到", turns[0]["message"])

    def test_cancel_action_word_is_not_swallowed_as_denial(self):
        """**「取消预约」是指令，不是「算了」。**

        否定词表里有「取消」，动作词表里有「取消预约」，两者是撞的。
        不先排除指令，用户说「取消预约」会被当成一句应付话打发掉，
        整个取消功能就废了。
        """
        self.assertEqual(actions.detect("取消预约"), "cancel_viewing")
        turns = self.converse(["取消预约"])
        self.assertEqual(turns[0]["status"], "need_slot")

    def test_multi_turn_never_writes_before_confirmation(self):
        """整段对话跑完，一次写操作都不该发生。"""
        self.converse(["帮我约看房", "第一个", "周六下午两点", "好的",
                       "帮我交房租", "好的"])
        self.assertEqual(self.backend.writes, [],
                         "对话过程中写了后端：%s" % self.backend.writes)


if __name__ == "__main__":
    unittest.main()
