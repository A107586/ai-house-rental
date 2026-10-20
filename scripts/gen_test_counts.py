# -*- coding: utf-8 -*-
"""《测试说明》里的用例清单与总数，从代码生成。

## 为什么要生成

这个数，三份文档各说各的，**没有一个是对的**：

    docs/测试说明.md       智能体 493 + 后端 700 = 1193
    docs/技术架构说明.md    server 813 + agent 632 = 1,445
    docs/概要设计说明书.md   1,580 条

更难看的是那张清单本身——**同样七行被复制粘贴了四遍**，
一直没人发现，因为没有人会去数一张五十行的表。

这类数字有个共同点：**改代码的人不会想起来改它**，
而看文档的人没有办法发现它已经不对了。
所以按这个仓库已经立下的规矩办：生成它，并用测试盯住。
（同类：`gen_api_list.py`、`gen_db_doc.py`、`gen_db_xlsx.py`）

## 怎么数的

**真的把用例装载一遍数出来**，不是数源码里有几个 `def test_`——
参数化、继承、`load_tests` 都会让这两个数对不上。

两个项目必须**分别起进程**数：`agent/` 和 `server/` 下各有一个顶级包
都叫 `app`，同一个解释器里先导入谁，后面那个就被挡住了。

每个模块的说明取自**模块 docstring 的第一行**。所以不必在文档里
另抄一份描述——描述就写在测试文件自己头上，改测试时顺手就改了。

## 管的不止那张表

正文里零散提到总数的地方一并改写，见 `INLINE`。
**那几处才是最容易烂掉的**：一张表还能看出行数不对，
一句「全部 1,445 条自动化测试」混在段落中间，谁也不会去核。

各个 README 也在其中，而且是**最先被人看到**的地方：2026-10-15 答辩前核对，
根目录 README 的「规模」表还写着 31 张表 · 127 个接口 · 16 个智能体接口 ·
1193 个测试（当时实际是 36 · 152 · 19 · 1,837），`server/README.md`
写 29 个用例（实际 932），`agent/README.md` 写 92 个（实际 905）——
都是手写的，都不在任何 `--check` 里。表数与接口数照旧不在这里数，
从《概要设计说明书》读，见 `_outline_numbers`。

## 用法

    python scripts/gen_test_counts.py            # 重写
    python scripts/gen_test_counts.py --check    # 只比对，不写（测试用）
"""

import ast
import io
import json
import os
import re
import subprocess
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(ROOT, "docs")
DOC = os.path.join(DOCS, "测试说明.md")
PROJECTS = ("agent", "server")

BEGIN = "<!-- 自动生成：测试清单，勿手改；见 scripts/gen_test_counts.py -->"
END = "<!-- /自动生成：测试清单 -->"

