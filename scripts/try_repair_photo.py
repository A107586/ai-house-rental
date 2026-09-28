# -*- coding: utf-8 -*-
"""拿一张真实的故障照片，走一遍「拍照报修」。

为什么单独留这个脚本：**这个功能的正例还没有量过**。

仓库里 14 张房源照片全是完好的房间，只够证明「不会无中生有」
（18 张完好照片，18 次都答「没有损坏」）。至于「真坏了能不能看出来、
说得准不准」，缺一批真实的故障照片，到现在还是未知数。

所以这一步交给拿得到照片的人：手机拍一张漏水、发霉、裂缝、
电线外露，运行

    python scripts/try_repair_photo.py 照片路径 [一句描述]

脚本会打印三样东西——**模型从照片里读到了什么**、
分到哪个工种、判了什么紧急程度。前两样对不上，就是识图的问题；
只有第三样不对，那是 `classify.py` 的词表该补，与识图无关。

前置：
  1. 模型服务带视觉头启动  powershell -File .\\start-local-llm.ps1 -Vision
  2. 智能体在 8000 端口跑着
不加 -Vision 时脚本照样能跑，只是「照片读到」一栏会是空的——
识图不可用，报修自动退回纯文字，这本身也值得看一眼。
"""

import base64
import json
import os
import sys
import time
import urllib.request

# Windows 上输出重定向到文件时用的是本地编码，打印中文会抛
# UnicodeEncodeError 把脚本断在半路（终端里看不出来）。
# 原委见 server/app/console.py
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

AGENT = os.environ.get("AGENT_BASE_URL", "http://127.0.0.1:8000")

EXT_TO_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".png": "image/png", ".gif": "image/gif",
               ".webp": "image/webp"}


def data_uri(path):
    """读成 data URI。

    **不走上传接口**，是为了让这个脚本不依赖登录与后端：
    智能体那一侧本来就同时收 data URI 和站内地址，
    而这里要验的是识图，不是上传。
    """
    ext = os.path.splitext(path)[1].lower()
    mime = EXT_TO_MIME.get(ext)
    if mime is None:
        raise SystemExit("只认 jpg / png / gif / webp，拿到的是 %s" % (ext or "无扩展名"))
    with open(path, "rb") as f:
        blob = f.read()
    if len(blob) > 5 * 1024 * 1024:
        raise SystemExit("照片超过 5 MB（服务端上限），先压一下")
    return "%s;base64,%s" % ("data:" + mime, base64.b64encode(blob).decode())


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    path = sys.argv[1]
    text = sys.argv[2] if len(sys.argv) > 2 else ""
    if not os.path.exists(path):
        raise SystemExit("找不到照片：%s" % path)

    body = {"text": text, "submit": False, "images": [data_uri(path)]}
    req = urllib.request.Request(
        AGENT + "/agent/repair", data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    started = time.time()
    try:
        out = json.loads(urllib.request.urlopen(req, timeout=300).read())
    except Exception as exc:                                     # noqa: BLE001
        raise SystemExit("请求失败：%s %s" % (type(exc).__name__, exc))

    print("照片        %s" % path)
    print("你写的描述  %s" % (text or "（没写，全靠照片）"))
    print("耗时        %.1f 秒" % (time.time() - started))
    print("-" * 56)
    print("照片里读到  %s" % (out.get("vision") or "（空——没开 -Vision，"
                                                  "或者模型说照片里没有损坏）"))
    print("故障类型    %s" % (out.get("category") or "—"))
    print("紧急程度    %s" % (out.get("urgency") or "—"))
    print("建议派单    %s" % (out.get("assignee") or "—"))
    print("助手回复    %s" % (out.get("message") or ""))
    print("-" * 56)
    print("submit=False，只分析不落库，跑多少遍都不会产生工单。")


if __name__ == "__main__":
    main()
