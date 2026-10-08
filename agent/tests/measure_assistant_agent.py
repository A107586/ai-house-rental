# -*- coding: utf-8 -*-
"""agent 版统一入口的工具选择准确率实测——和 `measure_toolbox.py` 同一份评测集、同一个口径。

    # 需要配好 agent/.env 里的 LLM_*，不需要起服务
    python tests/measure_assistant_agent.py

口径照搬 legacy 那份，方便两边并排比：屏幕上列着 DL0001–DL0003、当前打开 DL0001，
身份按用例；**只看 agent 第一步的决定**——第一步调了哪个工具，或者直接回答
（记作 none）。第一步一出来就停，不真的去查后端：这里量的是「挑得准不准」，
不是「答得好不好」。

和 legacy 的一个区别：agent 没有「没接住」这一类。legacy 的选择器挑不出来会退回
规则链路；agent 挑不出来就是它自己直接回答了，所以只分「对」和「挑错」，
另记一类「模型不可用」（超时、熔断、输出坏了）——那是网络和服务的问题，
不算进准确率，但会单列出来，不会悄悄当成 none 计分。
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

from types import SimpleNamespace  # noqa: E402

from app.agents.assistant import loop  # noqa: E402
from app.agents.assistant.loop import Outcome  # noqa: E402
from app.agents.assistant.toolkit import Toolkit  # noqa: E402
from app.config import settings  # noqa: E402
from app.llm.base import LLMUnavailable  # noqa: E402
from app.llm.providers import build_provider  # noqa: E402
from tests.fixtures import toolbox_eval as fixture  # noqa: E402

CONTEXT = [
    "用户正打开着房源 DL0001 的详情页，「这套」指它。",
    "屏幕上的房源列表（用户说「第几套」按这个序号）：\n"
    "1. DL0001 中山区青泥洼桥 两室一厅 70平 整租 3050元/月\n"
    "2. DL0002 沙河口区黑石礁 一室一厅 57平 整租 2550元/月\n"
    "3. DL0003 甘井子区华南 两室一厅 82平 整租 2800元/月",
]
ROLE_NAMES = {"tenant": "租客", "landlord": "房东", "agent": "中介", "admin": "管理员"}


class FirstStep(Toolkit):
    """第一步一出来就记下并结束这一轮，不去查后端。"""

    def __init__(self, role):
        super().__init__(SimpleNamespace(logged_in=bool(role), role=role, runner_ctx={}))
        self.picked = None

    def call(self, name, params, text="", images=()):
        self.picked = (name, params)
        return Outcome({}, stop=True, message="")


def ask_once(text, provider, role):
    kit = FirstStep(role)
    who = ("用户身份：%s。" % ROLE_NAMES[role]) if role else "用户身份：未登录（查个人数据、办事都要先登录）。"
    started = time.time()
    try:
        loop.run(text, [], provider, kit, context_lines=[who] + CONTEXT)
    except LLMUnavailable as exc:
        return None, str(exc), int((time.time() - started) * 1000)
    got = kit.picked[0] if kit.picked else "none"
    return got, (kit.picked or ("", {}))[1], int((time.time() - started) * 1000)


# 验证集：评测集里没有的新说法，**改提示词之后写的，没拿它调过任何东西**。
# 第一版跑评测集时，9 条办事的句子 agent 第一步都先去调查询工具「确认一下」，
# 于是在提示词里写明了「办事直接调写工具」——这是照着评测集的失败改的，
# 再用同一份评测集报数会偏乐观。这一份用来回答「改完之后换个说法还灵不灵」。
HELD_OUT = [
    ("帮我把后天下午那场看房取消掉", "cancel_viewing", "tenant"),
    ("周六的看房能不能挪到下周一上午", "reschedule_viewing", "tenant"),
    ("把欠的水电费交一下", "pay_bill", "tenant"),
    ("合同快到期了，再续半年", "renew_contract", "tenant"),
    ("我不租了，想提前退", "end_contract", "tenant"),
    ("把第二套收藏起来", "favorite_house", "tenant"),
    ("厨房水龙头一直滴水，找人来修", "submit_repair", "tenant"),
    ("把 DL0005 重新上架", "toggle_house", "landlord"),
    ("拒绝明天那个看房预约", "handle_appointment", "landlord"),
    ("催一下 3 号楼那个租客交房租", "remind_tenant", "landlord"),
    ("退租要提前多久说", "knowledge", "tenant"),
    ("刚才那几套里哪套最便宜", ("compare_houses", "none"), "tenant"),
]


def _label(expected):
    if expected is None:
        return "（不调工具）"
    if isinstance(expected, (tuple, list, set)):
        return "/".join(expected)
    return expected


def main():
    provider = build_provider(settings)
    if provider is None or not getattr(provider, "available", False):
        print("没有可用的大模型（检查 agent/.env 的 LLM_*）。")
        return 2
    for label, cases in (("评测集", fixture.CASES), ("验证集（新说法）", HELD_OUT)):
        print("\n模型：%s · agent 第一步 · %s %d 条\n" % (settings.llm_model, label, len(cases)))
        measure(cases, provider)
    return 0


def _allowed(expected):
    allowed = (set(expected) if isinstance(expected, (tuple, list, set))
               else {expected or "none"})
    # legacy 的 converse 是「不查数据，直接回话」——在 agent 里没有这个工具，
    # 对应的就是模型直接回答（记作 none）
    if "converse" in allowed:
        allowed.add("none")
    return allowed


def measure(cases, provider):
    hit = wrong = down = 0
    times = []
    for text, expected, role in cases:
        got, params, elapsed = ask_once(text, provider, role)
        if got is None:
            down += 1
            print("  不可用 %-22s %s" % (text, params[:40]))
            continue
        allowed = _allowed(expected)
        kind = "对" if got in allowed else "挑错"
        hit += kind == "对"
        wrong += kind == "挑错"
        times.append(elapsed)
        print("  %-4s %-22s 期望 %-22s 实得 %-18s %6dms %s"
              % (kind, text, _label(expected), got, elapsed,
                 str(params)[:40] if params else ""))
    reached = hit + wrong
    print("\n" + "=" * 74)
    if reached:
        print("命中率（问到了模型的 %d 条） %d/%d = %.1f%%" % (reached, hit, reached, 100.0 * hit / reached))
        times.sort()
        print("第一步耗时：中位 %.1fs，最慢 %.1fs" % (times[len(times) // 2] / 1000, times[-1] / 1000))
    print("  挑错 %d · 模型不可用 %d（不计入命中率）" % (wrong, down))


if __name__ == "__main__":
    sys.exit(main())