# 正文里零散引用这些数字的地方。
#
# 每条是 (文档, 匹配正则, 改成什么)，文档路径相对仓库根。正则必须**恰好命中一处**：
# 命中零处或多处都说明文档结构变了，那时候宁可报错，也不要猜着改。
# 正则里不要用捕获组——`re.findall` 有组时返回的是组，不是整段命中。
INLINE = [
    ("docs/技术架构说明.md",
     r"全部 [\d,]+ 条自动化测试能在没有 API Key 的机器上跑通",
     "全部 {total} 条自动化测试能在没有 API Key 的机器上跑通"),
    ("docs/技术架构说明.md",
     r"server [\d,]+ \+ agent [\d,]+ = \*\*[\d,]+ 条\*\*",
     "server {server} + agent {agent} = **{total} 条**"),
    ("docs/概要设计说明书.md",
     r"功能需求已全部完成、[\d,]+ 条自动化测试全绿",
     "功能需求已全部完成、{total} 条自动化测试全绿"),
    ("docs/概要设计说明书.md",
     r"项目的 [\d,]+ 条自动化测试全部跑在这个模式下",
     "项目的 {total} 条自动化测试全部跑在这个模式下"),
    ("docs/代码导读.md",
     r"全部 [\d,]+ 条自动化测试都走这条分支",
     "全部 {total} 条自动化测试都走这条分支"),
    ("docs/需求分析说明书.md",
     r"[\d,]+ 句，覆盖 [\d,]+ 个工具",
     "{eval_cases} 句，覆盖 {eval_tools} 个工具"),
    ("docs/需求分析说明书.md",
     r"[\d,]+ 个自动化测试在不联网",
     "{total} 个自动化测试在不联网"),

    # 《技术架构说明》那张「代码量」表。接口数与表数以
    # `gen_api_list.py` 写进《概要设计说明书》的那份为准（它已经有
    # --check 盯着），这里只是不让第二处再各说一个数。
    ("docs/技术架构说明.md",
     r"\| 业务后端 \| [\d,]+ 个文件，[\d,]+ 行，[\d,]+ 个接口，[\d,]+ 张表，[\d,]+ 步迁移 \|",
     "| 业务后端 | {server_files} 个文件，{server_lines} 行，"
     "{endpoints} 个接口，{tables} 张表，{migrations} 步迁移 |"),
    ("docs/技术架构说明.md",
     r"\| 智能体 \| [\d,]+ 个文件，[\d,]+ 行，[\d,]+ 个子智能体 \|",
     "| 智能体 | {agent_files} 个文件，{agent_lines} 行，"
     "{subagents} 个子智能体 |"),
    ("docs/技术架构说明.md",
     r"\| 前端 \| [\d,]+ 个文件，[\d,]+ 行 \|",
     "| 前端 | {web_files} 个文件，{web_lines} 行 |"),

    # 《测试说明》页首那行版本摘要。同上，表数与接口数以概要设计为准。
    ("docs/测试说明.md",
     r"[\d,]+ 张数据表 · [\d,]+ 个后端接口 · [\d,]+ 个智能体接口 · [\d,]+ 个自动化测试",
     "{tables} 张数据表 · {endpoints} 个后端接口 · "
     "{agent_endpoints} 个智能体接口 · {total} 个自动化测试"),

    # 根目录 README 的「规模」表，答辩材料直接引用这四个数。
    ("README.md",
     r"\| 数据表 \| [\d,]+ \|",
     "| 数据表 | {tables} |"),
    ("README.md",
     r"\| 后端接口 \| [\d,]+ \|",
     "| 后端接口 | {endpoints} |"),
    ("README.md",
     r"\| 智能体接口 \| [\d,]+ \|",
     "| 智能体接口 | {agent_endpoints} |"),
    ("README.md",
     r"\| 自动化测试 \| \*\*[\d,]+\*\*（后端 [\d,]+ \+ 智能体 [\d,]+） \|",
     "| 自动化测试 | **{total}**（后端 {server} + 智能体 {agent}） |"),

    # 两个子项目各自的 README 只报自己那一半。
    ("server/README.md",
     r"[\d,]+ 个用例，跑在内存数据库上",
     "{server} 个用例，跑在内存数据库上"),
    ("agent/README.md",
     r"目前 \*\*[\d,]+ 个单元测试，全部不依赖网络",
     "目前 **{agent} 个单元测试，全部不依赖网络"),
    # 目录树那一行：前面那串空格是为了和上下几行的注释对齐，
    # 放进后顾断言里只认不改。
    ("agent/README.md",
     r"(?<=├── tests/ {19})[\d,]+ 个单元测试",
     "{agent} 个单元测试"),
]

# 数代码量时跳过的目录。**测试不算进「代码量」**——
# 它们在上面那张清单里单独报，算两遍等于把同一份工作报两次。
_SKIP_DIRS = {"__pycache__", "node_modules", "dist", "tests", ".venv"}

# 子进程里跑的那几行：装载 tests 目录，按模块数出用例条数。
_COUNTER = """
import json, sys, unittest
sys.path.insert(0, '.')
counts = {}
def walk(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            walk(item)
        elif isinstance(item, unittest.TestCase):
            mod = type(item).__module__.rsplit('.', 1)[-1]
            counts[mod] = counts.get(mod, 0) + 1
        else:
            print('LOAD-ERROR ' + repr(item), file=sys.stderr)
walk(unittest.TestLoader().discover('tests'))
print(json.dumps(counts))
"""


