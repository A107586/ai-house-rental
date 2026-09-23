# -*- coding: utf-8 -*-
"""MTP 开 / 关的对照测量。

两个臂**不能同时跑**——一张卡只装得下一个引擎，所以分两次：

    powershell -File .\\start-local-llm.ps1 -Vision
    python mtp_ab.py off

    powershell -File .\\start-local-llm.ps1 -Mtp -Vision
    python mtp_ab.py on

    python mtp_ab.py compare

量三样，**按「离用户有多近」从远到近排**：

1. 纯解码 t/s —— 引擎指标，最好看，但用户感觉不到；
2. 本项目真实负载 —— 工具选择那一类，输出只有二三十个 token，
   投机解码在短输出上摊不开，这一档才是实际收益；
3. 整轮对话延迟 —— 用户唯一真正感觉得到的东西。

**第 3 项是判据，前两项只是解释第 3 项为什么是那个数。**
只报纯解码翻了多少倍，是拿一个用户感觉不到的数字邀功。
"""

import json
import os
import statistics
import subprocess
import sys
import time
import urllib.request

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

LLM = "http://127.0.0.1:18240/v1/chat/completions"
BE = "http://127.0.0.1:8080"
AG = "http://127.0.0.1:8000"
OUT = "D:/kvmem/mtp_%s.json"

# 本项目真实会发出去的那类请求：让模型在工具表里挑一个，输出很短。
PLAN = [
    {"role": "system", "content": "只输出 JSON，不要解释。"},
    {"role": "user", "content": "从下面的工具里挑一个：search_houses / my_bills / "
                                "my_contracts / knowledge / none。"
                                "用户说：帮我找中山区两室一厅三千以内。"
                                '输出 {"tool":"...","params":{}}'},
]

# 整轮对话。和长对话测试用同一种维护方式，不另造一套上下文管理
TURNS = ["我想在沙河口租房", "三千以内", "两室一厅", "介绍下第一套",
         "这套有电梯吗", "我的合同还有几天到期", "这个月房租交了吗"]


def post(url, body, timeout=300):
    req = urllib.request.Request(
        url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    started = time.time()
    data = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
    return data, time.time() - started


def llm(messages, max_tokens):
    body = {"model": "Ternary-Bonsai-2-27B-PQ2_0.gguf", "messages": messages,
            "max_tokens": max_tokens, "temperature": 0.0}
    data, elapsed = post(LLM, body)
    usage = data.get("usage") or {}
    return elapsed, usage.get("prompt_tokens"), usage.get("completion_tokens")


def measure():
    out = {}

    # 1) 纯解码：让它一直吐，看稳态速度
    runs = []
    for _ in range(3):
        el, _pin, pout = llm(
            [{"role": "user", "content": "从 1 数到 200，用逗号隔开。"}], 200)
        runs.append((pout or 0) / el)
    out["decode_tps"] = statistics.median(runs)

    # 2) 本项目真实负载
    runs, outs = [], []
    llm(PLAN, 150)                               # 预热，别把首次的图构建算进去
    for _ in range(5):
        el, _pin, pout = llm(PLAN, 150)
        runs.append(el)
        outs.append(pout or 0)
    out["plan_sec"] = statistics.median(runs)
    out["plan_out_tokens"] = statistics.median(outs)
    out["plan_tps"] = statistics.median(outs) / statistics.median(runs)

    # 3) 整轮对话——用户唯一感觉得到的
    token = post(BE + "/api/auth/login",
                 {"username": "tenant1", "password": "test123"})[0]["token"]
    hist, last, house, secs = [], [], "", []
    for text in TURNS:
        body = {"text": text, "token": token, "session_id": "mtp-ab",
                "history": hist, "last_house_nos": last}
        if house:
            body["house_no"] = house
        data, elapsed = post(AG + "/agent/chat", body)
        secs.append(elapsed)
        hist.append(text)
        nos = [x.get("house_no") for x in (data.get("items") or [])
               if x.get("house_no")]
        if nos:
            last = nos
        if data.get("house_no"):
            house = data["house_no"]
    out["turn_median"] = statistics.median(secs)
    out["turn_max"] = max(secs)
    out["turns"] = [round(x, 2) for x in secs]
    return out


def acceptance_from_log():
    """引擎日志里如果报了接受率就捡出来。捡不到不算错——这是旁证，不是判据。"""
    hits = []
    for name in ("llm3.log", "llm2.log", "llm_vision.log", "mtp_try.log"):
        path = os.path.join("D:/kvmem", name)
        if not os.path.exists(path):
            continue
        try:
            text = open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        for line in text.splitlines():
            low = line.lower()
            if any(w in low for w in ("accept", "spec", "draft")) and any(
                    c.isdigit() for c in line):
                hits.append(line.strip()[:160])
    return hits[-8:]


def main():
    what = sys.argv[1] if len(sys.argv) > 1 else "off"

    if what == "compare":
        rows = {}
        for tag in ("off", "on"):
            path = OUT % tag
            if not os.path.exists(path):
                raise SystemExit("还没有 %s 那一臂的结果：先跑 python mtp_ab.py %s"
                                 % (tag, tag))
            rows[tag] = json.load(open(path, encoding="utf-8"))
        off, on = rows["off"], rows["on"]

        def line(label, key, unit="", better="high"):
            a, b = off[key], on[key]
            ratio = (b / a) if better == "high" else (a / b)
            print("  %-22s %8.2f%s %8.2f%s   %s %.2f×"
                  % (label, a, unit, b, unit,
                     "快" if ratio > 1 else "慢", ratio if ratio > 1 else 1 / ratio))

        print("                          MTP 关    MTP 开")
        line("纯解码 t/s", "decode_tps")
        line("工具选择 t/s", "plan_tps")
        line("工具选择 单次秒", "plan_sec", "s", "low")
        line("整轮 中位秒", "turn_median", "s", "low")
        line("整轮 最慢秒", "turn_max", "s", "low")
        print("\n  每轮明细  关：%s" % off["turns"])
        print("            开：%s" % on["turns"])
        print("\n  判据是「整轮中位秒」。纯解码翻几倍是引擎指标，"
              "用户感觉不到——\n  本项目一次工具选择只输出 %d 个 token，"
              "投机解码在这个长度上摊不开。" % on["plan_out_tokens"])
        return 0

    print("正在量（约一分钟）…")
    result = measure()
    result["tag"] = what
    result["log_hits"] = acceptance_from_log()
    json.dump(result, open(OUT % what, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("[%s] 纯解码 %.1f t/s  工具选择 %.2fs（%d token, %.1f t/s）  "
          "整轮中位 %.2fs 最慢 %.2fs"
          % (what, result["decode_tps"], result["plan_sec"],
             result["plan_out_tokens"], result["plan_tps"],
             result["turn_median"], result["turn_max"]))
    if result["log_hits"]:
        print("引擎日志里和投机解码有关的几行：")
        for h in result["log_hits"]:
            print("   ", h)
    return 0


if __name__ == "__main__":
    sys.exit(main())
