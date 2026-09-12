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

from app import memory  # noqa: E402
from app.agents.support import agent as support  # noqa: E402
from app.llm.base import LLMProvider, LLMUnavailable  # noqa: E402
from app import main  # noqa: E402
from app.main import ChatRequest, agent_chat  # noqa: E402
from unittest import mock  # noqa: E402


class _FakeRouterProvider(LLMProvider):
    """只用来驱动统一路由的假大模型：路由那一问返回 payload，其余一律不可用。

    `complete` 保持不可用，是为了让链路的其余部分（组织回答、润色）
    仍旧走规则——这条测试要验的是路由接得上，不是文案好不好看。
    """

    available = True

    def __init__(self, payload):
        self.payload = payload

    def complete_json(self, system, prompt, max_tokens=None):
        if "路由" in system:
            return self.payload
        raise LLMUnavailable("这条测试只放行路由")

    def complete(self, system, prompt, max_tokens=None):
        raise LLMUnavailable("这条测试只放行路由")


def patched_provider(payload):
    """把 main 里的全局 provider 临时换成假的。"""
    return mock.patch("app.main.provider", _FakeRouterProvider(payload))


def ask(text, house_no="", **kwargs):
    """从 /agent/chat 入口提一个问题。

    `house_no` 通过 last_house_nos 注入——统一入口就是这么拿到
    「第一套」指的是哪一套的。
    """
    if house_no:
        kwargs.setdefault("last_house_nos", [house_no])
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

    def test_llm_routed_service_reaches_the_recommend_agent(self):
        """路由判成「推荐房源」时，统一入口要真的走 AI-07，不是回一句客套话。

        规则模式下路由永远给不出 service，所以这条必须用假大模型走一遍——
        否则「模型能选，但选了没人接」这类断链没有任何测试拦得住。
        """
        with patched_provider({"kind": "service", "name": "recommend_house"}):
            result = ask("有什么合适的房子推荐吗")
        self.assert_shape(result)
        self.assertEqual(result["intent"], "recommend")

    def test_llm_routed_search_falls_through_to_the_search_branch(self):
        """search_house 不自己处理，要交回找房分支——历史槽位只有那里有。"""
        with patched_provider({"kind": "service", "name": "search_house"}):
            result = ask("中山区两室一厅")
        self.assert_shape(result)
        self.assertEqual(result["intent"], "search")

    def test_pending_action_outranks_the_router(self):
        """代办没做完时，「都可以」这种半句话要回到代办层，不能被路由劫走。"""
        with patched_provider({"kind": "service", "name": "search_house"}):
            result = ask("都可以", pending={"action": "pay_bill",
                                            "slot": "bill_no", "slots": {}})
        self.assert_shape(result)
        self.assertNotEqual(result["intent"], "search")

    def test_vague_search_still_answers_with_something(self):
        """一个条件都没说时不能只回一句「说得具体一点」。

        用户正是因为说不具体才来问的。没登录也就没有画像，推荐给不出结果，
        那时回退到找房提示才算合理——但话术里至少要给一条能照着说的例子。
        """
        result = ask("有什么合适的房源推荐吗")
        self.assert_shape(result)
        self.assertTrue(result.get("items") or "中山区" in result["message"])

    def test_repair_intent_previews_without_submitting(self):
        """统一入口只做分析预览，落库要走 /agent/repair 并带房源编号。"""
        result = ask("厨房水管漏水了")
        self.assert_shape(result)
        self.assertEqual(result["intent"], "repair")

    def test_booking_request_goes_to_the_action_layer(self):
        """预约请求要走代办层出确认卡片，不再交给旧的 booking 子智能体。

        旧 agent 只会解析时间然后回一句「请到具体房源页面点预约看房」——
        用户明明是来让助手代办的，却被打发回页面上自己点。
        """
        result = ask("预约这周六下午看房")
        self.assert_shape(result)
        self.assertEqual(result["intent"], "support")
        self.assertEqual(result["retrieval"].get("mode"), "action")
        self.assertEqual(result["retrieval"].get("action"), "book_viewing")

    def test_booking_choices_stay_with_previous_search_results(self):
        """A follow-up booking request must not introduce unrelated houses."""
        shown = ["DL0007", "DL0008", "DL0009"]
        result = ask("预约看房", token="token", last_house_nos=shown)
        self.assertEqual(result["status"], "need_slot")
        self.assertEqual([option["value"] for option in result["options"]], shown)

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


