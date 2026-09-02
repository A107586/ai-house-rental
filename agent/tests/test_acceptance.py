"""验收标准量化测试。

需求说明书给 AI-01 / AI-02 / AI-04 / AI-06 定了**可量化的验收标准**
（准确率 ≥ 85% 之类）。这类指标如果只写在文档里而从没统计过，
答辩时被问一句「85% 是怎么算出来的」就答不上来——和性能指标
N-01 是同一类问题。

这里的每个类对应一条验收标准，用例数与需求写的样本量一致：
AI-01 二十条、AI-02 十份、AI-04 五十条、AI-06 二十条。

与其他测试文件的分工：那边逐个功能点断言「这个输入应该得到那个输出」，
这里只统计**整体命中率**。前者定位缺陷，后者回答验收问题。
失败时会把没命中的样本逐条打印出来，方便直接去补规则。
"""

import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agents.booking.tools.parse_datetime import parse_datetime  # noqa: E402
from app.agents.contract.tools.extract import extract_clauses  # noqa: E402
from app.agents.contract.tools.risk import check_risks  # noqa: E402
from app.agents.property_finder.tools.parse_query import parse_query  # noqa: E402
from app.agents.repair.tools.classify import classify_repair  # noqa: E402


def report(title, hits, total, threshold, misses):
    """统一的结果输出。达标与否都打印比率，便于写进文档。"""
    rate = hits / total * 100 if total else 0
    line = "\n[%s] %d/%d = %.1f%%（要求 ≥ %.0f%%）" % (
        title, hits, total, rate, threshold * 100)
    if misses:
        line += "\n  未命中：\n" + "\n".join("    - " + m for m in misses)
    return rate, line


# ==================================================== AI-01
# 「20 条测试语句中，结构化参数解析准确率 ≥ 85%」

AI01_CASES = [
    ("中山区两室一厅，租金三千以内，最好带电梯",
     {"suozaidiqu": "中山区", "fangwuhuxing": "两室一厅", "zujia_max": 3000}),
    ("沙河口 合租 单间 1500以下",
     {"suozaidiqu": "沙河口区", "fangyuanleixing": "合租", "zujia_max": 1500}),
    ("甘井子区 80平以上的三室 预算不超过五千",
     {"suozaidiqu": "甘井子区", "mianji_min": 80, "zujia_max": 5000}),
    ("有没有金州区2000到3000的一居室",
     {"suozaidiqu": "金州区", "zujia_min": 2000, "zujia_max": 3000}),
    ("中山区两室一厅，租金两千五以内",
     {"suozaidiqu": "中山区", "zujia_max": 2500}),
    ("旅顺口区三室两厅一千以内",
     {"suozaidiqu": "旅顺口区", "fangwuhuxing": "三室两厅", "zujia_max": 1000}),
    ("西岗区整租一室一厅", {"suozaidiqu": "西岗区", "fangyuanleixing": "整租"}),
    ("帮我找一个月租3000以内两室一厅的房子",
     {"zujia_max": 3000, "fangwuhuxing": "两室一厅"}),
    ("金州区100平以上的房子", {"suozaidiqu": "金州区", "mianji_min": 100}),
    ("中山区 50平以下 单间", {"suozaidiqu": "中山区", "mianji_max": 50}),
    ("沙河口区两千以上三千以下", {"zujia_min": 2000, "zujia_max": 3000}),
    ("甘井子公寓", {"suozaidiqu": "甘井子区", "fangyuanleixing": "公寓"}),
    ("一万以内的别墅", {"zujia_max": 10000}),
    ("中山区三室一厅120平", {"suozaidiqu": "中山区", "fangwuhuxing": "三室一厅"}),
    ("西岗区租金1800", {"suozaidiqu": "西岗区"}),
    ("两室两厅 面积60到90平", {"fangwuhuxing": "两室两厅", "mianji_min": 60,
                              "mianji_max": 90}),
    ("旅顺口区一室", {"suozaidiqu": "旅顺口区", "fangwuhuxing": "一室"}),
    ("金州区合租不超过1200", {"suozaidiqu": "金州区",
                             "fangyuanleixing": "合租", "zujia_max": 1200}),
    ("中山区四千左右两室", {"suozaidiqu": "中山区"}),
    # 光说「80平」既没说以上也没说以下，按「大约 80 平」理解，
    # 解析成上下各 15% 的区间——说 80 平的人不会因为一套 78 平就不看
    ("沙河口区80平三室两厅整租", {"suozaidiqu": "沙河口区",
                                "fangwuhuxing": "三室两厅",
                                "fangyuanleixing": "整租",
                                "mianji_min": 68, "mianji_max": 92}),
]


