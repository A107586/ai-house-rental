# -*- coding: utf-8 -*-
"""开场白：先看这个人有什么没办完，再决定说什么。

## 为什么要有这一层

原先的开场白是一段固定的自我介绍——一百来字，讲自己能干什么，
然后把「该问点什么」这个问题原样丢回给用户。

购物软件的客服不是这么做的。淘宝店小蜜 5.0 把这件事总结成
「被动服务升级成主动服务、标准化服务升级成个性化服务」：点开客服，
第一屏是**你自己的事**——一笔待付款、一个在派送的包裹、一张能领的券，
每条后面跟着一个能点的按钮。用户不需要先学会怎么描述需求。

租房场景里对应的东西更明确：**待缴的账单、等房东确认的预约、
处理中的工单、还没评价的租约、没读的消息。** 这些都是他打开助手时
心里真正装着的事。

## 三条约束

1. **不调大模型。** 开场白要在面板弹出的一瞬间就出来，而这个项目接的
   中转站单次调用要三四秒。这里的内容完全可以由数据算出来，
   没有任何需要「生成」的部分——用大模型做这件事是纯粹的浪费。
2. **取不到就退回通用问候。** 未登录、后端抖动、某个接口 404，
   一律降级成原来那句静态开场白，绝不因为开场白挂掉而打不开面板。
3. **只报数，不替用户做决定。** 每条状态配一个**跳转到那件事**的按钮，
   但不预先生成任何确认卡片——两阶段边界在这里同样成立。
"""

from datetime import datetime

from app.backend import BackendError

# 未登录、或者一件待办都没有时说的话。保留原来那段自我介绍的信息量，
# 但把「你可以问我什么」变成下面几个能点的例子。
FALLBACK = ("你好，我是租房助手。找房可以一句一句地补充条件，我都记得住；"
            "平台规则直接问我就行——我只按知识库里写的回答，"
            "查不到会直说，不会瞎编。也可以让我帮你办事：交房租、约看房、报修，"
            "我会先把要做的事列给你看，你点确认我才动手。")

GUEST_CHIPS = ["中山区两室一厅", "三千以内的", "押金怎么退", "水电费怎么算"]


def _rows(data):
    if isinstance(data, dict):
        return data.get("list") or []
    return data if isinstance(data, list) else []


def _safe(backend, path, token, params=None):
    """取一类数据。**任何异常都当成「没有」**——开场白不值得为它报错。"""
    try:
        if params:
            return backend.get(path, params=params, token=token)
        return backend.get(path, token=token)
    except (BackendError, Exception):       # noqa: B014 - 就是要全吞
        return {}


from app.agents.support.tools.money import money_text as _money


def _upcoming(rows):
    """还没到点的。时间读不出来的照留"""
    now = datetime.now()
    kept = []
    for row in rows:
        raw = str(row.get("viewing_time") or "")[:16].replace("T", " ")
        try:
            if datetime.strptime(raw, "%Y-%m-%d %H:%M") <= now:
                continue
        except ValueError:
            pass
        kept.append(row)
    return kept


def collect(backend, token):
    """把这个人手头没办完的事收集起来。返回若干条 ``{text, chip, action}``。

    顺序就是重要性顺序：**要花钱的排最前，等别人的排中间，
    自己有空再弄的排最后。** 一屏只放得下三四条，排错了等于没做。
    """
    items = []

    # 只算我要交的，房东账号的应收不是「待缴」
    unpaid = [b for b in _rows(_safe(backend, "/api/bills/mine", token,
                                     {"side": "tenant"}))
              if b.get("status") == "unpaid"]
    if unpaid:
        total = sum(float(b.get("amount") or 0) for b in unpaid)
        first = sorted(unpaid, key=lambda b: b.get("due_date") or "")[0]
        overdue = [b for b in unpaid if b.get("overdue")]
        text = "%d 笔待缴账单，合计 %s 元" % (len(unpaid), _money(total))
        if overdue:
            text += "（其中 %d 笔已逾期）" % len(overdue)
        items.append({
            "text": text,
            "chip": "交最近一笔房租",
            "detail": "最近一笔 %s 到期" % (first.get("due_date") or "待定"),
        })

    # 只算**我约的**、还没到点的。不带 side 时房东账号拿到的是别人约他房子的预约，
    # 却被说成「在等房东确认」；过了点的待确认也确认不了了（2026-10-23）
    waiting = _upcoming([a for a in _rows(_safe(backend, "/api/appointments/mine", token,
                                                {"side": "tenant"}))
                         if a.get("status") in ("pending", "待审核")])
    if waiting:
        items.append({
            "text": "%d 条看房预约在等房东确认" % len(waiting),
            "chip": "查我的预约进度",
            "detail": (waiting[0].get("viewing_time") or "")[:16],
        })

    # 同上：「我的报修」是我报的那几单，不是别人报到我房子上的
    open_repairs = [r for r in _rows(_safe(backend, "/api/repairs", token,
                                           {"side": "tenant"}))
                    if r.get("status") not in ("done", "已完成", "closed")]
    if open_repairs:
        items.append({
            "text": "%d 条报修还在处理中" % len(open_repairs),
            "chip": "我的报修到哪一步了",
            "detail": (open_repairs[0].get("content") or "")[:16],
        })

    unread = _safe(backend, "/api/messages", token)
    count = unread.get("unread", 0) if isinstance(unread, dict) else 0
    if count:
        items.append({
            "text": "%d 条未读消息" % count,
            "chip": "我有未读消息吗",
            "detail": "",
        })

    pending_reviews = _rows(_safe(backend, "/api/reviews/pending", token))
    if pending_reviews:
        items.append({
            "text": "%d 份租约还没评价" % len(pending_reviews),
            "chip": "帮我评价",
            "detail": "",
        })

    return items


def compose(items, name=""):
    """把待办列表写成一句话。"""
    who = "你" if not name else name
    if not items:
        return ""
    heads = [item["text"] for item in items[:3]]
    if len(heads) == 1:
        body = heads[0]
    else:
        body = "、".join(heads[:-1]) + "，还有 " + heads[-1]
    return "%s现在有 %s。要我帮你办哪件？" % (who + "，", body) if name else \
        "你现在有 %s。要我帮你办哪件？" % body


def build(backend, token, house=None, name=""):
    """生成开场白。返回 ``{"message", "chips", "personalised"}``。

    `house` 是用户此刻正在看的那套房。**从房源详情页点进来的会话，
    第一句就该是关于这套房的**——购物软件从商品页进客服也是这么做的，
    用户不必再把房源编号打一遍。
    """
    if house:
        title = (house.get("title") or house.get("community") or "").strip()
        no = house.get("house_no") or ""
        price = house.get("rent_price")
        head = "关于 %s%s" % (no, ("（%s）" % title) if title else "")
        if price:
            head += "，租金 %s 元/月" % _money(price)
        return {
            "message": head + "。想问什么？这套房的情况我可以直接查，"
                              "也可以帮你约看房或者收藏它。",
            "chips": ["这套能养宠物吗", "约看房", "收藏这套", "周边有什么"],
            "personalised": True,
            "house_no": no,
        }

    if not token:
        return {"message": FALLBACK, "chips": list(GUEST_CHIPS),
                "personalised": False}

    items = collect(backend, token)
    if not items:
        return {"message": FALLBACK, "chips": list(GUEST_CHIPS),
                "personalised": False}

    chips = [item["chip"] for item in items[:3]]
    # 待办之外留一个通用出口，免得用户想问别的却只看见自己的账单
    chips.append("押金怎么退")
    return {"message": compose(items, name), "chips": chips,
            "personalised": True, "items": items}
