# -*- coding: utf-8 -*-
"""把「今天」挪到别的日子，再跑一遍智能体的全套测试。

    python scripts/run_at_dates.py                        # 扫一圈预设的日子
    python scripts/run_at_dates.py --dates 2027-02-27     # 只跑某一天

## 为什么需要这个

**时间相关的用例会自己过期，而且过期的那天多半没人在跑测试。**
这一条是被咬了两次才写下来的：

1. AI-06 的验收集里有一批期望值没有套用解析器
   「这个点已经过去，那说的是下一次」的规则。它只在**周六 09:00 之后**
   失败，别的日子全绿——藏了很久，而且即使那几条全错，
   命中率也正好压在 90% 的线上。
2. 代办层的 `TestCancelNarrowsByTime` 把预约钉死在 2026-09-12/20/21，
   句子里说「取消 9 月 20 号那个看房」。**9-20 一过就红**，
   隔了一周才被发现。

两次都不是功能坏了，是用例自己过期了。差别在于：功能坏了会一直红，
用例过期只在某些日子红——**恰好是没人看的那些日子**。

## 它怎么做到的：只挪**项目自己的模块**，而且是平移不是冻结

先照常 discover（此时项目代码按真实 datetime 导入完毕），
然后遍历 `sys.modules`，**只在文件位于本仓库里的模块**上做替换：
凡是引用到 `datetime.date` / `datetime.datetime` / datetime 模块本身的名字，
一律换成「今天 = 指定那天」的版本。三方库一个都不碰。

**是平移，不是冻结**：`now()` 返回的是「真实此刻 + 一个固定偏移」，
所以**时间照常在走**。冻死会出事——见下面第四次。

每一天都开一个**子进程**跑。同一个进程里挪第二次没有意义：
模块已经导入过，那些在导入期抄走的引用不会跟着变。
（这一条也是实测撞出来的：第一次验证时两个日期返回同一个结果，
差点得出「脚本没用」的结论。）

## 这个脚本自己错了五次，逐条记下来

**它是用来抓 bug 的，所以它自己先得不造 bug。** 五次的形状都不一样：

**一、全局换掉两个类 → SQLAlchemy 不认自己算出来的日期。**
拿 server 870 条一跑红了 15 条，差点当成真的发现：

    sqlalchemy.exc.StatementError: (builtins.TypeError)
    SQLite DateTime type only accepts Python datetime and date objects

SQLAlchemy 的 SQLite 方言在**调用时**才取 `datetime.datetime` 做
`isinstance`，取到的正是被换掉的子类；而 CPython 的
`datetime + timedelta` **返回基类、不返回子类**——应用里算出来的日期
于是全都不被认。

**二、给子类加元类放宽 isinstance → pydantic 直接导不进来。**

    TypeError: metaclass conflict: the metaclass of a derived class
    must be a (non-strict) subclass of the metaclasses of all its bases

它要拿 `date` 当基类去建校验器类。结论：**三方库有权假定
`datetime.datetime` 就是那个类**，要拨钟就只在自己的代码里拨。
（元类这条路后来在第五次又走回来了——**区别是时机**：那时是导入之前
全局换，现在是 discover 之后只换项目模块，库不会再去继承它。）

**三、脚本把自己也当成项目模块，于是谁都没换到。**
它就放在仓库里，跑起来叫 `__main__`，模块字典里正好有
`_datetime` / `_REAL_DATE` / `_REAL_DATETIME`。先把自己这三个换了，
之后 `value is _REAL_DATE` 的右边已经是新类，**项目模块一个都没换到**——
十个日子全绿，因为根本没挪。**这一次最危险：前两次是红的，会逼人去看；
这一次是绿的，不数一眼替换计数就过去了。**

**四、把时间冻死 → 「时间会走」的功能全塌。**
冻结版跑 server，对照跑（挪到今天）就红了 5 条：登出全部设备、
改密码、降权之后旧令牌该失效，合同时间线该记下结束时刻。
道理很直白：**发令牌和吊销令牌之间本来是有时间差的**，
冻死之后两个时刻一模一样，「晚于签发时间」的判断就不成立了。
这不是 bug，是我把时间焊住了。所以改成平移。

**五、平移之后，项目代码里的 `isinstance(x, datetime.date)` 不认真 date。**
2026-09-28 拉下一周的新代码后，对照跑（挪 0 天，本该毫无影响）红了四条：

    agent/.../tools/dates.py 的 _parse() 把一个真的 date 判成
    「认不出的值」→ 返回 None → until_text 崩在 `if days > 1`

CPython 的日期运算一律返回基类（`x + timedelta`、`strptime(...).date()`
都是），所以真 date 不算平移子类的实例。这次的解法是给两个平移类加元类
重写 `__instancecheck__` / `__subclasscheck__`——**和第二次同一招，
但这次安全**，因为换的时机在导入之后，没有库会拿它当基类。

**这一条又是对照跑抓到的。** 第四次也是它抓的：一个「挪 0 天」的空操作
居然让测试变红，这种事只能靠专门跑一次空操作才看得见。

## 为什么只扫 agent，不扫 server

试过，**扫不了**——不是懒。服务端有三处钟不在同一条轴上：

- `models.py` 里 `default=datetime.now`，这个引用在**建表类的时候**
  就绑到了真的 `datetime` 上，换模块级的名字换不到它；
- `auth.py` 的令牌用 `time.time()` 发 `iat` / `exp`，压根不走 datetime；
- 有些用例在**函数体里**才 `from datetime import datetime`，
  取的是 `sys.modules` 里真的那个。

于是「应用以为现在是五个月后，数据库盖的还是今天的戳」，
两个日期上各红 8 条——**全是脚本造的，不是用例过期**
（工单刚建出来就被算成超时、令牌刚发就被算成过期）。

要真扫服务端，得先让应用的「现在」从一个地方来（比如 `app.clock.now()`），
而不是散落在 `datetime.now` / `time.time()` / 列默认值里。
那是一次改动，不是一个脚本能绕过去的。**所以这里只保证 agent，
服务端的日期相关用例目前只能靠人看。**

## 三道自检

1. **排掉脚本自己**，比较用的真类先取到局部变量，换不掉。
2. **换完当场验**：逐个问「今天几号」，答不上那天就停；
   再反扫一遍，项目模块里还能找到真 `datetime` 就算漏网。
3. **每次扫描先拿今天做对照跑**：挪到今天还红，说明红的不是日期，
   是脚本或者代码——直接停下，不往下扫，免得又去「修」一批没坏的用例。

还有一处限制得写明：**测试跑起来之后才第一次被导入的项目模块，
钟是挪不到的**（discover 之后才替换）。绝大多数模块在 discover
阶段就导进来了，但如果哪天某条用例在运行时才 import，这条要记得。
"""

