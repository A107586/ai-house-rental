"""房源筛选条件 → SQL。**全站只有这一处**（K-09）。

原先这套条件只长在 `/api/houses` 的函数体里。保存搜索要判断
「一套新上架的房源符不符合用户存下的条件」，如果在那边再写一遍判断逻辑，
两份实现必然会分叉——而分叉的表现是最难查的那种：用户存的条件在列表页
筛得到这套房，上新提醒却不发；或者反过来，提醒说匹配上了，点进去
列表里根本没有。

所以这里把条件构造抽出来，两边共用：

- 列表接口：`apply_filters(select(House), params)` 然后分页；
- 上新匹配：同一句 `apply_filters(...)`，再加一条 `House.id == 这套房`，
  查得到就说明匹配。**匹配用的是同一条 SQL，不可能和列表页说法不一致。**
"""

import re

from datetime import date, datetime, timedelta

from sqlalchemy import Float, cast, func, literal, or_, select

from app.models import House, Review


# 低/中/高楼层的比例区间（所在层 ÷ 总层数），左开右闭。
# 三等分而不是写死层数：6 层老楼的第 5 层是高楼层，
# 33 层塔楼的第 5 层是低楼层，固定阈值对不上这两种情况。
FLOOR_LEVEL_RANGE = {
    "低楼层": (None, 1 / 3),
    "中楼层": (1 / 3, 2 / 3),
    "高楼层": (2 / 3, None),
}

# 租期档位 → (月数阈值, 是否「不少于」)。
# 前四档问的是「最短租多久就能签」，年租问的是「房东要求至少租一年」，
# 方向正好相反，所以要带一个标志位而不是统一取上界。
LEASE_TERM_RULE = {
    "monthly": (1, False),
    "quarter": (3, False),
    "half": (6, False),
    "yearly": (12, True),
}

# 设施偏好里已经升格成独立列的那些。列比标签可靠：
# 标签是「有没有写」，列是「是不是真的有」。
FEATURE_COLUMNS = {
    "电梯": lambda: House.elevator.is_(True),
    "精装": lambda: House.decoration == "精装",
    "集中供暖": lambda: House.heating == "集中供暖",
}

# 保存搜索允许存下来的键。白名单而不是黑名单——用户存的条件是一份
# 会被原样喂回查询的 JSON，放任未知键进来等于把查询参数的口子敞开。
ALLOWED_KEYS = (
    "district", "layout", "rent_type", "keyword",
    "rent_price_min", "rent_price_max", "area_min", "area_max",
    "orientation", "floor_level", "elevator", "decoration", "heating",
    "lease_term", "available_days", "features",
    # 首页分栏用：published_days 取「最近 N 天上架的」，
    # min_rating 取「均分不低于 N 的」。两者都是筛选不是排序——
    # 「最新发布」若只排序不筛，翻到第十页仍叫最新发布，名不副实。
    "published_days", "min_rating",
)

# 这几个键必须是数字。**存进来的条件之后会被当成可信数据用**
# （拼提示文案、喂回查询），所以要在入口处就把类型定死，
# 而不是等到 `describe()` 里 int(float("abc")) 时才炸。
NUMERIC_KEYS = ("rent_price_min", "rent_price_max",
                "area_min", "area_max", "available_days",
                "published_days", "min_rating")

# 取值有限的几个键。不认识的值直接丢掉——`apply_filters` 本来就会
# 忽略它们（筛不出任何东西），但留在条件里会跟着跑进搜索名称，
# 用户看到一个「中山区 · {'a': 1}」只会觉得系统坏了。
ENUM_KEYS = {
    "floor_level": set(FLOOR_LEVEL_RANGE),
    "lease_term": set(LEASE_TERM_RULE),
}


def split_features(raw):
    """`features=电梯,可养宠物` 拆成列表，最多五个。

    上限是防呆：拼一百个设施进来会生成一百个 AND 子句，
    查询规划器会被拖垮，而正常用户提不出五个以上的硬条件。
    """
    if isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        items = str(raw or "").split(",")
    # 先转成字符串再 strip。列表里混进一个数字时，`f.strip()` 会直接
    # AttributeError——而这个列表是用户 POST 上来的，不能假设元素类型
    return [str(f).strip() for f in items if str(f).strip()][:5]


def feature_clause(feature):
    """一个设施条件对应的 SQL。

    **不能写成 `tags LIKE '%电梯%'`。** 「无电梯」里也含「电梯」二字，
    子串匹配会把它一并命中，筛出来的结果是反的。所以两条路都走精确匹配：
    有独立列的直接比列；其余的把标签串两端补上逗号做**整词**匹配。
    """
    feature = feature.strip()
    if feature in FEATURE_COLUMNS:
        return FEATURE_COLUMNS[feature]()
    padded = literal(",") + House.tags + literal(",")
    return padded.like("%," + feature + ",%")


