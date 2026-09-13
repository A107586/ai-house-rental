# -*- coding: utf-8 -*-
"""大模型档的 AI-03 实测。

    # 三个服务先起来
    python tests/measure_llm_mode.py

## 为什么单独一个脚本，不放进测试套件

它**要联网、要真花钱、结果还不完全可复现**——同一批问题两次跑，
模型措辞不同、上游偶尔超时。这三条里任何一条都足以把它挡在
`unittest` 之外：自动化测试必须离线、确定、可重复。

但「离线指标 100%」和「接上模型之后还是不是 100%」是两个问题。
《AI-03 智能客服设计方案》的「还没做到的」第 3 条写的就是这件事：

> 大模型档（配了 Key）没跑过验收。闸门的第三条判据与生成式回答
> 都只在桩替身下测过。

这个脚本就是去跑那一档。

## 它测什么

同一份验收集，走**线上真实链路**（`POST /agent/support`），记四件事：

1. **两条硬指标有没有掉**——有依据正确率、无依据拒答率。
   按设计它们不该掉：生成过不了校验就退回抽取式，退回的结果
   和离线档一模一样。**这条推断需要被验证，不是被相信。**
2. **生成采用率**——模型的回答有多少真的被用了。
3. **退回原因的分布**——`answer_note` 里写着每一次退回的理由，
   「数字对不上」和「上游超时」是完全不同的两件事：前者说明模型在编，
   后者说明网络不稳。混在一起看就什么也看不出来。
4. **延迟**——线上感知，不是离线的微秒数。
"""

import collections
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

from tests.fixtures import support_eval as ev  # noqa: E402

AGENT = os.environ.get("AGENT_BASE_URL", "http://127.0.0.1:8000")
TIMEOUT = 90


def ask(question):
    body = json.dumps({"text": question}).encode()
    req = urllib.request.Request(AGENT + "/agent/support", data=body,
                                 method="POST")
    req.add_header("Content-Type", "application/json")
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception as exc:                                  # noqa: BLE001
        return {"status": "错误", "note": str(exc)[:60],
                "ms": int((time.time() - started) * 1000)}
    retrieval = data.get("retrieval") or {}
    return {
        "status": data.get("status"),
        "source": retrieval.get("answer_source") or "",
        "note": retrieval.get("answer_note") or "",
        "cited": [c.get("id") for c in (data.get("citations") or [])],
        "message": data.get("message") or "",
        "ms": retrieval.get("latency_ms") or int((time.time() - started) * 1000),
    }


def golds(value):
    return set(value) if isinstance(value, (list, tuple, set)) else {value}


def main():
    try:
        with urllib.request.urlopen(AGENT + "/health", timeout=10) as response:
            health = json.loads(response.read().decode("utf-8"))
    except Exception as exc:                                  # noqa: BLE001
        print("连不上智能体（%s）：%s\n先把三个服务起起来。" % (AGENT, exc))
        return 2

    config = health.get("config") or {}
    print("智能体：%s · 模型 %s · 分词器 %s"
          % (config.get("mode"), config.get("llm_model"),
             "有" if health.get("word_level") else "无"))
    if not config.get("llm_enabled"):
        print("**没配大模型，这一轮量的还是离线档**，测不出这个脚本要测的东西。")
    print()

    rows = []
    correct = 0
    for question, gold in ev.ANSWERABLE:
        result = ask(question)
        hit = (result["status"] == "answered"
               and bool(golds(gold) & set(result["cited"])))
        correct += hit
        rows.append(("有依据", question, result, hit))
        print("  %s %-22s %-11s %5dms %s"
              % ("✓" if hit else "✗", question, result.get("source", ""),
                 result["ms"], result.get("note", "")))

    # **UNANSWERABLE 里是 (问句, 分类) 二元组，不是裸字符串。**
    # 第一版直接把元组当问句发出去，服务端 422，而 422 不等于 answered，
    # 于是七条全被记成「正确拒答」——**7/7 = 100% 是假的**。
    # 一个只会往好里错的统计口径，比没有统计更危险。
    refused = 0
    for question, _kind in ev.UNANSWERABLE:
        result = ask(question)
        # 连不上、参数错这类**根本没问到**的情况不能算「拒答成功」
        if result["status"] == "错误":
            print("  !  %-22s 这一条没问成：%s" % (question, result.get("note")))
            continue
        ok = result["status"] != "answered"
        refused += ok
        rows.append(("无依据", question, result, ok))
        print("  %s %-22s %-11s %5dms %s"
              % ("✓" if ok else "✗", question, result.get("source", ""),
                 result["ms"], result.get("note", "")))

    print()
    print("=" * 64)
    print("有依据问题正确率  %2d/%-2d = %5.1f%%   （离线档 20/20 = 100%%，要求 ≥ 80%%）"
          % (correct, len(ev.ANSWERABLE), 100.0 * correct / len(ev.ANSWERABLE)))
    print("无依据问题拒答率  %2d/%-2d = %5.1f%%   （离线档 10/10 = 100%%，要求 ≥ 90%%）"
          % (refused, len(ev.UNANSWERABLE),
             100.0 * refused / len(ev.UNANSWERABLE)))

    sources = collections.Counter(r[2].get("source", "") for r in rows)
    print("\n回答由谁给出：")
    for name, count in sources.most_common():
        print("   %-12s %d" % (name or "（拒答，没走到生成）", count))

    notes = collections.Counter(r[2].get("note", "") for r in rows
                                if r[2].get("note"))
    if notes:
        print("\n退回抽取式的原因（这一栏最值得看）：")
        for name, count in notes.most_common():
            print("   %-28s %d" % (name, count))

    latencies = sorted(r[2]["ms"] for r in rows)
    if latencies:
        print("\n线上延迟：中位 %dms · p95 %dms · 最慢 %dms"
              % (latencies[len(latencies) // 2],
                 latencies[int(len(latencies) * 0.95)], latencies[-1]))

    bad = [(kind, q, r) for kind, q, r, ok in rows if not ok]
    if bad:
        print("\n没达到的：")
        for kind, question, result in bad:
            print("   [%s] %-22s status=%s cited=%s"
                  % (kind, question, result["status"], result.get("cited")))
            print("        %s" % result.get("message", "")[:100])
    return 0


if __name__ == "__main__":
    sys.exit(main())