import argparse
import datetime as _datetime
import os
import subprocess
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

_REAL_DATE = _datetime.date
_REAL_DATETIME = _datetime.datetime

# 七个星期几 + 月末 + 年末 + 闰日。星期几标在注释里，方便看漏没漏。
DATES = [
    "2026-10-06",  # 周二
    "2026-11-18",  # 周三
    "2026-12-31",  # 周四，顺带年末
    "2027-01-01",  # 周五，顺带年初
    "2027-02-27",  # 周六 —— AI-06 那次就栽在周六
    "2027-03-31",  # 周三，月末
    "2027-05-30",  # 周日
    "2028-02-29",  # 闰日
    "2029-02-28",  # 闰年的后一年
    "2029-09-17",  # 周一
]

SUITE_DIR = "agent"      # 只扫智能体，理由见模块开头


class _AnyDate(type):
    """让「真的 date」也算平移 date 的实例。

    **第五次踩坑，2026-09-28。** 平移之后，项目代码里的
    `isinstance(x, datetime.date)` 拿到的是平移子类，而真 date 不算它的实例
    （CPython 的日期运算一律返回基类，`strptime(...).date()` 也是）。
    后果：

        agent/app/agents/support/tools/dates.py 的 _parse() 把一个
        真的 date 判成「认不出的值」→ 返回 None → until_text 崩在
        `if days > 1`，四条本来全绿的用例变红。

    对照跑（平移 0 天，本该毫无影响）就红了，**这一条正是对照跑存在的理由**。

    元类只在 discover 之后、只加在项目模块上，所以不会重演当初
    pydantic 拿 `date` 当基类建校验器时的 metaclass conflict——
    那次是**导入之前**就全局换掉，库自己会去继承它。
    """

    def __instancecheck__(cls, obj):
        return isinstance(obj, _REAL_DATE)

    def __subclasscheck__(cls, other):
        return issubclass(other, _REAL_DATE)


