# -*- coding: utf-8 -*-
"""把标准输出切成 UTF-8。

## 为什么需要这一段

Windows 上，Python 给标准输出挑编码的依据是**它连到了哪里**：连到
交互终端时用终端的编码，重定向到文件或管道时用系统的本地编码
（本机是 cp1252）。cp1252 里没有汉字，于是：

    python seed.py              # 好好的
    python seed.py > seed.log   # UnicodeEncodeError，脚本当场断在半路

**在终端里永远看不见这个问题**，而 CI、`start-all.ps1` 里的日志重定向、
`python x.py | tee` 全都走的是重定向那条路。更麻烦的是 `seed.py` 这种
会写库的脚本：它不是启动时失败，是**干到一半才失败**，留下一个填了
一半的数据库。

日志里也一样。`main.py` 的慢请求告警是中文的，服务端的输出一旦被
重定向，那行日志就不会出现——出现的是一大段 `--- Logging error ---`
（logging 模块吞掉了异常，所以服务不会挂，只是诊断信息没了）。

`errors="replace"` 是兜底：真遇到这个编码表达不了的字符时，宁可打出
问号，也不要让一条日志把整个流程掀翻。
"""

import sys


def force_utf8():
    """让 stdout / stderr 无论连到哪里都用 UTF-8。可以重复调用。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            # 被替换成 StringIO 之类没有 reconfigure 的对象（测试里常见），
            # 或者流已经关了。**这是锦上添花的事，不该因此报错**
            pass
