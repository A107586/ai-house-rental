# -*- coding: utf-8 -*-
"""测试文件自身的体检。

起因：`if __name__ == "__main__": unittest.main()` 被写在了文件**中间**，
后面又陆续追加了新的测试类。用 `python -m unittest discover` 跑没问题，
可直接 `python tests/xxx.py` 跑的时候，执行到那一行就开始跑了，
**后面的类根本没被定义**——它少跑了 25 条用例，然后打印一个绿色的 OK。

「跑绿了」和「跑全了」是两件事。这条用例守的是后者。
七个文件中过招，所以值得留一条。
"""

import io
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


class TestMainBlockIsLast(unittest.TestCase):

    def test_nothing_comes_after_the_main_block(self):
        offenders = []
        for name in sorted(os.listdir(HERE)):
            if not name.startswith("test_") or not name.endswith(".py"):
                continue
            path = os.path.join(HERE, name)
            with io.open(path, encoding="utf-8") as handle:
                lines = handle.read().split("\n")
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
