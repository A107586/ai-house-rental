# -*- coding: utf-8 -*-
"""长表单：一次问一项，问到一半跑题也回得来。

## 为什么要单独做

助手原先能办的事都只缺一两个槽——约看房问「哪一套、什么时候」两句就完了。
但「发布房源」有十几个字段，「发起签约」要房源、租客、起租日、月数四项。
这类事情照旧办法做出来是灾难：

- 一次把十几个问题倒给用户，等于把表单念了一遍，不如让他去页面填；
- 中途他问一句「租金一般填多少合适」，`_resume` 接不上就把整个待办丢了，
  填过的六项全没，得从头再来。**这一条最伤**——用户不敢问问题了。

所以这一层要解决的就两件事：**一次只问一项**，以及**跑题之后回得来**。

## 一次只问一项

表单写成一张声明式的表：字段顺序、问法、可点选项、校验、是否必填。
提议器按顺序找第一个还没填的必填项，问它；全填完了才出确认卡片。
每次追问都带上「第 3/7 项」，用户知道自己在哪、还剩多少。

## 跑题之后回得来

打断的判断放在 `agent._resume`：用户这句话像在提问（而不是在回答），
就不往槽里塞，正常去答他的问题，**同时把 pending 原样带回前端**，
并在答完之后补一句「刚才填到 X，接着填吗」。填过的槽一个不丢。

## 为什么不让大模型来抽字段

一句话里抽七个字段听起来更聪明，实际是把校验、追问、纠错全交给了模型。
表单里每一项都会落库，错一项就是一条错数据，而用户不会逐字复核确认卡。
按顺序一项一项问，慢一点，但每一项都是他自己给的、也都当场校验过。
"""

import re

# 字段类型 -> 校验函数。返回 (是否通过, 清洗后的值或错误提示)


def _text(value, spec):
    raw = (value or "").strip()
    if len(raw) < spec.get("min_len", 1):
        return False, "这一项太短了，再写具体点。"
    limit = spec.get("max_len", 200)
    return True, raw[:limit]


def _number(value, spec):
    raw = (value or "").strip().replace(",", "")
    found = re.search(r"\d+(?:\.\d+)?", raw)
    if not found:
        return False, "这一项要填数字。"
    number = float(found.group(0))
    if "min" in spec and number < spec["min"]:
        return False, "不能小于 %g。" % spec["min"]
    if "max" in spec and number > spec["max"]:
        return False, "不能大于 %g。" % spec["max"]
    return True, int(number) if number == int(number) else number


def _choice(value, spec):
    raw = (value or "").strip()
    # 选项来自实时查询（比如「改哪一套房源」）时，表里没有静态清单。
    # 原先直接取 spec["options"] 会 KeyError——实测的表现很怪：
    # 用户点了给出来的房源编号，却被回一句「这个选项我没找到」。
    # 这种字段交给后端去判合不合法，这里只保证不是空的。
    options = spec.get("options") or []
    if not options:
        return (True, raw) if raw else (False, "这一项还没填。")

    for option in options:
        if option["value"] == raw or option["label"] == raw:
            return True, option["value"]
    # 用户可能只说了选项里的一部分（「两室」对「两室一厅」）
    for option in options:
        if raw and (raw in option["value"] or raw in option["label"]):
            return True, option["value"]
    return False, "这个选项我没找到，点一个上面给的吧。"


def _date(value, spec):
    raw = (value or "").strip().replace("/", "-").replace(".", "-")
    found = re.search(r"(20\d{2})-(\d{1,2})-(\d{1,2})", raw)
    if not found:
        return False, "日期写成 2026-10-01 这样的格式就行。"
    year, month, day = (int(x) for x in found.groups())
    if not 1 <= month <= 12 or not 1 <= day <= 31:
        return False, "这个日期不对，再看一下。"
    return True, "%04d-%02d-%02d" % (year, month, day)


VALIDATORS = {"text": _text, "number": _number, "choice": _choice,
              "date": _date}


def option(value, label=None):
    return {"value": value, "label": label or value}


def choices(*values):
    return [option(v) for v in values]


# --------------------------------------------------------------- 表单定义

DISTRICTS = ("中山区", "西岗区", "沙河口区", "甘井子区", "旅顺口区", "金州区")
LAYOUTS = ("一室", "一室一厅", "两室一厅", "两室两厅", "三室一厅", "三室两厅")
RENT_TYPES = ("整租", "合租", "公寓")
ORIENTATIONS = ("朝南", "朝北", "朝东", "朝西", "南北通透")

