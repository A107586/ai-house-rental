# -*- coding: utf-8 -*-
"""办事进度：把一条工单／一条预约走到哪一步了，摆成一条可视的轨迹。

## 为什么单独做这个

京东智能客服最被用户认可的一点，是**在对话页里直接看物流实时状态**——
不是回一句「运输中」，而是把「已揽收 → 已发出 → 派送中 → 已签收」
整条轨迹摆出来，走到哪一步一眼就能看见。

租房里对应的东西是报修工单和看房预约。我们原先回的是一句
「当前状态：待处理」——**信息是对的，但用户看完仍然不知道
「还要等什么」和「下一步该谁动」**，于是他还是要去问一次人。

## 没有改表

`repairs` 只有一个 status 字段，没有逐节点的时间戳；数据库评审已经过了，
封版在即，为一条轨迹改表不合算。所以这里只用**已有字段**推出节点：

    已提交   created_at 一定有
    已派单   assignee 非空
    处理中   status == processing
    已完成   status == done

推不出精确时间的节点就不给时间，只标状态。**宁可少说，不可编。**
真要做到每步都有时间戳，得加一张 repair_status_log 表——那是下一个迭代
该做的事，不是现在。
"""

from datetime import datetime

DONE = "done"          # 已经走过
CURRENT = "current"    # 正卡在这一步
FUTURE = "future"      # 还没到


def _now():
    """当前时间，格式与后端返回的时间串一致，可以直接比大小。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _step(label, state, at="", note=""):
    return {"label": label, "state": state, "at": at, "note": note}


def repair_steps(row):
    """一条报修工单的轨迹。"""
    row = row or {}
    status = row.get("status") or "pending"
    assignee = (row.get("assignee") or "").strip()
    created = (row.get("created_at") or "")[:16]

    finished = status in ("done", "已完成", "closed")
    working = status in ("processing", "处理中")
    assigned = bool(assignee) or working or finished

    return [
        _step("已提交", DONE, created,
              row.get("category") or ""),
        _step("已派单", DONE if assigned else CURRENT, "",
              ("维修师傅：%s" % assignee) if assignee else "等待管理员派单"),
        # 派了单不等于已经开工。status 还是 pending 时，师傅只是接到了单，
        # 这一步该说「在等上门」，不能含糊成一个「维修中」——
        # 用户看轨迹就是想知道**还要等谁**。
        _step("维修中",
              DONE if finished else (CURRENT if assigned else FUTURE), "",
              "" if working or finished else
              ("已派单，等师傅上门" if assigned else "")),
        _step("已完成", DONE if finished else FUTURE, "", ""),
    ]


def booking_steps(row):
    """一条看房预约的轨迹。"""
    row = row or {}
    status = row.get("status") or "pending"
    created = (row.get("created_at") or "")[:16]
    when = (row.get("viewing_time") or "")[:16]

    if status in ("cancelled", "已取消"):
        return [
            _step("已提交", DONE, created),
            _step("已取消", DONE, "", "这条预约已经取消了"),
        ]
    if status in ("rejected", "已拒绝"):
        return [
            _step("已提交", DONE, created),
            _step("房东未通过", DONE, "", row.get("reply") or "可以换个时间再约"),
        ]

    confirmed = status in ("confirmed", "已确认")
    # 房东确认了不等于已经看过房。看房时间还没到就是「等着去看」，
    # 标成已完成会让用户以为这件事结束了——**轨迹说错一步，
    # 比不给轨迹更糟**，他会据此决定今天还要不要出门。
    been = confirmed and bool(when) and when <= _now()
    if confirmed:
        last = _step("按时看房", DONE if been else CURRENT, when,
                     "已经看过了" if been else "记得带身份证件，到时间直接过去")
    else:
        last = _step("按时看房", FUTURE, when, "")

    return [
        _step("已提交", DONE, created),
        _step("等房东确认", DONE if confirmed else CURRENT, "",
              "" if confirmed else "结果会通过站内消息通知你"),
        last,
    ]


# 事务类型 -> 该用哪套轨迹。没列进来的类型不出轨迹卡，
# 硬给每类数据都套一条时间轴只会变成装饰。
BUILDERS = {
    "repair": repair_steps,
    "booking": booking_steps,
}


def attach(topic, data):
    """给查库结果补一条轨迹。改的是 `data` 本身，返回它。

    只对**最近那一条**出轨迹：一次给五条时间轴，用户反而找不到重点。
    """
    build = BUILDERS.get(topic)
    if not build or not isinstance(data, dict):
        return data
    rows = data.get("list") or []
    # 取数函数用 `focus` 标出「刚才那句话说的是哪一条」。轨迹必须跟着它，
    # 否则文字讲的是待确认的那条、轨迹画的却是列表里最新的那条。
    row = data.get("focus") or (rows[0] if rows else None)
    if not row:
        return data
    data["timeline"] = {"of": row, "steps": build(row)}
    return data
