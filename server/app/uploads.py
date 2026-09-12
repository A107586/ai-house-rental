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

    **只看前缀是不够的。** `/static/uploads/../../../etc/passwd` 也以
    正确的前缀开头，照样能存进相册表。今天它顶多是一张裂图（浏览器会
    先把 `..` 规范化掉，StaticFiles 也只认自己那棵目录树，实测三种写法
    全是 404），但一条**指向 uploads 目录之外**的地址留在库里，
    等哪天图片改由别的东西来发（缩略图服务、CDN 回源、代理），
    它就是现成的入口。`remove()` 那边早就单独防了一次，这里补齐。
    """
    if not url or not url.startswith("/static/%s/" % UPLOAD_DIRNAME):
        return False
    # 反斜杠是 Windows 的分隔符，`%2e%2e` 是编码过的 `..`，
    # 控制字符能骗过一些按行处理的中间件
    lowered = url.lower()
    if ".." in url or "\\" in url or "%2e" in lowered or "%5c" in lowered:
        return False
    return all(ch >= " " and ch != chr(0x7f) for ch in url)


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



# ---------------------------------------------------------------- 图片元数据

# PNG 文件头。用常量而不是字面量写在判断里，免得转义出错时不易察觉
_PNG_MAGIC = b"\x89PNG\r\n\x1a\x1a"[:4] + b"\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8"

# 带尺寸的 JPEG 段标记（SOF0–SOF15，跳过 0xC4/0xC8/0xCC 这类非尺寸段）
_JPEG_SOF = frozenset([0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                       0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF])


def measure(data):
    """从图片字节里读出宽高，并算一个内容哈希（K-12）。

    **只读文件头，不解码整张图。** 引 Pillow 就为拿两个数字不划算，
    而这台机器 pip 也装不上（见 `app/migrations.py` 里同样的处境）。
    PNG / JPEG / GIF 的尺寸都写在头几十个字节里，自己读就够了。

    哈希用的是**整份文件内容**：盗图和「同一张图发十套房」是虚假房源
    最常见的形态，有哈希才查得出来；没有它，举报了也只能靠人眼比对。

    读不出尺寸时返回 0，**不抛异常**——尺寸是锦上添花，
    不该让一张读不懂的图把整个上传流程打断。
    """
    import hashlib
    import struct

    result = {"width": 0, "height": 0, "file_size": len(data),
              "hash": hashlib.sha256(data).hexdigest()}

    try:
        if data[:8] == _PNG_MAGIC and data[12:16] == b"IHDR":
            result["width"], result["height"] = struct.unpack(">II", data[16:24])
        elif data[:3] == b"GIF":
            result["width"], result["height"] = struct.unpack("<HH", data[6:10])
        elif data[:2] == _JPEG_MAGIC:
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in _JPEG_SOF:
                    height, width = struct.unpack(">HH", data[i + 5:i + 9])
                    result["width"], result["height"] = width, height
                    break
                if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                length = struct.unpack(">H", data[i + 2:i + 4])[0]
                if length <= 0:
                    break
                i += 2 + length
    except (struct.error, IndexError):
        pass

    result["width"] = int(result["width"])
    result["height"] = int(result["height"])
    return result