class _AnyDateTime(type):

    def __instancecheck__(cls, obj):
        return isinstance(obj, _REAL_DATETIME)

    def __subclasscheck__(cls, other):
        return issubclass(other, _REAL_DATETIME)


def _shifted_types(day):
    """造出「今天是 day」的 date / datetime，以及一份对应的 datetime 模块。

    **平移，不是冻结**：整条时间轴往后（或往前）挪一个固定的天数，
    时刻照常在走。冻死会让「发令牌 → 吊销令牌」这类前后关系塌掉。
    """
    offset = _datetime.timedelta(days=(day - _REAL_DATE.today()).days)

    class ShiftedDate(_REAL_DATE, metaclass=_AnyDate):
        @classmethod
        def today(cls):
            moved = _REAL_DATE.today() + offset
            return cls(moved.year, moved.month, moved.day)

    class ShiftedDateTime(_REAL_DATETIME, metaclass=_AnyDateTime):
        @classmethod
        def now(cls, tz=None):
            moved = _REAL_DATETIME.now(tz) + offset
            return cls.fromtimestamp(moved.timestamp(), tz) if tz \
                else cls.fromtimestamp(moved.timestamp())

        @classmethod
        def utcnow(cls):
            moved = _REAL_DATETIME.utcnow() + offset
            return cls(moved.year, moved.month, moved.day, moved.hour,
                       moved.minute, moved.second, moved.microsecond)

        @classmethod
        def today(cls):
            return cls.now()

    shifted_module = types.ModuleType("datetime")
    shifted_module.__dict__.update(_datetime.__dict__)
    shifted_module.date = ShiftedDate
    shifted_module.datetime = ShiftedDateTime
    return ShiftedDate, ShiftedDateTime, shifted_module


def _project_modules():
    """本仓库自己的模块。**排掉这个脚本自己**，理由见模块开头第三条。"""
    here = os.path.abspath(__file__)
    for module in list(sys.modules.values()):
        path = getattr(module, "__file__", None) or ""
        if not path.startswith(ROOT) or "site-packages" in path:
            continue
        if os.path.abspath(path) == here:
            continue
        yield module


def _move_project_modules(day):
    """只在本仓库的模块里把「今天」挪到 day，返回换掉的 (模块名, 名字) 清单。

    连别名一起换：遍历模块字典，凡是**就是**那两个类（或 datetime 模块）
    的名字都替换，所以 `from datetime import datetime as dt` 也管用。
    """
    shifted_date, shifted_datetime, shifted_module = _shifted_types(day)
    real_date, real_datetime = _REAL_DATE, _REAL_DATETIME
    real_module = _datetime
    swapped = []
    for module in _project_modules():
        for name, value in list(vars(module).items()):
            if value is real_date:
                setattr(module, name, shifted_date)
            elif value is real_datetime:
                setattr(module, name, shifted_datetime)
            elif value is real_module:
                setattr(module, name, shifted_module)
            else:
                continue
            swapped.append((module.__name__, name))

    # `sys.modules` 里的那一份也换掉。
    #
    # **否则在函数体里 `import datetime` 的代码拿到的还是真的。**
    # 这一条是 2026-09-28 实测撞出来的：`test_contracts_expiring_soon`
    # 在函数里 import，算出的 `today + 20 天` 是真实的今天往后 20 天，
    # 而被测代码看到的「今天」已经挪到五个月后——于是那份合同成了
    # 132 天前到期，用例红了，而它本身写得没有任何问题。
    #
    # 换 `sys.modules` 之后，运行期才 import 的三方库也会拿到平移版。
    # 上面那两个元类正是为此要放宽 `isinstance`：
    # 库拿到平移类去判断真日期时，不能判成「不是日期」。
    sys.modules["datetime"] = shifted_module
    return swapped