class TestActionFirstRouting(unittest.TestCase):
    """代办要先于意图路由。**这一组直接对应用户反馈的「一点也不能用」。**

    supervisor 会把「报修」判成 repair、「预约看房」判成 booking，
    路由到各自的子智能体。可那些子智能体只会分析与追问，**没有确认卡片，
    也不会真的落库**——用户说「帮我报修，断电了」，得到一句
    「已识别为电路故障、紧急程度紧急」，然后就没有然后了。
    他追问「你不能像查房源一样帮我弹框办理吗」，问的正是这个。
    """

    def setUp(self):
        support.reset_knowledge_base()

    def test_repair_request_produces_a_card_not_an_analysis(self):
        result = ask("帮我报修，厨房断电了", token="")
        # 未登录时至少要走到代办分支要求登录，而不是被 repair 子智能体
        # 拿去做一次「分析预览」
        self.assertEqual(result["retrieval"].get("mode"), "action")
        self.assertEqual(result["retrieval"].get("action"), "submit_repair")

    def test_booking_request_goes_through_the_action_layer(self):
        result = ask("帮我预约看房", token="")
        self.assertEqual(result["retrieval"].get("mode"), "action")

    def test_new_search_interrupts_pending_booking(self):
        pending = {
            "action": "book_viewing",
            "slots": {"house_no": "DL0007"},
            "ask": {"slot": "viewing_time", "question": "什么时候去看？"},
        }
        result = ask("帮我找中山区两室一厅", token="token", pending=pending)
        self.assertEqual(result["intent"], "search")
        self.assertIn("items", result)
        self.assertIsNone(result.get("pending"))

    def test_plain_search_still_goes_to_the_finder(self):
        """代办优先不能把普通找房也抢走。"""
        result = ask("中山区两室一厅")
        self.assertEqual(result["intent"], "search")
        self.assertIn("items", result)


class TestHouseSpecificQuestions(unittest.TestCase):
    """针对某一套房的提问，定向取房源详情，不走检索。

    「讲解第一套的收费」以前一路走到检索、被闸门拒掉，
    用户看到「这个我没查到相关规定」——可信息就在房源页上。
    """

    def setUp(self):
        support.reset_knowledge_base()

    def test_house_question_without_context_is_not_hijacked(self):
        """没有房源上下文时不能瞎答——「多大面积」得先知道问的是哪套。"""
        from app.agents.support.tools import house as house_tool
        self.assertEqual(house_tool.asks_about_house("多大面积", ""), "")

    def test_aspect_detection(self):
        from app.agents.support.tools import house as house_tool
        self.assertEqual(house_tool.asks_about_house("讲解收费", "DL0007"), "rent")
        self.assertEqual(house_tool.asks_about_house("多大面积", "DL0007"), "size")
        self.assertEqual(house_tool.asks_about_house("几楼", "DL0007"), "floor")

    def test_platform_rule_in_house_context_is_not_treated_as_house_attribute(self):
        """在房源详情页里问「押金怎么退」问的是平台规则，不是这套房的属性。

        注意这条与上一条的边界：「押金」既可能问这套房收多少（属性），
        也可能问退还流程（规则）。带「怎么退」就是规则。
        """
        result = ask("押金怎么退", house_no="DL0007")
        self.assertEqual(result["status"], "answered")
        self.assertTrue(result["citations"], "平台规则的回答必须带依据引用")


class TestRentFlow(unittest.TestCase):
    """「帮我租下来」——平台的签约由房东在看房通过后发起，代办不了。"""

    def setUp(self):
        support.reset_knowledge_base()

    def test_explains_the_flow_instead_of_pretending(self):
        result = ask("帮我租一套房子")
        self.assertEqual(result["status"], "answered")
        self.assertIn("预约看房", result["message"])
        self.assertIn("房东", result["message"])

    def test_never_claims_it_can_sign_for_you(self):
        """不能假装能一键租下。签约关系到双方确认合同条款。"""
        result = ask("直接帮我租")
        for word in ("已为你", "已经帮你", "已签约"):
            self.assertNotIn(word, result["message"])



