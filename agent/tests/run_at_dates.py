# -*- coding: utf-8 -*-
"""把「今天」换成别的日子，再跑一遍全套测试。

    cd agent
    python tests/run_at_dates.py              # 扫一圈预设的日子
    python tests/run_at_dates.py 2027-02-28   # 只跑某一天

## 为什么需要这个

**时间相关的用例会自己过期，而且过期的那天多半没人在跑测试。**
这一条是被咬了两次才写下来的：

1. AI-06 的验收集里有一批期望值没有套用解析器
   「这个点已经过去，那说的是下一次」的规则。它只在**周六 09:00 之后**
   失败，别的日子全绿——藏了很久。
2. 代办层的 `TestCancelNarrowsByTime` 把预约钉死在 2026-09-12/20/21，
   句子里说「取消 9 月 20 号那个看房」。**9-20 一过就红**，
   而它被发现时离上一次跑测试隔了一周。

两次都不是功能坏了，是用例自己过期了。差别在于：功能坏了会一直红，
用例过期只在某些日子红——**恰好是没人看的那些日子**。

## 它怎么做到的

在导入任何项目代码之前，把 `datetime.date` / `datetime.datetime`
换成两个子类，`today()` 和 `now()` 返回指定的那一天。
`from datetime import date` 这种写法在导入时就把引用抄走了，
所以**顺序不能反**——和 `run_degraded.py` 挡 jieba 是同一个道理。

每一天都开一个**子进程**跑。同一个进程里跑第二遍没有意义：
模块已经导入过，那些在导入期算好的日期不会跟着变。

## 挑这些日子的理由

七个星期几各来一次（AI-06 那次就是星期几的事），加上月末、年末、
闰年 2-29、以及闰年的后一年 2-28——这些是日期算术最容易出错的位置。
"""

import datetime as _datetime
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass


# 七个星期几 + 月末 + 年末 + 闰日。星期几标在注释里，方便看漏没漏。
DATES = [
    "2026-09-21",  # 周一
    "2026-10-06",  # 周二
    "2026-11-18",  # 周三
    "2026-12-31",  # 周四，顺带年末
    "2027-01-01",  # 周五，顺带年初
    "2027-02-27",  # 周六 —— AI-06 那次就栽在周六
    "2027-03-31",  # 周三，月末
    "2027-05-30",  # 周日
    "2028-02-29",  # 闰日
    "2029-02-28",  # 闰年的后一年
]


def _install_clock(day):
    """把「今天」钉在 day 上。**必须在导入项目代码之前调用。**"""
    stamp = _datetime.datetime.combine(day, _datetime.time(10, 30))

    class FrozenDate(_datetime.date):
        @classmethod
        def today(cls):
            return cls(day.year, day.month, day.day)

    class FrozenDateTime(_datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return cls.fromtimestamp(stamp.timestamp())
            return _datetime.datetime.fromtimestamp(stamp.timestamp(), tz)

        @classmethod
        def utcnow(cls):
            return cls.fromtimestamp(stamp.timestamp())

        @classmethod
        def today(cls):
            return cls.now()

    _datetime.date = FrozenDate
    _datetime.datetime = FrozenDateTime


def run_one(day):
    """当前进程里把钟拨到 day，然后跑全套。子进程调用的是这一支。"""
    import unittest

    _install_clock(day)
    if _datetime.date.today() != day:
        print("钟没拨动，这一轮不作数。")
        return 2

    suite = unittest.TestLoader().discover("tests", top_level_dir=".")
    result = unittest.TextTestRunner(verbosity=0).run(suite)
    for case, _ in list(result.failures) + list(result.errors):
        print("   %s" % case.id())
    return 0 if result.wasSuccessful() else 1


def main(argv):
    if len(argv) > 1 and argv[1] == "--one":
        day = _datetime.date.fromisoformat(argv[2])
        return run_one(day)

    days = argv[1:] or DATES
    print("把「今天」依次换成 %d 个日子，每个开一个子进程跑全套\n" % len(days))
    bad = []
    for text in days:
        day = _datetime.date.fromisoformat(text)
        weekday = "一二三四五六日"[day.weekday()]
        done = subprocess.run(
            [sys.executable, os.path.abspath(__file__), "--one", text],
            cwd=os.path.dirname(HERE), capture_output=True, text=True,
            encoding="utf-8", errors="replace")
        ok = done.returncode == 0
        print("  %s %s（周%s）" % ("✓" if ok else "✗", text, weekday))
        if not ok:
            bad.append(text)
            tail = [line for line in (done.stdout or "").splitlines()
                    if line.strip()][-6:]
            for line in tail:
                print("      %s" % line)

    print()
    if bad:
        print("在这些日子上会红：%s" % "、".join(bad))
        print("**先确认是功能坏了还是用例过期了**——"
              "用例过期就把日期改成从今天推算，别去改功能。")
        return 1
    print("%d 个日子全绿。" % len(days))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