def _verify_clock(day, swapped):
    """**确认钟真的挪进了项目代码**，而不是只在这个脚本里挪了个寂寞。

    两头都查：换进去的东西问它「今天几号」要答得上那一天；
    并且项目模块里**再也找不到**真的 date / datetime / datetime 模块。
    """
    # 先查 `sys.modules` 里那一份——函数体里 `import datetime` 拿的就是它
    if sys.modules["datetime"].date.today() != day:
        return "sys.modules 里的 datetime 还没挪：今天是 %s" % (
            sys.modules["datetime"].date.today())

    checked = 0
    for module_name, name in swapped:
        obj = getattr(sys.modules[module_name], name)
        today = getattr(obj, "today", None)
        if today is None and hasattr(obj, "date"):        # 换进去的是模块
            today = obj.date.today
        if today is None:
            continue
        got = today()
        if _REAL_DATE(got.year, got.month, got.day) != day:
            return "「%s.%s」的今天是 %s，不是 %s" % (module_name, name, got, day)
        checked += 1
    if not checked:
        return "换是换了，却没有一个能问出「今天是哪天」，等于没验证"

    for module in _project_modules():
        for name, value in vars(module).items():
            if (value is _REAL_DATE or value is _REAL_DATETIME
                    or value is _datetime):
                return "「%s.%s」还指着真的 datetime，漏网了" % (module.__name__,
                                                                name)
    return ""


def run_one(day):
    """当前进程里把「今天」挪到 day，然后跑全套。子进程调用的是这一支。"""
    import unittest

    # 先导入（用真实 datetime），再挪钟——顺序见模块开头
    suite = unittest.TestLoader().discover("tests", top_level_dir=".")
    swapped = _move_project_modules(day)
    if not swapped:
        print("一个名字都没换到，钟根本没挪动，这一轮不作数。")
        return 2
    complaint = _verify_clock(day, swapped)
    if complaint:
        print("钟没挪稳，这一轮不作数：%s" % complaint)
        return 2

    result = unittest.TextTestRunner(verbosity=0).run(suite)
    print("换掉 %d 个日期引用，跑了 %d 条，失败 %d，错误 %d"
          % (len(swapped), result.testsRun, len(result.failures),
             len(result.errors)))
    for case, _ in list(result.failures) + list(result.errors):
        print("   %s" % case.id())
    return 0 if result.wasSuccessful() else 1


def _sweep_one(suite_dir, text):
    day = _REAL_DATE.fromisoformat(text)
    done = subprocess.run(
        [sys.executable, os.path.abspath(__file__), "--one", text],
        cwd=os.path.join(ROOT, suite_dir), capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    return day, done


def _print_tail(done):
    for line in [x for x in (done.stdout or "").splitlines() if x.strip()][-8:]:
        print("      %s" % line)


def main(argv=None):
    parser = argparse.ArgumentParser(description="把「今天」挪到别的日子跑全套测试")
    parser.add_argument("--dates", nargs="*", default=None,
                        help="要试的日子（YYYY-MM-DD），缺省用预设的十个")
    parser.add_argument("--one", help="内部用：在本进程里跑这一天")
    args = parser.parse_args(argv)

    if args.one:
        return run_one(_REAL_DATE.fromisoformat(args.one))

    days = args.dates or DATES

    # 对照跑：挪到今天（等于不挪）。这一轮必须绿，否则红的不是日期，
    # 是脚本或者代码。
    today = _REAL_DATE.today().isoformat()
    print("先拿今天（%s）做对照——挪到今天还红，就不是日期的事" % today)
    _, done = _sweep_one(SUITE_DIR, today)
    if done.returncode != 0:
        print("  ✗ 对照跑就红了，**这一轮不作数**：")
        _print_tail(done)
        print("\n先确认真实日期下跑全套是不是绿的。是绿的，"
              "那就是这个脚本换掉 datetime 之后引入的，别去改用例。")
        return 2
    print("  ✓ 对照跑通过\n")

    print("把「今天」依次挪到 %d 个日子\n" % len(days))
    bad = []
    for text in days:
        day, done = _sweep_one(SUITE_DIR, text)
        ok = done.returncode == 0
        print("  %s %s（周%s）" % ("✓" if ok else "✗", text,
                                   "一二三四五六日"[day.weekday()]))
        if not ok:
            bad.append(text)
            _print_tail(done)

    print()
    if bad:
        print("在这些日子上会红：%s" % "、".join(bad))
        print("**先确认是功能坏了还是用例过期了**——"
              "用例过期就把日期改成从今天推算，别去改功能。")
        return 1
    print("%d 个日子全绿。" % len(days))
    return 0


if __name__ == "__main__":
    sys.exit(main())
