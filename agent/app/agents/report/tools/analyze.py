"""工具：把平台统计数据分析成一份可读的经营报告（AI-05 的核心）。

需求 AI-05 有一条硬性验收标准：**报告中所有数字必须可追溯到数据库记录，
不允许出现模型编造的数据。** 所以数字一律从 `/api/stats` 直接取，
大模型只允许润色措辞，不允许参与计算。

报告分四块：整体概况、收租情况、房源分布、待办提醒。
每块都给出「结论 + 依据数字 + 建议」，而不是只把数字念一遍。
"""

# 出租率的判断线。低于 40% 说明空置严重，高于 75% 说明可以考虑提价。
OCCUPANCY_LOW = 40
OCCUPANCY_HIGH = 75


def _pct(part, whole):
    return round(part / whole * 100, 1) if whole else 0.0


def analyze(stats):
    """生成结构化的分析结论。

    返回 ``{"sections": [...], "highlights": [...], "alerts": [...]}``，
    每个 section 含标题、正文与依据数字。
    """
    if not stats:
        return {"sections": [], "highlights": [], "alerts": []}

    sections = []
    highlights = []
    alerts = []

    total = stats.get("house_total") or 0
    rented = stats.get("house_rented") or 0
    available = stats.get("house_available") or 0
    occupancy = stats.get("occupancy_rate") or _pct(rented, total)
    avg_rent = stats.get("avg_rent") or 0

    # ---------------- 整体概况 ----------------
    if occupancy < OCCUPANCY_LOW:
        verdict = "出租率偏低，空置房源较多"
        advice = "建议核查空置房源的定价与图片描述，必要时下调租金或补充房源照片。"
    elif occupancy > OCCUPANCY_HIGH:
        verdict = "出租率处于高位，房源较为紧俏"
        advice = "可考虑对新上架房源适度上调定价，或补充房源供给。"
    else:
        verdict = "出租率处于正常区间"
        advice = "保持现状，关注空置时间较长的个别房源。"

    sections.append({
        "title": "整体概况",
        "text": "平台共有房源 %d 套，其中在租 %d 套、可租 %d 套，出租率 %.1f%%，"
                "平均租金 %d 元。%s。%s"
                % (total, rented, available, occupancy, round(avg_rent),
                   verdict, advice),
        "figures": {"房源总数": total, "在租": rented, "可租": available,
                    "出租率": "%.1f%%" % occupancy, "平均租金": round(avg_rent)},
    })
    highlights.append("出租率 %.1f%%" % occupancy)

    # ---------------- 收租情况 ----------------
    received = stats.get("income_received") or 0
    pending = stats.get("income_pending") or 0
    overdue = stats.get("overdue_count") or 0
    monthly = stats.get("monthly_income") or []

    收缴率 = _pct(received, received + pending)
    trend = _trend(monthly)

    text = ("已收租金 %d 元，待收 %d 元，收缴率 %.1f%%。"
            % (round(received), round(pending), 收缴率))
    if trend:
        text += trend + " "
    if overdue:
        text += "存在 %d 笔逾期账单，建议优先催收。" % overdue
        alerts.append("%d 笔账单已逾期" % overdue)
    else:
        text += "当前没有逾期账单。"

    sections.append({
        "title": "收租情况",
        "text": text,
        "figures": {"已收": round(received), "待收": round(pending),
                    "收缴率": "%.1f%%" % 收缴率, "逾期笔数": overdue},
    })
    highlights.append("已收 %d 元" % round(received))

    # ---------------- 房源分布 ----------------
    by_district = stats.get("by_district") or []
    if by_district:
        top = by_district[0]
        priciest = max(by_district, key=lambda d: d.get("avg_rent") or 0)
        cheapest = min(by_district, key=lambda d: d.get("avg_rent") or 0)
        sections.append({
            "title": "区域分布",
            "text": "房源最集中的是 %s（%d 套），均价最高的是 %s（%d 元），"
                    "均价最低的是 %s（%d 元），差距 %d 元。"
                    "定价时可参考同区均价，偏离过大的房源需要复核。"
                    % (top["district"], top["count"],
                       priciest["district"], round(priciest["avg_rent"]),
                       cheapest["district"], round(cheapest["avg_rent"]),
                       round(priciest["avg_rent"] - cheapest["avg_rent"])),
            "figures": {d["district"]: "%d 套 / 均 %d 元"
                        % (d["count"], round(d["avg_rent"]))
                        for d in by_district[:6]},
        })

    # ---------------- 待办提醒 ----------------
    appointment_pending = stats.get("appointment_pending") or 0
    repair_pending = stats.get("repair_pending") or 0
    repair_urgent = stats.get("repair_urgent") or 0
    contract_active = stats.get("contract_active") or 0

    todos = []
    if appointment_pending:
        todos.append("%d 条看房预约待确认" % appointment_pending)
    if repair_urgent:
        todos.append("%d 条紧急报修待处理" % repair_urgent)
        alerts.append("%d 条紧急报修" % repair_urgent)
    if repair_pending - repair_urgent > 0:
        todos.append("%d 条一般报修待处理" % (repair_pending - repair_urgent))

    sections.append({
        "title": "运营待办",
        "text": ("当前生效合同 %d 份。" % contract_active)
                + ("需要处理：" + "、".join(todos) + "。" if todos
                   else "暂无待处理事项。"),
        "figures": {"生效合同": contract_active,
                    "待确认预约": appointment_pending,
                    "待处理报修": repair_pending,
                    "其中紧急": repair_urgent},
    })

    return {"sections": sections, "highlights": highlights, "alerts": alerts}


def _trend(monthly):
    """月度收入趋势。只有两个月以上才谈得上趋势。"""
    if len(monthly) < 2:
        return ""
    last, prev = monthly[-1], monthly[-2]
    delta = (last.get("amount") or 0) - (prev.get("amount") or 0)
    if abs(delta) < 1:
        return "%s 与上月持平。" % last.get("month", "")
    direction = "增加" if delta > 0 else "减少"
    base = prev.get("amount") or 0
    ratio = (" %.1f%%" % abs(delta / base * 100)) if base else ""
    return "%s 收租较上月%s %d 元%s。" % (last.get("month", ""), direction,
                                         round(abs(delta)), ratio)


def to_text(result):
    """拼成一段完整的报告正文。"""
    if not result["sections"]:
        return "暂无可分析的数据。"
    return "\n\n".join("【%s】%s" % (s["title"], s["text"])
                       for s in result["sections"])
