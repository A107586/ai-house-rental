# -*- coding: utf-8 -*-
"""冒烟：对着**跑起来的**两个服务，把一条真实用户路径走一遍。

    python scripts/smoke.py                                  # 本机（8080 / 8000）
    SMOKE_API=https://域名 SMOKE_AGENT=https://域名 python scripts/smoke.py   # 线上

只用标准库，不依赖测试框架——冒烟要验的是**跑起来的系统**，
不是导进来的模块。

## 为什么单元测试之外还要有它

**2026-09-28 它抓到一个单元测试从结构上看不见的洞。**
`test_client_ip.py` 全绿，而对着真服务：

    连错 10 次: 401×8, 429, 429
    带 X-Forwarded-For: 8.8.8.8 → 401      ← 又放行了

原因在 uvicorn：它的 `proxy_headers` **默认开着**、默认信任 127.0.0.1，
在请求到达应用之前就拿转发头改写了客户端地址。`TestClient` 不经过
uvicorn 的中间件，所以测试永远看不到这一层。现在每个启动 uvicorn 的
地方都显式 `--no-proxy-headers`，由 `server/tests/test_launch_flags.py` 盯着。

## 副作用（线上跑之前要知道）

- 会注册一个 `smoke` 开头的测试账号；
- 会把本机出口 IP 的登录次数用满，五分钟内这台机器登录不了——
  这正是它在验的东西。
"""
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request

# 线上 nginx 把 /api 与 /agent 收在同一个域名下，两个都填域名即可
API = (os.environ.get("SMOKE_API") or "http://127.0.0.1:8080").rstrip("/")
AGENT = (os.environ.get("SMOKE_AGENT") or "http://127.0.0.1:8000").rstrip("/")

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

passed = []
failed = []


def call(url, payload=None, token=None, method=None, headers=None, timeout=60):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data,
                                 method=method or ("POST" if data else "GET"))
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            return response.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except ValueError:
            return exc.code, {"raw": body[:200]}
    except Exception as exc:                                  # noqa: BLE001
        return 0, {"error": str(exc)[:200]}


def check(name, ok, detail=""):
    (passed if ok else failed).append(name)
    print("  %s %-44s %s" % ("✓" if ok else "✗", name, detail))


print("=" * 78)
print("一、两个服务的健康检查")
status, health = call(API + "/health")
check("业务后端 /health", status == 200 and health.get("status") == "ok",
      "房源 %s 套" % health.get("houses"))
# 先问线上路径 /agent/health（nginx 单独转到智能体的 /health），
# 404 再问本机直连的 /health。**只看状态码不够**：线上裸 /health
# 会落到前端 index.html，200 + 一页 HTML——下面按 JSON 的 status 判。
status, ahealth = call(AGENT + "/agent/health")
if status == 404:
    status, ahealth = call(AGENT + "/health")
check("智能体 /health", status == 200 and ahealth.get("status") == "ok",
      "分词器 %s · %s" % (ahealth.get("word_level"),
                          (ahealth.get("config") or {}).get("mode")))
check("分词器就位（word_level）", ahealth.get("word_level") is True,
      "false 会让 AI-03 有依据正确率低一截")

print()
print("二、房源列表与详情")
status, houses = call(API + "/api/houses?page=1&page_size=5")
rows = (houses or {}).get("list") or []
check("房源列表", status == 200 and len(rows) > 0, "返回 %d 条" % len(rows))
first = rows[0]["house_no"] if rows else ""
status, detail = call(API + "/api/houses/%s" % first)
check("房源详情", status == 200 and detail.get("house_no") == first, first)
before = detail.get("view_count", 0)
call(API + "/api/houses/%s" % first)
status, detail2 = call(API + "/api/houses/%s" % first)
check("浏览量自增", detail2.get("view_count", 0) > before,
      "%s → %s" % (before, detail2.get("view_count")))

