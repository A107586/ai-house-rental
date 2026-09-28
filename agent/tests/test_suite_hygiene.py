# -*- coding: utf-8 -*-
"""测试文件自身的体检。

起因：`if __name__ == "__main__": unittest.main()` 被写在了文件**中间**，
后面又陆续追加了新的测试类。用 `python -m unittest discover` 跑没问题，
可直接 `python tests/xxx.py` 跑的时候，执行到那一行就开始跑了，
**后面的类根本没被定义**——它少跑了 25 条用例，然后打印一个绿色的 OK。

「跑绿了」和「跑全了」是两件事。这条用例守的是后者。
七个文件中过招，所以值得留一条。

后来又添了一条同一性质的：**加了工具却忘了加评测**。
工具选择准确率是这条主线唯一的数字，新工具不进评测集，
那个数字就只是在给老工具打分——而它照样是绿的、照样报一个好看的百分比。
"""

import io
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))


class TestMainBlockIsLast(unittest.TestCase):

    def test_nothing_comes_after_the_main_block(self):
        offenders = []
        for name in sorted(os.listdir(HERE)):
            if not name.startswith("test_") or not name.endswith(".py"):
                continue
            path = os.path.join(HERE, name)
            lines = io.open(path, encoding="utf-8").read().split("\n")
            for i, line in enumerate(lines):
                if not line.startswith("if __name__"):
                    continue
                rest = [x for x in lines[i + 3:] if x.strip()]
                if rest:
                    offenders.append("%s：第 %d 行之后还有 %d 行代码，"
                                     "直接运行这个文件时跑不到"
                                     % (name, i + 1, len(rest)))
                break
        self.assertFalse(offenders, "\n".join(offenders))


class TestEveryToolIsEvaluated(unittest.TestCase):
    """工具表里每一个工具，评测集里都得有一句话打到它。

    实测来源：补完中介的三个工具（`agent_commissions` /
    `agent_delegations` / `agent_viewings`）之后，`toolbox_eval` 还是
    42 句、覆盖 28 个工具，而工具表已经有 31 个。评测照跑、照出数字，
    只是那个数字跟新工具没有关系。
    """

    def test_toolbox_eval_covers_every_tool(self):
        from app.agents.toolbox import catalog
        from tests.fixtures import toolbox_eval

        covered = set()
        for case in toolbox_eval.CASES:
            expected = case[1]
            if expected is None:
                continue
            if isinstance(expected, (tuple, list, set)):
                covered.update(expected)
            else:
                covered.add(expected)

        # `none` 是「别接这一句」的标记，不是一个能被问到的工具
        missing = sorted({t["name"] for t in catalog.TOOLS} - covered - {"none"})
        self.assertEqual(
            missing, [],
            "这些工具在 tests/fixtures/toolbox_eval.py 里没有任何一条用例："
            + "、".join(missing)
            + "。加工具时一并加一句评测，否则准确率只是在给老工具打分。")

    def test_eval_does_not_reference_tools_that_no_longer_exist(self):
        """反过来也要盯：删了工具而评测集里还留着，那条永远红。"""
        from app.agents.toolbox import catalog
        from tests.fixtures import toolbox_eval

        known = {t["name"] for t in catalog.TOOLS}
        stale = []
        for case in toolbox_eval.CASES:
            expected = case[1]
            if expected is None:
                continue
            names = (expected if isinstance(expected, (tuple, list, set))
                     else [expected])
            stale.extend(n for n in names if n not in known)
        self.assertEqual(sorted(set(stale)), [],
                         "评测集里引用了工具表中不存在的工具名")


if __name__ == "__main__":
    unittest.main()
