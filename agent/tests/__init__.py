# -*- coding: utf-8 -*-
"""测试包的引导：**先把大模型关掉，再让任何 app 模块被 import。**

## 为什么需要这一段

`app/config.py` 会读 `agent/.env`。谁在本机填了密钥，测试就会跟着
**真的往厂商发请求**——实测表现是整个套件卡死：每条用例几秒到二十几秒，
几百条跑不完；跑完了也要计费，而且结果随模型心情变化，
「同一份代码两次跑出不同结论」这种测试等于没有。

项目一直宣称「测试不依赖网络、数据库与大模型」。在没人填密钥的机器上
这话自动成立，可它是**靠运气成立的**，不是被保证的。今天给中转站接上
GPT 之后，第一件炸掉的就是测试套件。

## 做法

`load_dotenv` 默认不覆盖已经存在的环境变量。所以在这里先把这几个键
置空，`.env` 里写了什么都盖不过来，`settings.llm_enabled` 恒为 False，
`build_provider` 返回 `NullProvider`，全套用例继续跑在纯规则模式下。

要专门测「接上大模型之后」的行为，用假 provider 显式注入
（见 `test_router.py` 的 `FakeProvider`），而不是依赖本机配置——
那样才可复现。
"""

import os

# 超时也要一起清掉：`test_fusion_shortcut` 断言的是**代码里的默认值**
# （8 秒），而本机 .env 里为了迁就中转站的延迟填了别的数字，
# 不清的话那条用例会红——测的是配置文件，不是代码。
for _key in ("LLM_PROVIDER", "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL",
             "LLM_TIMEOUT", "LLM_MAX_TOKENS", "FUSION_ENABLED",
             "EMBED_BASE_URL", "EMBED_API_KEY", "EMBED_MODEL"):
    os.environ[_key] = ""

# 输出重定向到文件时（CI 就是这么跑的），Windows 上标准输出用的是本地
# 编码，而验收用例会打印中文指标表——`python -m unittest > out.txt`
# 会变成 7 个 UnicodeEncodeError，看上去像 7 条用例挂了。
# 在终端里跑永远复现不出来。原委见 server/app/console.py
import sys

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass
