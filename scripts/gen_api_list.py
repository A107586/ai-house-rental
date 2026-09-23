# -*- coding: utf-8 -*-
"""生成《概要设计说明书》的附录 A 接口清单，并校验正文里引用的接口真的存在。

    python scripts/gen_api_list.py            # 生成附录 A
    python scripts/gen_api_list.py --check    # 只校验：附录是否最新、正文有没有引用到不存在的接口

## 为什么要有「校验正文」这一步

概要设计的正文里有一堆手写的接口表格。**手写的接口名会写错**——
本文档第一稿就写了三条根本不存在的接口：

    POST /api/role-requests            实际是 POST /api/auth/become-landlord
    PATCH /api/admin/role-requests/{id}  实际是 POST（方法也错了）
    POST /api/password-reset/request   实际是 POST /api/auth/forgot

评审时如果没人按图索骥去试，这三条会一直躺在文档里；
真有人照着调，就会得到 404 并开始怀疑是自己搞错了。

所以这个脚本除了生成清单，还**把正文里出现的每一条 `METHOD /path`
拿去和运行中的应用对一遍**，对不上就报错。这是一条无法靠自觉维持的纪律，
只能交给脚本。

## 清单本身从哪来

从两个 FastAPI 应用的路由表里导出——**不是从代码里正则抓装饰器**。
路由表是应用真正在用的那份，抓装饰器会漏掉 `include_router` 的前缀，
也会把注释掉的装饰器算进来。

一个坑记在这里：`app.routes` 里挂的不全是普通路由，
`include_router` 进来的是 `_IncludedRouter` 对象，**它没有 `.path`**，
要顺着 `original_router` 往下走。第一版没走这一层，导出的 87 条里
少了 8 个路由器的 63 条接口，而数字看起来很正常——
**清单少了一截是不会报错的，只会悄悄地少。**
"""

import argparse
import inspect
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOC = os.path.join(ROOT, "docs", "概要设计说明书.md")
BEGIN = "<!-- API-LIST:BEGIN 本节由 scripts/gen_api_list.py 生成，不要手改 -->"
END = "<!-- API-LIST:END -->"
# 第 3 章开头那张「模块 / 表数 / 接口数 / 对应需求」的总表也是生成的。
# **手写的数字必然是估的**：初稿手写的那一版九个模块加起来 160，
# 而全站只有 150 个接口——差出来的 10 个没人发现，因为没人会去加一遍。
SUM_BEGIN = "<!-- MODULE-TABLE:BEGIN 本表由 scripts/gen_api_list.py 生成，不要手改 -->"
SUM_END = "<!-- MODULE-TABLE:END -->"

# 正文里**散落在标记段之外**的接口数。
#
# 原先只有两个标记段是生成的，而那张 ASCII 架构图和「HTTP 入口，N 个接口」
# 这几处是手写的——于是同一份文档里出现了三个数：附录说 151、
# 架构图说 150、入口那行说 18 而附录说 19。加一个接口就又岔开一次。
#
# 每条 (正则, 模板)，必须**恰好命中一处**：命中零处或多处说明文档结构
# 变了，那时候宁可报错，也不要猜着改。
INLINE = [
    (r"│  [\d,]+ 个接口 · [\d,]+ 张表 · ",
     "│  {server} 个接口 · {tables} 张表 · "),
    (r"│  [\d,]+ 个接口 · [\d,]+ 个子智能体 ",
     "│  {agent} 个接口 · {subagents} 个子智能体 "),
    (r"agent/app/main\.py ──────── HTTP 入口，[\d,]+ 个接口",
     "agent/app/main.py ──────── HTTP 入口，{agent} 个接口"),
]

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db_meta import (MODULES, REQUIREMENTS, load_metadata,  # noqa: E402
                     module_of_path)

# 正文里引用接口的写法：`GET /api/houses`、`POST /agent/support`
CITED = re.compile(r"`(GET|POST|PUT|PATCH|DELETE)(?:/(?:GET|POST|PUT|PATCH|DELETE))*"
                   r"\s+(/[A-Za-z0-9_\-{}/.]+)`")
# 「`GET/POST /api/conversations`」这种合写也要拆开检查
MULTI = re.compile(r"`((?:GET|POST|PUT|PATCH|DELETE)"
                   r"(?:/(?:GET|POST|PUT|PATCH|DELETE))+)\s+(/[A-Za-z0-9_\-{}/.]+)`")