def _counts(project):
    """`{模块名: 用例数}`。单独起一个进程，见模块头注释。

    模块导入失败时 unittest 会塞一个 `_FailedTest` 进来。
    **不能当成 0 条悄悄跳过**——那会让一个根本没跑起来的模块
    在文档里显示成「0 条」，看着像是还没写测试。
    """
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.run([sys.executable, "-c", _COUNTER],
                          cwd=os.path.join(ROOT, project),
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env)
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        raise SystemExit("数 %s 的用例时失败：\n%s"
                         % (project, (proc.stderr or "")[-2000:]))
    if "LOAD-ERROR" in (proc.stderr or ""):
        raise SystemExit("%s 有测试模块导入失败，先修好再生成：\n%s"
                         % (project, proc.stderr[-2000:]))
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _summary(project, module):
    """模块 docstring 的第一行，当作这一行的说明。"""
    path = os.path.join(ROOT, project, "tests", module + ".py")
    if not os.path.exists(path):
        return ""
    doc = ast.get_docstring(ast.parse(io.open(path, encoding="utf-8").read()))
    if not doc:
        return ""
    # 标题式的 docstring 常以句号收尾，表格里不需要
    return doc.strip().splitlines()[0].strip().rstrip("。 ")


def _eval_numbers():
    """工具评测集的两个数：总句数、覆盖到的工具数。

    用 `ast` 读那份 fixture，不 import——它在 agent 项目里，
    导进来会把那边的 `app` 包一起带进当前进程。
    """
    path = os.path.join(ROOT, "agent", "tests", "fixtures", "toolbox_eval.py")
    cases = None
    for node in ast.parse(io.open(path, encoding="utf-8").read()).body:
        if (isinstance(node, ast.Assign)
                and getattr(node.targets[0], "id", "") == "CASES"):
            cases = ast.literal_eval(node.value)
    if cases is None:
        raise SystemExit("toolbox_eval.py 里找不到 CASES")
    tools = set()
    for case in cases:
        expected = case[1]
        if expected is None:
            continue
        if isinstance(expected, (tuple, list, set)):
            tools.update(expected)
        else:
            tools.add(expected)
    return {"eval_cases": len(cases), "eval_tools": len(tools)}


def _source_size(root, exts):
    """`(文件数, 行数)`。只数源码，跳过 `_SKIP_DIRS`。"""
    files = lines = 0
    for base, dirs, names in os.walk(os.path.join(ROOT, root)):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for name in names:
            if name.endswith(exts):
                files += 1
                lines += len(io.open(os.path.join(base, name),
                                     encoding="utf-8",
                                     errors="replace").read().splitlines())
    return files, lines


def _outline_numbers():
    """接口数（后端、智能体）与表数，**从《概要设计说明书》里读**。

    那份是 `gen_api_list.py` 生成并且有 `--check` 盯着的，已经是
    这两个数的权威。这里再数一遍只会多出第二套口径——
    真出分歧时谁也说不清该信哪个。
    """
    # 两句都是生成的、都有 --check 盯着：附录 A 的开头（gen_api_list 的标记段）
    # 与 4.2 的「共 N 张表」（gen_api_list 的 INLINE）。原先读的是系统结构图里
    # 的「N 个接口 · M 张表」，那张图改成图片之后这句话就不在正文里了。
    text = io.open(os.path.join(DOCS, "概要设计说明书.md"),
                   encoding="utf-8").read()
    endpoints = re.search(
        r"业务后端 \*\*(\d+)\*\* 个接口，智能体服务 \*\*(\d+)\*\* 个接口", text)
    tables = re.search(r"共 \*\*(\d+) 张表、", text)
    if not (endpoints and tables):
        raise SystemExit("《概要设计说明书》里找不到附录 A 的接口数或 4.2 的表数，"
                         "先跑 python scripts/gen_api_list.py")
    return {"endpoints": endpoints.group(1),
            "agent_endpoints": endpoints.group(2),
            "tables": tables.group(1)}


def _code_stats():
    """《技术架构说明》那张代码量表要用的数字。"""
    out = {}
    for key, root, exts in (("server", "server/app", (".py",)),
                            ("agent", "agent/app", (".py",)),
                            ("web", "web/src", (".js", ".jsx"))):
        files, lines = _source_size(root, exts)
        out[key + "_files"] = "{:,}".format(files)
        out[key + "_lines"] = "{:,}".format(lines)
    # 子智能体数。`toolbox` 在同一个目录下，但它**不是子智能体**——
    # 它是「让模型挑哪个工具」的选路层，挑完才轮到子智能体干活。
    # 第一版忘了排除它，数出来 11 个，而全篇别处都写着 10 个。
    agents_dir = os.path.join(ROOT, "agent", "app", "agents")
    not_an_agent = {"__pycache__", "toolbox"}
    out["subagents"] = str(len([
        d for d in os.listdir(agents_dir)
        if os.path.isdir(os.path.join(agents_dir, d)) and d not in not_an_agent]))
    steps = io.open(os.path.join(ROOT, "server", "app", "migrations.py"),
                    encoding="utf-8").read()
    out["migrations"] = str(len(re.findall(r"^@step\(", steps, re.M))
                            or len(re.findall(r"^def _m\d", steps, re.M)))
    out.update(_outline_numbers())
    return out