class TestAI01ParseAccuracy(unittest.TestCase):
    """AI-01：结构化参数解析准确率 ≥ 85%。

    判定口径：期望的每一个字段都要解析对才算命中。多解析出别的字段不算错
    ——那是额外信息，不影响检索正确性。
    """

    THRESHOLD = 0.85

    def test_accuracy(self):
        hits, misses = 0, []
        for text, expect in AI01_CASES:
            got = parse_query(text)["filters"]
            bad = {k: (v, got.get(k)) for k, v in expect.items()
                   if got.get(k) != v}
            if bad:
                misses.append("%s → 期望 %s，实得 %s" % (text, expect, got))
            else:
                hits += 1

        rate, line = report("AI-01 解析准确率", hits, len(AI01_CASES),
                            self.THRESHOLD, misses)
        self.assertGreaterEqual(rate / 100, self.THRESHOLD, line)
        print(line)


# ==================================================== AI-04
# 「50 条报修描述中，故障分类准确率 ≥ 85%；
#   漏水／断电／煤气等关键词必须判为紧急」

AI04_CASES = [
    ("卫生间水管漏水，地上都是水", "水暖", "紧急"),
    ("厨房下水道堵了", "水暖", None),
    ("马桶冲不下去", "水暖", None),
    ("热水器不出热水", "水暖", None),
    ("水龙头一直滴水", "水暖", None),
    ("暖气不热", "水暖", None),
    ("洗手池下水慢", "水暖", None),
    ("水表好像坏了", "水暖", None),
    ("卫生间地漏返味", "水暖", None),
    ("阳台水管爆了", "水暖", "紧急"),

    ("客厅灯泡坏了，不急", "电路", "可延后"),
    ("卧室插座没电", "电路", None),
    ("电闸老是跳", "电路", None),
    ("全屋断电了", "电路", "紧急"),
    ("开关按不动", "电路", None),
    ("厨房电线外露", "电路", "紧急"),
    ("灯管一闪一闪的", "电路", None),
    ("电表读数不对", "电路", None),

    ("空调不制冷了", "家电", None),
    ("冰箱不制冷", "家电", None),
    ("洗衣机不脱水", "家电", None),
    ("油烟机不转了", "家电", None),
    ("热水器显示故障码", "家电", None),
    ("电视打不开", "家电", None),
    ("微波炉不加热", "家电", None),
    ("空调外机噪音很大", "家电", None),

    ("卧室门锁打不开", "门窗", "紧急"),
    ("窗户关不上", "门窗", None),
    ("阳台推拉门卡住了", "门窗", None),
    ("防盗门变形了", "门窗", None),
    ("纱窗破了个洞", "门窗", None),
    ("窗户玻璃裂了", "门窗", None),
    ("门把手松了", "门窗", None),

    ("衣柜门掉了", "家具", None),
    ("床板断了", "家具", None),
    ("沙发塌陷", "家具", None),
    ("餐桌腿松动", "家具", None),
    ("书架歪了", "家具", None),
    ("鞋柜门关不严", "家具", None),

    ("闻到煤气味了", "燃气", "紧急"),
    ("燃气灶点不着火", "燃气", None),
    ("燃气报警器一直响", "燃气", "紧急"),
    ("天然气表不走字", "燃气", None),

    ("墙面渗水发霉", "水暖", None),
    ("天花板掉皮", "其他", None),
    ("地板翘起来了", "其他", None),
    ("楼道声控灯不亮", "电路", None),
    ("门禁刷不开", "其他", None),
    ("墙上有裂缝", "其他", None),
    ("窗帘轨道掉了", "其他", None),
]