def collect(app):
    """把一个 FastAPI 应用的路由摊平成 (方法, 路径, 处理函数, 文件)。"""
    rows = []

    def walk(routes, prefix=""):
        for route in routes:
            inner = getattr(route, "original_router", None)
            if inner is not None:                    # include_router 进来的
                walk(inner.routes, prefix + getattr(inner, "prefix", ""))
                continue
            if hasattr(route, "routes"):             # 子应用 / 挂载
                walk(route.routes, prefix + getattr(route, "prefix", ""))
                continue
            methods = getattr(route, "methods", None)
            if not methods:
                continue
            handler = getattr(route, "endpoint", None)
            try:
                where = os.path.basename(inspect.getsourcefile(handler))
            except Exception:                        # noqa: BLE001
                where = ""
            for method in sorted(methods - {"HEAD", "OPTIONS"}):
                rows.append((method, prefix + route.path,
                             getattr(handler, "__name__", ""), where))

    walk(app.routes)
    return rows


def load_server():
    sys.path.insert(0, os.path.join(ROOT, "server"))
    os.environ.setdefault("DATABASE_URL", "sqlite://")
    from app.main import app
    return app


def load_agent():
    # 两个服务都有 `app` 包，同一个进程里导第二个会拿到第一个。
    # 所以智能体那边开子进程去问——**比清理 sys.modules 可靠**。
    import json
    import subprocess
    code = (
        "import sys, os, inspect, json;"
        "sys.path.insert(0, r'%s');"
        "from app.main import app;"
        "rows=[];"
        "\nfor r in app.routes:\n"
        "    ms=getattr(r,'methods',None)\n"
        "    if not ms: continue\n"
        "    fn=getattr(r,'endpoint',None)\n"
        "    try: where=os.path.basename(inspect.getsourcefile(fn))\n"
        "    except Exception: where=''\n"
        "    for m in sorted(ms-{'HEAD','OPTIONS'}):\n"
        "        rows.append([m, r.path, getattr(fn,'__name__',''), where])\n"
        "print(json.dumps(rows))"
    ) % os.path.join(ROOT, "agent")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          cwd=os.path.join(ROOT, "agent"))
    if done.returncode != 0:
        raise RuntimeError("问不到智能体的路由：%s" % (done.stderr or "")[-400:])
    return [tuple(r) for r in json.loads(done.stdout.strip().splitlines()[-1])]


SKIP = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc", "/"}


def table(rows, title):
    lines = ["### %s" % title, "",
             "| 方法 | 路径 | 处理函数 | 所在文件 |",
             "|---|---|---|---|"]
    for method, path, handler, where in rows:
        lines.append("| %s | `%s` | `%s` | `%s` |"
                     % (method, path, handler or "—", where or "—"))
    lines.append("")
    return lines


def build(server_rows, agent_rows):
    server_rows = sorted([r for r in server_rows if r[1] not in SKIP],
                         key=lambda r: (r[1], r[0]))
    agent_rows = sorted([r for r in agent_rows if r[1] not in SKIP],
                        key=lambda r: (r[1], r[0]))
    lines = [
        BEGIN,
        "",
        "> 本节由 `scripts/gen_api_list.py` 从**运行中的两个应用**的路由表导出，",
        "> 不是从代码里正则抓装饰器——抓装饰器会漏掉 `include_router` 的前缀。",
        "> 模型或路由改了重跑一次即可；`--check` 会在测试里盯着它别过期。",
        "",
        "业务后端 **%d** 个接口，智能体服务 **%d** 个接口。"
        % (len(server_rows), len(agent_rows)),
        "",
    ]
    lines += table(server_rows, "A.1 业务后端（:8080）")
    lines += table(agent_rows, "A.2 智能体服务（:8000）")
    lines.append(END)
    return "\n".join(lines)


def _code_key(code):
    """需求编号按「前缀 + 数字」排，不是按字符串——否则 K-10 会排在 K-2 前面。"""
    prefix, _, number = code.partition("-")
    return (prefix, int(number) if number.isdigit() else 0)


def module_table(server_rows, agent_rows):
    """第 3 章的模块总表：表数、接口数、对应需求，全部数出来。"""
    counts = {}
    for _, path, _, _ in server_rows:
        if path in SKIP:
            continue
        module = module_of_path(path)
        counts[module] = counts.get(module, 0) + 1

    load_metadata()                      # 只为确认模型能导入
    order = ["账号与权限", "房源", "看房预约", "合同与账单", "报修与维修",
             "互动与消息", "中介", "平台治理", "智能客服与知识"]
    lines = [SUM_BEGIN, "",
             "| 序号 | 模块 | 表数 | 接口数 | 对应需求 |",
             "|---|---|:--:|:--:|---|"]
    for i, module in enumerate(order, 1):
        tables = MODULES.get(module, [])
        codes = sorted({code for name in tables
                        for code in REQUIREMENTS.get(name, [])},
                       key=_code_key)
        lines.append("| 3.%d | %s | %d | %d | %s |"
                     % (i, module, len(tables), counts.get(module, 0),
                        " ".join(codes)))
    agent_count = len([r for r in agent_rows if r[1] not in SKIP])
    lines.append("| 3.10 | 智能体服务（独立进程） | — | %d | AI-01～AI-09 N-06 N-07 N-08 |"
                 % agent_count)
    lines += ["",
              "> 业务后端 **%d** 个接口（含 `/health`），智能体服务 **%d** 个。"
              % (sum(counts.values()), agent_count),
              "> 接口按路径前缀归并到模块（`scripts/db_meta.py` 的 `PATH_MODULES`），",
              "> 逐条清单见附录 A。",
              "",
              SUM_END]
    return "\n".join(lines)