class TestOrdinalReference(unittest.TestCase):
    """「第一套」「第二套」指的是上一轮列出来的哪一条。

    实测里这三句全都失败了——助手把上一轮的搜索结果原样又列了一遍：

        「第一套第二套哪个好」「对比第一套和第二套」「帮我介绍下第一套」

    **用户已经看到列表了**，他再开口要的是判断。
    """

    def test_two_ordinals_resolve_to_two_houses(self):
        """`resolve_house` 只返回先命中的那一个，拿它做对比等于
        「拿第一套和第一套比」。"""
        self.assertEqual(
            memory.resolve_houses("对比第一套和第二套", ["A", "B", "C"]),
            ["A", "B"])

    def test_order_follows_the_sentence_not_the_table(self):
        self.assertEqual(
            memory.resolve_houses("第三套和第一套差多少", ["A", "B", "C"]),
            ["C", "A"])

    def test_the_same_house_is_not_listed_twice(self):
        """重复一次，对比表就会凭空多出一列。"""
        self.assertEqual(
            memory.resolve_houses("第2套和第二套", ["A", "B"]), ["B"])

    def test_out_of_range_ordinals_are_dropped(self):
        self.assertEqual(
            memory.resolve_houses("第五套和第一套", ["A", "B"]), ["A"])

    def test_no_context_means_nothing_to_resolve(self):
        self.assertEqual(memory.resolve_houses("对比第一套和第二套", []), [])


class TestPitchRouting(unittest.TestCase):
    """讲解与对比要被认出来，而且**不能把规则问题也拽进去**。"""

    def test_comparison_and_description_are_recognised(self):
        from app.agents.support.tools import router
        cases = [("第一套第二套哪个好", "compare_houses"),
                 ("对比第一套和第二套", "compare_houses"),
                 ("帮我介绍下第一套", "describe_house"),
                 ("这套怎么样", "describe_house"),
                 ("第二套值不值", "describe_house")]
        for text, want in cases:
            got = router.route(text, provider=None)
            self.assertEqual(got["name"], want, "「%s」应判为 %s" % (text, want))

    def test_a_rule_question_that_says_which_is_better_is_not_a_comparison(self):
        """**必须指着某一套。**

        「押一付三和押一付六哪个好」也含「哪个好」，但它问的是平台规则，
        该走知识库。少了「指代」这个条件，这类问题会被拽去对比房源。
        """
        from app.agents.support.tools import router
        for text in ["押一付三和押一付六哪个好", "整租和合租哪个划算",
                     "哪个好"]:
            got = router.route(text, provider=None)
            self.assertNotIn(got["name"], ("compare_houses", "describe_house"),
                             "「%s」问的是规则，不是房源" % text)


class TestProactiveRecommendation(unittest.TestCase):
    """找完房**主动多说一句**：哪一套最值得先看、接下来能说什么。

    列表只回答「有哪些」，回答不了「我该看哪个」。实测里用户拿到
    三套条件相近的房之后，接着打的三句话全是「哪个好」「对比一下」
    「介绍下第一套」——既然十有八九要问，不如先说。
    """

    def setUp(self):
        support.reset_knowledge_base()

    # 直接喂结果，不连后端——这一层测的是「挑没挑、说没说」，
    # 不是「后端有没有房」。连了后端反而会在演示库变动时莫名其妙地跳过。
    ITEMS = [
        {"house_no": "DL0007", "title": "中山区青泥洼桥 两室一厅 78平",
         "area": 78.6, "rent_price": 4150, "rent_type": "公寓",
         "tags": ["可做饭", "有阳台"], "rating": 0, "review_count": 0},
        {"house_no": "DL0008", "title": "中山区桂林街 两室一厅 62平",
         "area": 62.2, "rent_price": 2650, "rent_type": "整租",
         "tags": ["地铁", "电梯", "可做饭", "有阳台", "可养宠物"],
         "rating": 0, "review_count": 0},
    ]

    def _result(self):
        return {"status": "found", "items": [dict(x) for x in self.ITEMS],
                "message": "为你找到 2 套。", "filters": {}}

    def test_search_result_carries_a_pick_and_some_chips(self):
        result = self._result()
        main._push_recommendation(result, [])
        self.assertIn("最推荐", result["message"])
        self.assertEqual(result["recommended"]["house_no"], "DL0008")
        self.assertTrue(result.get("options"), "结果后面要给可点的推荐词")

    def test_chips_never_outnumber_one_line(self):
        result = self._result()
        main._push_recommendation(result, [])
        self.assertLessEqual(len(result.get("options") or []), 4)

    def test_a_single_result_gets_no_pick(self):
        """就一套房，「我最推荐第 1 套」是句废话。"""
        result = dict(self._result(), items=[dict(self.ITEMS[0])])
        main._push_recommendation(result, [])
        self.assertNotIn("最推荐", result["message"])

    def test_an_empty_search_is_left_alone(self):
        """一套都没找到时别再推荐——那只会显得没在听。"""
        result = {"status": "empty", "items": [], "message": "没有找到。"}
        main._push_recommendation(result, [])
        self.assertEqual(result["message"], "没有找到。")


