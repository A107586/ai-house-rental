"""房源推荐智能体（RecommendAgent）：完成 AI-07。

流程：取足迹与收藏 → 提炼偏好画像 → 给候选房源打分 → 给出理由。

刻意不接大模型。推荐要的是**可解释**：每条推荐都要说清「为什么推给你」，
统计规律做得到，大模型反而容易编出听着合理但对不上数据的理由。
大模型在这里的价值不如在解析自然语言时大。
"""

from app.agents.recommend.tools.profile import build_profile, describe
from app.agents.recommend.tools.score import recommend as pick

# 画像置信度低于此值就不硬推，退回最新房源
MIN_CONFIDENCE = 0.35

# 候选池大小。受后端 /api/houses 的 limit 上限约束，不能超过 100。
CANDIDATE_POOL = 100


def run(token=None, limit=6, backend=None):
    """推荐入口。

    未登录、或浏览记录太少时，返回 ``status="fallback"`` 让前端
    退回展示最新房源——硬推一堆不相关的比不推更伤信任。
    """
    from app.backend import client as default_client
    backend = backend or default_client

    try:
        history = backend.list_history(token)
        favorites = backend.list_favorites(token)
    except Exception:
        history, favorites = [], []

    profile = build_profile(history, favorites)

    if profile["confidence"] < MIN_CONFIDENCE:
        return {
            "intent": "recommend",
            "status": "fallback",
            "ok": False,
            "profile": profile,
            "items": [],
            "message": "多看几套房源后，这里会根据你的偏好推荐。"
                       if profile["sample"] else "还没有浏览记录，先看看最新房源吧。",
        }

    try:
        # 后端 /api/houses 的 limit 上限是 100，超了会被参数校验拒绝
        candidates = backend.list_candidates(limit=CANDIDATE_POOL)
    except Exception:
        return {
            "intent": "recommend", "status": "error", "ok": False,
            "profile": profile, "items": [],
            "message": "房源服务暂时不可用，请稍后再试。",
        }

    seen = {h.get("house_no") for h in history} | {f.get("house_no") for f in favorites}
    items = pick(candidates, profile, seen=seen, limit=limit)

    if not items:
        return {
            "intent": "recommend", "status": "empty", "ok": False,
            "profile": profile, "items": [],
            "message": "暂时没有更匹配的新房源，换个条件搜搜看。",
        }

    return {
        "intent": "recommend",
        "status": "ok",
        "ok": True,
        "profile": profile,
        "reason": describe(profile),
        "items": items,
        "message": "为你挑了 %d 套匹配度较高的房源。%s" % (len(items), describe(profile)),
    }


# ---------------------------------------------------------------- 大模型讲理由

_EXPLAIN_SYSTEM = "你是租房平台的推荐助手，只输出 JSON。"
_EXPLAIN_PROMPT = """用户的浏览偏好：{profile}
给他推荐了这几套：
{items}

用一两句口语化的话（不超过 60 个字）告诉他为什么推荐这几套、各自适合什么样的需求。
只能用上面给出的信息，不要编造数字、配套和距离。
输出：{{"text": "……"}}"""


def explain(result, provider):
    """给「为你推荐」写一句人话的推荐理由。

    推荐的打分和挑选仍然是规则算的（可解释、可复现）；原先页面上的理由也是规则
    拼的一串「常看 中山区、甘井子区，两室一厅，租金多在 850–3950 元」。
    用户反馈「不要只靠规则」——这里让大模型基于同一份数据写一句话。
    **只认数据里有的数字**：话里出现的数字在素材里找不到，整句作废，
    退回规则那句。
    """
    import re

    from app.llm.base import LLMUnavailable

    if not result.get("ok") or provider is None or not getattr(provider, "available", False):
        return ""
    lines = []
    for i, h in enumerate(result.get("items", [])[:4], 1):
        lines.append("%d. %s，%s，%s 元/月，匹配理由：%s" % (
            i, h.get("title", ""), h.get("district", ""), _num(h.get("rent_price")),
            "；".join(h.get("match_reasons") or []) or "无"))
    material = "%s\n%s" % (result.get("reason", ""), "\n".join(lines))
    try:
        data = provider.complete_json(_EXPLAIN_SYSTEM, _EXPLAIN_PROMPT.format(
            profile=result.get("reason", ""), items="\n".join(lines)))
    except LLMUnavailable:
        return ""
    text = str((data or {}).get("text") or "").replace("**", "").strip() if isinstance(data, dict) else ""
    if not text or len(text) > 90:
        return ""
    # 按整数比，不按子串：「5 分钟」里的 5 是 3950 的一部分，子串比会放过它
    known = set(re.findall(r"\d+", material))
    if any(n not in known for n in re.findall(r"\d+", text)):
        return ""
    return text


def _num(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return str(value or "")
    return str(int(value)) if value == int(value) else str(value)
