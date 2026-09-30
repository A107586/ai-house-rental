# -*- coding: utf-8 -*-
"""agent 版统一入口：循环本身、工具层的边界、main 里的接线。

模型换成按剧本出牌的假模型，工具的后端换成假的：这里验的是**机制**——
工具结果能回到模型手里、写操作出卡片就停、编出来的工具名和参数不被执行、
序号只能从屏幕上的列表里查、确认卡片的「好的」仍走确定性流程。
模型答得好不好是另一件事，那要接真模型跑评测（scripts/eval_assistant_agent.py）。
不依赖网络、数据库与大模型。
"""

import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import main  # noqa: E402
from app.agents.assistant import loop  # noqa: E402
from app.agents.assistant.toolkit import Toolkit, _whitelist  # noqa: E402
from app.llm.base import LLMUnavailable  # noqa: E402
from app.main import ChatRequest  # noqa: E402


class ScriptedLLM:
    """按顺序吐出预设的回复，并记下每次看到的消息。"""

    available = True

    def __init__(self, *replies):
        self.replies = [r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)
                        for r in replies]
        self.calls = []

    def complete(self, system, prompt, max_tokens=None, deadline=None):
        # 只为让 llm_budget.DeadlineProvider 能检查签名；agent 只用 chat
        raise LLMUnavailable("agent 不该调 complete")

    def chat(self, system, messages, max_tokens=None, deadline=None, temperature=0.0):
        self.calls.append({"system": system, "messages": [dict(m) for m in messages]})
        if not self.replies:
            raise LLMUnavailable("剧本演完了")
        return self.replies.pop(0)


def deps(**over):
    base = dict(
        logged_in=True, role="tenant",
        runner_ctx={"last_house_nos": ["DL0007", "DL0041"]},
        run_read=lambda name, params, ctx: {
            "message": "找到 2 套", "total": 2,
            "items": [{"house_no": "DL0007", "title": "两室一厅", "rent_price": 3050},
                      {"house_no": "DL0041", "title": "一室一厅", "rent_price": 2550}]},
        knowledge=lambda q: {"status": "answered", "message": "押金 7 个工作日内原路退还。",
                             "citations": [{"title": "押金怎么退"}]},
        recommend=lambda: {"items": [{"house_no": "DL0099"}]},
        propose=lambda action, params, text, images: {
            "status": "confirm", "message": "要我帮你缴纳 3 月房租 3050 元吗？",
            "action": {"name": action, "summary": "缴纳 3 月房租 3050 元"},
            "pending": {"action": action, "card": {"summary": "缴纳 3 月房租 3050 元"}}},
    )
    base.update(over)
    return SimpleNamespace(**base)


def run(llm, text="你好", images=(), **dep):
    return loop.run(text, list(images), llm, Toolkit(deps(**dep)))