def cited_endpoints(text):
    """把正文里引用到的接口找出来（附录本身不算）。"""
    body = text.split(BEGIN)[0]
    found = set()
    for methods, path in MULTI.findall(body):
        for method in methods.split("/"):
            found.add((method, path))
    for method, path in CITED.findall(body):
        found.add((method, path))
    return found


def known(server_rows, agent_rows):
    return {(m, p) for m, p, _, _ in list(server_rows) + list(agent_rows)}


def main():
    parser = argparse.ArgumentParser(description="生成并校验接口清单")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    server_rows = collect(load_server())
    agent_rows = load_agent()
    text = io.open(DOC, encoding="utf-8").read()

    # 一、正文引用的接口必须真的存在
    real = known(server_rows, agent_rows)
    real_paths = {p for _, p in real}
    bogus = []
    for method, path in sorted(cited_endpoints(text)):
        if (method, path) in real:
            continue
        if path in real_paths:
            ok = sorted(m for m, p in real if p == path)
            bogus.append("`%s %s` 的方法不对，实际是 %s"
                         % (method, path, "/".join(ok)))
        else:
            bogus.append("`%s %s` 这个接口不存在" % (method, path))

    # 二、正文里散落的那几个接口数
    inline_stale = []
    # `toolbox` 在 agents/ 目录下，但它**不是子智能体**——它是「让模型挑
    # 哪个工具」的选路层，挑完才轮到子智能体干活
    agents_dir = os.path.join(ROOT, "agent", "app", "agents")
    subagents = [d for d in os.listdir(agents_dir)
                 if os.path.isdir(os.path.join(agents_dir, d))
                 and d not in ("__pycache__", "toolbox")]
    counts = {
        "server": str(len([r for r in server_rows if r[1] not in SKIP])),
        "agent": str(len([r for r in agent_rows if r[1] not in SKIP])),
        "tables": str(len(load_metadata().tables)),
        "subagents": str(len(subagents)),
    }
    for pattern, template in INLINE:
        hits = re.findall(pattern, text)
        if len(hits) != 1:
            print("  ! 这条正则命中了 %d 处（应当恰好 1 处）：%s"
                  % (len(hits), pattern))
            return 1
        want = template.format(**counts)
        if hits[0] != want:
            inline_stale.append((pattern, want))

    # 三、附录与模块总表都要是最新的
    appendix = build(server_rows, agent_rows)
    summary = module_table(server_rows, agent_rows)
    start = text.index(BEGIN)
    stop = text.index(END) + len(END)
    sum_start = text.index(SUM_BEGIN)
    sum_stop = text.index(SUM_END) + len(SUM_END)
    stale = (text[start:stop] != appendix
             or text[sum_start:sum_stop] != summary)

    if args.check:
        for problem in bogus:
            print("  ! 正文里 %s" % problem)
        if stale:
            print("  ! 附录 A 过期了：接口有增减，重跑 python scripts/gen_api_list.py")
        for _pattern, want in inline_stale:
            print("  ! 正文里这句过期了，应为：%s" % want.strip())
        if bogus or stale or inline_stale:
            return 1
        print("接口清单是最新的，正文引用的 %d 条接口全部存在。"
              % len(cited_endpoints(text)))
        return 0

    if bogus:
        for problem in bogus:
            print("  ! 正文里 %s" % problem)
        print("先把正文里的接口改对，再生成。")
        return 1

    # 先改正文里那几处零散的数——它们在标记段之外，改了不影响下面的下标计算
    for pattern, want in inline_stale:
        text = re.sub(pattern, lambda _m, w=want: w, text, count=1)

    # 先换模块总表，再换附录——换完总表位置会变，附录的下标要重新算
    sum_start = text.index(SUM_BEGIN)
    sum_stop = text.index(SUM_END) + len(SUM_END)
    text = text[:sum_start] + summary + text[sum_stop:]
    start = text.index(BEGIN)
    stop = text.index(END) + len(END)
    io.open(DOC, "w", encoding="utf-8", newline="\n").write(
        text[:start] + appendix + text[stop:])
    print("附录 A 与模块总表已更新：业务后端 %d 条、智能体 %d 条；"
          "正文引用的 %d 条接口全部核对通过。"
          % (len([r for r in server_rows if r[1] not in SKIP]),
             len([r for r in agent_rows if r[1] not in SKIP]),
             len(cited_endpoints(text))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
