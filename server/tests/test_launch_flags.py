# -*- coding: utf-8 -*-
"""每一处启动 uvicorn 的地方都必须带 `--no-proxy-headers`。

## 为什么要用一条测试去查启动参数

因为**单元测试从结构上就看不到这个问题**。

uvicorn 的 `proxy_headers` 默认是开的，并且默认信任 127.0.0.1：
只要连接来自本机，它就拿 `X-Forwarded-For` 改写 `request.client.host`——
在请求到达应用之前。而 `TestClient` 根本不经过 uvicorn 的中间件，
所以 `test_client_ip.py` 全绿，真服务却照样能被绕过。

2026-09-28 冒烟实测（对着 `python -m uvicorn app.main:app` 起的真服务）：

    同一来源连错 10 次: 401×8, 429, 429     ← 限住了
    带 X-Forwarded-For: 8.8.8.8 → 401       ← 又放行了
    不带转发头         → 429

部署单元里原先还写着「刻意不加 `--proxy-headers`」——**不加等于开着**，
那句注释描述的是一个不存在的状态。

转发头只让一层管：应用按 `TRUSTED_PROXY_HOPS` 自己处理（`app/netaddr.py`）。
所以 uvicorn 那一层必须明确关掉。
"""

import glob
import io
import json
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _read(path):
    with io.open(path, encoding="utf-8-sig") as handle:
        return handle.read()


def uvicorn_launches():
    """找出仓库里所有启动 uvicorn 的地方，返回 (来源, 那一段启动命令)。"""
    found = []

    for path in glob.glob(os.path.join(ROOT, "deploy", "*.service")):
        text = _read(path)
        # systemd 的续行符 `\` 把一条命令拆成几行，先拼回去
        joined = re.sub(r"\\\s*\n\s*", " ", text)
        for line in joined.splitlines():
            if line.startswith("ExecStart=") and "uvicorn" in line:
                found.append((os.path.relpath(path, ROOT), line))

    script = os.path.join(ROOT, "start-all.ps1")
    if os.path.exists(script):
        for line in _read(script).splitlines():
            if "uvicorn app.main:app" in line and not line.lstrip().startswith("#"):
                found.append(("start-all.ps1", line.strip()))

    # 一键启动的控制面板（9-28 之后新加的入口）。**第一版的这条测试没扫它**，
    # 面板于是又起了两个不带 --no-proxy-headers 的 uvicorn——
    # 2026-10-21 读面板代码时才发现。启动入口每多一个，这里就要跟着多扫一处。
    panel = os.path.join(ROOT, "launcher", "panel.py")
    if os.path.exists(panel):
        for match in re.finditer(r"lambda:\s*\[([^\]]*)\]", _read(panel), re.S):
            if '"uvicorn"' in match.group(1):
                found.append((os.path.join("launcher", "panel.py"),
                              " ".join(match.group(1).split())))

    launch = os.path.join(ROOT, ".claude", "launch.json")
    if os.path.exists(launch):
        for cfg in json.loads(_read(launch)).get("configurations", []):
            args = cfg.get("runtimeArgs", [])
            if "uvicorn" in args:
                found.append((".claude/launch.json:%s" % cfg.get("name"),
                              " ".join(args)))
    return found


class TestUvicornProxyHeadersAreOff(unittest.TestCase):

    def test_launches_were_found(self):
        """先确认真的找到了启动命令——**找不到就全绿**是最危险的一种绿。"""
        sources = {source.split(":")[0] for source, _ in uvicorn_launches()}
        self.assertIn(os.path.join("deploy", "rental-api.service"), sources)
        self.assertIn(os.path.join("deploy", "rental-agent.service"), sources)
        self.assertIn("start-all.ps1", sources)
        self.assertIn(os.path.join("launcher", "panel.py"), sources)
        panel = [s for s, _ in uvicorn_launches() if s.startswith("launcher")]
        self.assertEqual(len(panel), 2, "面板里应当有业务后端、智能体两条 uvicorn 启动命令")

    def test_every_launch_turns_proxy_headers_off(self):
        missing = [source for source, command in uvicorn_launches()
                   if "--no-proxy-headers" not in command]
        self.assertEqual(missing, [],
                         "这些地方启动 uvicorn 没关 proxy_headers（它默认是开的）："
                         "%s" % missing)

    def test_nobody_turns_it_back_on(self):
        """有人为了「取到真实 IP」顺手加回 `--proxy-headers`，同样要拦。"""
        enabled = [source for source, command in uvicorn_launches()
                   if re.search(r"--proxy-headers(?!\S)", command)]
        self.assertEqual(enabled, [])


if __name__ == "__main__":
    unittest.main()