print()
print("三、注册 → 登录 → 收藏（一条真实用户路径）")
suffix = "%d%d" % (int(time.time()) % 100000, random.randint(10, 99))
user = "smoke%s" % suffix
phone = "13" + "".join(random.choice("0123456789") for _ in range(9))
status, reg = call(API + "/api/auth/register", {
    "username": user, "password": "Smoke#2026", "real_name": "冒烟测试",
    "phone": phone, "role": "tenant"})
check("注册", status in (200, 201), user)
status, login = call(API + "/api/auth/login",
                     {"username": user, "password": "Smoke#2026"})
token = (login or {}).get("token", "")
check("登录并拿到令牌", status == 200 and bool(token), "令牌 %d 字符" % len(token))
status, me = call(API + "/api/auth/me", token=token)
check("凭令牌取当前用户", status == 200 and me.get("username") == user,
      me.get("role", ""))
status, _ = call(API + "/api/favorites/%s" % first, {}, token=token)
check("收藏房源", status in (200, 201), first)
status, favs = call(API + "/api/favorites", token=token)
check("收藏列表里有它", any(f.get("house_no") == first
                            for f in (favs or {}).get("list", [])))
status, _ = call(API + "/api/favorites/%s" % first, token=token, method="DELETE")
check("取消收藏", status == 200)

print()
print("四、未登录不得越界")
status, _ = call(API + "/api/bills/mine")
check("未登录查账单被拒", status in (401, 403), "HTTP %d" % status)
status, _ = call(API + "/api/admin/users", token=token)
check("租客访问管理端被拒", status in (401, 403), "HTTP %d" % status)

print()
print("五、智能体（这一段慢，要等大模型）")
started = time.time()
status, ans = call(AGENT + "/agent/support", {"text": "押金怎么退"}, timeout=120)
elapsed = int((time.time() - started) * 1000)
cites = [c.get("id") for c in (ans or {}).get("citations", [])]
check("客服答「押金怎么退」", status == 200 and ans.get("status") == "answered",
      "%dms · 引用 %s" % (elapsed, cites or "无"))
check("回答带引用（可追溯）", bool(cites))
status, refuse = call(AGENT + "/agent/support",
                      {"text": "帮我订一张去上海的机票"}, timeout=120)
check("超范围问题拒答而不是猜",
      status == 200 and refuse.get("status") != "answered",
      refuse.get("status", ""))
started = time.time()
status, parsed = call(AGENT + "/agent/parse",
                      {"text": "中山区两室一厅，三千以内"}, timeout=120)
slots = (parsed or {}).get("data") or (parsed or {}).get("filters") or {}
check("找房解析出条件", status == 200 and bool(slots),
      "%dms · %s" % (int((time.time() - started) * 1000),
                     json.dumps(slots, ensure_ascii=False)[:90]))

print()
print("六、限流，以及本次修掉的那个洞")
codes = []
for _ in range(10):
    code, _body = call(API + "/api/auth/login",
                       {"username": user, "password": "wrong-on-purpose"},
                       headers={"X-Forwarded-For": "9.9.9.9"})
    codes.append(code)
check("连错密码会被限流", 429 in codes, "前十次：%s" % codes)
code, _ = call(API + "/api/auth/login",
               {"username": user, "password": "wrong-on-purpose"},
               headers={"X-Forwarded-For": "8.8.8.8"})
check("换一个 X-Forwarded-For 不能重置计数", code == 429,
      "HTTP %d（改之前这里会是 401，等于限流被绕过）" % code)

print()
print("=" * 78)
# 头注释里写了，但跑完紧接着去网页上点登录的人不会回去看头注释——
# 2026-10-21 就是这样：冒烟全绿，紧接着演示账号登录 429，像是坏了
print("注意：这台机器的登录次数已被用满，约 5 分钟内登录都会返回 429。")
print("通过 %d 条，失败 %d 条" % (len(passed), len(failed)))
for name in failed:
    print("  ✗ %s" % name)
sys.exit(1 if failed else 0)
