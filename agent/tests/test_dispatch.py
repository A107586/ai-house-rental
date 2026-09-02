# -*- coding: utf-8 -*-
"""统一对话入口 `/agent/chat` 的回归测试。

**这个文件是补出来的，起因是一次真实事故。**

`property_finder.understand()` 从返回三个值改成四个（融合层上线），
改动带了完整的单元测试，但**没人改调用方 `main.py::_dispatch`**。
于是 `/agent/chat` 里所有找房请求都抛 `ValueError: too many values to unpack`，
前端把 500 显示成「智能体暂时联系不上，稍后再试」——用户看到的是
「连不上服务器」，实际上服务好好地跑着。

已有的测试为什么没发现：它们全都直接测子智能体的 `understand()` 与 `run()`，
**没有任何一条走过 `_dispatch`**。子智能体的测试再多，也测不出
「调用方跟被调用方对不上」这类问题——那正是集成层才会有的缺陷。

所以这里的用例都从 `agent_chat` 这个入口进，每个意图分支至少走一次。
它们跑得不快（要建知识索引），但拦住的是**最贵的一类 bug**：
子模块全绿、系统不可用。

全部运行在纯规则模式，不依赖网络、数据库与大模型。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agents.support import agent as support  # noqa: E402
from app.main import ChatRequest, agent_chat  # noqa: E402


def ask(text, **kwargs):
    return agent_chat(ChatRequest(text=text, session_id="test", **kwargs))


class TestChatDispatch(unittest.TestCase):
    """每个意图分支都要能走通，且返回形状一致。"""

    @classmethod
    def setUpClass(cls):
        support.reset_knowledge_base()

    def assert_shape(self, result):
        """所有分支必须返回同一种形状，前端只处理一种结构。"""
        for key in ("intent", "status", "ok", "message"):
            self.assertIn(key, result, "缺少字段 %s：%s" % (key, result))
        self.assertIsInstance(result["message"], str)
        self.assertTrue(result["message"], "message 不该为空")

    def test_search_intent_does_not_crash(self):
        """**这条就是那次事故的回归测试。**

        `understand()` 的返回值个数一变，这里立刻红。
        """
        result = ask("中山区两室一厅")
        self.assert_shape(result)
        self.assertEqual(result["intent"], "search")
        self.assertIn("items", result)

    def test_search_carries_divergence_field(self):
        """融合层的分歧说明要透传到接口，答辩时要展示「模型改了什么」。"""
        result = ask("中山区两室一厅")
        self.assertIn("divergence", result)
        self.assertIsInstance(result["divergence"], list)

    def test_search_keeps_context_across_turns(self):
        """多轮：第二句只说预算，也该被理解成继续找房。"""
        first = ask("中山区两室一厅")
        self.assertEqual(first["intent"], "search")
        second = ask("三千以内的", history=["中山区两室一厅"])
        self.assert_shape(second)
        self.assertEqual(second["intent"], "search")

    def test_support_intent_answers_with_citation(self):
        result = ask("押金怎么退")
        self.assert_shape(result)
        self.assertEqual(result["intent"], "support")
        self.assertEqual(result["status"], "answered")
        self.assertTrue(result["citations"])

    def test_unknown_falls_back_to_support_not_a_dead_end(self):
        """认不出意图时要走检索兜底，而不是甩一句「没太理解」。

        意图关键词表覆盖不了所有说法，但答案可能就写在知识库里。
        有可答性闸门兜底，检索不到照样拒答，不存在编造风险。
        """
        result = ask("电子合同有效吗")
        self.assert_shape(result)
        self.assertEqual(result["status"], "answered")
        self.assertTrue(result["citations"])

    def test_unknown_and_unanswerable_still_guides(self):
        """既认不出意图、检索也没依据时，仍要给出路而不是死路。"""
        result = ask("帮我订个外卖")
        self.assert_shape(result)
        self.assertEqual(result["status"], "refused")
        self.assertTrue(result["alternatives"], "拒答必须给「猜你想问」")

    def test_suggestions_are_not_noise_when_nothing_matches(self):
        """检索完全没沾边时，「猜你想问」不能拿 top-3 充数。

        曾经问一个完全无关的问题，检索前三名是「入住时水电表读数要核对吗」
        「站内消息在哪看」这种毫不相关的条目，推给用户比不推更糟。
        """
        result = ask("帮我订个外卖")
        joined = "".join(result["alternatives"])
        self.assertNotIn("水电表", joined)
        self.assertNotIn("站内消息", joined)

    def test_expected_income_reaches_the_landlord_dashboard(self):
        """「预期收入」曾经是「不理解」的典型：一个意图关键词都不命中，
        落到 unknown 分支被甩一句「没太理解你的意思」。

        它其实是房东在问自己名下房源的经营数据，答案在 landlord/dashboard
        里，而不是 FAQ 里。现在应当走查库（未登录时提示登录），
        **绝不能拿 FAQ 编一个收入数字**。
        """
        result = ask("预期收入")
        self.assert_shape(result)
        self.assertEqual(result["status"], "need_login")
        self.assertEqual(result["retrieval"]["mode"], "account")
        self.assertEqual(result["retrieval"]["tool"], "income")
        self.assertEqual(result["citations"], [],
                         "事务型问题不该带 FAQ 引用")

    def test_contract_knowledge_question_is_answered_not_redirected(self):
        """规则型合同问题该直接答，不该一律支去点「智能审阅」。"""
        result = ask("电子合同有效吗")
        self.assert_shape(result)
        self.assertEqual(result["status"], "answered")

    def test_contract_review_request_is_redirected(self):
        """审阅型才需要合同正文，统一入口拿不到，引导到合同页。"""
        result = ask("帮我看看合同有没有坑")
        self.assert_shape(result)
        self.assertEqual(result["status"], "need_content")
        self.assertIn("我的租约", result["message"])

    def test_repair_intent_previews_without_submitting(self):
        """统一入口只做分析预览，落库要走 /agent/repair 并带房源编号。"""
        result = ask("厨房水管漏水了")
        self.assert_shape(result)
        self.assertEqual(result["intent"], "repair")

    def test_booking_intent_does_not_crash(self):
        result = ask("预约这周六下午看房")
        self.assert_shape(result)
        self.assertEqual(result["intent"], "booking")

    def test_transactional_question_without_token_asks_login(self):
        """「我的账单还差多少」要走查库，未登录时引导登录，
        绝不能拿 FAQ 编一个听起来合理的金额。"""
        result = ask("我的账单还差多少没交")
        self.assert_shape(result)
        self.assertEqual(result["status"], "need_login")

    def test_empty_input_is_handled(self):
        result = ask("   ")
        self.assert_shape(result)

    def test_every_branch_reports_context_text(self):
        """非找房的轮次也要带回上下文，否则界面上「当前记住」
        会被一句「帮我报修」清空，用户以为条件丢了。"""
        for text in ["押金怎么退", "厨房水管漏水了", "预约这周六下午看房"]:
            result = ask(text)
            self.assertIn("context_text", result, "「%s」缺 context_text" % text)


if __name__ == "__main__":
    unittest.main()
