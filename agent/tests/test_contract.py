"""AI-02 合同智能审阅测试。

分两块：条款抽取的准确性，以及风险识别的召回——
后者是这个功能真正的价值，漏判比误判更糟。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agents.contract import agent  # noqa: E402
from app.agents.contract.tools import risk  # noqa: E402
from app.agents.contract.tools.extract import extract_clauses  # noqa: E402
from app.agents.contract.tools.risk import check_risks  # noqa: E402
from app.llm.base import LLMProvider, LLMUnavailable, NullProvider  # noqa: E402

# 平台模板生成的规范合同
GOOD = """房屋租赁合同

出租方（甲方）：王建国
承租方（乙方）：林小雨

第二条 租赁期限
租赁期自 2026 年 09 月 10 日 起至 2027 年 09 月 10 日 止。

第三条 租金及支付方式
月租金为人民币 2900 元，乙方按每 1 个月支付一次。

第四条 押金
乙方应于签约时支付押金人民币 2900 元。

第五条 维修责任
房屋主体结构及原有设施的自然损坏由甲方负责维修；
因乙方使用不当造成的损坏由乙方承担维修费用。

第六条 违约责任
任何一方提前解除合同的，应提前 30 日书面通知对方，
并向对方支付相当于一个月租金的违约金。

第七条 其他约定
乙方不得擅自转租。
"""

# 故意写满坑的合同
BAD = """房屋租赁合同

第一条 租赁期限
租赁期自 2026 年 10 月 1 日 起至 2027 年 10 月 1 日 止。

第二条 租金及押金
月租金为人民币 3000 元，乙方按每 3 个月支付一次。
乙方应支付押金人民币 12000 元。押金不予退还。

第三条 租金调整
甲方有权根据市场情况随时调整租金。

第四条 房屋使用
甲方有权随时进入房屋查看使用情况。乙方不得擅自转租。

第五条 维修责任
房屋内一切维修费用均由乙方承担。

第六条 合同解除
甲方有权单方面解除本合同。乙方不得提前退租。
另行支付违约金 10000 元。

