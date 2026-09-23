# -*- coding: utf-8 -*-
"""下带 MTP 头的那份权重。断了能接着下，不用从头再来。"""
import os
import sys
import time
import urllib.request

URL = ("https://www.modelscope.cn/api/v1/models/ProCreations/"
       "Ternary-Bonsai-2-27B-MTP/repo?Revision=master"
       "&FilePath=Ternary-Bonsai-2-27B-PQ2_0-MTP-Q8_0.gguf")
DEST = "D:/models/bonsai/Ternary-Bonsai-2-27B-PQ2_0-MTP-Q8_0.gguf"
PART = DEST + ".part"
EXPECT = 7657  # MB，用来判断下完没有

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass


def size(path):
    return os.path.getsize(path) if os.path.exists(path) else 0


def main():
    if size(DEST) > EXPECT * 1000 * 1000:
        print("已经有了：%.3f GB" % (size(DEST) / 1e9))
        return 0

    started = time.time()
    for attempt in range(1, 21):
        done = size(PART)
        req = urllib.request.Request(URL, headers={
            "User-Agent": "Mozilla/5.0",
            **({"Range": "bytes=%d-" % done} if done else {})})
        try:
            resp = urllib.request.urlopen(req, timeout=120)
            total = done + int(resp.headers.get("Content-Length") or 0)
            with open(PART, "ab") as out:
                last = time.time()
                while True:
                    chunk = resp.read(4 << 20)
                    if not chunk:
                        break
                    out.write(chunk)
                    done += len(chunk)
                    if time.time() - last > 20:
                        last = time.time()
                        mb = done / 1e6
                        speed = mb / max(time.time() - started, 1)
                        print("  %.0f / %.0f MB  (%.1f MB/s)"
                              % (mb, total / 1e6, speed), flush=True)
        except Exception as exc:                                 # noqa: BLE001
            print("第 %d 次中断（已下 %.0f MB）：%s"
                  % (attempt, size(PART) / 1e6, str(exc)[:90]), flush=True)
            time.sleep(min(5 * attempt, 30))
            continue

        if size(PART) > EXPECT * 1000 * 1000:
            os.replace(PART, DEST)
            print("下完：%.3f GB，用时 %.1f 分钟"
                  % (size(DEST) / 1e9, (time.time() - started) / 60))
            return 0
        print("大小不对（%.0f MB），继续接" % (size(PART) / 1e6), flush=True)

    print("重试用尽，只下到 %.0f MB" % (size(PART) / 1e6))
    return 1


if __name__ == "__main__":
    sys.exit(main())