def _as_bool(value):
    """查询字符串里的布尔。`elevator=false` 必须是假，不能因为
    「非空字符串为真」而变成真——那会让「无电梯」筛出有电梯的房。"""
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("true", "1", "yes", "on"):
        return True
    if text in ("false", "0", "no", "off"):
        return False
    return None


# 关键词要搜的字段。**标签和房源编号原先不在里面**，
# 于是「有电梯的」搜不到（电梯在 tags 里），直接贴一个「DL0041」也搜不到。
_KEYWORD_COLUMNS = ("title", "description", "community", "address",
                    "business_area", "tags", "house_no", "layout",
                    "district", "orientation")

# 切词用的分隔符。中文不靠空格断句，但用户输入时常常自己加空格或标点
# 来分隔几个条件（「桂林街 两室」「中山区,电梯」）。
_SPLIT = re.compile(r"[\s,，、;；/|]+")


def _apply_keyword(stmt, raw):
    """普通关键词检索：多个词之间是**且**的关系。

    原先是把整串话直接 LIKE 一遍。于是「桂林街 两室」一条也搜不出来——
    没有任何一个字段同时包含这两个词加中间那个空格。而用户输入两个词，
    意思显然是「两个都要满足」，不是「有这么一整串字符」。

    现在按分隔符切开，**每个词各自在所有字段里找一遍，词与词之间取交集**：

        桂林街 两室  →  (任一字段含「桂林街」) 且 (任一字段含「两室」)

    切不出多个词时（纯中文没有空格）就退回整串匹配，行为和以前一致。

    为什么不上分词器：房源字段是短文本，用户输入也短，
    切词切错的代价（搜不到）比不切大。**空格是用户自己给的信号**，
    顺着它走最稳妥。
    """
    words = [w for w in _SPLIT.split(raw.strip()) if w]
    if not words:
        return stmt
    for word in words[:6]:      # 超过六个词基本是粘贴了一整段话，截断
        like = "%" + word + "%"
        any_field = None
        for name in _KEYWORD_COLUMNS:
            column = getattr(House, name, None)
            if column is None:
                continue
            clause = column.like(like)
            any_field = clause if any_field is None else (any_field | clause)
        if any_field is not None:
            stmt = stmt.where(any_field)
    return stmt


def _as_number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def apply_filters(stmt, params, today=None):
    """把一份筛选条件加到查询上。

    `params` 是键值字典，键取自 `ALLOWED_KEYS`；**空值一律当作没传**，
    这样前端把空字符串一起发过来也不会误筛掉全部房源。
    """
    get = params.get
    today = today or date.today()

    if get("district"):
        stmt = stmt.where(House.district.like("%" + str(get("district")) + "%"))
    if get("layout"):
        stmt = stmt.where(House.layout.like("%" + str(get("layout")) + "%"))
    if get("rent_type"):
        stmt = stmt.where(House.rent_type == get("rent_type"))

    for key, column, op in (
        ("rent_price_min", House.rent_price, "ge"),
        ("rent_price_max", House.rent_price, "le"),
        ("area_min", House.area, "ge"),
        ("area_max", House.area, "le"),
    ):
        number = _as_number(get(key))
        if number is None:
            continue
        stmt = stmt.where(column >= number if op == "ge" else column <= number)

    if get("keyword"):
        stmt = _apply_keyword(stmt, str(get("keyword")))

    if get("orientation"):
        stmt = stmt.where(House.orientation == get("orientation"))
    if get("decoration"):
        stmt = stmt.where(House.decoration == get("decoration"))
    if get("heating"):
        stmt = stmt.where(House.heating == get("heating"))

    elevator = _as_bool(get("elevator")) if get("elevator") not in (None, "") else None
    if elevator is not None:
        stmt = stmt.where(House.elevator == elevator)

    level = get("floor_level")
    if level in FLOOR_LEVEL_RANGE:
        low, high = FLOOR_LEVEL_RANGE[level]
        ratio = cast(House.floor_no, Float) / cast(House.floor_total, Float)
        # 楼层不全的房源不参与这个筛选——猜不了，也不该猜
        stmt = stmt.where(House.floor_no.isnot(None),
                          House.floor_total.isnot(None),
                          House.floor_total > 0)
        if low is not None:
            stmt = stmt.where(ratio > low)
        if high is not None:
            stmt = stmt.where(ratio <= high)

    term = get("lease_term")
    if term in LEASE_TERM_RULE:
        bound, at_least = LEASE_TERM_RULE[term]
        stmt = stmt.where(House.min_months >= bound if at_least
                          else House.min_months <= bound)

    days = get("available_days")
    if days not in (None, ""):
        number = _as_number(days)
        if number is not None:
            # 可入住日没填的房源一并算进来：它的状态已经是「可租」了，
            # 缺的只是一个具体日期，把它藏起来对房东不公平
            deadline = today + timedelta(days=max(int(number), 0))
            stmt = stmt.where(or_(House.available_from.is_(None),
                                  House.available_from <= deadline))

    # 最近 N 天上架。用「发布时间」而不是「可入住日」——
    # 前者是房源出现在平台上的时刻，后者是能搬进去的日子，两回事。
    days = get("published_days")
    if days not in (None, ""):
        number = _as_number(days)
        if number is not None:
            since = datetime.combine(today, datetime.min.time())                 - timedelta(days=max(int(number), 0))
            stmt = stmt.where(House.published_at >= since)

    # 均分不低于 N。
    #
    # `House.avg_rating` 是 Python 属性、不是数据库列，SQL 里排不了序也
    # 筛不了；在应用层算又要把整表实例化一遍。所以这里用子查询：
    # 按房源聚合出均分，再拿它比较。软删除的评价要排除——
    # 被管理员屏蔽的差评不该继续压着这套房。
    low = get("min_rating")
    if low not in (None, ""):
        number = _as_number(low)
        if number is not None:
            rated = (select(Review.house_id)
                     .where(Review.deleted_at.is_(None))
                     .group_by(Review.house_id)
                     .having(func.avg(Review.rating) >= number))
            stmt = stmt.where(House.id.in_(rated))

    for feature in split_features(get("features")):
        stmt = stmt.where(feature_clause(feature))

    return stmt


