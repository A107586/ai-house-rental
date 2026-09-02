"""逐条验证《答辩演示脚本》里的每一步（docs/答辩演示脚本.md）。

演示脚本里的步骤如果有一步走不通，比没有脚本更糟——台上照着做会直接
卡住。改完代码跑一次，就知道演示流程有没有被改坏。

用法（三个服务都要先起来）：

    python scripts/verify_demo.py

注意：会**改动演示数据**（确认一份合同、缴一笔费、发一套测试房源）。
演示当天跑完请重新生成一次：cd server && python seed.py
"""
import json
import urllib.error
import urllib.request

API = "http://127.0.0.1:8080"
AG = "http://127.0.0.1:8000"
ok_count = 0
fail = []


def call(url, data=None, token=None, method=None):
    req = urllib.request.Request(
        url, data=json.dumps(data).encode() if data is not None else None,
        method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def check(label, cond, detail=""):
    global ok_count
    if cond:
        ok_count += 1
        print("  OK   %s %s" % (label, detail))
    else:
        fail.append(label)
        print("  FAIL %s %s" % (label, detail))


def login(u, p):
    return call(API + "/api/auth/login", {"username": u, "password": p})[1]["token"]


print("第 1 段 · 自然语言找房")
_, r = call(AG + "/agent/search",
            {"text": "中山区两室一厅，租金三千以内，最好带电梯", "limit": 12})
check("条件解析", r["filters"].get("suozaidiqu") == "中山区"
      and r["filters"].get("zujia_max") == 3000, str(r["filters"]))
check("偏好识别", "电梯" in r.get("features", []), str(r.get("features")))
check("标注规则解析", r.get("source") == "rules", r.get("source"))
check("电梯房排前", "电梯" in (r["items"][0].get("tags") or []),
      str((r["items"][0].get("tags") or [])[:3]) if r["items"] else "无结果")

_, r = call(AG + "/agent/search", {"text": "有没有金州区2000到3000的一居室"})
check("区间识别", r["filters"].get("zujia_min") == 2000
      and r["filters"].get("zujia_max") == 3000, str(r["filters"]))

print()
print("第 2 段 · 多轮对话记忆")
tok = login("tenant1", "test123")
sid = "sess-demo-verify-0001"
call(API + "/api/chat-messages?session_id=" + sid, token=tok, method="DELETE")
turns = ["中山区两室一厅", "三千以内的", "再便宜点", "换成沙河口"]
last = {}
for t in turns:
    _, last = call(AG + "/agent/chat",
                   {"text": t, "session_id": sid, "token": tok})
    print("    「%s」→ 记住：%s" % (t, last.get("context_text") or "—"))
check("累积生效", "两室一厅" in (last.get("context_text") or ""))
check("区域被替换", "沙河口" in (last.get("context_text") or ""))
check("等比降价到 2400", "2400" in (last.get("context_text") or ""),
      last.get("context_text", ""))

_, cold = call(AG + "/agent/chat", {"text": "再便宜点", "session_id": "",
                                    "token": "", "history": []})
check("无上文时不猜", "预算" in cold.get("message", "")
      or cold.get("intent") == "unknown", cold.get("message", "")[:40])

print()
print("第 3 段 · 交易链")
_, mine = call(API + "/api/contracts/mine", token=tok)
pending = [c for c in mine["list"] if c["status"] == "pending"]
check("存在待确认合同", len(pending) == 1,
      pending[0]["contract_no"] if pending else "没有！演示第一步就走不通")

if pending:
    no = pending[0]["contract_no"]
    code, _ = call(API + "/api/contracts/%s/activate" % no, {}, tok)
    check("租客可确认签约", code == 200, "HTTP %d" % code)
    _, bills = call(API + "/api/bills/mine", token=tok)
    mine_bills = [b for b in bills.get("list", [])
                  if b.get("contract_no") == no]
    check("一次排满整期账单", len(mine_bills) == 13, "%d 条" % len(mine_bills))
    unpaid = [b for b in mine_bills if b["status"] == "unpaid"]
    if unpaid:
        code, _ = call(API + "/api/bills/%s/pay" % unpaid[0]["bill_no"],
                       {}, tok)
        check("可缴费", code == 200, "HTTP %d" % code)

print()
print("第 4 段 · 管理端")
admin = login("admin", "admin123")
landlord = login("landlord1", "test123")

_, rep = call(AG + "/agent/report", {})
check("经营分析可生成", rep.get("status") == "ok",
      "来源 %s，%d 段" % (rep.get("source"), len(rep.get("sections", []))))
check("报告数字带依据", all(s.get("figures") for s in rep.get("sections", [])))

code, r = call(API + "/api/houses", {
    "title": "中山区两室一厅", "district": "中山区", "rent_price": 3000,
    "area": 80, "layout": "两室一厅",
    "description": "看房直接加微信 zhangsan888"}, landlord)
check("留微信被拦", code == 400, r.get("detail", "")[:36])

code, r = call(API + "/api/houses", {
    "title": "全网最低价 中山区两室一厅", "district": "中山区",
    "rent_price": 3000, "area": 80, "layout": "两室一厅",
    "description": "采光好"}, landlord)
check("夸大宣传放行", code == 200, r.get("house", {}).get("house_no", ""))
_, flags = call(API + "/api/admin/flags?status=pending", token=admin)
check("进入复核队列", flags.get("pending", 0) >= 1,
      "%d 条待复核" % flags.get("pending", 0))

code, _ = call(API + "/api/admin/config",
               {"items": {"viewing_minutes": "120"}}, admin, method="PATCH")
check("配置可改", code == 200, "HTTP %d" % code)
_, cfg = call(API + "/api/admin/config", token=admin)
row = next(c for c in cfg["list"] if c["key"] == "viewing_minutes")
check("配置已生效", row["value"] == "120", "看房时段 = %s 分钟" % row["value"])
call(API + "/api/admin/config", {"items": {"viewing_minutes": "60"}},
     admin, method="PATCH")

print()
print("=" * 46)
print("通过 %d 项，失败 %d 项" % (ok_count, len(fail)))
if fail:
    print("失败的步骤：" + "、".join(fail))
