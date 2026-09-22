# -*- coding: utf-8 -*-
"""看报修照片，说出照片里坏的是什么。

**这一层只把图片翻译成一句中文，不做任何判断。**
翻译出来的那句话仍旧交给 `classify_repair` 与 `analyze` 去定故障类型、
紧急程度和派单工种——那套规则已经有 50 个用例盯着（AI-04），
让识图另起一套判断逻辑，等于把一个测过的东西换成一个没测过的。

为什么值得做：租客拍一张照片，比让他用文字描述「厨房水槽下面
那根管子的接口在渗水」容易得多，而**描述得越糊，分派就越容易错**。
报修单上写「厨房有点问题」，规则引擎只能落到「其他」。

三条护栏，每一条都对应一种真实的坏法：

1. **照片说不出问题时必须说「没有」。** 用户拍一张干净的厨房，
   系统派个水暖工上门，这比没有这个功能糟糕得多。所以提示词里
   把「摆设凌乱、东西多、家具旧不算损坏」写死，并且要求第一行
   只回「有」或「没有」——**由这一行决定后面那句话作不作数**，
   而不是去猜描述里有没有出现「漏」字。

2. **识图失败不影响报修。** 拿不到描述就当没这张照片，
   用户原来怎么报修还怎么报修。`LLMUnavailable` 在这里被吞掉，
   和项目里其它可选通道（嵌入、融合）的处理一致。

3. **描述要进工单正文，让人看得见。** 照片经过模型变成文字，
   这中间可能出错；把那句话原样写进工单，师傅上门前能自己判断，
   用户也能当场说「不是这个」。绝不拿一个用户没见过的句子去派单。
"""

import base64
import re
import urllib.request

from app.llm.base import LLMUnavailable

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

_SYSTEM = "你是房屋租赁平台的报修审核员。只看照片，不猜照片外的事。"

_PROMPT = """这是租客提交的报修照片。判断房屋或屋内设施**本身**
有没有损坏、故障需要维修：漏水、渗水、裂缝、脱落、破损、发霉、
堵塞、电线外露、器具不工作等。

**不算损坏的情况**：东西摆得乱、物品多、家具旧、装修风格不好看、
光线暗、墙面有正常使用痕迹。这些不需要维修。

按下面两行输出，不要写别的：
第一行：只写「有」或「没有」
第二行：一句不超过 40 字的中文，说清楚**哪个位置、什么东西、
什么现象**；第一行写「没有」时，这一行写照片拍的是哪里。"""


def describe_fault(images, provider, deadline=None):
    """看图，返回 ``(有没有损坏, 一句描述)``。

    识图不可用、模型答非所问时一律返回 ``(False, "")``——
    调用方把它当成「这张照片没提供信息」，照原路走文字报修。
    """
    urls = [u for u in (images or []) if u][:MAX_IMAGES]
    if not urls or provider is None:
        return False, ""

    try:
        text = provider.complete_vision(
            _SYSTEM, _PROMPT, urls, max_tokens=120, deadline=deadline)
    except LLMUnavailable:
        return False, ""
    except Exception:                                            # noqa: BLE001
        # 识图是可选增强。任何没预料到的错误都不该让报修提交不上去。
        return False, ""

    return parse(text)


def parse(text):
    """把模型那两行拆成 ``(有没有损坏, 描述)``。

    单独成函数是为了能脱离模型测边界：模型只回一行、回反了顺序、
    把「有」写成「有损坏」、整段裹在 Markdown 里，都在这里兜住。
    """
    lines = [ln.strip().strip("*# 　") for ln in (text or "").splitlines()]
    lines = [ln for ln in lines if ln]
    if not lines:
        return False, ""

    head = lines[0]
    body = " ".join(lines[1:]).strip()

    # **只认开头那个字。** 「有」和「没有」都含「有」，用 in 判断
    # 会把「没有」判成「有」——这正是会让干净厨房派出水暖工的那一步。
    if head.startswith("没有") or head.startswith("无"):
        return False, body
    if head.startswith("有"):
        # 「有」单独成行时描述在第二行；模型偶尔写成「有：xxx」
        detail = body or head.lstrip("有").lstrip("：: ").strip()
        return True, detail
    # 没按格式回，就当这张照片没说清楚。
    return False, ""