class TestAI04ClassifyAccuracy(unittest.TestCase):
    """AI-04：故障分类准确率 ≥ 85%，且安全隐患必须判紧急。"""

    THRESHOLD = 0.85

    def test_category_accuracy(self):
        hits, misses = 0, []
        for text, want, _ in AI04_CASES:
            got = classify_repair(text)["category"]
            if got == want:
                hits += 1
            else:
                misses.append("%s → 期望「%s」，实得「%s」" % (text, want, got))

        rate, line = report("AI-04 分类准确率", hits, len(AI04_CASES),
                            self.THRESHOLD, misses)
        self.assertGreaterEqual(rate / 100, self.THRESHOLD, line)
        print(line)

    def test_safety_keywords_are_always_urgent(self):
        """漏水、断电、煤气这类安全隐患**必须**判紧急。

        这一条不是准确率，是硬性要求：漏一个就可能出事，
        所以单独断言、不允许任何比例的失败。
        """
        must = [(t, u) for t, _, u in AI04_CASES if u == "紧急"]
        bad = []
        for text, _ in must:
            got = classify_repair(text)["urgency"]
            if got != "紧急":
                bad.append("%s → 实得「%s」" % (text, got))
        self.assertEqual(bad, [], "安全隐患未判紧急：\n" + "\n".join(bad))

    def test_polite_downplay_does_not_lower_urgency(self):
        """租客自己说「不急」，但涉及漏水这类隐患仍要判紧急——
        不能被客气话降级。"""
        r = classify_repair("厨房水管漏水，不急，有空来看看就行")
        self.assertEqual(r["urgency"], "紧急")


# ==================================================== AI-06
# 「20 条含相对时间的表达中，解析准确率 ≥ 90%」

def _mk(days, hour, minute=0):
    base = datetime.now() + timedelta(days=days)
    return base.replace(hour=hour, minute=minute, second=0, microsecond=0)


class TestAI06TimeAccuracy(unittest.TestCase):
    """AI-06：相对时间解析准确率 ≥ 90%。

    判定口径只看「解析出来的时刻对不对」，不看措辞。
    相对日期依赖运行当天是星期几，所以期望值按运行时计算，
    不写死具体日期——写死的用例过一天就红了。
    """

    THRESHOLD = 0.90

    def cases(self):
        today = datetime.now()
        # 本周六：本周内还没到的周六；已过则算下周
        days_to_sat = (5 - today.weekday()) % 7
        return [
            ("明天下午两点", _mk(1, 14)),
            ("明天上午十点", _mk(1, 10)),
            ("后天下午三点", _mk(2, 15)),
            ("今天下午四点", _mk(0, 16)),
            ("这周六下午两点", _mk(days_to_sat, 14)),
            ("这周六上午九点", _mk(days_to_sat, 9)),
            ("下周一下午三点半", None),      # 只校验能解析出时刻
            ("明天中午十二点", _mk(1, 12)),
            ("明早九点", _mk(1, 9)),
            ("明天晚上六点", _mk(1, 18)),
            ("后天上午十一点", _mk(2, 11)),
            ("大后天下午两点", _mk(3, 14)),
            ("明天下午2点", _mk(1, 14)),
            ("明天14:00", _mk(1, 14)),
            ("明天下午两点半", _mk(1, 14, 30)),
            ("这周日上午十点", None),
            ("下周三下午四点", None),
            ("今天晚上七点", _mk(0, 19)),
            ("明天早上八点", _mk(1, 8)),
            ("后天中午", None),
        ]

    def test_accuracy(self):
        hits, misses = 0, []
        cases = self.cases()
        for text, want in cases:
            got = parse_datetime(text)
            # parse_datetime 把日期与时刻分开返回，这里拼成一个时间点
            moment = (datetime.combine(got["date"], got["time"])
                      if got.get("date") and got.get("time") else None)
            if moment is None:
                misses.append("%s → 没解析出时刻" % text)
                continue
            if want is None:
                # 只要求解析出一个未来的时刻
                hits += 1 if moment > datetime.now() else 0
                if moment <= datetime.now():
                    misses.append("%s → 解析成了过去的时间 %s" % (text, moment))
                continue
            if moment.replace(second=0, microsecond=0) == want:
                hits += 1
            else:
                misses.append("%s → 期望 %s，实得 %s"
                              % (text, want.strftime("%m-%d %H:%M"),
                                 moment.strftime("%m-%d %H:%M")))

        rate, line = report("AI-06 时间解析准确率", hits, len(cases),
                            self.THRESHOLD, misses)
        self.assertGreaterEqual(rate / 100, self.THRESHOLD, line)
        print(line)