# 每张表单：字段按**问的顺序**排。顺序不是随便的——
# 先问区域再问户型，是因为用户脑子里就是这么想房子的；
# 把「标题」放最后，是因为前面填完之后他才知道该怎么起名。
FORMS = {
    "publish_house": {
        "label": "发布房源",
        "risk": "medium",
        # **谁能办这件事，开问之前就要知道。**
        # 不写这一行的后果实测过：租客把十一项全填完、点确认，
        # 才收到一句「没有操作权限」——十一次输入白费，
        # 而这件事在第一句话之前就该说清楚。
        "roles": ("landlord", "admin"),
        "denied": "发布房源是房东端的功能，你当前的身份做不了。"
                  "要出租房子的话，先在账号设置里申请成为房东。",
        "intro": "好，我们一项一项来。随时可以问我别的，问完回来接着填。",
        "warning": "提交后房源进入待审核，管理员通过后才对外展示。",
        "fields": [
            {"name": "district", "type": "choice", "question": "房子在哪个区？",
             "options": choices(*DISTRICTS)},
            {"name": "community", "type": "text", "question": "小区或街道叫什么？",
             "max_len": 60},
            {"name": "address", "type": "text", "question": "详细地址呢？门牌号写上。",
             "max_len": 200},
            {"name": "layout", "type": "choice", "question": "户型是？",
             "options": choices(*LAYOUTS)},
            {"name": "area", "type": "number", "question": "面积多少平米？",
             "min": 5, "max": 1000},
            {"name": "rent_price", "type": "number", "question": "月租多少钱？",
             "min": 100, "max": 100000},
            {"name": "deposit", "type": "number", "question": "押金多少？",
             "min": 0, "max": 200000},
            {"name": "rent_type", "type": "choice", "question": "整租还是合租？",
             "options": choices(*RENT_TYPES)},
            {"name": "floor", "type": "text", "question": "第几层／共几层？比如 6/18。",
             "max_len": 20},
            {"name": "orientation", "type": "choice", "question": "朝向？",
             "options": choices(*ORIENTATIONS)},
            {"name": "title", "type": "text", "question":
                "最后起个标题，一句话说清是什么房子。",
             "min_len": 2, "max_len": 60},
        ],
    },
    "sign_contract": {
        "label": "发起签约",
        "risk": "high",
        "roles": ("landlord", "admin"),
        "denied": "签约要由房东发起。你是租客的话，等房东发起后"
                  "在「我的租约」里确认就行。",
        "intro": "好，我们把签约信息一项一项确认下来。",
        "warning": "发起后租客会收到待确认的合同，他点确认之后才生效。",
        "fields": [
            {"name": "house_no", "type": "choice", "question": "给哪套房源签约？",
             "source": "landlord_houses"},
            {"name": "tenant_username", "type": "text",
             "question": "租客的账号是？", "max_len": 50},
            {"name": "start_date", "type": "date",
             "question": "起租日写哪天？比如 2026-10-01。"},
            {"name": "months", "type": "number", "question": "租期几个月？",
             "min": 1, "max": 60},
            # 谈定的月租。**不追问**：多数时候就是挂牌价，后端不填就按挂牌价走；
            # 用户自己说了「月租 2800」才带上（2026-10-22 起合同里可以议价）
            {"name": "rent", "type": "number", "question": "谈定的月租是多少？",
             "min": 1, "max": 1000000, "optional": True},
        ],
    },
    "edit_house": {
        "label": "修改房源",
        "risk": "medium",
        "roles": ("landlord", "admin"),
        "denied": "修改房源是房东端的功能，你当前的身份做不了。",
        "intro": "改哪一项？一次改一项，改完还能接着改别的。",
        "warning": "**修改后房源会退回待审核状态**，要重新过一遍审核才对外展示。",
        "fields": [
            {"name": "house_no", "type": "choice", "question": "改哪一套？",
             "source": "landlord_houses"},
            {"name": "field", "type": "choice", "question": "改哪一项？",
             "options": [option("rent_price", "月租"), option("deposit", "押金"),
                         option("title", "标题"), option("address", "地址"),
                         option("description", "房源描述")]},
            # **值的类型取决于上一项改的是哪一项。**
            # 一律当文本收的后果实测过：改租金填「三千五」，卡片好好的，
            # 一点确认后端回 float_parsing——又是「卡片对、执行错」。
            {"name": "value", "type": "text", "question": "改成什么？",
             "max_len": 500,
             "type_by": {"field": {"rent_price": "number",
                                   "deposit": "number"}},
             "min": 0, "max": 1000000},
        ],
    },
}


def spec(name):
    return FORMS.get(name)


