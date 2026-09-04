"""`/agent/parse`：只解析，不取数（K-06）。

这个接口存在的理由是一条**架构判断**，不是一个功能点：

网站原先只有「智能找房」一个检索入口，条件识别和取数捆在一起。
后果是智能体一挂，整站就搜不了房——一个本该锦上添花的能力，
成了所有人找房的必经之路。而且用筛选器会把 AI 的结果整个清空，
「先说一句话搜出来、再点一下有电梯缩小范围」这种最自然的操作做不到。

拆开之后，智能体只负责把人话翻译成条件，取数统一走后端。
所以这份测试盯的是**边界**：这个接口绝不能去查房源。
"""

import unittest

from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def parse(text):
    return client.post("/agent/parse", json={"text": text}).json()


class TestParse(unittest.TestCase):

    def test_extracts_structured_conditions(self):
        body = parse("中山区两室一厅，租金三千以内")
        self.assertTrue(body["ok"])
        self.assertEqual(body["filters"]["suozaidiqu"], "中山区")
        self.assertEqual(body["filters"]["fangwuhuxing"], "两室一厅")
        self.assertEqual(body["filters"]["zujia_max"], 3000)

    def test_extracts_features(self):
        body = parse("想找带电梯、可以养猫的房子")
        self.assertIn("电梯", body["features"])
        self.assertIn("可养宠物", body["features"])

    def test_feature_only_query_is_usable(self):
        """只有设施偏好、没有区域价格，也算解析成功。

        「可以养猫的房子」已经足够执行一次检索了，
        判成「没听懂」会让用户以为系统连这句话都读不了。
        """
        body = parse("可以养猫的房子")
        self.assertTrue(body["ok"])
        self.assertEqual(body["filters"], {})
        self.assertEqual(body["features"], ["可养宠物"])

    def test_unrelated_text_is_not_ok(self):
        """听不懂就明说。

        前端据此**保留用户已有的筛选条件**——把筛选清空再告诉他
        「没听懂」，等于一句话毁掉他刚调好的条件。
        """
        body = parse("今天天气怎么样")
        self.assertFalse(body["ok"])
        self.assertEqual(body["filters"], {})
        self.assertEqual(body["features"], [])

    def test_reports_how_it_understood(self):
        """要说清是规则解析还是大模型解析。

        「它为什么这么理解我」是用户会问的问题，也是答辩时要展示的
        决策过程；只给一个结果是解释不了的。
        """
        body = parse("中山区两室一厅")
        self.assertIn(body["source"], ("rules", "fused", "llm", "rules_fallback"))
        self.assertTrue(body["source_label"])
        self.assertIsInstance(body["divergence"], list)

    def test_does_not_return_houses(self):
        """**边界**：这个接口不查房源。

        一旦它开始返回列表，「取数只有一条路径」就名存实亡了——
        前端会有两个房源来源，翻页、筛选、总数又会各说各话。
        """
        body = parse("中山区两室一厅，租金三千以内")
        for key in ("items", "total", "list", "houses"):
            self.assertNotIn(key, body)

    def test_works_without_the_backend(self):
        """后端不可达也要能解析。

        解析是纯文本处理，不需要碰数据库。这一条防的是有人图省事，
        在这里顺手调一次房源接口去「验证一下条件」——那样后端一挂，
        连翻译都做不了了。
        """
        from app import backend

        class Dead(object):
            def list_properties(self, params):
                raise backend.BackendError("后端不可用")

        original = backend.client
        backend.client = Dead()
        try:
            body = parse("中山区两室一厅，租金三千以内")
        finally:
            backend.client = original
        self.assertTrue(body["ok"])
        self.assertEqual(body["filters"]["suozaidiqu"], "中山区")


class TestEmptyInput(unittest.TestCase):

    def test_blank_text(self):
        body = parse("   ")
        self.assertFalse(body["ok"])