# ==================================================== AI-02
# 「对 10 份样例合同，关键条款抽取完整率 ≥ 90%；
#   能识别出预设的 5 类不利条款」

def sample_contract(rent=3000, deposit=3000, months=12, extra=""):
    return """房屋租赁合同

甲方（出租方）：王建国
乙方（承租方）：林小雨

第一条 租赁期限
本合同租赁期限为 %d 个月，自 2026 年 9 月 1 日起至 2027 年 8 月 31 日止。

第二条 租金
月租金为人民币 %d 元。

第三条 押金
乙方应于签订本合同时向甲方支付押金人民币 %d 元。

第四条 付款方式
租金按月支付，乙方应于每月 1 日前支付当月租金。

第五条 违约责任
任何一方违约的，应向对方支付违约金人民币 3000 元。

第六条 维修责任
房屋及附属设施的自然损坏由甲方负责维修。
%s
""" % (months, rent, deposit, extra)


# 五类预设的不利条款，各配一句原文
BAD_CLAUSES = [
    ("押金不予退还", "押金"),
    ("甲方有权单方面解除本合同", "解除"),
    ("甲方有权根据市场情况随时调整租金", "租金"),
    ("房屋内一切维修费用均由乙方承担", "维修"),
    ("乙方不得提前退租", "退租"),
]


class TestAI02ContractAccuracy(unittest.TestCase):
    """AI-02：条款抽取完整率 ≥ 90%，且能识别 5 类不利条款。"""

    THRESHOLD = 0.90
    # 需求列的六项关键条款
    KEY_TERMS = ("租期", "租金", "押金", "付款方式", "违约责任", "维修责任")

    def test_extraction_completeness(self):
        """十份合同，每份查六项条款，统计整体完整率。"""
        contracts = [
            sample_contract(),
            sample_contract(rent=1800, deposit=1800, months=6),
            sample_contract(rent=5200, deposit=10400, months=24),
            sample_contract(rent=2500, deposit=2500, months=12),
            sample_contract(rent=980, deposit=980, months=3),
            sample_contract(rent=7800, deposit=7800, months=36),
            sample_contract(rent=3300, deposit=6600),
            sample_contract(rent=4100, deposit=4100, months=18),
            sample_contract(rent=2200, deposit=2200, months=9),
            sample_contract(rent=6000, deposit=12000, months=12),
        ]
        hits, total, misses = 0, 0, []
        for index, text in enumerate(contracts, 1):
            found = set(extract_clauses(text)["clauses"])
            for want in self.KEY_TERMS:
                total += 1
                if want in found:
                    hits += 1
                else:
                    misses.append("第 %d 份缺「%s」" % (index, want))

        rate, line = report("AI-02 条款抽取完整率", hits, total,
                            self.THRESHOLD, misses[:10])
        self.assertGreaterEqual(rate / 100, self.THRESHOLD, line)
        print(line)

    def test_recognises_all_five_bad_clause_types(self):
        """五类不利条款必须**全部**识别出来。

        这不是准确率而是覆盖率：漏掉一类，那类合同就永远不会被提示。
        """
        missed = []
        for sentence, _ in BAD_CLAUSES:
            risks = check_risks(sample_contract(extra=sentence))["risks"]
            if not risks:
                missed.append(sentence)
        self.assertEqual(missed, [],
                         "以下不利条款没被识别：\n" + "\n".join(missed))

    def test_clean_contract_has_no_high_risk(self):
        """规范合同被报出高风险的话，功能就成了狼来了，
        用户会直接忽略所有提示。"""
        risks = check_risks(sample_contract())["risks"]
        high = [r for r in risks if r.get("level") == "高"]
        self.assertEqual(high, [], "规范合同不该有高风险：%s" % high)