第七条 续约
租赁期满乙方未提出异议的，本合同自动续约一年。
"""


class FakeProvider(LLMProvider):

    def __init__(self, payload=None, fail=False):
        self.payload = payload
        self.fail = fail
        self.calls = 0

    def complete(self, system_prompt, user_prompt, max_tokens=None):
        self.calls += 1
        if self.fail:
            raise LLMUnavailable("模拟故障")
        return self.payload


class TestExtract(unittest.TestCase):

    def test_full_coverage_on_template(self):
        """平台模板合同应该六项全抽到，不需要过大模型。"""
        result = extract_clauses(GOOD)
        self.assertEqual(result["coverage"], 1.0)
        self.assertEqual(result["missing"], [])

    def test_rent_and_deposit(self):
        clauses = extract_clauses(GOOD)["clauses"]
        self.assertEqual(clauses["租金"], 2900)
        self.assertEqual(clauses["押金"], 2900)

    def test_pay_months(self):
        self.assertIn("1", extract_clauses(GOOD)["clauses"]["付款方式"])
        self.assertIn("3", extract_clauses(BAD)["clauses"]["付款方式"])

    def test_liability_clauses_captured(self):
        clauses = extract_clauses(GOOD)["clauses"]
        self.assertIn("违约", clauses["违约责任"])
        self.assertIn("维修", clauses["维修责任"])

    def test_empty(self):
        result = extract_clauses("")
        self.assertEqual(result["coverage"], 0.0)
        self.assertEqual(result["clauses"], {})

    def test_unrelated_text(self):
        result = extract_clauses("今天天气不错，适合出门散步。")
        self.assertLess(result["coverage"], 0.5)


class TestRisk(unittest.TestCase):

    def test_good_contract_is_clean(self):
        """规范合同不该被报出高风险，否则功能就成了狼来了。"""
        result = check_risks(GOOD, extract_clauses(GOOD)["clauses"])
        self.assertEqual(result["levels"]["high"], 0)
        self.assertGreaterEqual(result["score"], 90)

    def test_bad_contract_flagged(self):
        result = check_risks(BAD, extract_clauses(BAD)["clauses"])
        self.assertGreaterEqual(result["levels"]["high"], 3)
        self.assertEqual(result["score"], 0)

    def test_detects_each_pattern(self):
        result = check_risks(BAD, extract_clauses(BAD)["clauses"])
        found = {r["id"] for r in result["risks"]}
        for expected in ("deposit_no_refund", "unilateral_terminate",
                         "rent_adjust", "tenant_all_repair", "no_early_exit",
                         "auto_renew", "landlord_entry"):
            self.assertIn(expected, found, expected)

    def test_deposit_too_high(self):
        """押金 12000 是月租 3000 的四倍，要靠数值比对才能发现。"""
        result = check_risks(BAD, extract_clauses(BAD)["clauses"])
        self.assertIn("deposit_too_high", {r["id"] for r in result["risks"]})

    def test_penalty_too_high(self):
        result = check_risks(BAD, extract_clauses(BAD)["clauses"])
        self.assertIn("penalty_too_high", {r["id"] for r in result["risks"]})

    def test_normal_deposit_not_flagged(self):
        result = check_risks(GOOD, extract_clauses(GOOD)["clauses"])
        self.assertNotIn("deposit_too_high", {r["id"] for r in result["risks"]})

    def test_every_risk_has_advice(self):
        """光说有风险没用，必须告诉租客能怎么办。"""
        result = check_risks(BAD, extract_clauses(BAD)["clauses"])
        for risk in result["risks"]:
            self.assertTrue(risk["advice"], risk["id"])
            self.assertTrue(risk["why"], risk["id"])

    def test_empty(self):
        result = check_risks("")
        self.assertEqual(result["risks"], [])


class TestAgent(unittest.TestCase):

    def test_template_still_asks_llm_but_rules_win(self):
        """规则抽全了也要问大模型（用户要求：不要只靠规则），冲突时规则的值优先。"""
        provider = FakeProvider(payload='{"租金": 1}')
        result = agent.run(GOOD, provider=provider)
        self.assertEqual(provider.calls, 2)              # 抽条款一次 + 补查风险一次
        self.assertEqual(result["source"], "llm")
        self.assertEqual(result["clauses"]["租金"], 2900)

    def test_template_without_llm_is_rules(self):
        result = agent.run(GOOD, provider=None)
        self.assertEqual(result["source"], "rules")

    def test_low_coverage_calls_llm(self):
        provider = FakeProvider(
            payload='{"租期":"一年","租金":2000,"押金":2000,'
                    '"付款方式":"月付","违约责任":"按约定","维修责任":"甲方负责"}')
        result = agent.run("这是一份格式很怪的协议，双方约定如下。",
                           provider=provider)
        self.assertEqual(provider.calls, 2)
        self.assertEqual(result["source"], "llm")
        self.assertEqual(result["clauses"]["租金"], 2000)

    def test_llm_failure_falls_back(self):
        provider = FakeProvider(fail=True)
        result = agent.run("格式很怪的协议", provider=provider)
        self.assertEqual(result["source"], "rules_fallback")

    def test_llm_garbage_rejected(self):
        provider = FakeProvider(payload='{"房价": "很贵"}')
        result = agent.run("格式很怪的协议", provider=provider)
        self.assertEqual(result["source"], "rules_fallback")

    def test_llm_risk_needs_a_real_quote(self):
        """大模型补查的风险必须摘抄合同原文；原文对不上的丢掉，重复规则已有的也丢掉。"""
        text = GOOD + "\n第十二条 乙方不得在房屋内做饭，违者罚款五百元。"
        provider = FakeProvider(payload=(
            '{"risks": ['
            '{"title": "禁止做饭并罚款", "quote": "乙方不得在房屋内做饭，违者罚款五百元",'
            ' "why": "限制正常居住", "advice": "删掉或改成只禁明火"},'
            '{"title": "编出来的条款", "quote": "第九十九条 房东可随时收回房屋",'
            ' "why": "x", "advice": "y"}]}'))
        extra = agent.llm_risks(text, [], provider)
        self.assertEqual([r["title"] for r in extra], ["禁止做饭并罚款"])
        self.assertEqual(extra[0]["source"], "llm")
        self.assertEqual(agent.llm_risks(text, extra, provider), [])     # 已有的不重复

    def test_llm_risk_failure_is_silent(self):
        self.assertEqual(agent.llm_risks(GOOD, [], FakeProvider(fail=True)), [])

    def test_rules_win_over_llm_on_conflict(self):
        """规则抽到的值更可靠，不能被大模型覆盖。"""
        provider = FakeProvider(payload='{"租金": 99999}')
        result = agent.understand(GOOD, provider=provider)[0]
        self.assertEqual(result["clauses"]["租金"], 2900)

    def test_empty_text(self):
        result = agent.run("", provider=NullProvider())
        self.assertEqual(result["status"], "need_content")
        self.assertFalse(result["ok"])

    def test_response_shape(self):
        result = agent.run(GOOD, provider=NullProvider())
        for key in ("intent", "status", "ok", "message", "clauses",
                    "risks", "score"):
            self.assertIn(key, result)



class TestFavourableClauses(unittest.TestCase):
    """审阅要能分出高下（E1）。

    验收意见是「所有的审阅条款长得都一样，评分也是」。根因有两层，
    两层都补了才算修好：

    1. **合同正文本来就一模一样**——每份都由同一个模板渲染出来。
       已在 server 侧按房源特征拼差异化条款
       （`leasing.house_specific_clauses`）。
    2. **评分只减不加**——新条款一条规则都不命中，审阅照旧只看得见
       主干里那两条坑，于是正文变了、结论没变。这里补的是第二层。

    实测四份不同的房源：评分从「全是 87」变成 99 / 98 / 96 / 95，
    有利条款分别是 6 / 5 / 4 / 4 条。
    """

    def test_favourable_clauses_are_found(self):
        text = (
            "本合同一式两份。\n"
            "双方应于交房时共同清点并签署《物品交接清单》。\n"
            "未签署清单的，退租时甲方不得就物品缺损主张扣款。\n"
            "甲方应退还已付但尚未居住月份的租金。"
        )
        ids = {item["id"] for item in risk.check_favourable(text)}
        self.assertIn("inventory_signed", ids)
        self.assertIn("no_claim_without_inventory", ids)
        self.assertIn("prepaid_refundable", ids)

    def test_favourable_clauses_raise_the_score(self):
        bare = "第七条 其他约定\n乙方不得擅自转租。"
        rich = bare + (
            "\n第八条 家具家电\n"
            "双方应于交房时共同清点并签署《物品交接清单》。"
            "未签署清单的，退租时甲方不得就物品缺损主张扣款。"
        )
        self.assertGreater(risk.check_risks(rich)["score"],
                           risk.check_risks(bare)["score"])

    def test_more_protective_clauses_score_higher(self):
        """**条款越细，分越高**——否则这个分数区分不了任何东西。

        一律给同一个加分权重试过：四份条款数不同的合同全都撞到加分上限，
        又变成同一个分了。所以权重按「这一条实际值多少钱」分层。
        """
        # **底子里要先有一条风险，分数才有余量。**
        # 一条风险都没有时分数从 100 起算、加多少都封顶，
        # 这条用例的前提就不成立了——第一版正是这么写错的。
        base = "乙方不得擅自转租。"
        few = base + "甲方同意乙方在本房屋内饲养宠物。"
        many = few + (
            "双方应于交房时共同清点并签署《物品交接清单》。"
            "甲方应退还已付但尚未居住月份的租金。"
        )
        self.assertLess(risk.check_risks(base)["score"], 100,
                        "底子里得有风险，否则下面比的是两个 100")
        self.assertGreater(risk.check_risks(many)["score"],
                           risk.check_risks(few)["score"])

    def test_good_clauses_cannot_outweigh_a_high_risk(self):
        """**合同审阅的本职是提醒风险。**

        靠堆几条好条款把一条高风险盖过去，会让人以为这份合同没问题——
        那恰恰是审阅最不该传达的印象。加分总额封顶 12，
        比一条中风险（10）多一点，比一条高风险（25）少得多。
        """
        good = (
            "双方应于交房时共同清点并签署《物品交接清单》。"
            "未签署清单的，退租时甲方不得就物品缺损主张扣款。"
            "甲方应退还已付但尚未居住月份的租金。"
            "电梯停运超过 48 小时的按停运天数减免租金。"
            "取暖费由甲方承担。"
            "免收押金不免除乙方的赔偿责任。"
            "甲方同意乙方在本房屋内饲养宠物。"
        )
        risky = good + "押金一律不予退还。"
        self.assertLess(risk.check_risks(risky)["score"],
                        risk.check_risks(good)["score"])
        self.assertLess(risk.check_risks(risky)["score"], 90)

    def test_empty_contract_still_has_the_key(self):
        """空合同也要带上这个字段，调用方才不用到处判 None。"""
        self.assertEqual(risk.check_risks("")["favourable"], [])


if __name__ == "__main__":
    unittest.main()
