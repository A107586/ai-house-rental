"""系统配置的默认值与读写（M-09）。

**这里的每一项都真的接进了业务逻辑**，不是存起来给人看的。存了值却没人
读的配置页是假功能，答辩时一问「改了它会怎样」就露馅。下表最后一列写明
每项在哪里生效：

===========================  ==================================================
配置项                        生效位置
===========================  ==================================================
site_name / contact_phone     前端顶栏与页脚（公开项，未登录也能读）
viewing_minutes               预约看房的时段冲突判断窗口
appointment_daily_limit       每人每天最多提交多少条预约
house_auto_approve            房东发布的房源是否免审核直接上架
deposit_months                创建合同时押金的默认月数
review_window_days            退租后多少天内还能评价
repair_sla_*_hours            工单超时判定，超时的会在管理端标红并计数
===========================  ==================================================

读取策略是每次从库里查，不做进程内缓存。配置表只有个位数行、且带唯一索引，
一次查询的代价远低于「改了配置要重启服务才生效」带来的困惑。
"""

from sqlalchemy import select

from app.models import SystemConfig

# (key, 默认值, 类型, 名称, 说明, 分组, 是否公开)
DEFAULTS = (
    ("site_name", "大连智租", "text", "站点名称",
     "显示在页面顶栏。", "站点", True),
    ("contact_phone", "0411-88888888", "text", "客服电话",
     "显示在页面底部，供用户联系平台。", "站点", True),

    ("viewing_minutes", "60", "int", "看房时段占用（分钟）",
     "同一房源在这个时间窗口内只接受一条预约，用于判断时段冲突。",
     "预约", False),
    ("appointment_daily_limit", "5", "int", "每人每日预约上限",
     "同一账号一天最多提交多少条看房预约，防止刷单占位。", "预约", False),

    ("house_auto_approve", "false", "bool", "房源免审核",
     "开启后房东发布的房源直接上架，不再需要管理员审核。", "房源", False),

    ("deposit_months", "1", "int", "默认押金月数",
     "创建合同时押金的默认值，按月租金的倍数计算。", "交易", False),
    ("review_window_days", "30", "int", "评价有效期（天）",
     "退租后超过这个天数就不能再评价了。", "交易", False),

    ("repair_sla_urgent_hours", "4", "int", "紧急工单处理时限（小时）",
     "紧急报修超过这个时长仍未处理，会在管理端标记为超时。", "报修", False),
    ("repair_sla_normal_hours", "48", "int", "一般工单处理时限（小时）",
     "一般报修超过这个时长仍未处理，会在管理端标记为超时。", "报修", False),
)

_DEFAULT_MAP = {item[0]: item for item in DEFAULTS}


def ensure_defaults(session):
    """补齐缺失的配置项。

    只插入不存在的键，不覆盖已有值——否则每次重启都会把管理员改过的
    配置打回默认，那配置页就白改了。
    """
    existing = set(session.scalars(select(SystemConfig.key)).all())
    added = 0
    for key, value, vtype, label, desc, group, public in DEFAULTS:
        if key in existing:
            continue
        session.add(SystemConfig(key=key, value=value, value_type=vtype,
                                 label=label, description=desc,
                                 group_name=group, is_public=public))
        added += 1
    if added:
        session.commit()
    return added


def raw(session, key):
    row = session.scalar(select(SystemConfig).where(SystemConfig.key == key))
    if row is not None:
        return row.value
    fallback = _DEFAULT_MAP.get(key)
    return fallback[1] if fallback else None


def get_int(session, key, default=None):
    """读整数配置。

    值来自管理员手输，可能是空串或写错的文本；转不动就退回默认值，
    绝不让一个笔误把接口打成 500。
    """
    try:
        return int(str(raw(session, key)).strip())
    except (TypeError, ValueError):
        if default is not None:
            return default
        fallback = _DEFAULT_MAP.get(key)
        return int(fallback[1]) if fallback else 0


def get_bool(session, key, default=False):
    value = str(raw(session, key) or "").strip().lower()
    if value in ("true", "1", "yes", "on"):
        return True
    if value in ("false", "0", "no", "off", ""):
        return False
    return default


def get_text(session, key, default=""):
    value = raw(session, key)
    return default if value is None else value


def validate(key, value):
    """校验待写入的值，返回 ``(是否合法, 规范化后的值或错误说明)``。

    整数项统一要求为正：看房时段填 0 会让冲突窗口塌成一个点，
    预约上限填 0 则等于把功能关掉，两者都不是「配置」该表达的意思。
    """
    item = _DEFAULT_MAP.get(key)
    if not item:
        return False, "未知的配置项"
    vtype = item[2]
    text = str(value).strip()

    if vtype == "int":
        try:
            number = int(text)
        except ValueError:
            return False, "该项必须填整数"
        if number <= 0:
            return False, "该项必须大于 0"
        return True, str(number)

    if vtype == "bool":
        if text.lower() in ("true", "1", "yes", "on"):
            return True, "true"
        if text.lower() in ("false", "0", "no", "off"):
            return True, "false"
        return False, "该项只能是 true 或 false"

    if not text:
        return False, "该项不能为空"
    return True, text[:255]


def set_value(session, key, value):
    """写入配置。调用方需先用 validate 校验。"""
    row = session.scalar(select(SystemConfig).where(SystemConfig.key == key))
    if row is None:
        item = _DEFAULT_MAP[key]
        row = SystemConfig(key=key, value_type=item[2], label=item[3],
                           description=item[4], group_name=item[5],
                           is_public=item[6])
        session.add(row)
    row.value = value
    return row


def list_all(session, public_only=False):
    ensure_defaults(session)
    query = select(SystemConfig)
    if public_only:
        query = query.where(SystemConfig.is_public.is_(True))
    rows = session.scalars(query).all()
    # 按 DEFAULTS 的定义顺序排，界面上的分组才不会每次刷新都跳来跳去
    order = {key: index for index, (key, *_) in enumerate(DEFAULTS)}
    rows.sort(key=lambda row: order.get(row.key, 999))
    return rows
