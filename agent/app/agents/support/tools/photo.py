"""用户在助手对话里随手发的照片：看着照片回答他。

对话里的照片可以是任何东西——坏了的水龙头、一张账单、合同的一页、
别的平台的房源截图。**路由不看照片**：用户那句话照原样走报修、找房、客服，
那几十条路径一行没改。只有路由没有办成一件具体的事（没出确认卡片、没追问、
没列房源、没查到他的数据）时，才由这里看着照片作答；路由答出来的那段话
作为参考一并给模型——知识库里的条款和照片里的数字，经常得放在一起才答得上。

几条和报修识图（`repair/tools/vision.py`）一样的规矩：

1. **只说照片里看得见的。** 看不清就直说看不清，不补、不猜。
2. **识图失败不影响对话。** 拿不到回答就返回空串，调用方保留路由原本的回答。
3. **不替用户办事。** 看出漏水只提醒「可以说『帮我报修』」，
   报修仍然走原来那条要用户确认的路。
"""

from app.llm.base import LLMUnavailable

# 给模型看几张。对话里一次发太多，后面的也看不过来
MAX_IMAGES = 3
MAX_CHARS = 150

_SYSTEM = ("你是房屋租赁平台的智能助手，用户在对话里发来了照片。"
           "只根据照片里看得见的内容和给出的参考信息回答，不编造。")

_PROMPT = """用户说：「%s」
%s
用中文回答：先说照片里和他的话相关的内容，再回答他。
- 看得出损坏或故障（漏水、渗水、发霉、裂缝、脱落、破损、电线外露、设备坏了）：
  说清楚是什么、在哪个位置，并提醒他可以直接说「帮我报修」
- 账单、合同、票据、聊天记录、网页截图：先读出关键文字和数字，再回答
- 看不清，或照片和他的话对不上，就直说
不超过 %d 个字，不要用 Markdown 标题和列表符号。"""

_REFERENCE = "平台助手不看照片时给出的回答（可能与照片无关，无关就忽略）：「%s」\n"


def answer(images, provider, said="", reference="", deadline=None):
    """看着照片回答用户这句话；看不了返回空串。

    `images` 必须已经过 `app.images.safe_images`（data URI），这里不再做地址校验。
    """
    urls = [u for u in (images or []) if u][:MAX_IMAGES]
    if not urls or provider is None:
        return ""
    ref = _REFERENCE % reference.strip()[:400] if (reference or "").strip() else ""
    prompt = _PROMPT % ((said or "").strip()[:300] or "（没有说话，只发了照片）", ref, MAX_CHARS)
    try:
        text = provider.complete_vision(_SYSTEM, prompt, urls, max_tokens=400,
                                        deadline=deadline)
    except LLMUnavailable:
        return ""
    except Exception:                                            # noqa: BLE001
        # 看图是可选增强，任何没预料到的错误都不该让这一轮对话失败
        return ""
    return clean(text)


def clean(text):
    """去掉 Markdown 记号和空行；段落之间保留换行。"""
    lines = []
    for raw in (text or "").splitlines():
        line = raw.replace("**", "").strip().lstrip("-*#>•· ").strip()
        if line:
            lines.append(line)
    return "\n".join(lines).strip("“”\"'「」 ")
