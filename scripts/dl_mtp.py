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
# 完整文件的确切字节数。**用相等判据，不用「大于某个阈值」**——
# 阈值判据对「少了最后几兆」是盲的，而断点续传恰恰最容易在末尾出问题。
# 这个数从远端拿（见 `remote_size`），下面这个只是取不到时的兜底。
EXPECT_BYTES = 7657489728

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass


def size(path):
    return os.path.getsize(path) if os.path.exists(path) else 0


def remote_size():
    """问远端这个文件到底多少字节。取不到就用写死的兜底值。"""
    try:
        req = urllib.request.Request(URL, headers={
            "Range": "bytes=0-0", "User-Agent": "Mozilla/5.0"})
        rng = urllib.request.urlopen(req, timeout=40).headers.get("Content-Range")
        return int(rng.split("/")[-1])
    except Exception:                                            # noqa: BLE001
        return EXPECT_BYTES


def main():
    want = remote_size()
    if size(DEST) == want:
        print("已经有了：%.3f GB" % (size(DEST) / 1e9))
        return 0

    started = time.time()
    # **有进展就把重试次数清零**，只有「连着失败而且一个字节都没多下来」
    # 才算真的卡住。
    #
    # 第一版写的是 `for attempt in range(1, 21)`——总共只给 20 次机会。
    # 实测这条线路每三百来兆断一次（SSL: DECRYPTION_FAILED_OR_BAD_RECORD_MAC），
    # 于是 7.66 GB 下到 7.28 GB（95%）时次数用尽，**明明一直在前进却放弃了**。
    stuck = 0
    while stuck < 8:
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
            grew = size(PART) > done
            stuck = 0 if grew else stuck + 1
            print("中断（已下 %.0f MB，%s）：%s"
                  % (size(PART) / 1e6,
                     "有进展，重试次数清零" if grew else "没进展 %d/8" % stuck,
                     str(exc)[:90]), flush=True)
            time.sleep(min(5 * (stuck + 1), 30))
            continue

        if size(PART) == want:
            os.replace(PART, DEST)
            print("下完：%.3f GB，用时 %.1f 分钟"
                  % (size(DEST) / 1e9, (time.time() - started) / 60))
            return 0
        stuck = 0 if size(PART) > done else stuck + 1
        print("还差 %.0f MB，继续接" % ((want - size(PART)) / 1e6), flush=True)

    print("连着 8 次没有任何进展，只下到 %.0f MB（再跑一次会接着下）"
          % (size(PART) / 1e6))
    return 1


if __name__ == "__main__":
    sys.exit(main())