class TestLoop(unittest.TestCase):

    def test_direct_answer(self):
        llm = ScriptedLLM({"thought": "寒暄", "answer": "你好，想找什么样的房子？"})
        out = run(llm)
        self.assertEqual(out["message"], "你好，想找什么样的房子？")
        self.assertEqual(out["trace"], [])

    def test_tool_result_goes_back_to_the_model(self):
        llm = ScriptedLLM({"thought": "找房", "tool": "search_houses", "params": {"district": "中山区"}},
                          {"answer": "中山区有 2 套，最便宜的是 DL0041。"})
        out = run(llm, "中山区的房子")
        self.assertEqual(out["artifacts"]["items"][0]["house_no"], "DL0007")
        seen = llm.calls[1]["messages"][-1]["content"]
        self.assertIn("工具 search_houses 的结果", seen)
        self.assertIn("DL0041", seen)
        self.assertEqual(out["trace"][0]["tool"], "search_houses")

    def test_multi_step(self):
        llm = ScriptedLLM({"tool": "my_bills", "params": {}},
                          {"tool": "knowledge", "params": {"question": "逾期怎么算"}},
                          {"answer": "你有 2 笔；逾期按合同约定。"})
        out = run(llm, "我的账单逾期了会怎样")
        self.assertEqual([s["tool"] for s in out["trace"]], ["my_bills", "knowledge"])
        self.assertEqual(out["artifacts"]["citations"], [{"title": "押金怎么退"}])

    def test_write_tool_stops_with_the_card(self):
        llm = ScriptedLLM({"tool": "pay_bill", "params": {"request": "交 3 月房租"}})
        out = run(llm, "帮我把房租交了")
        self.assertEqual(out["stopped_by"], "pay_bill")
        self.assertEqual(out["message"], "要我帮你缴纳 3 月房租 3050 元吗？")
        self.assertEqual(out["artifacts"]["action"]["name"], "pay_bill")
        self.assertEqual(len(llm.calls), 1)          # 出卡片后不再问模型

    def test_write_that_cannot_be_done_goes_back_to_the_model(self):
        llm = ScriptedLLM({"tool": "pay_bill", "params": {"request": "交房租"}},
                          {"answer": "你的账单都结清了。"})
        out = run(llm, "交房租", propose=lambda *a: {"status": "not_applicable",
                                                  "message": "你的账单都结清了，没有需要缴的。"})
        self.assertIsNone(out["stopped_by"])
        self.assertIn("结清", llm.calls[1]["messages"][-1]["content"])

    def test_unknown_tool_is_not_run(self):
        called = []
        llm = ScriptedLLM({"tool": "delete_everything", "params": {}}, {"answer": "好的"})
        out = run(llm, run_read=lambda *a: called.append(a))
        self.assertEqual(called, [])
        self.assertIn("没有叫「delete_everything」的工具", llm.calls[1]["messages"][-1]["content"])
        self.assertEqual(out["trace"][0]["error"], "unknown_tool")

    def test_repeat_call_is_not_rerun(self):
        count = []
        read = lambda *a: count.append(1) or {"message": "ok"}   # noqa: E731
        call = {"tool": "my_bills", "params": {}}
        llm = ScriptedLLM(call, call, {"answer": "好了"})
        run(llm, run_read=read)
        self.assertEqual(len(count), 1)

    def test_step_limit_forces_an_answer(self):
        calls = [{"tool": "search_houses", "params": {"rent_max": i}} for i in range(loop.MAX_STEPS)]
        llm = ScriptedLLM(*calls, {"answer": "给你列在下面了。"})
        out = run(llm)
        self.assertEqual(out["message"], "给你列在下面了。")
        self.assertIn("直接回答", llm.calls[-1]["messages"][-1]["content"])

    def test_plain_text_reply_counts_as_answer(self):
        out = run(ScriptedLLM("押金一般退租后 7 个工作日内退还。"))
        self.assertEqual(out["message"], "押金一般退租后 7 个工作日内退还。")

    def test_garbage_twice_raises(self):
        with self.assertRaises(LLMUnavailable):
            run(ScriptedLLM("{坏的", "{还是坏的"))

    def test_images_go_to_the_model(self):
        llm = ScriptedLLM({"answer": "是厨房"})
        run(llm, "这是哪", images=["data:image/jpeg;base64,xx"])
        first = llm.calls[0]["messages"][-1]["content"]
        self.assertEqual(first[0]["type"], "image_url")
        self.assertEqual(first[-1]["text"], "这是哪")

    def test_dialogue_and_context_reach_the_model(self):
        llm = ScriptedLLM({"answer": "嗯"})
        loop.run("那第二套呢", [], llm, Toolkit(deps()),
                 context_lines=["屏幕上的房源列表：1. DL0007  2. DL0041"],
                 dialogue=[{"role": "user", "content": "中山区的房子"},
                           {"role": "assistant", "content": "找到 2 套"}])
        call = llm.calls[0]
        self.assertIn("DL0041", call["system"])
        self.assertEqual(call["messages"][0]["content"], "中山区的房子")


class TestToolkit(unittest.TestCase):

    def test_anonymous_users_do_not_see_account_or_write_tools(self):
        names = {t["name"] for t in Toolkit(deps(logged_in=False, role="")).tools()}
        self.assertIn("search_houses", names)
        self.assertIn("knowledge", names)
        self.assertNotIn("my_bills", names)
        self.assertNotIn("pay_bill", names)
        self.assertNotIn("converse", names)

    def test_write_tools_take_a_request(self):
        tools = {t["name"]: t for t in Toolkit(deps()).tools()}
        self.assertIn("request", tools["book_viewing"]["params"])

    def test_whitelist(self):
        tool = {"params": {"rent_max": ("number", ""), "features": ("string[]", ""),
                           "district": ("string", "")}}
        self.assertEqual(_whitelist({"rent_max": "3000", "features": "地铁", "district": "中山区",
                                     "evil": "x", "district2": {"a": 1}}, tool),
                         {"rent_max": 3000, "features": ["地铁"], "district": "中山区"})
        self.assertEqual(_whitelist({"rent_max": "三千"}, tool), {})

    def test_knowledge_without_basis_says_so(self):
        kit = Toolkit(deps(knowledge=lambda q: {"status": "refused", "message": "暂时没有查到",
                                                "alternatives": [{"question": "押金怎么退"}],
                                                "handoff": {"reason": "no_basis"}}))
        out = kit.call("knowledge", {"question": "能养蛇吗"})
        self.assertFalse(out.observation["有依据"])
        self.assertIn("不要凭常识", out.observation["说明"])
        self.assertEqual(out.artifacts["handoff"], {"reason": "no_basis"})
        self.assertEqual(out.artifacts["citations"], [])

    def test_account_rows_under_any_key_with_money_summary(self):
        bills = [{"id": i, "bill_no": "ZD%d" % i, "type": "rent", "amount": a}
                 for i, a in enumerate([750, 3050, 120])]
        kit = Toolkit(deps(run_read=lambda *a: {"message": "3 笔待缴",
                                                "account": {"tool": "my_bills",
                                                            "data": {"unpaid": bills, "total": 3}}}))
        obs = kit.call("my_bills", {}).observation
        self.assertEqual(obs["unpaid_条数"], 3)
        self.assertEqual(obs["unpaid_最大一笔"]["bill_no"], "ZD1")
        self.assertEqual(obs["unpaid_合计"], 3920)
        self.assertNotIn("id", obs["unpaid"][0])

    def test_out_of_range_ordinal_is_explained(self):
        kit = Toolkit(deps(run_read=lambda *a: {"_out_of_range": True}))
        self.assertIn("一共只有 2 套", kit.call("house_detail", {"ordinal": 9}).observation)