def clean_conditions(raw):
    """把用户提交的条件洗成可以存库、也可以原样喂回查询的字典。

    只留白名单里的键、丢掉空值，并把设施偏好归一成逗号串。
    洗过之后这份 JSON 就是**可信的**——存进 `saved_searches.conditions`，
    下次匹配时直接交给 `apply_filters`，中间不需要再校验一遍。
    """
    if not isinstance(raw, dict):
        return {}
    clean = {}
    for key in ALLOWED_KEYS:
        value = raw.get(key)
        if value is None or value == "" or value == [] or value == {}:
            continue
        if key == "features":
            features = split_features(value)
            if features:
                clean[key] = ",".join(features)
            continue
        if key in NUMERIC_KEYS:
            number = _as_number(value)
            # 数不成数就整条丢掉，**不要留着**——留着等于把一个
            # 会让后续代码崩掉的值存进了库
            if number is not None:
                clean[key] = number
            continue
        if key in ENUM_KEYS:
            text = str(value)
            if text in ENUM_KEYS[key]:
                clean[key] = text
            continue
        if key == "elevator":
            # 认不出的布尔一律丢掉。留着不会筛错（apply_filters 也认不出），
            # 但会让「有电梯 / 无电梯」在名称里显示成一个错的值
            flag = _as_bool(value)
            if flag is not None:
                clean[key] = flag
            continue
        # 其余一律按字符串存。前端可能传上来数字或布尔，
        # 统一类型之后下游就不用到处判断了
        clean[key] = value if isinstance(value, bool) else str(value)
    return clean


def describe(conditions):
    """把条件拼成一句人话，用作保存搜索的默认名称与提醒正文。

    用户不给搜索起名是常态，「未命名搜索 3」对他毫无意义——
    他需要一眼认出这是哪一组条件。

    **对坏数据要免疫。** `clean_conditions` 已经在入口处把类型定死了，
    但这个函数也会作用在**从库里读回来的**条件上——那些行可能是更早的
    版本写的，也可能被人手工改过。为了一句展示文案把接口打崩不值得。
    """
    parts = []
    if conditions.get("district"):
        parts.append(str(conditions["district"]))
    if conditions.get("business_area"):
        parts.append(str(conditions["business_area"]))
    if conditions.get("layout"):
        parts.append(str(conditions["layout"]))
    if conditions.get("rent_type"):
        parts.append(str(conditions["rent_type"]))

    low = _as_number(conditions.get("rent_price_min"))
    high = _as_number(conditions.get("rent_price_max"))
    if low and high:
        parts.append("%d–%d 元" % (int(low), int(high)))
    elif high:
        parts.append("%d 元以内" % int(high))
    elif low:
        parts.append("%d 元以上" % int(low))

    if conditions.get("floor_level"):
        parts.append(str(conditions["floor_level"]))
    if conditions.get("orientation"):
        # 朝向两种写法都可能传进来（「南」和「朝南」，见 schemas.ORIENTATIONS
        # 那段说明）。无脑加「朝」会写出「朝朝南」，所以先看有没有。
        facing = str(conditions["orientation"])
        parts.append(facing if facing.startswith("朝") or facing == "南北通透"
                     else "朝" + facing)
    if conditions.get("decoration"):
        parts.append(str(conditions["decoration"]))
    elevator = _as_bool(conditions.get("elevator")) \
        if conditions.get("elevator") not in (None, "") else None
    if elevator is True:
        parts.append("有电梯")
    elif elevator is False:
        parts.append("无电梯")
    parts.extend(split_features(conditions.get("features")))
    if conditions.get("keyword"):
        parts.append("含「%s」" % conditions["keyword"])

    return " · ".join(parts) if parts else "全部房源"
