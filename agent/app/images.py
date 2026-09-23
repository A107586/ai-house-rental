# -*- coding: utf-8 -*-
"""把前端传来的图片地址，收敛成一组可以交给模型的 data URI。

**单独成模块，是因为这件事和「拿图片干什么」无关。** 报修要看照片、
房源相册要写图片说明，将来还可能有别的；但「哪些地址允许交给模型」
只能有一套答案，而且必须只有一套——分散成两份，迟早有一份忘了补。

这里防的是 SSRF，理由见 `safe_images` 的文档。
"""

import base64
import re
import urllib.request

# 单次最多看几张。报修现场拍三张足够说明问题，再多只是让人等。
MAX_IMAGES = 3

# 单张上限，与服务端 `uploads.MAX_BYTES` 取同一个数。
# 两处不一致的话，会出现「传得上去但看不了」或者反过来的怪事。
MAX_BYTES = 5 * 1024 * 1024

# PNG 的文件头。写成常量而不是在 bytes 字面量里直接写那个字节——
# 源文件是 UTF-8，`b"\x89PNG"` 里的 0x89 不是 ASCII，整份文件会解析不了。
_PNG_MAGIC = bytes([0x89]) + b"PNG"

# 只认这几种图。和服务端 `uploads.MAGIC` 认的是同一批。
_DATA_URI = re.compile(
    r"^data:image/(jpeg|jpg|png|gif|webp);base64,([A-Za-z0-9+/=\s]+)$")

# 本系统自己的静态目录。**只认这两个前缀**，理由见 `safe_images`。
_MANAGED_PREFIXES = ("/static/uploads/", "/static/houses/")


def safe_images(urls, backend_base_url, fetch=None):
    """把前端传来的图片地址收敛成一组可以交给模型的 data URI。

    **不能把用户给的地址原样转发给模型服务。** 识图请求是由模型那一端
    去取图的，等于让用户指定「让服务器去访问哪个地址」——内网地址、
    云厂商的元数据接口、一个会记录每次访问的外部图床，都在射程之内。
    这是 SSRF，跟 C1 里「评价配图只收本系统上传的地址」防的是同一件事。

    所以这里只接受两种输入：

    * `data:image/...;base64,...` —— 图就在请求里，没有任何一方要去取
    * `/static/uploads/…`、`/static/houses/…` —— 本系统自己的路径，
      由**本服务**去固定的后端地址取回来，再变成 data URI 交给模型

    其余一律丢弃：带协议头的、跳目录的、别的路径的。
    丢弃不报错——识图本来就是可选增强，少一张图不该让报修提交不上去。
    """
    fetch = fetch or _fetch
    out = []
    for raw in (urls or []):
        url = (raw or "").strip()
        if not url:
            continue
        if _DATA_URI.match(url):
            if _decoded_size(url) <= MAX_BYTES:
                out.append(url)
        elif url.startswith(_MANAGED_PREFIXES) and ".." not in url:
            got = fetch(backend_base_url.rstrip("/") + url)
            if got:
                out.append(got)
        if len(out) >= MAX_IMAGES:
            break
    return out


def _decoded_size(data_uri):
    """base64 解出来有多大。不真去解，按长度估——解一次 5 MB 只为了量尺寸不值。"""
    body = data_uri.split(",", 1)[-1]
    return len(body) * 3 // 4


def _fetch(url):
    """取回本系统的一张图，变成 data URI。取不到返回空串。"""
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = resp.read(MAX_BYTES + 1)
    except Exception:                                            # noqa: BLE001
        return ""
    if not data or len(data) > MAX_BYTES:
        return ""
    kind = "png" if data.startswith(_PNG_MAGIC) else "jpeg"
    return "data:image/%s;base64,%s" % (kind, base64.b64encode(data).decode())
