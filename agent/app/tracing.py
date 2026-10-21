"""请求编号 + 结构化访问日志（可观测性）。

## 解决什么问题

用户说「助手好慢」「点了没反应」，排查时要回答三个问题：**是哪一次请求、经过了
哪些服务、每一段花了多久**。原先只有「超过 500ms 的慢请求」一行中文日志，
而且智能体和业务后端各记各的，对不上是同一次操作。

## 做法

- 每个请求一个编号：请求头里带了 `X-Request-ID` 就沿用，没带就生成；**智能体调业务后端时
  把这个编号带过去**（app/backend.py），两边的日志就能用同一个编号串起来。
- 每个请求一行 JSON 日志：时间、服务名、编号、方法、路径、状态码、耗时。
  一行一条、字段固定，`grep 编号` 就能把一次操作在两个服务里的记录全捞出来，
  以后要接日志平台（ELK、Loki）也不用改格式。

业务后端那边有一份同样的 `server/app/tracing.py`。两个服务各自独立部署，
为这几十行代码跨服务共享一个模块不划算（同 app/main.py 开头内联 UTF-8 设置的理由）。

测试时设 `ACCESS_LOG=0` 关掉输出（见 tests/__init__.py），不然一千条测试刷满屏幕。
"""

import contextvars
import json
import logging
import os
import re
import sys
import time
import uuid

HEADER = "X-Request-ID"
SERVICE = "agent"

REQUEST_ID = contextvars.ContextVar("request_id", default="")

# 只接受看起来像编号的值：请求头是外部输入，原样写进日志会被拿来伪造日志行
_VALID = re.compile(r"^[A-Za-z0-9-]{8,64}$")

# 静态资源和健康检查太频繁、又没有排查价值，不记
_QUIET_PREFIXES = ("/assets/", "/static/")
_QUIET_PATHS = {"/health", "/favicon.ico"}

_logger = logging.getLogger("rental.access")


def setup():
    """把访问日志接到 stdout。重复调用无害。"""
    if os.environ.get("ACCESS_LOG", "1") == "0" or _logger.handlers:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(handler)
    _logger.setLevel(logging.INFO)
    _logger.propagate = False


def current():
    """当前这个请求的编号；不在请求里时是空串。"""
    return REQUEST_ID.get()


def log(event, **fields):
    """写一行结构化日志，自动带上时间、服务名和当前请求编号。"""
    record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "service": SERVICE,
              "event": event, "request_id": current()}
    record.update(fields)
    _logger.info(json.dumps(record, ensure_ascii=False, default=str))


async def trace_requests(request, call_next):
    """中间件：给请求分配编号、写回响应头、记一行访问日志。"""
    incoming = request.headers.get(HEADER, "")
    request_id = incoming if _VALID.match(incoming) else uuid.uuid4().hex[:16]
    token = REQUEST_ID.set(request_id)
    started = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers[HEADER] = request_id
        return response
    finally:
        path = request.url.path
        if path not in _QUIET_PATHS and not path.startswith(_QUIET_PREFIXES):
            log("request", method=request.method, path=path, status=status,
                ms=round((time.perf_counter() - started) * 1000, 1))
        REQUEST_ID.reset(token)
