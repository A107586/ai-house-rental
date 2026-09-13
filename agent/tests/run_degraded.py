# -*- coding: utf-8 -*-
"""把 jieba 挡掉，再跑一遍全套测试。

    cd agent
    python tests/run_degraded.py

## 为什么需要这个

`agent/requirements.txt` 里 jieba 是必装项，但项目一直保证
「没装它一切仍然能跑，只是 AI-03 的有依据正确率低一截」。
**这句保证从来没被验证过**——因为跑测试的机器上装了 jieba。

代价是一次真实事故：可答性闸门加了一道「挑不出话题词就拒答」的门
（G0），而没装 jieba 时 `topic_term` 对**任何**句子都返回空。
于是线上「押金怎么退」也答不出来，而离线 707 条全绿、AI-03 报 100%。

本机恰好有两个 Python：测试跑在装了 jieba 的那个上，服务起在
miniforge 那个上（有 fastapi、没 jieba）。**「在我机器上是好的」
这句话的完整版本是「在我跑测试的那个解释器上是好的」。**

## 它测的不是功能，是那句承诺

降级模式下指标会低一些，这是设计允许的；不允许的是**报错、崩溃，
或者像 G0 那样把所有问题一律拒掉**。所以这里只看有没有失败用例，
不比对指标数字。
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass


# `sys.modules[name] = None` 是解释器认的「这个模块导不进来」标记——
# 后续 `import jieba` 直接抛 ImportError。
#
# 一开始写的是往 `sys.meta_path` 插一个带 `find_module` 的钩子，
# **在 Python 3.12+ 上根本不生效**：那套旧 finder 协议已经被移除，
# 钩子被静默跳过，jieba 照常导入成功。脚本还老老实实报了
# 「挡不住 jieba」——**幸好它自己检查了一下 `WORD_LEVEL`**，
# 否则这一轮会假装跑过了降级测试。
_BLOCKED = ("jieba", "jieba.posseg", "jieba.analyse", "jieba.finalseg")


def block_jieba():
    for name in list(sys.modules):
        if name == "jieba" or name.startswith("jieba."):
            del sys.modules[name]
    for name in _BLOCKED:
        sys.modules[name] = None


def main():
    block_jieba()

    from app.retrieval.tokenize import WORD_LEVEL
    if WORD_LEVEL:
        print("挡不住 jieba——它已经被别的模块导入过了，这一轮不作数。")
        return 2
    print("已确认 WORD_LEVEL = False，开始跑降级模式的全套测试\n")

    suite = unittest.TestLoader().discover("tests", top_level_dir=".")
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    print("\n降级模式：跑了 %d 条，失败 %d，错误 %d"
          % (result.testsRun, len(result.failures), len(result.errors)))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
