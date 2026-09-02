"""图片上传的校验与落盘。

文件上传是最容易被做成漏洞的一类接口，这里的每条规则都对应一种真实攻击：

* **按文件头判类型，不看扩展名。** 扩展名是客户端说了算的，把 `shell.php`
  改名成 `a.jpg` 一秒钟的事。真正决定内容是什么的是文件头。
* **文件名一律重新生成，绝不沿用客户端传来的。** 客户端可以把文件名写成
  `../../app/main.py`，拼进路径就直接覆盖源码了。
* **保存时用嗅探出来的扩展名。** 就算前面两条都做了，若还按客户端的扩展名
  落盘，一个内容是 GIF、扩展名是 `.html` 的文件仍能被浏览器当页面渲染，
  变成存储型 XSS。
* **限大小。** 不限的话一个请求就能把磁盘写满。

这些函数刻意不依赖 FastAPI，输入是 bytes，方便脱离 HTTP 直接测。
"""

import os
import uuid
from datetime import datetime

# 单个文件上限。房源照片手机直出通常 2–4 MB，5 MB 够用又不至于被塞爆。
MAX_BYTES = 5 * 1024 * 1024

# 文件头 → 规范扩展名。只认这几种，其余一律拒绝。
MAGIC = (
    (b"\xff\xd8\xff", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
)

UPLOAD_DIRNAME = "uploads"


def sniff(data):
    """按文件头判断图片类型，返回规范扩展名；认不出返回 None。

    WebP 的头是 ``RIFF....WEBP``，中间四字节是文件长度，要跳过再比。
    """
    if not data:
        return None
    for prefix, ext in MAGIC:
        if data.startswith(prefix):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def validate(data):
    """校验上传内容，返回 ``(是否合法, 扩展名或错误说明)``。"""
    if not data:
        return False, "文件是空的"
    if len(data) > MAX_BYTES:
        return False, "图片不能超过 %d MB" % (MAX_BYTES // 1024 // 1024)
    ext = sniff(data)
    if not ext:
        # 这里刻意不告诉调用方「我们支持哪些魔数」，只说支持哪些格式
        return False, "只支持 JPG、PNG、GIF、WebP 格式的图片"
    return True, ext


def build_name(ext):
    """生成落盘文件名。

    完全由服务端生成：客户端传来的文件名可能是 `../../app/main.py`，
    拼进路径就直接覆盖源码了。
    """
    return "%s.%s" % (uuid.uuid4().hex, ext)


def save(static_dir, data, ext):
    """把图片写入 ``static/uploads/年月/``，返回可直接访问的 URL 路径。

    按年月分目录，避免单个目录堆到几万个文件——那样连 `ls` 都会卡住。
    """
    bucket = datetime.now().strftime("%Y%m")
    folder = os.path.join(static_dir, UPLOAD_DIRNAME, bucket)
    os.makedirs(folder, exist_ok=True)

    name = build_name(ext)
    with open(os.path.join(folder, name), "wb") as fp:
        fp.write(data)
    return "/static/%s/%s/%s" % (UPLOAD_DIRNAME, bucket, name)


def is_managed(url):
    """这个 URL 是不是本系统上传出来的。

    删除接口据此判断能不能动磁盘上的文件——种子数据里的演示图片
    不在 uploads 目录下，不能被误删。
    """
    return bool(url) and url.startswith("/static/%s/" % UPLOAD_DIRNAME)


def remove(static_dir, url):
    """删除已上传的文件。不是本系统上传的一律不动。

    路径要再校验一次落在 uploads 目录内：URL 来自数据库，而数据库里的
    值最初来自一次上传——只要有一环出过问题，这里就是最后一道闸。
    """
    if not is_managed(url):
        return False
    root = os.path.abspath(os.path.join(static_dir, UPLOAD_DIRNAME))
    target = os.path.abspath(os.path.join(
        static_dir, url[len("/static/"):].replace("/", os.sep)))
    if not target.startswith(root + os.sep):
        return False
    if os.path.isfile(target):
        os.remove(target)
        return True
    return False
