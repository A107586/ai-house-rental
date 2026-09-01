"""下载房源演示图片。

用 Unsplash 的免费图片（Unsplash License 允许免费商用与修改，无需署名，
但我们仍会在页脚注明来源）。图片存进 server/static/houses/，
由后端作为静态资源提供。
"""

import io
import os
import sys
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

OUT = r"C:\Competition1\rental\server\static\houses"
os.makedirs(OUT, exist_ok=True)

# Unsplash 上的公寓／居室实拍，按「客厅、卧室、厨房、卫生间、书房、阳台」搭配
PHOTO_IDS = [
    "1502672260266-1c1ef2d93688",  # 明亮客厅
    "1522708323590-d24dbb6b0267",  # 现代客厅
    "1560448204-e02f11c3d0e2",     # 沙发区
    "1493809842364-78817add7ffb",  # 简约客厅
    "1586023492125-27b2c045efd7",  # 北欧客厅
    "1567767292278-a4f21aa2d36e",  # 卧室
    "1540518614846-7eded433c457",  # 卧室窗景
    "1522771739844-6a9f6d5f14af",  # 双人卧室
    "1556911220-bff31c812dba",     # 厨房
    "1556909212-d5b604d0c90d",     # 开放式厨房
    "1600607687939-ce8a6c25118c",  # 餐厅
    "1584622650111-993a426fbf0a",  # 卫生间
    "1600566753086-00f18fb6b3ea",  # 书房
    "1595526114035-0d45ed16cfbf",  # 阳台
    "1600585154340-be6161a56a0c",  # 户型全景
    "1568605114967-8130f3a36994",  # 楼栋外观
]

BASE = "https://images.unsplash.com/photo-%s?w=900&q=72&auto=format&fit=crop"

ok = 0
for index, photo_id in enumerate(PHOTO_IDS, start=1):
    path = os.path.join(OUT, "h%02d.jpg" % index)
    if os.path.exists(path) and os.path.getsize(path) > 10000:
        print("  跳过 h%02d.jpg（已存在）" % index)
        ok += 1
        continue
    try:
        req = urllib.request.Request(BASE % photo_id,
                                     headers={"User-Agent": "Mozilla/5.0"})
        data = urllib.request.urlopen(req, timeout=60).read()
        if len(data) < 10000:
            print("  跳过 h%02d.jpg（响应过小，可能不是图片）" % index)
            continue
        with open(path, "wb") as fp:
            fp.write(data)
        print("  下载 h%02d.jpg  %d KB" % (index, len(data) // 1024))
        ok += 1
    except Exception as exc:
        print("  失败 h%02d.jpg  %s" % (index, type(exc).__name__))

print("\n共 %d 张，存放于 %s" % (ok, OUT))