class TestLayoutWithoutTheHall(unittest.TestCase):
    """「三千以内的两室」——**少一个「厅」字就不算找房了。**

    实测这句话被判成 unknown，回了一句「没太理解你想做什么」；
    而规则解析器同一句话解析出了区域、租价上限、户型三个字段。
    意图关键词表里写的是「室一厅」，可「两室」是再自然不过的说法。
    """

    def setUp(self):
        support.reset_knowledge_base()

    def test_two_conditions_without_a_keyword_still_mean_search(self):
        result = ask("金州区三千以内的两室")
        self.assertEqual(result.get("intent"), "search")
        self.assertNotIn("没太理解", result.get("message", ""))

    def test_one_lonely_condition_does_not_hijack_other_requests(self):
        """**门槛卡在两个字段是保守的一侧。**

        「中山区的」只有一个字段，可能是别的请求里的半句话；
        一个字段就抢过来当找房，代价是把别的意图判错。
        """
        for text in ["押金一般收几个月", "水电费怎么算", "我还欠多少房租"]:
            result = ask(text)
            self.assertNotEqual(result.get("intent"), "search",
                                "「%s」不是找房" % text)


class TestOneConditionPlusAHouseWord(unittest.TestCase):
    """「中山区的房子」——**一个条件配一个「房」字，就已经确定了。**

    上面那条门槛卡在「两个字段」，把最自然的几种说法挡在了外面：
    「中山区的房子」「沙河口区有什么房」「有电梯的房子」「三千以内的房子」
    各只有一个条件，于是一路掉到客服兜底，回一句
    「这个我没查到相关规定」。用户说的是最普通的中文。
    """

    def setUp(self):
        support.reset_knowledge_base()

    def test_house_noun_rescues_a_single_condition(self):
        for text in ["中山区的房子", "沙河口区有什么房", "有电梯的房子",
                     "三千以内的房子"]:
            result = ask(text)
            self.assertEqual(result.get("intent"), "search",
                             "「%s」是在找房" % text)

    def test_hunt_verb_rescues_a_single_condition(self):
        for text in ["我想找个两室", "想租个一居室", "有没有三千以内的"]:
            result = ask(text)
            self.assertEqual(result.get("intent"), "search",
                             "「%s」是在找房" % text)

    def test_rule_questions_are_not_stolen(self):
        """加了这条之后，客服问题一条都不能被抢走。"""
        for text in ["可以养宠物吗", "附近有地铁吗", "水电费怎么算",
                     "押金一般收几个月"]:
            result = ask(text)
            self.assertNotEqual(result.get("intent"), "search",
                                "「%s」不是找房" % text)


class TestNoMoreResults(unittest.TestCase):
    """问「还有别的吗」而确实没有了，要说没有了。

    原先按 `seen // PAGE_SIZE + 1` 算页码：上一批只有 3 套、一页装 10 套，
    算出来还是第 1 页，于是**把同样的三套原样再端一遍**，
    配着一句「为你找到 3 套」——用户分不清这是新的还是刚才那批。

    用假后端固定三套，不靠本机有没有起着业务后端：
    这套测试一直宣称不依赖网络与数据库，那就不能在这里破例。
    """

    HOUSES = [
        {"id": 1, "house_no": "DL9001", "title": "中山区青泥洼桥 两室一厅 70平",
         "district": "中山区", "community": "青泥洼桥", "rent_price": 3000,
         "area": 70, "layout": "两室一厅", "rent_type": "整租", "tags": []},
        {"id": 2, "house_no": "DL9002", "title": "中山区人民路 两室一厅 66平",
         "district": "中山区", "community": "人民路", "rent_price": 3200,
         "area": 66, "layout": "两室一厅", "rent_type": "整租", "tags": []},
        {"id": 3, "house_no": "DL9003", "title": "中山区桂林街 两室一厅 61平",
         "district": "中山区", "community": "桂林街", "rent_price": 2800,
         "area": 61, "layout": "两室一厅", "rent_type": "整租", "tags": []},
    ]

    def setUp(self):
        support.reset_knowledge_base()
        houses = self.HOUSES

        def fake_list(params):
            # 第 2 页起没有东西了——真后端也是这么答的
            page = int(params.get("page") or 1)
            return {"list": houses if page == 1 else [], "total": len(houses)}

        patcher = mock.patch(
            "app.agents.property_finder.tools.search_properties.client"
            ".list_properties", side_effect=fake_list)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_says_so_instead_of_repeating(self):
        first = ask("中山区两室一厅")
        nos = [i["house_no"] for i in (first.get("items") or [])]
        self.assertEqual(len(nos), 3)

        again = ask("还有别的吗", history=["中山区两室一厅"],
                    seen=len(nos), last_house_nos=nos)
        self.assertEqual(again.get("items"), [], "没有更多了就不该再给房源")
        self.assertIn("没有更多", again.get("message", ""))

    def test_a_full_page_still_pages_forward(self):
        """满页的时候照旧往下翻，序号接着数。"""
        again = ask("还有别的吗", history=["中山区两室一厅"], seen=10,
                    last_house_nos=["DL9001"])
        self.assertIn("没有更多", again.get("message", ""))


