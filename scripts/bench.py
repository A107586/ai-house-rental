"""并发性能实测（对应非功能需求 N-01 / N-02）。

需求写的是「传统 CRUD 接口响应时间 < 1s（100 并发）」。这条指标如果
只写在文档里而从没测过，答辩时被追问一句「你怎么知道」就答不上来。

用法（三个服务先起来）：

    python scripts/bench.py

用标准库的线程池发压，不引第三方压测工具——组员拉下代码就能跑，
不用再装东西。线程池发压的绝对数值会比专业工具略保守，
但用来验证「是否满足 <1s」这个量级的判断是够的。
"""

import json
import urllib.parse
import statistics
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

API = "http://127.0.0.1:8080"
AGENT = "http://127.0.0.1:8000"

CONCURRENCY = 100


def call(url, payload=None, token=None):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode() if payload is not None else None)
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
            return time.perf_counter() - started, r.status
    except urllib.error.HTTPError as e:
        e.read()
        return time.perf_counter() - started, e.code
    except Exception:
        return time.perf_counter() - started, 0


def bench(label, url, payload=None, token=None, n=CONCURRENCY):
    """并发发 n 个请求，返回耗时统计。"""
    with ThreadPoolExecutor(max_workers=n) as pool:
        futures = [pool.submit(call, url, payload, token) for _ in range(n)]
        results = [f.result() for f in futures]

    times = sorted(t for t, _ in results)
    codes = {}
    for _, c in results:
        codes[c] = codes.get(c, 0) + 1

    p50 = statistics.median(times)
    p95 = times[int(len(times) * 0.95) - 1]
    worst = times[-1]
    ok = all(c == 200 for _, c in results) and worst < 1.0

    print("  %-34s p50 %6.0fms  p95 %6.0fms  最慢 %6.0fms  %s  %s"
          % (label, p50 * 1000, p95 * 1000, worst * 1000,
             "达标" if ok else "未达标",
             "" if set(codes) == {200} else "状态码 %s" % codes))
    return ok


def main():
    try:
        _, code = call(API + "/api/stats")
        if code != 200:
            raise RuntimeError
    except Exception:
        print("业务后端（8080）没起来，先启动服务再跑。")
        return 1

    _, body = call(API + "/api/auth/login",
                   {"username": "tenant1", "password": "test123"})
    token = None
    try:
        with urllib.request.urlopen(urllib.request.Request(
                API + "/api/auth/login",
                data=json.dumps({"username": "tenant1",
                                 "password": "test123"}).encode(),
                headers={"Content-Type": "application/json"}),
                timeout=10) as r:
            token = json.loads(r.read().decode())["token"]
    except Exception:
        pass

    print("N-01 传统 CRUD 接口 · %d 并发 · 判定线 1000ms" % CONCURRENCY)
    print()
    passed = []

    # 预热一次，避免把首次导入与连接建立的开销算进去
    bench("（预热，不计入）", API + "/api/houses?limit=12", n=20)
    print()

    passed.append(bench("GET /api/houses（列表 12 条）",
                        API + "/api/houses?limit=12"))
    # 中文参数必须编码，否则 urllib 会在 putrequest 阶段就抛 UnicodeEncodeError，
    # 表现为一片状态码 0，看着像服务端挂了
    filtered = "/api/houses?" + urllib.parse.urlencode({
        "district": "中山区", "layout": "两室一厅",
        "sort": "rent_price", "limit": 12})
    passed.append(bench("GET /api/houses（筛选 + 排序）", API + filtered))
    passed.append(bench("GET /api/houses/DL0001（详情）",
                        API + "/api/houses/DL0001"))
    passed.append(bench("GET /api/stats（全量聚合）", API + "/api/stats"))
    passed.append(bench("GET /api/notices", API + "/api/notices"))
    if token:
        passed.append(bench("GET /api/favorites（需鉴权）",
                            API + "/api/favorites", token=token))

    print()
    print("N-02 智能体接口 · 判定线：单次请求 < 3s")
    print()
    print("  注意：N-02 说的是「首字返回 < 3s」，指的是**单个用户的感知")
    print("  延迟**，不是并发吞吐。所以这里测单次往返，并另附一个")
    print("  20 并发的参考值——那不是需求指标，只是想知道扛不扛得住。")
    print()
    try:
        _, code = call(AGENT + "/health")
        if code != 200:
            raise RuntimeError
    except Exception:
        print("  智能体（8000）没起来，跳过。")
        print()
        print("=" * 60)
        if all(passed):
            print("N-01 达标：全部接口在 %d 并发下最慢一次仍 < 1s。" % CONCURRENCY)
            return 0
        print("N-01 未达标，见上方标记。")
        return 1

    payload = {"text": "中山区两室一厅，租金三千以内", "limit": 12}
    singles = []
    for _ in range(10):
        t, code = call(AGENT + "/agent/search", payload)
        if code == 200:
            singles.append(t)
    if singles:
        singles.sort()
        agent_ok = singles[-1] < 3.0
        print("  %-34s 中位 %6.0fms  最慢 %6.0fms  %s"
              % ("POST /agent/search 单次 ×10",
                 statistics.median(singles) * 1000, singles[-1] * 1000,
                 "达标" if agent_ok else "未达标"))
        passed.append(agent_ok)

    bench("（参考）同一接口 20 并发", AGENT + "/agent/search", payload, n=20)

    print()
    print("=" * 60)
    if all(passed):
        print("N-01 / N-02 均达标。")
        return 0
    print("有指标未达标，见上方标记。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