def numbers():
    """所有要写进文档的数字，外加渲染好的那张表。"""
    rows = []
    totals = {}
    for project in PROJECTS:
        counts = _counts(project)
        totals[project] = sum(counts.values())
        label = "智能体" if project == "agent" else "后端"
        for module in sorted(counts):
            rows.append((label, "%s/tests/%s.py" % (project, module),
                         _summary(project, module), counts[module]))

    total = totals["agent"] + totals["server"]
    values = {"agent": "{:,}".format(totals["agent"]),
              "server": "{:,}".format(totals["server"]),
              "total": "{:,}".format(total)}
    values.update(_eval_numbers())
    values.update(_code_stats())
    values = {k: (str(v) if not isinstance(v, str) else v)
              for k, v in values.items()}
    return values, rows, totals, total


def build(rows, totals, total):
    """渲染文档里那一整段（含首尾标记）。"""
    lines = [
        BEGIN,
        "",
        "**预期：智能体 %d 个通过，后端 %d 个通过，共 %d 个，零失败。**"
        % (totals["agent"], totals["server"], total),
        "",
        "| 端 | 文件 | 盯什么 | 用例数 |",
        "|---|---|---|---:|",
    ]
    for label, path, summary, count in rows:
        lines.append("| %s | `%s` | %s | %d |" % (label, path, summary, count))
    lines += ["| | | **合计** | **%d** |" % total, "", END]
    return "\n".join(lines)


def _rewrite_inline(values, check):
    """改写正文里零散引用的那几个数。返回还对不上的位置。"""
    stale = []
    for name, pattern, template in INLINE:
        path = os.path.join(ROOT, name)
        text = io.open(path, encoding="utf-8").read()
        hits = re.findall(pattern, text)
        if len(hits) != 1:
            raise SystemExit(
                "%s 里这条正则命中了 %d 处（应当恰好 1 处），"
                "文档结构可能改了，先确认再动：%s" % (name, len(hits), pattern))
        want = template.format(**values)
        if hits[0] == want:
            continue
        stale.append("%s：「%s」应为「%s」" % (name, hits[0], want))
        if not check:
            # `want` 里可能含有正则替换的特殊字符（`\1` 之类），用函数形式替换
            io.open(path, "w", encoding="utf-8", newline="\n").write(
                re.sub(pattern, lambda _m: want, text, count=1))
    return stale


def main():
    check = "--check" in sys.argv
    values, rows, totals, total = numbers()
    fresh = build(rows, totals, total)

    stale = _rewrite_inline(values, check)

    text = io.open(DOC, encoding="utf-8").read()
    if BEGIN not in text or END not in text:
        raise SystemExit(
            "在 docs/测试说明.md 里找不到标记段，请手工插入这两行再重跑：\n"
            "  %s\n  %s" % (BEGIN, END))

    start = text.index(BEGIN)
    stop = text.index(END) + len(END)
    table_stale = text[start:stop].replace("\r\n", "\n") != fresh

    if not table_stale and not stale:
        print("测试清单与正文里的数字都是最新的。")
        return 0

    if check:
        if table_stale:
            print("《测试说明》里的用例清单与实际对不上了。")
        for item in stale:
            print(item)
        print("重跑 python scripts/gen_test_counts.py 后提交。")
        return 1

    if table_stale:
        io.open(DOC, "w", encoding="utf-8", newline="\n").write(
            text[:start] + fresh + text[stop:])
    # 《测试说明》既有清单又有页首那行 INLINE，可能两边都改了，只报一次
    print("已更新：%s" % "、".join(sorted(
        ({"docs/测试说明.md"} if table_stale else set())
        | {s.split("：")[0] for s in stale})))
    return 0


if __name__ == "__main__":
    sys.exit(main())