class TestRuleQuestionsBeatTheActionFlows(unittest.TestCase):
    """**「预约了怎么取消」问的是规矩，不是要约看房。**

    词表见到「预约」「看房」「报修」就往办事流程里送，于是这类问题
    得到的是「没听清你想什么时候看房」或者一条「其他 / 一般」的故障单。

    判据不靠猜：办事流程自己说接不了（预约解析不出任何时间、
    报修归不了类），而且这句话确实是个问句，才转去检索。
    """

    def setUp(self):
        support.reset_knowledge_base()

    def test_booking_questions_get_answered(self):
        for text in ["预约了怎么取消", "几点可以看房", "看房要给带看费吗",
                     "预约完多久房东会确认"]:
            result = ask(text)
            self.assertEqual(result.get("status"), "answered",
                             "「%s」该被答上来，实得 %s/%s"
                             % (text, result.get("intent"), result.get("status")))

    def test_repair_questions_get_answered(self):
        for text in ["报修要不要拍照", "什么情况算紧急报修", "报修多久上门"]:
            result = ask(text)
            self.assertEqual(result.get("status"), "answered",
                             "「%s」该被答上来，实得 %s/%s"
                             % (text, result.get("intent"), result.get("status")))

    def test_real_bookings_are_untouched(self):
        """句子里有时间的，一律还是走预约。"""
        for text in ["明天下午两点看房", "这周六去看房"]:
            result = ask(text)
            self.assertEqual(result.get("intent"), "booking", text)

    def test_real_faults_are_untouched(self):
        """真的在报故障的，一律还是走报修。"""
        for text in ["厨房水管漏水了", "暖气不热", "厨房灯不亮", "洗衣机坏了"]:
            result = ask(text)
            self.assertEqual(result.get("intent"), "repair", text)

    def test_statements_are_not_sent_to_retrieval(self):
        """**不是问句就别转。**

        「我想租房」也解析不出条件，但它不是问题；转去检索换回来的是
        一条风马牛不相及的规定（实测：「任一方提前解除合同的，
        应提前 30 日书面通知对方」）。
        """
        result = ask("我想租房")
        self.assertEqual(result.get("intent"), "search")
        self.assertNotIn("违约金", result.get("message", ""))


class TestSmallTalk(unittest.TestCase):
    """打招呼要有人应。

    不接的话「你好」会掉进客服检索，回一句「这个我没查到相关规定」——
    用户打的第一个招呼就被告知查无此事。
    """

    def setUp(self):
        support.reset_knowledge_base()

    def test_greetings_get_a_greeting(self):
        for text in ["你好", "你好啊", "在吗"]:
            self.assertIn("租房助手", ask(text).get("message", ""), text)

    def test_thanks_and_goodbye(self):
        self.assertIn("不客气", ask("谢谢").get("message", ""))
        self.assertIn("再见", ask("再见").get("message", ""))

    def test_a_greeting_with_a_request_is_not_swallowed(self):
        """「你好，我想找个两室」——寒暄只占前两个字，后半句才是正事。"""
        result = ask("你好，我想找个两室")
        self.assertNotIn("租房助手", result.get("message", ""))


if __name__ == "__main__":
    unittest.main()
