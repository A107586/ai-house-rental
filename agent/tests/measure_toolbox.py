# -*- coding: utf-8 -*-
"""工具选择准确率实测。

    # 需要配好 agent/.env 里的 LLM_*，不需要起服务
    python tests/measure_llm_mode.py   # 客服那条线
    python tests/measure_toolbox.py    # 这条线

## 为什么单独一个脚本

和 `measure_llm_mode.py` 同理：**要联网、要花钱、结果不完全可复现**，
所以不能进 `unittest`。自动化测试必须离线、确定、可重复。

## 它补的是哪个窟窿

项目的主线改动是「把选路从关键词表换成模型选工具，规则兜底」。
AI-01/02/03/04/06 每一项都有验收数字，**唯独这条主线没有**。
`test_toolbox.py` 把管道测得很细（回退、参数白名单、序数消解、
字段映射），但没有一个数字回答「模型到底挑得准不准」。

主线没有指标，答辩时说「换成模型选路更准了」就只能靠嘴说。

## 三个数分开看

1. **命中率**——挑对了工具的比例。这是主指标。
2. **未接住率**——模型没选中任何工具（超时、输出坏了、选了不存在的）。
   **这一类不算错**：规则链路会接着跑，用户照样有回应。
   它和「挑错工具」是两件完全不同的事——挑错会把人带进另一条路，
   没接住只是退回原来那条路。混在一起看就分不清模型是不准还是网不稳。
3. **挑错率**——挑了，但挑错了。**这一类才是真正的损失。**
"""

import collections
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

from app.agents.toolbox import catalog, planner  # noqa: E402
from app.config import settings  # noqa: E402
from app.llm.providers import build_provider  # noqa: E402
from tests.fixtures import toolbox_eval as fixture  # noqa: E402


def _label(expected):
    if expected is None:
        return "（不接）"
    if isinstance(expected, (tuple, list, set)):
        return "/".join(expected)
    return expected


def ask_once(text, provider, role):
    started = time.time()
    decision = planner.plan(text, provider,
                            logged_in=bool(role), role=role,
                            last_house_nos=["DL0001", "DL0002", "DL0003"],
                            house_no="DL0001")
    return decision, int((time.time() - started) * 1000)


def _circuit_open(decision):
    return "熔断" in (decision.get("why") or "")


def _reopen_seconds(provider):
    """问熔断器还有多久放行。问不到就按默认的 60 秒等。"""
    try:
        return max(1, int(provider.stats().get("circuit_reopen_in") or 0) + 2)
    except Exception:                                        # noqa: BLE001
        return 62


def main():
    provider = build_provider(settings)
    if provider is None or not getattr(provider, "available", False):
        print("没有可用的大模型（检查 agent/.env 的 LLM_*）。"
              "这个脚本量的就是模型，没有模型就没什么可量的。")
        return 2

    print("模型：%s · 超时 %ss" % (settings.llm_model, settings.llm_timeout))
    print("工具总数：%d\n" % len(catalog.TOOLS))

    hit = missed = wrong = 0
    rows = []
    for text, expected, role in fixture.CASES:
        # **熔断打开时不要接着打。**
        #
        # 第一版没有这段，结果上游一断，熔断器一开，后面 14 条在 0ms
        # 内全部返回 none，报出来「命中率 54.8%」——那不是模型的准确率，
        # 是**上游死了一半的准确率**。指标要么量到，要么说没量到，
        # 不能把网络故障算进模型头上。
        decision, elapsed = ask_once(text, provider, role)
        if _circuit_open(decision):
            wait = _reopen_seconds(provider)
            print("  ·· 熔断打开，等 %d 秒再问「%s」" % (wait, text))
            time.sleep(wait)
            decision, elapsed = ask_once(text, provider, role)

        got = decision.get("tool") or "none"

        # 期望值可以是一个元组（几个都算对），见 fixture 的说明
        allowed = (set(expected) if isinstance(expected, (tuple, list, set))
                   else {expected})
        if expected is None:
            # 不该交给工具层：只有 none 算对
            kind = "对" if got == "none" else "挑错"
        elif got == "none":
            kind = "没接住"
        elif got in allowed:
            kind = "对"
        else:
            kind = "挑错"

        hit += kind == "对"
        missed += kind == "没接住"
        wrong += kind == "挑错"
        rows.append((kind, text, expected, got, decision, elapsed))
        print("  %-4s %-22s 期望 %-22s 实得 %-22s %5dms %s"
              % (kind, text, _label(expected), got, elapsed,
                 decision.get("why", "")[:36]))

    total = len(fixture.CASES)
    reached = hit + wrong                      # 真的问到了模型的那些
    print()
    print("=" * 74)
    if reached:
        print("命中率（只算问到了模型的 %d 条） %2d/%-2d = %5.1f%%   ← 主指标"
              % (reached, hit, reached, 100.0 * hit / reached))
    print("覆盖率（这一轮真的问到了几条） %2d/%-2d = %5.1f%%"
          % (reached, total, 100.0 * reached / total))
    print()
    print("  命中   %2d   挑对了工具" % hit)
    print("  挑错   %2d   ← 真正的损失：把人带进了另一条路" % wrong)
    print("  没接住 %2d   ← 模型没给出工具，退回规则链路，用户照样有回应"
          % missed)
    if missed > total * 0.2:
        print("\n  ⚠ 没接住的比例偏高，这一轮多半是上游不稳，"
              "**别把它当模型的准确率报出去**。")

    reasons = collections.Counter(r[4].get("why", "")[:40] for r in rows
                                  if r[0] == "没接住")
    if reasons:
        print("\n没接住的原因：")
        for name, count in reasons.most_common():
            print("   %-42s %d" % (name or "（模型说不用工具）", count))

    if wrong:
        print("\n挑错的（逐条看，这些才要改提示词或工具描述）：")
        for kind, text, expected, got, _, _ in rows:
            if kind == "挑错":
                print("   %-22s 期望 %-22s 实得 %s" % (text, _label(expected), got))

    latencies = sorted(r[5] for r in rows)
    print("\n选一次工具的耗时：中位 %dms · p95 %dms · 最慢 %dms"
          % (latencies[len(latencies) // 2],
             latencies[int(len(latencies) * 0.95)], latencies[-1]))

    # 身份过滤：未登录的那几句，绝不能挑中「必须登录」的工具。
    # 这不是准确率问题，是**越权问题**——挑中了说明可见工具集没按身份裁剪。
    anonymous = {text for text, _, role in fixture.CASES if not role}
    leaked = [(text, got) for _, text, _, got, _, _ in rows
              if text in anonymous and got in fixture.LOGIN_ONLY]
    print("\n身份过滤：未登录的 %d 句里，挑中「必须登录」工具 %d 次"
          % (len(anonymous), len(leaked)))
    for text, got in leaked:
        print("   !! %-22s -> %s" % (text, got))
    return 0


if __name__ == "__main__":
    sys.exit(main())