def field_spec(form, field_name):
    for field in form["fields"]:
        if field["name"] == field_name:
            return field
    return None


def _options_for(field, context):
    """字段的可点选项。`source` 的那几个要现查，写死在表里会过期。"""
    if field.get("options"):
        return list(field["options"])
    source = field.get("source")
    if source == "landlord_houses":
        return [option(h.get("house_no"),
                       "%s %s" % (h.get("house_no"), (h.get("title") or "")[:16]))
                for h in (context.get("_houses") or [])[:5]]
    return []


def is_filled(slots, name):
    """这一项填过没有。

    **不能写成 `str(value).strip()`。** 上游的 `resolve_house` 在没有房源
    上下文时返回的是 `None`，而 `str(None)` 是字符串 "None"——非空。
    实测的表现是「修改房源」「发起签约」**默默跳过第一项**：
    用户没被问过是哪一套，助手却认为问过了。
    """
    value = (slots or {}).get(name)
    if value is None:
        return False
    return bool(str(value).strip())


def next_missing(name, slots):
    """下一个还没填的必填字段。全填完返回 None。"""
    form = FORMS[name]
    for field in form["fields"]:
        if field.get("optional"):
            continue
        if not is_filled(slots, field["name"]):
            return field
    return None


def progress(name, slots):
    """填到第几项 / 一共几项。用户得知道自己在哪、还剩多少。"""
    form = FORMS[name]
    required = [f for f in form["fields"] if not f.get("optional")]
    done = sum(1 for f in required if is_filled(slots, f["name"]))
    return done, len(required)


def type_of(field, slots):
    """字段的实际类型。多数是写死的，少数看别的槽位。

    「改成什么？」这一项，改的是租金就得按数字校验，改的是标题就是文本。
    """
    rule = field.get("type_by") or {}
    for slot, mapping in rule.items():
        picked = str((slots or {}).get(slot, ""))
        if picked in mapping:
            return mapping[picked]
    return field["type"]


def validate(name, field_name, value, slots=None):
    """校验一个字段。返回 ``(是否通过, 清洗后的值或错误提示)``。"""
    field = field_spec(FORMS[name], field_name)
    if not field:
        return False, "这一项我不认识。"
    checker = VALIDATORS.get(type_of(field, slots), _text)
    return checker(value, field)


def allowed_roles(name):
    """这张表单允许谁办。返回空表示不限。"""
    return (FORMS.get(name) or {}).get("roles") or ()


def denied_message(name):
    return (FORMS.get(name) or {}).get("denied") or "你当前的身份做不了这件事。"


def ask_for(name, field, slots, context=None):
    """把一个字段包装成一次追问。"""
    done, total = progress(name, slots)
    return {
        "slot": field["name"],
        # 「第 3/11 项」这四个字很便宜，但没有它，用户不知道自己是在
        # 填第三项还是第十项，也就不知道还要不要继续
        "question": "（第 %d/%d 项）%s" % (done + 1, total, field["question"]),
        "options": _options_for(field, context or {}),
    }


def summary(name, slots):
    """确认卡片上那句话：**把每一项都写出来**。

    表单填了十几项，用户不可能全记得。确认前列一遍是最后一道人工校验，
    省掉它等于让人蒙着眼提交。
    """
    form = FORMS[name]
    parts = []
    for field in form["fields"]:
        value = slots.get(field["name"])
        if value in (None, ""):
            continue
        # 值本身也可能是个内部名。「改 rent_price，改成 3500」——
        # 用户在选项上点的是「月租」，卡片却回给他一个字段名。
        for option in (field.get("options") or []):
            if option["value"] == value:
                value = option["label"]
                break
        label = field["question"].rstrip("？?").replace("最后起个标题，", "")
        parts.append("%s%s" % (_short_label(field, label), value))
    return "%s：%s" % (form["label"], "，".join(parts))


_LABELS = {
    "district": "区域 ", "community": "小区 ", "address": "地址 ",
    "layout": "户型 ", "area": "面积 ", "rent_price": "月租 ",
    "deposit": "押金 ", "rent_type": "类型 ", "floor": "楼层 ",
    "orientation": "朝向 ", "title": "标题 ", "house_no": "房源 ",
    "tenant_username": "租客 ", "start_date": "起租 ", "months": "租期 ", "rent": "月租 ",
    "field": "改 ", "value": "改成 ", "description": "描述 ",
}


def label_of(field):
    """字段的短名，给前端的已填清单用。"""
    return _LABELS.get(field["name"], field["name"]).strip()


def _short_label(field, fallback):
    return _LABELS.get(field["name"], fallback + " ")
