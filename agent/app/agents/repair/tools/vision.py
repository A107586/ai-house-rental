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

# `safe_images` / `MAX_IMAGES` 从 `app.images` 引入后原样再导出：
# 「哪些图片地址允许交给模型」是所有识图场景共用的一条规矩，
# 搬到公共模块之后，调用方（`app/main.py`）不必跟着改 import。
from app.images import MAX_IMAGES, safe_images          # noqa: F401
from app.llm.base import LLMUnavailable

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
