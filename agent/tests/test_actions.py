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

import datetime
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

    def test_polite_requests_are_still_requests(self):
        """**中文里请人办事，十有八九是包在问句里的。**

        实测：用户约完看房说「能不能把这个看房改成本周日下午三点」，
        问句词一票否决，助手回「时间没问题，请到房源页面自己提交」——
        他刚在这儿约成的。他的原话是「你应该帮我改啊」。

        客气不该被当成犹豫。放行的口子开得很窄：要有祈使成分，
        而且去掉客气话之后不能再剩别的疑问词。
        """
        cases = [("能不能把这个看房改成本周日下午三点", "reschedule_viewing"),
                 ("可以帮我把房租交了吗", "pay_bill"),
                 ("能不能帮我约看房", "book_viewing"),
                 ("可不可以帮我把这套收藏了", "favorite_house")]
        for question, want in cases:
            self.assertEqual(actions.detect(question), want,
                             "「%s」是客气的请求，不是提问" % question)

    def test_politeness_does_not_open_the_door_to_consultations(self):
        """**口子只开到「说法明确」为止。**

        「能不能帮我看看退租要注意什么」同样又客气又带祈使，但它是咨询。
        松到宽泛的动词×对象共现，这句里的「退 × 租」就会变成一张
        退租确认卡——那正是问句否决要防的事，而退租不可撤销。
        """
        for question in ["能不能帮我看看退租要注意什么",
                         "能不能帮我查一下退租流程",
                         "可不可以帮我说说押金一般什么时候退"]:
            self.assertEqual(actions.detect(question), "",
                             "「%s」是咨询，不该被当成指令" % question)

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
        也会显示在房东的工单列表里。

        描述填完之后还会问一句要不要传照片，所以这里直接把那一步跳过。
        """
        backend = RecordingBackend(ACTIVE_CONTRACT)
        _, _, card, _ = actions.propose(
            "submit_repair", "厨房水管漏水了帮我报修", "tok", backend,
            context={"images": actions.SKIP_UPLOAD})
        self.assertEqual(card["params"]["content"], "厨房水管漏水了")


class TestRepairPhotos(unittest.TestCase):
    """报修传照片。

    「哪里坏了」用文字往往说不清：「厨房水管漏水」到底是接头渗水还是
    管子裂了，师傅带的工具完全不一样。所以问一句，但**必须能一键跳过**——
    为了传张图卡住报修是本末倒置。
    """

    def setUp(self):
        self.backend = RecordingBackend(ACTIVE_CONTRACT)

    def propose(self, **context):
        return actions.propose("submit_repair", "厨房水管漏水了帮我报修",
                               "tok", self.backend, context=context or None)

    def test_asks_for_a_photo_after_the_description(self):
        status, message, card, ask = self.propose()
        self.assertEqual(status, actions.NEED_SLOT)
        self.assertEqual(ask["slot"], "images")
        # 前端靠这个标记渲染上传按钮，而不是让用户对着输入框发愣
        self.assertEqual(ask.get("kind"), "upload")
        self.assertTrue(ask["options"], "必须给一个跳过的出口")

    def test_skipping_goes_straight_to_the_card(self):
        status, _, card, _ = self.propose(images=actions.SKIP_UPLOAD)
        self.assertEqual(status, actions.CONFIRM)
        self.assertEqual(card["params"]["images"], "")
        self.assertNotIn("照片", card["summary"])

    def test_photos_show_up_on_the_card(self):
        _, _, card, _ = self.propose(
            images="/static/uploads/a.jpg,/static/uploads/b.jpg")
        self.assertIn("附 2 张照片", card["summary"])
        self.assertEqual(card["params"]["images"],
                         "/static/uploads/a.jpg,/static/uploads/b.jpg")

    def test_outside_links_are_dropped(self):
        """**只收本系统上传的地址。**

        后端也会再校验一次，但让一个外链一路走到执行才被拒，
        用户看到的是「报修失败」——错在最后一步，而原因在第一步。
        """
        _, _, card, _ = self.propose(
            images="https://example.com/x.jpg,/static/uploads/ok.jpg")
        self.assertEqual(card["params"]["images"], "/static/uploads/ok.jpg")

    def test_the_skip_token_never_reaches_the_backend(self):
        """跳过用的是一个不可能混淆的串，不能把「跳过」两个字当成图片地址。"""
        self.assertEqual(actions._image_urls(actions.SKIP_UPLOAD), [])
        self.assertEqual(actions._image_urls("跳过"), [])
        self.assertEqual(actions._image_urls(None), [])

    def test_the_description_survives_the_photo_question(self):
        """**故障描述不能被照片那一轮的回答覆盖。**

        描述是从「当前这句话」现推的。追问照片之后，「当前这句话」变成了
        照片答案或者一个跳过标记——实测的表现是工单内容变成 `__skip__`
        或者一串图片地址。**这是会写坏数据的**：房东打开工单看到的是
        一行 `__skip__`，根本不知道哪儿坏了。
        """
        status, _, _, ask = self.propose()
        self.assertEqual(status, actions.NEED_SLOT)
        # 描述要跟着追问一起带走，下一轮才取得回来
        self.assertEqual(ask["carry"]["content"], "厨房水管漏水了")

        # 下一轮：用户点了跳过，描述从槽位里取回来
        _, _, card, _ = self.propose(content="厨房水管漏水了",
                                     images=actions.SKIP_UPLOAD)
        self.assertEqual(card["params"]["content"], "厨房水管漏水了")
        self.assertNotIn(actions.SKIP_UPLOAD, card["summary"])

        # 传了图也一样
        _, _, card, _ = self.propose(content="厨房水管漏水了",
                                     images="/static/uploads/a.jpg")
        self.assertEqual(card["params"]["content"], "厨房水管漏水了")

    def test_a_failed_photo_does_not_fail_the_whole_ticket(self):
        """**工单已经建出来了，挂图失败不能报错。**

        报错会让用户以为报修没成，再报一遍就是两条重复工单。
        """
        class Flaky(RecordingBackend):
            def post(self, path, payload=None, token=None):
                if "/images" in path:
                    raise BackendError("挂图失败")
                self.writes.append(("POST", path, payload))
                return {"ok": True, "repair": {"id": 7}}

        backend = Flaky()
        okay, message, _ = actions.execute(
            "submit_repair",
            {"house_no": "DL0001", "content": "水管漏水",
             "images": "/static/uploads/a.jpg"}, "tok", backend)
        self.assertTrue(okay, message)
        self.assertIn("再补", message)

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

    def test_a_polite_answer_is_still_an_answer(self):
        """**中文里带商量语气的回答，长得就像问句。**

        实测：助手问「你想什么时候去看？」，用户答「本周六上午九点可以吗」，
        句尾一个「吗」，整句被判成插问、丢去检索知识库，回了一句
        「暂时没有查到相关规定」——**他明明已经把时间说了**。
        按问句处理等于惩罚说话客气的人。
        """
        turns = self.converse(["帮我约看房", "第一个", "本周六上午九点可以吗"])
        self.assertEqual(turns[2]["status"], "confirm",
                         "「…可以吗」是在回答时间，不是在提问")
        self.assertIn("09:00", turns[2]["action"]["summary"])

    def test_a_real_question_still_interrupts(self):
        """**放宽只针对「填得进这一格」的话。**

        「押金怎么退」填不进时间那一格，它就是插问：照常去答，
        并且把没做完的待办原样带回来。
        """
        turns = self.converse(["帮我约看房", "第一个", "押金怎么退"])
        self.assertEqual(turns[2]["status"], "answered")
        self.assertIsNotNone(turns[2].get("pending"), "待办不该被丢掉")

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


class TestEveryAskCanBeAnswered(unittest.TestCase):
    """**助手问出去的问题，自己必须接得住。**

    追问要带 `ask`（问的是哪个槽、有哪些可点选项）。少了它，
    `_resume` 那句 `if not ask: return None` 会直接放手，
    用户的回答落到知识检索上——实测过的两种表现：

        取消收藏 →「要取消收藏哪一套？」→ 用户答「DL0007」
                 → 「这个我没查到相关规定」
        确认预约 →「要处理哪一条？」   → 用户答「第一条」
                 → 「带身份证件即可，平台不收任何看房费用」

    第二种尤其糟：不是听不懂，是**答了一个毫不相干的问题**。
    这条测试对所有动作一起断言，就是为了不再一个一个地漏。
    """

    # 每个动作在「什么上下文都没有」时需要读到的数据。给全了才走得到追问，
    # 给不全会提前返回 NOT_APPLICABLE（「你没有待评价的租约」），
    # 那条路不是这里要测的。
    PAYLOADS = {
        "/api/bills/mine": {"list": [
            {"bill_no": "ZD001", "amount": 2200, "status": "unpaid",
             "bill_type": "租金", "due_date": "2026-09-01"},
            {"bill_no": "ZD002", "amount": 1800, "status": "unpaid",
             "bill_type": "水电", "due_date": "2026-09-10"}]},
        "/api/contracts/mine": {"list": [
            {"contract_no": "HT001", "status": "active", "house_no": "DL0001",
             "start_date": "2026-01-01", "end_date": "2026-12-31"},
            {"contract_no": "HT002", "status": "active", "house_no": "DL0002",
             "start_date": "2026-02-01", "end_date": "2027-01-31"}]},
        "/api/favorites": {"list": [
            {"house_no": "DL0007", "title": "中山区青泥洼桥 两室一厅"},
            {"house_no": "DL0008", "title": "中山区桂林街 两室一厅"}]},
        "/api/history": {"list": [
            {"house_no": "DL0009", "title": "西岗区北京街 两室一厅"},
            {"house_no": "DL0010", "title": "中山区海军广场 两室"}]},
        "/api/appointments/mine": {"list": [
            {"id": 1, "house_no": "DL0007", "status": "pending",
             "viewing_time": "2026-09-05 14:00:00"},
            {"id": 2, "house_no": "DL0008", "status": "pending",
             "viewing_time": "2026-09-06 10:00:00"}]},
        # 陷阱夹具：这些 id 不该出现在任何「我的预约」动作里。
        # /api/appointments 是公开的时段占用查询，不按人过滤
        "/api/appointments": {"list": [
            {"id": 11, "house_no": "DL0101", "status": "pending",
             "viewing_time": "2026-09-05 14:00:00"},
            {"id": 12, "house_no": "DL0102", "status": "pending",
             "viewing_time": "2026-09-06 10:00:00"}]},
        "/api/repairs": {"list": [
            {"id": 21, "content": "卧室门锁打不开", "status": "pending"},
            {"id": 22, "content": "厨房水管漏水", "status": "processing"}]},
        "/api/reviews/pending": {"list": [
            {"contract_no": "HT900", "house_no": "DL0201",
             "start_date": "2025-01-01", "end_date": "2025-12-31"},
            {"contract_no": "HT901", "house_no": "DL0202",
             "start_date": "2025-03-01", "end_date": "2026-02-28"}]},
        "/api/landlord/houses": {"list": [
            {"house_no": "DL0101", "title": "中山区人民路 三室",
             "status": "available"},
            {"house_no": "DL0102", "title": "西岗区北京街 两室",
             "status": "offline"}]},
        "/api/messages": {"list": [{"id": 1, "title": "合同已结束"}],
                          "unread": 1},
    }

    # 「确认签约」要的是待生效合同，和上面那份生效中的不是一回事
    PAYLOADS_EXTRA = {
        "/api/contracts/mine": {"list": [
            # 合同里租金的字段名是 rent（房源才是 rent_price）。
            # 用错名字卡片照样出，只是金额那一项会悄悄消失。
            {"contract_no": "HT800", "status": "pending", "house_no": "DL0300",
             "start_date": "2026-10-01", "end_date": "2027-09-30",
             "rent": 2600, "deposit": 2600},
            {"contract_no": "HT801", "status": "pending", "house_no": "DL0301",
             "start_date": "2026-11-01", "end_date": "2027-10-31",
             "rent": 3100, "deposit": 3100},
        ]},
        "/api/landlord/houses": {"list": [
            {"house_no": "DL0101", "title": "中山区人民路 三室",
             "status": "available", "verify_status": "none"},
            {"house_no": "DL0102", "title": "西岗区北京街 两室",
             "status": "offline", "verify_status": "none"},
        ]},
    }

    # 动作 -> 一句「什么都没指定」的说法
    BARE = {
        "pay_bill": "帮我交房租",
        "cancel_viewing": "帮我取消预约",
        "reschedule_viewing": "帮我改约",
        "book_viewing": "帮我约看房",
        "submit_repair": "帮我报修",
        "renew_contract": "帮我续租",
        "end_contract": "帮我退租",
        "favorite_house": "帮我收藏",
        "unfavorite_house": "取消收藏",
        "read_all_messages": "全部已读",
        "clear_history": "清空浏览足迹",
        "submit_review": "帮我评价",
        "handle_appointment": "确认预约",
        "update_repair": "关闭工单",
        "toggle_house": "把房源下架",
        "activate_contract": "确认签约",
        "verify_request": "申请核验",
        "update_profile": "改手机号",
        # 三张长表单。触发语很短，问题全在后面那十几轮里
        "publish_house": "我要发布房源",
        "sign_contract": "帮我发起签约",
        "edit_house": "改一下房源",
    }

    def test_every_action_is_covered_by_this_test(self):
        """新增动作时别忘了补一句说法，否则这条保护会悄悄漏掉它。"""
        self.assertEqual(set(self.BARE), set(actions.PROPOSERS))

    def test_need_slot_always_carries_an_ask(self):
        for name, text in self.BARE.items():
            with self.subTest(action=name):
                backend = RecordingBackend({**self.PAYLOADS, **self.PAYLOADS_EXTRA})
                status, message, card, ask = actions.propose(
                    name, text, "tok", backend, context={})
                if status != actions.NEED_SLOT:
                    continue
                self.assertIsNotNone(
                    ask, "%s 追问了「%s」却没带 ask，用户答什么都接不住"
                         % (name, message))
                self.assertTrue(ask.get("slot"), "%s 的 ask 没有槽位名" % name)

    def test_choices_are_clickable_not_crammed_into_the_sentence(self):
        """有多个候选时要给可点选项，而不是把编号拼进一句话让用户照着打。"""
        for name in ("unfavorite_house", "favorite_house", "submit_review",
                     "handle_appointment", "update_repair", "toggle_house",
                     "activate_contract", "verify_request"):
            with self.subTest(action=name):
                backend = RecordingBackend({**self.PAYLOADS, **self.PAYLOADS_EXTRA})
                status, message, _, ask = actions.propose(
                    name, self.BARE[name], "tok", backend, context={})
                self.assertEqual(status, actions.NEED_SLOT, name)
                self.assertTrue(ask["options"], "%s 没给可点选项" % name)
                self.assertNotIn("；", message,
                                 "%s 把选项拼进了句子里" % name)

    def test_the_answer_to_that_question_produces_a_card(self):
        """回答追问之后要拿到确认卡片——这是这条链真正闭环的地方。"""
        cases = [
            ("unfavorite_house", "取消收藏", "house_no", "DL0007"),
            ("submit_review", "帮我评价", "contract_no", "HT901"),
            # 这里填的是 /api/appointments/mine 里的 id。
            # 夹具里 /api/appointments 那份用的是另一批 id（11、12），
            # **是故意留的陷阱**：那个接口是「按房源查已占用时段」的公开
            # 接口，带上 token 也不按人过滤，一旦有人把这个动作改回去读它，
            # 房东就会在选项里看到别人的预约——这条用例会立刻红。
            ("handle_appointment", "确认预约", "appointment_id", "2"),
            ("update_repair", "关闭工单", "repair_id", "22"),
            ("toggle_house", "把房源下架", "house_no", "DL0101"),
            ("activate_contract", "确认签约", "contract_no", "HT801"),
            ("verify_request", "申请核验", "house_no", "DL0102"),
            ("update_profile", "改手机号", "phone", "13800001234"),
        ]
        for name, text, slot, value in cases:
            with self.subTest(action=name):
                backend = RecordingBackend({**self.PAYLOADS, **self.PAYLOADS_EXTRA})
                status, message, card, _ = actions.propose(
                    name, text, "tok", backend, context={slot: value})
                self.assertEqual(status, actions.CONFIRM,
                                 "%s 填了 %s 仍然没出卡片：%s"
                                 % (name, slot, message))
                self.assertEqual(card["name"], name)
                self.assertFalse(backend.writes, "提议阶段写了后端")

    def test_the_signing_card_spells_out_everything(self):
        """**确认签约是全系统金额最大的一次点击。**

        点下去合同生效、整个租期的账单一次性排出来。摘要里少一项金额，
        等于让人蒙着眼签字——这条盯的就是那几个数字都在。
        """
        backend = RecordingBackend({**self.PAYLOADS, **self.PAYLOADS_EXTRA})
        _, _, card, _ = actions.propose(
            "activate_contract", "确认签约", "tok", backend,
            context={"contract_no": "HT801"})
        for must in ("DL0301", "2026-11-01", "2027-10-31", "3100"):
            self.assertIn(must, card["summary"], must)
        self.assertEqual(card["risk"], actions.RISK_HIGH)
        self.assertIn("不可撤销", card["warning"])
        self.assertFalse(backend.writes, "提议阶段写了后端")

    def test_profile_only_ever_touches_the_phone(self):
        """**改密码不该走对话框。** 白名单只放行 phone，

        即使有人构造出 {"password": "..."}，也进不了后端。
        """
        self.assertEqual(actions.ALLOWED_PARAMS["update_profile"], {"phone"})
        backend = RecordingBackend(self.PAYLOADS)
        okay, message, _ = actions.execute(
            "update_profile", {"phone": "13800001234", "password": "x"},
            "tok", backend)
        self.assertFalse(okay)
        self.assertIn("参数不合法", message)
        self.assertFalse(backend.writes)

    def test_a_bad_phone_number_is_asked_again_not_saved(self):
        backend = RecordingBackend(self.PAYLOADS)
        status, message, card, ask = actions.propose(
            "update_profile", "手机号改成 12345", "tok", backend, context={})
        self.assertEqual(status, actions.NEED_SLOT)
        self.assertIsNotNone(ask)
        self.assertFalse(backend.writes)

    def test_a_form_only_collects_its_own_fields(self):
        """**表单只收自己声明过的字段。**

        上游会往 context 里塞 house_no（用户此刻正看着哪套房）。全盘收下的话
        它会混进卡片参数，执行时被白名单拦成「操作参数不合法」——
        卡片看着好好的，一点确认就报错，这类问题最难查。
        """
        from app.agents.support.tools import forms
        backend = RecordingBackend({**self.PAYLOADS, **self.PAYLOADS_EXTRA})
        filled = {"district": "中山区", "community": "测试小区",
                  "address": "测试路 1 号", "layout": "两室一厅", "area": 88,
                  "rent_price": 3200, "deposit": 3200, "rent_type": "整租",
                  "floor": "6/18", "orientation": "朝南", "title": "测试房源"}
        _, _, card, _ = actions.propose(
            "publish_house", "发布房源", "tok", backend,
            context=dict(filled, house_no="DL0007", _houses=[]))
        self.assertEqual(set(card["params"]), set(filled))
        self.assertEqual(set(card["params"]),
                         actions.ALLOWED_PARAMS["publish_house"])

    def test_a_none_slot_is_not_treated_as_filled(self):
        """**`str(None)` 是 "None"，非空。**

        上游没有房源上下文时给的就是 None。当成填过的后果实测过：
        「修改房源」「发起签约」默默跳过第一问——用户没被问过是哪一套，
        助手却认为问过了，接着拿一个字符串 "None" 当房源编号往下走。
        """
        from app.agents.support.tools import forms
        self.assertFalse(forms.is_filled({"house_no": None}, "house_no"))
        self.assertFalse(forms.is_filled({"house_no": ""}, "house_no"))
        self.assertFalse(forms.is_filled({}, "house_no"))
        self.assertTrue(forms.is_filled({"house_no": "DL0007"}, "house_no"))

        self.assertEqual(
            forms.next_missing("edit_house", {"house_no": None})["name"],
            "house_no")
        self.assertEqual(forms.progress("edit_house", {"house_no": None}),
                         (0, 3))

    def test_form_starts_at_the_first_question_without_context(self):
        backend = RecordingBackend({**self.PAYLOADS, **self.PAYLOADS_EXTRA})
        _, message, _, ask = actions.propose(
            "edit_house", "我要改一下房源", "tok", backend,
            context={"house_no": None})
        self.assertEqual(ask["slot"], "house_no")
        self.assertIn("1/3", message)

    def test_every_form_field_is_whitelisted(self):
        """表单字段与参数白名单必须一一对应，缺一项执行就会被拦。"""
        from app.agents.support.tools import forms
        for name, form in forms.FORMS.items():
            with self.subTest(form=name):
                fields = {f["name"] for f in form["fields"]}
                self.assertEqual(fields, actions.ALLOWED_PARAMS[name])

    def test_picked_option_is_the_one_that_ends_up_on_the_card(self):
        """点了第二条就得是第二条。选项与卡片对不上比不给选项更危险。"""
        backend = RecordingBackend({**self.PAYLOADS, **self.PAYLOADS_EXTRA})
        _, _, card, _ = actions.propose(
            "update_repair", "关闭工单", "tok", backend,
            context={"repair_id": "22"})
        self.assertEqual(str(card["params"]["repair_id"]), "22")
        self.assertIn("厨房水管漏水", card["summary"])

        backend = RecordingBackend({**self.PAYLOADS, **self.PAYLOADS_EXTRA})
        _, _, card, _ = actions.propose(
            "handle_appointment", "确认预约", "tok", backend,
            context={"appointment_id": "2"})
        self.assertEqual(str(card["params"]["appointment_id"]), "2")
        self.assertIn("DL0008", card["summary"])


class TestImperativeBeatsTheQueryTopic(unittest.TestCase):
    """**同一个词既是动作名也是查询主题时，祈使句要判成动作。**

    「收藏」「评价」「工单」「预约」这些词在两张表里都出现。规则的顺序是
    先动作后查询，所以只要动作词表漏了一种祈使说法，那句话就会被
    查询接走——用户说「帮我收藏」，助手回一句「你收藏了 2 套房源」。
    不是听不懂，是**答成了另一件事**，比听不懂更让人以为系统坏了。

    反过来也要守住：动作词表不能只写一个「收藏」，否则「我的收藏」
    也会变成要收藏。所以每一条都配一句必须仍然判成查询的对照。
    """

    IMPERATIVE = [
        ("帮我收藏", "favorite_house"),
        ("收藏这套", "favorite_house"),
        ("把它收藏起来", "favorite_house"),
        ("取消收藏", "unfavorite_house"),
        ("帮我评价", "submit_review"),
        ("给个好评", "submit_review"),
        ("帮我报修", "submit_repair"),
        ("帮我交房租", "pay_bill"),
        ("把站内消息都标记成已读", "read_all_messages"),
        ("清空我的浏览足迹", "clear_history"),
        ("把这条工单标记为已完成", "update_repair"),
        ("把房源下架", "toggle_house"),
        ("确认签约", "activate_contract"),
        ("申请核验", "verify_request"),
        ("改手机号", "update_profile"),
    ]

    # 说法很像、但问的是「有哪些」「到哪一步」——必须仍然是查询
    STILL_A_QUESTION = [
        "我的收藏", "我收藏了哪些房子", "我有哪些租约没评价",
        "我的报修到哪一步了", "我有几条未读消息", "浏览记录能删掉吗",
        "我的账单还差多少没交", "我预约的看房通过了吗",
    ]

    def test_imperative_phrasings_are_actions(self):
        for text, name in self.IMPERATIVE:
            with self.subTest(text=text):
                self.assertEqual(actions.detect(text), name, text)

    def test_questions_are_not_actions(self):
        for text in self.STILL_A_QUESTION:
            with self.subTest(text=text):
                self.assertEqual(actions.detect(text), "", text)



class TestRescheduleViewing(unittest.TestCase):
    """看房改期。**后端没有「改时间」这个接口**，所以它其实是两步。"""

    ROWS = {"/api/appointments/mine": {"list": [
        {"id": 11, "house_no": "DL0007", "status": "pending",
         "viewing_time": "2026-09-12 10:00:00"},
        {"id": 12, "house_no": "DL0009", "status": "confirmed",
         "viewing_time": "2026-09-20 14:00:00"},
    ]}}

    def test_asks_which_one_then_asks_the_new_time(self):
        backend = RecordingBackend(self.ROWS)
        status, message, card, ask = actions.propose(
            "reschedule_viewing", "帮我改约", "tok", backend, context={})
        self.assertEqual(status, actions.NEED_SLOT)
        self.assertEqual(ask["slot"], "appointment_id")
        self.assertTrue(ask["options"])

    def test_new_time_is_not_confused_with_the_old_one(self):
        """**「改到周日下午三点」里的时间是新的，不是旧的。**

        追问选中哪一条之后，`text` 里仍然带着那句话。如果直接拿它去解析，
        解析器读到的可能是旧预约的日期——用户就会看到一张
        「从 9-12 改到 9-12」的卡片。
        """
        backend = RecordingBackend(self.ROWS)
        status, message, card, ask = actions.propose(
            "reschedule_viewing", "改到这周日下午三点", "tok", backend,
            context={"appointment_id": "11", "new_time": "这周日下午三点"})
        self.assertEqual(status, actions.CONFIRM)
        self.assertIn("改到", card["summary"])
        self.assertNotEqual(card["params"]["viewing_time"],
                            "2026-09-12 10:00")

    def test_execute_books_the_new_slot_before_dropping_the_old(self):
        """**顺序不能反。**

        先取消的话，新时段一旦建不出来（时段冲突、房源下架），
        用户就从「有一条看房」变成了「一条都没有」——而他只是想换个时间。
        """
        backend = RecordingBackend(self.ROWS)
        actions.execute("reschedule_viewing",
                        {"appointment_id": 11, "house_no": "DL0007",
                         "viewing_time": "2026-09-13 15:00"}, "tok", backend)
        wrote = [c for c in backend.writes
                 if c[0] in ("POST", "PATCH") and "appointment" in c[1]]
        self.assertEqual(wrote[0][0], "POST", "应当先约新的")
        self.assertEqual(wrote[1][0], "PATCH", "再退旧的")

    def test_says_so_when_the_old_one_could_not_be_cancelled(self):
        """新的约上了、旧的没退掉——**必须说出来**。

        不说的话用户以为只剩一条，房东那边却收到了两次带看请求。
        """
        class HalfBroken(RecordingBackend):
            def _request(self, method, path, params=None, payload=None,
                         token=None):
                if method == "PATCH":
                    raise RuntimeError("接口挂了")
                return super()._request(method, path, params, payload, token)

        backend = HalfBroken(self.ROWS)
        ok, message, _ = actions.execute(
            "reschedule_viewing",
            {"appointment_id": 11, "house_no": "DL0007",
             "viewing_time": "2026-09-13 15:00"}, "tok", backend)
        self.assertIn("没能取消", message)
        self.assertIn("手动", message)


class TestCancelNarrowsByTime(unittest.TestCase):
    """用户已经说了是哪一条，就别再把选择题丢回去。

    **日期一律从今天推算，不写死。** 这一组原本把预约钉在
    2026-09-12/20/21，句子里说「取消 9 月 20 号那个看房」。
    过了 9-20 之后，解析器按「这个日子已经过去，那说的是明年」
    把它解成 2027-09-20，筛不到任何一条，于是退回三选一——
    **红的不是功能，是用例自己过期了**。

    `_narrow_by_time` 走的是生产路径（`parse_datetime` 不注入 now），
    所以只能反过来：让预约落在今天之后，句子也从同一个日期生成。
    同一类过期在 AI-06 的验收集上已经发生过一次，那次是「只有周六
    才失败」，藏得更深。时间相关的用例写死具体日期，迟早都会到期。
    """

    @staticmethod
    def _day(offset):
        """今天往后第 offset 天，连同「用户会怎么说这一天」一起给出。

        offset 必须在一年以内：解析器的「已经过去就算明年」规则，
        只有在这个范围内才会把这句话还原回同一天。
        """
        day = datetime.date.today() + datetime.timedelta(days=offset)
        return day, "%d 月 %d 号" % (day.month, day.day)

    def setUp(self):
        first, _ = self._day(2)
        second, self.second_said = self._day(9)
        third, _ = self._day(30)
        # 谁都不是的那一天——用来测「筛空了怎么办」
        _, self.nobody_said = self._day(45)
        self.rows = {"/api/appointments/mine": {"list": [
            {"id": 11, "house_no": "DL0007", "status": "pending",
             "viewing_time": first.strftime("%Y-%m-%d") + " 10:00:00"},
            {"id": 12, "house_no": "DL0009", "status": "confirmed",
             "viewing_time": second.strftime("%Y-%m-%d") + " 14:00:00"},
            {"id": 13, "house_no": "DL0008", "status": "pending",
             "viewing_time": third.strftime("%Y-%m-%d") + " 09:00:00"},
        ]}}

    def test_a_time_in_the_sentence_picks_the_appointment(self):
        backend = RecordingBackend(self.rows)
        status, message, card, ask = actions.propose(
            "cancel_viewing", "取消 %s那个看房" % self.second_said,
            "tok", backend, context={})
        self.assertEqual(status, actions.CONFIRM)
        self.assertEqual(card["params"]["appointment_id"], 12)

    def test_still_asks_when_the_time_matches_nothing(self):
        """**筛空了要退回全部，不能悄悄缩到错误的那一条上。**

        解析错一次，取消掉的就是另一场看房。
        """
        backend = RecordingBackend(self.rows)
        status, message, card, ask = actions.propose(
            "cancel_viewing", "取消 %s那个看房" % self.nobody_said,
            "tok", backend, context={})
        self.assertEqual(status, actions.NEED_SLOT)
        self.assertEqual(len(ask["options"]), 3)


class TestOptionMatchingIsUnambiguous(unittest.TestCase):
    """选项对应错了，执行的就是**另一件事**。

    这一组全部来自一次实测事故：用户在七条预约里点了
    「2026-09-12 09:00 DL0007」，助手却把 DL0075 那条改了期。
    """

    OPTIONS = [{"value": "21", "label": "2026-09-13 15:00 DL0007"},
               {"value": "22", "label": "2026-09-12 09:00 DL0007"},
               {"value": "17", "label": "2026-09-06 16:00 DL0075"}]

    def test_a_short_numeric_id_is_not_matched_inside_another_number(self):
        """**「2026」里含着「20」。**

        预约、工单的选项值是自增 id，两位数很常见。按子串找的话，
        任何带年份或时间的回答都会随机命中一条。
        """
        self.assertEqual(
            actions.pick_option("2026-09-12 09:00 DL0007", self.OPTIONS), "22")

    def test_saying_the_id_outright_still_works(self):
        self.assertEqual(actions.pick_option("22", self.OPTIONS), "22")

    def test_the_option_that_matches_most_wins_not_the_first_one(self):
        """几条预约共用同一个房源编号时，从上往下扫永远选中第一条。"""
        self.assertEqual(
            actions.pick_option("2026-09-06 16:00 DL0075", self.OPTIONS), "17")

    def test_an_ambiguous_answer_picks_nothing(self):
        """**并列第一说明他说的话本身就分不出来。**

        这时候按顺序猜一个，就是在赌；宁可回去再问一次。
        """
        self.assertEqual(actions.pick_option("DL0007", self.OPTIONS), "")


if __name__ == "__main__":
    unittest.main()