def request(**kw):
    kw.setdefault("session_id", "")
    return ChatRequest(**kw)


class TestMainWiring(unittest.TestCase):

    def setUp(self):
        patches = [
            mock.patch.object(main.settings, "assistant_mode", "agent"),
            mock.patch.object(main.backend_client, "list_chat", return_value=[]),
            mock.patch.object(main.backend_client, "log_chat"),
            mock.patch.object(main, "_whoami", return_value={"logged_in": True, "role": "tenant"}),
            mock.patch.object(main, "_fetch_houses", return_value=[]),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_agent_is_the_default_path(self):
        llm = ScriptedLLM({"answer": "你好"})
        with mock.patch.object(main, "provider", llm):
            result = main.agent_chat(request(text="你好"))
        self.assertEqual(result["message"], "你好")
        self.assertEqual(result["route_source"], "agent")

    def test_unavailable_model_says_so_instead_of_rules(self):
        with mock.patch.object(main, "provider", SimpleNamespace(available=False)):
            result = main.agent_chat(request(text="押金怎么退"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "unavailable")

    def test_confirming_a_card_stays_deterministic(self):
        pending = {"action": "pay_bill", "card": {"summary": "缴纳 3 月房租"}}
        llm = ScriptedLLM()
        with mock.patch.object(main, "provider", llm), \
             mock.patch.object(main.support, "run", return_value={"message": "好的", "auto_confirm": True}) as run_:
            result = main.agent_chat(request(text="好的", token="t", pending=pending))
        self.assertEqual(llm.calls, [])
        self.assertEqual(run_.call_args.kwargs["pending"], pending)
        self.assertEqual(result["route_source"], "protocol")

    def test_photo_answers_an_upload_ask(self):
        pending = {"action": "submit_repair", "slots": {},
                   "ask": {"slot": "images", "question": "要拍张照片吗？", "options": [], "kind": "upload"}}
        with mock.patch.object(main, "provider", ScriptedLLM()), \
             mock.patch.object(main.support, "run", return_value={"message": "要我帮你提交报修吗？"}) as run_:
            main.agent_chat(request(text="", token="t", pending=pending,
                                    images=["/static/uploads/a.jpg"]))
        self.assertEqual(run_.call_args.args[0], "/static/uploads/a.jpg")

    def test_interrupting_keeps_the_pending_form(self):
        pending = {"action": "book_viewing", "slots": {"house_no": "DL0007"},
                   "ask": {"slot": "viewing_time", "question": "什么时候去看？"}}
        with mock.patch.object(main, "provider", ScriptedLLM({"answer": "押金退租后退。"})):
            result = main.agent_chat(request(text="押金怎么退", token="t", pending=pending))
        self.assertEqual(result["pending"], pending)

    def test_propose_resolves_ordinal_from_the_screen(self):
        payload = request(text="约第二套", token="t", last_house_nos=["DL0007", "DL0041"])
        with mock.patch.object(main.support_actions, "propose",
                               return_value=(main.support_actions.CONFIRM, "", {"summary": "预约", "risk": "low"}, None)) as prop:
            main._agent_propose(payload, "book_viewing", {"request": "约看房", "ordinal": 2}, "", [])
        self.assertEqual(prop.call_args.args[4]["house_no"], "DL0041")

    def test_propose_rejects_ordinal_out_of_range(self):
        payload = request(text="约第九套", token="t", last_house_nos=["DL0007"])
        out = main._agent_propose(payload, "book_viewing", {"request": "约看房", "ordinal": 9}, "", [])
        self.assertIn("没有第 9 套", out["message"])

    def test_repair_never_targets_the_house_being_browsed(self):
        payload = request(text="水龙头滴水", token="t", house_no="DL0001",
                          last_house_nos=["DL0001", "DL0002"])
        with mock.patch.object(main.support_actions, "propose",
                               return_value=(main.support_actions.CONFIRM, "", {"summary": "报修", "risk": "medium"}, None)) as prop:
            main._agent_propose(payload, "submit_repair",
                                {"request": "厨房水龙头滴水", "house_no": "DL0001", "ordinal": 1}, "", [])
        self.assertNotIn("house_no", prop.call_args.args[4])

    def test_propose_ignores_made_up_house_numbers(self):
        payload = request(text="约 DL9999", token="t", last_house_nos=["DL0007"])
        with mock.patch.object(main.support_actions, "propose",
                               return_value=(main.support_actions.NEED_SLOT, "哪一套？", None,
                                             {"slot": "house_no", "question": "哪一套？"})) as prop:
            main._agent_propose(payload, "book_viewing", {"request": "约看房", "house_no": "DL9999"}, "", [])
        self.assertNotEqual(prop.call_args.args[4].get("house_no"), "DL9999")


if __name__ == "__main__":
    unittest.main()
