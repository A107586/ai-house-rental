# -*- coding: utf-8 -*-
"""对着**跑起来的**智能体问 10 句，逐句看答没答出来、花了多久、调了几次模型。

    # 先起好智能体（一键启动面板，或 start-all.ps1）
    python tests/measure_chat_latency.py
    python tests/measure_chat_latency.py --agent http://127.0.0.1:8000

## 为什么要有它

2026-10-21 换成中转站的模型后，「测试连接」一句话 4.2 秒，看着很健康；
真问助手却 8 句只答出 1 句。原因是本机 `LLM_TIMEOUT=7`：助手每答一句要调
2～4 次模型，每次都带着完整的工具清单，单次就常常超过 7 秒；超时不重试，
连续两次超时还会熔断 60 秒，后面每一句都瞬间回「连不上大模型」。

**单测一句话的耗时定不了超时**，得照用户的用法真问几句。
和 `measure_toolbox.py` 一样：要联网、要花钱、结果随服务商负载波动，所以不进 unittest。

## 怎么看结果

- `status` 是 `unavailable` 的就是没答出来；
- `calls` 来自 `/health` 里调用计数的差，`avg/call` 是这一句平均每次调用的耗时——
  拿它和 `LLM_TIMEOUT` 比，比超时还长的那几句就是被超时卡掉的；
- 末尾打印 `/health` 的 `llm`：`circuit_open` 为真说明已经熔断。
"""

import argparse
import json
import sys
import time
import urllib.request

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

QUESTIONS = ["押金怎么退", "中山区两室一厅三千以内", "退租要提前多久", "有没有带电梯的房子",
             "怎么预约看房", "高新园区一室户", "报修一般多久有人来", "合同到期了怎么续租",
             "附近有地铁的房子", "我想租个便宜点的"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", default="http://127.0.0.1:8000")
    base = ap.parse_args().agent.rstrip("/")

    def health():
        with urllib.request.urlopen(base + "/health", timeout=10) as r:
            return json.loads(r.read())

    def ask(text):
        req = urllib.request.Request(base + "/agent/chat", json.dumps({"text": text}).encode("utf-8"),
                                     {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read())

    config = health()["config"]
    print("模型 %s · %s" % (config.get("llm_model"), config.get("mode")))
    answered, seconds = 0, []
    for text in QUESTIONS:
        before = health()["llm"]["calls"]
        t0 = time.time()
        status = ask(text).get("status")
        dt = time.time() - t0
        calls = health()["llm"]["calls"] - before
        answered += status != "unavailable"
        seconds.append(dt)
        print("%5.1fs  calls=%d  avg/call=%4.1fs  %-11s %s" % (dt, calls, dt / max(calls, 1), status, text))
    seconds.sort()
    print("\n答出 %d/%d · 每句中位 %.1f 秒 · 最慢 %.1f 秒"
          % (answered, len(QUESTIONS), seconds[len(seconds) // 2], seconds[-1]))
    print(health()["llm"])


if __name__ == "__main__":
    main()
