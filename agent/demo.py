"""命令行演示脚本：不启动服务、不连数据库、不接大模型也能跑。

用途有二：
1. 组员本地验证解析效果；
2. 答辩演示时展示「意图路由 → 条件解析 → 查询参数」的完整决策链路。

    python demo.py                # 跑内置样例
    python demo.py "中山区两室一厅三千以内"   # 跑指定句子
"""

import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Windows 控制台默认代码页显示不了部分字符，统一切到 UTF-8
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from app import supervisor  # noqa: E402
from app.agents.booking import agent as booking  # noqa: E402
from app.agents.property_finder.agent import understand  # noqa: E402
from app.agents.property_finder.tools.search_properties import build_params  # noqa: E402
from app.llm.base import NullProvider  # noqa: E402

SAMPLES = [
    "中山区两室一厅，租金三千以内，最好带电梯",
    "帮我找一个月租3000以内，两室一厅，距离学校近一点，最好有地铁的房子",
    "沙河口 合租 单间 1500以下",
    "甘井子区 80平以上的三室 预算不超过五千",
    "有没有金州区2000到3000的一居室",
    "我想预约这周六下午两点看房",
    "最近三天都可以看房",
    "卫生间漏水了，能报修吗",
    "帮我看看这份合同的违约条款",
    "退租的时候押金怎么退",
    "今天股市怎么样",
]


def show(text, provider):
    print("─" * 66)
    print("输入：%s" % text)

    routed = supervisor.classify(text, provider=provider)
    print("  意图：%s（置信度 %s，来源 %s）"
          % (supervisor.label(routed["intent"]), routed["confidence"], routed["source"]))
    if routed["hits"]:
        print("  命中词：%s" % "、".join(routed["hits"]))

    if routed["intent"] == "booking":
        result = booking.run(text, house_no=None, taken=[])
        print("  状态：%s" % result["status"])
        print("  回复：%s" % result["message"])
        return

    if routed["intent"] != "search":
        return

    filters, features, source = understand(text, provider=provider)
    print("  筛选条件：%s" % json.dumps(filters, ensure_ascii=False))
    if features:
        print("  偏好：%s" % "、".join(features))
    print("  解析来源：%s" % source)
    print("  后端查询参数：%s" % json.dumps(build_params(filters), ensure_ascii=False))


def main():
    provider = NullProvider()
    print("运行模式：纯规则（未配置大模型，属预期的降级模式）")
    texts = sys.argv[1:] or SAMPLES
    for text in texts:
        show(text, provider)
    print("─" * 66)


if __name__ == "__main__":
    main()
