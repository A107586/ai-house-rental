# -*- coding: utf-8 -*-
"""从代码生成概要设计要的图：系统结构图、各模块类图、数据库 E-R 图。

    python scripts/gen_diagrams.py              # 生成 .mmd、渲染 PNG、写回两份文档
    python scripts/gen_diagrams.py --check      # 只校验：图与代码一致、PNG 与源一致
    python scripts/gen_diagrams.py --no-render  # 只更新 .mmd 与文档里的 Mermaid 块

## 为什么要生成，而不是在工具里画

《概要设计模板》每个模块要一张类图、数据库一节要一张表间关系图。
**手画的图第二天就过期**：本项目的表、接口、工具每天都在变，
原先《数据库设计说明书》里手写的那张 ER 图只画了 36 张表中的 22 张，
漏掉的 14 张没有任何人发现——图不会报错。

所以图和建库脚本、接口清单一样，从代码里读：

| 图 | 数据从哪来 |
|---|---|
| E-R 图 | `server/app/models.py` 的元数据：全部表、主键、唯一键、外键 |
| 业务后端各模块类图 | 模型类（字段、方法、ORM 关系）；运行中应用的路由表（处理函数、请求 DTO）；**处理函数的语法树**——它实际调用了哪个服务模块的哪个函数、用到了哪些实体 |
| 智能体服务类图 | `agent/app` 的语法树：类、继承、组合、各子智能体的 `run()` 与工具模块 |
| 系统结构图 | 接口数、表数、子智能体数、工具数，全部现数 |

模块归属用 `scripts/db_meta.py` 的 `MODULES` 与 `PATH_MODULES`——
和接口清单、数据库设计书是同一份，不另维护。

## 产物

- `docs/图/<名字>.mmd` —— Mermaid 源，GitHub 上直接能看
- `docs/图/<名字>.png` —— 渲染结果，Word 版概要设计与数据库设计书里插的是它
- `docs/图/manifest.json` —— 每张 PNG 是由哪一版源渲染的（源的 SHA-256）
- 两份 .md 文档里 `<!-- DIAGRAM:名字:BEGIN -->` 与 `END` 之间的那段

`--check` **不需要渲染环境**：它比的是源，以及 manifest 里记下的源的指纹。
所以测试套件能在任何一台机器上跑，渲染只在生成时需要。

## 渲染环境

用 mermaid-cli（`mmdc`），浏览器用本机的 Edge 或 Chrome，**不下载 Chromium**：

    cd scripts && set PUPPETEER_SKIP_DOWNLOAD=1 && npm install

找 `mmdc` 的顺序：环境变量 `MMDC` → `scripts/node_modules/.bin` → PATH。
找浏览器：环境变量 `PUPPETEER_EXECUTABLE_PATH` → 常见的 Edge / Chrome 安装位置。
"""

import argparse
import ast
import hashlib
import inspect
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db_meta import (MODULES, ROOT, TABLE_INFO, load_metadata,  # noqa: E402
                     module_of_path, ordered_tables)

FIG_DIR = os.path.join(ROOT, "docs", "图")
MANIFEST = os.path.join(FIG_DIR, "manifest.json")
OUTLINE = os.path.join(ROOT, "docs", "概要设计说明书.md")
DB_DOC = os.path.join(ROOT, "docs", "数据库设计说明书.md")
AGENT_APP = os.path.join(ROOT, "agent", "app")

BEGIN = "<!-- DIAGRAM:%s:BEGIN 本图由 scripts/gen_diagrams.py 从代码生成，不要手改 -->"
END = "<!-- DIAGRAM:%s:END -->"

MODULE_ORDER = ["账号与权限", "房源", "看房预约", "合同与账单", "报修与维修",
                "互动与消息", "中介", "平台治理", "智能客服与知识"]
MODULE_IDS = {
    "账号与权限": "class_account", "房源": "class_house",
    "看房预约": "class_appointment", "合同与账单": "class_contract",
    "报修与维修": "class_repair", "互动与消息": "class_interaction",
    "中介": "class_broker", "平台治理": "class_governance",
    "智能客服与知识": "class_support",
}

# 服务模块的「主场」：在主场的类图里列出全部公开函数，
# 在别的模块的类图里**只列那个模块真正调用到的函数**——
# 否则 `deps`、`permissions` 这种横切模块会在九张图里各摊一遍全貌。
SERVICE_HOME = {
    "auth": "账号与权限", "security": "账号与权限", "idcard": "账号与权限",
    "deps": "账号与权限", "permissions": "账号与权限",
    "house_query": "房源", "map_service": "房源", "pricing": "房源",
    "uploads": "房源",
    "leasing": "合同与账单", "gateway": "合同与账单",
    "notifier": "互动与消息",
    "oplog": "平台治理", "moderation": "平台治理", "config_store": "平台治理",
    "ratelimit": "平台治理", "netaddr": "平台治理",
}
SERVICES = {"app." + name for name in SERVICE_HOME}

# 实体类图里除主键、外键、唯一键之外还要露出来的列：状态与分类。
# 其余字段在数据库设计书里逐列列出，类图里全摊开就看不见关系了。
SHOWN_COLUMNS = {"status", "role", "type", "urgency", "category",
                 "license_status", "target_role", "is_published", "is_cover",
                 "viewing_time", "rent_price", "amount"}

# 与 gen_api_list.SKIP 同一口径：文档页与根路径不算接口
SKIP_PATHS = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc", "/"}

TYPE_NAMES = {"INTEGER": "int", "VARCHAR": "str", "STRING": "str",
              "TEXT": "text", "DATETIME": "datetime", "DATE": "date",
              "BOOLEAN": "bool", "FLOAT": "float", "NUMERIC": "decimal",
              "JSON": "json"}


# ============================================================== 小工具

def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def type_name(column):
    name = type(column.type).__name__.upper()
    return TYPE_NAMES.get(name, name.lower())


def safe(text):
    """类图成员行里不能出现 `{` `}`（会被当成类体的边界），
    路径参数 `{house_no}` 改写成 `:house_no`。"""
    return re.sub(r"\{(\w+)\}", r":\1", text).replace('"', "'")


def unique_columns(table):
    names = {c.name for c in table.columns if c.unique}
    for constraint in table.constraints:
        cols = list(getattr(constraint, "columns", []))
        if type(constraint).__name__ == "UniqueConstraint" and len(cols) == 1:
            names.add(cols[0].name)
    return names


def cn_name(table_name):
    return TABLE_INFO.get(table_name, (table_name, ""))[0]


def fk_edges(metadata):
    """(子表, 列, 父表, 父列, 可空, 唯一)。按表的业务顺序、列顺序排，输出稳定。"""
    tables = {t.name: t for t in metadata.sorted_tables}
    edges = []
    for name in ordered_tables(metadata):
        table = tables[name]
        uniques = unique_columns(table)
        for column in table.columns:
            for fk in sorted(column.foreign_keys, key=lambda f: f.target_fullname):
                edges.append((name, column.name, fk.column.table.name,
                              fk.column.name, column.nullable,
                              column.name in uniques))
    return edges


# ============================================================== E-R 图

def er_diagram(metadata, columns=True):
    """`columns=False` 出总览：只有实体与关系。36 张表连同键列画在一张图里，
    放进 A4 就看不清字了；总览负责「谁连着谁」，列在全量图与设计书里看。"""
    if not columns:
        lines = ["erDiagram", "    direction LR"]
        for name in ordered_tables(metadata):
            lines.append('    %s["%s %s"]' % (name, name, cn_name(name)))
        for child, col, parent, _pcol, nullable, unique in fk_edges(metadata):
            lines.append('    %s %s--%s %s : "%s"' % (
                parent, "|o" if nullable else "||", "o|" if unique else "o{",
                child, col))
        return "\n".join(lines) + "\n"
    tables = {t.name: t for t in metadata.sorted_tables}
    # 外键链最长只有五六层（用户→房源→合同→账单→缴费→退款），
    # 自上而下排会摊成一行十米宽；从左往右排，每层竖着叠，才放得进一页纸。
    lines = ["erDiagram", "    direction LR"]
    for name in ordered_tables(metadata):
        table = tables[name]
        uniques = unique_columns(table)
        fks = {c.name for c in table.columns if c.foreign_keys}
        lines.append('    %s["%s %s"] {' % (name, name, cn_name(name)))
        for column in table.columns:
            keys = []
            if column.primary_key:
                keys.append("PK")
            if column.name in fks:
                keys.append("FK")
            if column.name in uniques and not column.primary_key:
                keys.append("UK")
            if keys:
                lines.append("        %s %s %s" % (type_name(column), column.name,
                                                   ", ".join(keys)))
        lines.append("    }")
    for child, col, parent, _pcol, nullable, unique in fk_edges(metadata):
        left = "|o" if nullable else "||"
        right = "o|" if unique else "o{"
        lines.append('    %s %s--%s %s : "%s"' % (parent, left, right, child, col))
    return "\n".join(lines) + "\n"


# ============================================================== 业务后端的类图

def load_server():
    sys.path.insert(0, os.path.join(ROOT, "server"))
    os.environ.setdefault("DATABASE_URL", "sqlite://")
    from app.main import app
    return app


def server_routes(app):
    """(方法, 路径, 处理函数对象)。与 gen_api_list 同一套摊平逻辑。"""
    rows = []

    def walk(routes, prefix=""):
        for route in routes:
            inner = getattr(route, "original_router", None)
            if inner is not None:
                walk(inner.routes, prefix + getattr(inner, "prefix", ""))
                continue
            if hasattr(route, "routes"):
                walk(route.routes, prefix + getattr(route, "prefix", ""))
                continue
            methods = getattr(route, "methods", None)
            endpoint = getattr(route, "endpoint", None)
            if not methods or endpoint is None:
                continue
            for method in sorted(methods - {"HEAD", "OPTIONS"}):
                rows.append((method, prefix + route.path, endpoint))

    walk(app.routes)
    return sorted([r for r in rows if r[1] not in SKIP_PATHS],
                  key=lambda r: (r[1], r[0]))


class Uses(object):
    """一个函数（连同它调用的同文件辅助函数）用到了什么。"""

    def __init__(self):
        self.functions = set()      # (服务模块短名, 函数名)
        self.classes = set()        # (服务模块短名, 类名)
        self.entities = set()       # 模型类名


def _source_tree(func):
    try:
        return ast.parse(textwrap.dedent(inspect.getsource(func)))
    except (OSError, TypeError, SyntaxError):
        return None


def analyze(func, entity_classes, uses=None, seen=None, depth=0):
    """顺着语法树找出函数实际用到的服务函数、服务类与实体。

    **只认真正解析得到的对象**：名字先到函数的全局命名空间里查，
    查到的是 `app.leasing` 模块、是 `leasing.py` 里定义的函数、
    或是某个映射类，才算数。正则抓名字会把同名的局部变量也算进来。
    同文件的辅助函数会往下追三层——处理函数常把活儿交给 `_serialize_x`。
    """
    uses = uses if uses is not None else Uses()
    seen = seen if seen is not None else set()
    if func in seen or depth > 3:
        return uses
    seen.add(func)
    tree = _source_tree(func)
    if tree is None:
        return uses
    scope = dict(getattr(func, "__globals__", {}))

    for node in ast.walk(tree):                     # 函数体里的局部 import
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            base = node.module.split(".")[-1]
            module = sys.modules.get("app." + base)
            for alias in node.names:
                target = getattr(module, alias.name, None) if module else None
                if target is not None:
                    scope[alias.asname or alias.name] = target

    def take(obj, attr_of=None):
        if inspect.ismodule(obj) and obj.__name__ in SERVICES:
            return
        if inspect.isclass(obj) and obj in entity_classes:
            uses.entities.add(obj.__name__)
            return
        module = getattr(obj, "__module__", "")
        name = getattr(obj, "__name__", "")
        if module in SERVICES and not name.startswith("_"):
            short = module.split(".")[-1]
            if inspect.isclass(obj):
                uses.classes.add((short, name))
            elif inspect.isfunction(obj):
                uses.functions.add((short, name))
            return
        if (inspect.isfunction(obj) and module == func.__module__
                and obj is not func):
            analyze(obj, entity_classes, uses, seen, depth + 1)

    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)):
            owner = scope.get(node.value.id)
            if inspect.ismodule(owner) and owner.__name__ in SERVICES:
                take(getattr(owner, node.attr, None))
        elif isinstance(node, ast.Name):
            obj = scope.get(node.id)
            if obj is not None:
                take(obj)
    return uses


def public_members(module):
    """服务模块里**定义在本模块**的公开函数与类（import 进来的不算）。"""
    functions, classes = [], []
    for name, obj in vars(module).items():
        if name.startswith("_") or getattr(obj, "__module__", "") != module.__name__:
            continue
        if inspect.isfunction(obj):
            functions.append(name)
        elif inspect.isclass(obj):
            classes.append(name)
    order = lambda n: inspect.getsourcelines(getattr(module, n))[1]  # noqa: E731
    return sorted(functions, key=order), sorted(classes, key=order)


def signature(func, keep_self=False):
    try:
        params = list(inspect.signature(func).parameters.values())
    except (TypeError, ValueError):
        return "()"
    names = []
    for p in params:
        if p.name in ("self", "cls") and not keep_self:
            continue
        if p.kind is p.VAR_POSITIONAL:
            names.append("*" + p.name)
        elif p.kind is p.VAR_KEYWORD:
            names.append("**" + p.name)
        else:
            names.append(p.name)
    return "(%s)" % ", ".join(names)


def annotation_text(annotation):
    """类型注解写成类图里的类型名。`Optional[X]` 只写 X；`Literal[...]`
    写成取值的类型；`list[X]` 用 Mermaid 的泛型写法 `list~X~`。

    **不能看 `__name__`**：3.14 里 `Union`、`Literal` 也有 `__name__`，
    直接取会在图上写出一排「Union start_date」。"""
    import types
    import typing
    origin = typing.get_origin(annotation)
    args = [a for a in typing.get_args(annotation) if a is not type(None)]
    if origin in (typing.Union, types.UnionType):
        return "|".join(annotation_text(a) for a in args) or "None"
    if origin is typing.Literal:
        return type(args[0]).__name__ if args else "str"
    if origin in (list, set, tuple):
        return "%s~%s~" % (origin.__name__, annotation_text(args[0]) if args else "")
    if origin is dict:
        return "dict"
    return getattr(annotation, "__name__", None) or str(annotation).replace("typing.", "")


def dto_classes(endpoint):
    from pydantic import BaseModel
    found = []
    try:
        params = inspect.signature(endpoint).parameters.values()
    except (TypeError, ValueError):
        return found
    for p in params:
        ann = p.annotation
        if inspect.isclass(ann) and issubclass(ann, BaseModel):
            found.append(ann)
    return found


def dto_box(cls, lines, limit=10):
    own = [n for n in getattr(cls, "__annotations__", {}) if not n.startswith("_")]
    lines.append('    class %s["%s"] {' % (cls.__name__, cls.__name__))
    lines.append("        <<DTO>>")
    fields = getattr(cls, "model_fields", {})
    for name in own[:limit]:
        field = fields.get(name)
        ann = annotation_text(field.annotation) if field is not None else ""
        lines.append("        +%s %s" % (ann, name) if ann else "        +%s" % name)
    if len(own) > limit:
        lines.append("        +… 另 %d 项" % (len(own) - limit))
    lines.append("    }")


def entity_box(cls, table, lines):
    uniques = unique_columns(table)
    lines.append('    class %s["%s · %s"] {' % (cls.__name__, cls.__name__,
                                                cn_name(table.name)))
    lines.append("        <<entity>>")
    shown = 0
    for column in table.columns:
        marks = []
        if column.primary_key:
            marks.append("PK")
        if column.foreign_keys:
            marks.append("FK")
        if column.name in uniques and not column.primary_key:
            marks.append("UK")
        if marks or column.name in SHOWN_COLUMNS:
            suffix = (" «%s»" % ",".join(marks)) if marks else ""
            lines.append("        +%s %s%s" % (type_name(column), column.name, suffix))
            shown += 1
    rest = len(table.columns) - shown
    if rest:
        lines.append("        +… 另 %d 列（见数据库设计书）" % rest)
    for name, value in vars(cls).items():
        if name.startswith("_"):
            continue
        if isinstance(value, property):
            lines.append("        +/%s" % name)
        elif inspect.isfunction(value):
            lines.append("        +%s%s" % (name, signature(value)))
    lines.append("    }")


def module_diagram(module, routes, metadata, entity_classes):
    tables = {t.name: t for t in metadata.sorted_tables}
    own_tables = MODULES[module]
    by_table = {cls.__tablename__: cls for cls in entity_classes}
    # 从左往右：HTTP 层 → 服务 / DTO → 实体。自上而下排时 HTTP 层的
    # 十几个下游摊成一行，房源那张图九千像素宽，放进 Word 里字就没了。
    lines = ["classDiagram", "    direction LR"]

    mine = [(m, p, fn) for m, p, fn in routes if module_of_path(p) == module]
    router_uses = Uses()
    dtos = []
    for _m, _p, fn in mine:
        u = analyze(fn, entity_classes)
        router_uses.functions |= u.functions
        router_uses.classes |= u.classes
        router_uses.entities |= u.entities
        for dto in dto_classes(fn):
            if dto not in dtos:
                dtos.append(dto)

    # 1) HTTP 层
    api = "Api"
    lines.append('    class %s["%s · HTTP 接口（%d 个）"] {' % (api, module, len(mine)))
    lines.append("        <<router>>")
    for method, path, fn in mine:
        params = [d.__name__ for d in dto_classes(fn)]
        lines.append("        +%s(%s) %s %s" % (fn.__name__, ", ".join(params),
                                                method, safe(path)))
    lines.append("    }")

    # 2) 服务模块：主场全列，客场只列用到的
    services = {}
    for short, name in sorted(router_uses.functions):
        services.setdefault(short, set()).add(name)
    service_classes = set(router_uses.classes)
    for short, home in SERVICE_HOME.items():
        if home == module:
            mod = sys.modules.get("app." + short) or __import__(
                "app." + short, fromlist=["_"])
            functions, classes = public_members(mod)
            services.setdefault(short, set()).update(functions)
            service_classes |= {(short, c) for c in classes}

    service_uses = Uses()
    for short in sorted(services, key=lambda s: (SERVICE_HOME[s] != module, s)):
        mod = sys.modules["app." + short]
        names = [n for n in public_members(mod)[0] if n in services[short]]
        if not names:
            continue
        stereo = "service" if SERVICE_HOME[short] == module else "service · %s" % SERVICE_HOME[short]
        lines.append('    class m_%s["%s.py"] {' % (short, short))
        lines.append("        <<%s>>" % stereo)
        for name in names:
            func = getattr(mod, name)
            lines.append("        +%s%s" % (name, signature(func)))
            u = analyze(func, entity_classes)
            service_uses.entities |= {(short, e) for e in u.entities}
        lines.append("    }")
        lines.append("    %s ..> m_%s" % (api, short))

    class_ids = set()
    for short, name in sorted(service_classes):
        cls = getattr(sys.modules["app." + short], name)
        class_ids.add(name)
        lines.append('    class %s["%s（%s.py）"] {' % (name, name, short))
        methods = [n for n, v in vars(cls).items()
                   if inspect.isfunction(v) and (not n.startswith("_") or n == "__init__")]
        for n in methods:
            lines.append("        +%s%s" % (n, signature(getattr(cls, n))))
        lines.append("    }")
    for short, name in sorted(service_classes):
        cls = getattr(sys.modules["app." + short], name)
        for base in cls.__bases__:
            if base.__name__ in class_ids:
                lines.append("    %s <|-- %s" % (base.__name__, name))
        if (short, name) in router_uses.classes:
            lines.append("    %s ..> %s" % (api, name))
        elif short in services and not any(b.__name__ in class_ids for b in cls.__bases__):
            lines.append("    m_%s ..> %s" % (short, name))

    # 3) DTO
    for dto in dtos:
        dto_box(dto, lines)
        lines.append("    %s ..> %s" % (api, dto.__name__))
        for base in dto.__bases__:
            if base in dtos:
                lines.append("    %s <|-- %s" % (base.__name__, dto.__name__))

    # 4) 实体：本模块的表全列；外键指向的别的模块的表只画个名字
    shown_entities = set()
    for name in own_tables:
        cls = by_table[name]
        entity_box(cls, tables[name], lines)
        shown_entities.add(cls.__name__)
    stubs = []
    for child, col, parent, _pc, nullable, unique in fk_edges(metadata):
        if child not in own_tables or parent in own_tables:
            continue
        cls = by_table[parent]
        if cls.__name__ not in shown_entities and cls.__name__ not in stubs:
            stubs.append(cls.__name__)
            lines.append('    class %s["%s · %s（%s）"]' % (
                cls.__name__, cls.__name__, cn_name(parent),
                [m for m in MODULE_ORDER if parent in MODULES[m]][0]))
            lines.append("    <<entity>> %s" % cls.__name__)
    for child, col, parent, _pc, nullable, unique in fk_edges(metadata):
        if child not in own_tables:
            continue
        lines.append('    %s "%s" -- "%s" %s : %s' % (
            by_table[parent].__name__, "0..1" if nullable else "1",
            "0..1" if unique else "0..*", by_table[child].__name__, col))

    # 「HTTP 层 → 本模块实体」不画：每个接口都碰自己模块的表，画出来是
    # 满屏平行虚线，淹掉真正有信息量的「哪个服务改哪张表」。
    for short, entity in sorted(service_uses.entities):
        if entity in shown_entities and short in services:
            lines.append("    m_%s ..> %s" % (short, entity))
    return "\n".join(lines) + "\n"


# ============================================================== 智能体服务的类图

def parse(path):
    with io.open(path, encoding="utf-8") as f:
        return ast.parse(f.read())


def rel(path):
    return os.path.relpath(path, AGENT_APP).replace("\\", "/")


def func_sig(node, drop_self=True):
    args = [a.arg for a in node.args.posonlyargs + node.args.args]
    if drop_self and args and args[0] in ("self", "cls"):
        args = args[1:]
    if node.args.vararg:
        args.append("*" + node.args.vararg.arg)
    args += [a.arg for a in node.args.kwonlyargs]
    if node.args.kwarg:
        args.append("**" + node.args.kwarg.arg)
    return "(%s)" % ", ".join(args)


def top_functions(tree):
    return [n for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and not n.name.startswith("_")]


def app_imports(tree, here):
    """这个文件从 agent/app 里 import 了哪些模块（点分，相对 app）。"""
    here_pkg = os.path.dirname(rel(here)).replace("/", ".")
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                parts = here_pkg.split(".") if here_pkg else []
                parts = parts[:len(parts) - (node.level - 1)] if node.level > 1 else parts
                base = ".".join(p for p in parts + ([node.module] if node.module else []) if p)
            elif node.module == "app":                  # from app import fusion
                base = ""
            elif node.module and node.module.startswith("app."):
                base = node.module[4:]
            else:
                continue
            found.add(base)
            for alias in node.names:
                found.add("%s.%s" % (base, alias.name) if base else alias.name)
    return found


SUBAGENT_REQ = {
    "property_finder": "AI-01 找房", "contract": "AI-02 合同审阅",
    "support": "AI-03 智能客服", "repair": "AI-04 报修分析",
    "report": "AI-05 经营报告", "booking": "AI-06 预约",
    "recommend": "AI-07 推荐", "describe": "AI-08 房源描述",
    "viewing": "J-05 带看小结", "handoff": "AI-03 转人工",
}


def subagents():
    base = os.path.join(AGENT_APP, "agents")
    return sorted(d for d in os.listdir(base)
                  if os.path.isdir(os.path.join(base, d))
                  and d not in ("__pycache__", "toolbox"))


def tool_modules(agent):
    folder = os.path.join(AGENT_APP, "agents", agent, "tools")
    if not os.path.isdir(folder):
        return []
    return sorted(f[:-3] for f in os.listdir(folder)
                  if f.endswith(".py") and f != "__init__.py")


def catalog_counts():
    """toolbox 工具目录里有多少个工具、其中多少个是写操作（`none` 不算工具）。"""
    tree = parse(os.path.join(AGENT_APP, "agents", "toolbox", "catalog.py"))
    total = writes = 0
    for node in tree.body:
        if (isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "TOOLS" for t in node.targets)):
            for item in node.value.elts:
                if not isinstance(item, ast.Dict):
                    continue
                pairs = {k.value: v for k, v in zip(item.keys, item.values)
                         if isinstance(k, ast.Constant)}
                name = pairs.get("name")
                if isinstance(name, ast.Constant) and name.value == "none":
                    continue
                total += 1
                kind = pairs.get("kind")
                if isinstance(kind, ast.Constant) and kind.value == "write":
                    writes += 1
    return total, writes


def agent_routes():
    """智能体 main.py 里的路由：从装饰器读，**不导入**（两个服务都叫 `app`）。"""
    tree = parse(os.path.join(AGENT_APP, "main.py"))
    rows = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in node.decorator_list:
            if (isinstance(deco, ast.Call) and isinstance(deco.func, ast.Attribute)
                    and deco.func.attr in ("get", "post", "put", "patch", "delete")
                    and deco.args and isinstance(deco.args[0], ast.Constant)):
                body = [a.annotation.id for a in node.args.args
                        if isinstance(a.annotation, ast.Name)
                        and a.annotation.id.endswith("Request")]
                rows.append((deco.func.attr.upper(), deco.args[0].value,
                             node.name, body))
    rows = [r for r in rows if r[1] not in SKIP_PATHS]
    return sorted(rows, key=lambda r: (r[1], r[0]))


def agent_dispatch_diagram():
    main_tree = parse(os.path.join(AGENT_APP, "main.py"))
    main_imports = app_imports(main_tree, os.path.join(AGENT_APP, "main.py"))
    lines = ["classDiagram", "    direction LR"]
    routes = agent_routes()
    lines.append('    class AgentApi["智能体服务 · HTTP 接口（%d 个）"] {' % len(routes))
    lines.append("        <<router>>")
    for method, path, name, body in routes:
        lines.append("        +%s(%s) %s %s" % (name, ", ".join(body), method, safe(path)))
    lines.append("    }")

    boxes = {}                                      # 点分模块名 → 类 id
    sup = os.path.join(AGENT_APP, "supervisor.py")
    sup_tree = parse(sup)
    lines.append('    class supervisor["supervisor.py 总调度"] {')
    lines.append("        <<dispatcher>>")
    for fn in top_functions(sup_tree):
        lines.append("        +%s%s" % (fn.name, func_sig(fn)))
    lines.append("    }")
    boxes["supervisor"] = "supervisor"

    for mod in ("fusion", "memory"):
        path = os.path.join(AGENT_APP, mod + ".py")
        tree = parse(path)
        lines.append('    class %s["%s.py"] {' % (mod, mod))
        lines.append("        <<module>>")
        for fn in top_functions(tree):
            lines.append("        +%s%s" % (fn.name, func_sig(fn)))
        lines.append("    }")
        boxes[mod] = mod

    for mod in ("catalog", "planner", "runner"):
        path = os.path.join(AGENT_APP, "agents", "toolbox", mod + ".py")
        tree = parse(path)
        cid = "tb_" + mod
        lines.append('    class %s["toolbox/%s.py"] {' % (cid, mod))
        lines.append("        <<toolbox>>")
        if mod == "catalog":
            total, writes = catalog_counts()
            lines.append("        +TOOLS %d 个工具（写操作 %d 个）" % (total, writes))
        for fn in top_functions(tree):
            lines.append("        +%s%s" % (fn.name, func_sig(fn)))
        lines.append("    }")
        boxes["agents.toolbox.%s" % mod] = cid

    agent_trees = {}
    for agent in subagents():
        path = os.path.join(AGENT_APP, "agents", agent, "agent.py")
        tree = parse(path)
        agent_trees[agent] = (tree, path)
        cid = "ag_" + agent
        lines.append('    class %s["%s · %s"] {' % (cid, agent, SUBAGENT_REQ.get(agent, "")))
        lines.append("        <<agent>>")
        for fn in top_functions(tree):
            if fn.name == "run":
                lines.append("        +run%s" % func_sig(fn))
        for public in (c for c in tree.body if isinstance(c, ast.ClassDef)
                       and not c.name.startswith("_")):
            lines.append("        +%s «class»" % public.name)
        tools = tool_modules(agent)
        if tools:
            lines.append("        +tools %s" % ", ".join(tools))
        lines.append("    }")
        boxes["agents.%s" % agent] = cid
        boxes["agents.%s.agent" % agent] = cid

    def targets(imports):
        hit = set()
        for name in imports:
            for key, cid in boxes.items():
                if name == key or name.startswith(key + "."):
                    hit.add(cid)
        return hit

    edges = set()
    for cid in targets(main_imports):
        edges.add(("AgentApi", cid))
    edges |= {("supervisor", c) for c in targets(app_imports(sup_tree, sup))}
    for agent, (tree, path) in agent_trees.items():
        imports = set(app_imports(tree, path))
        for tool in tool_modules(agent):
            tpath = os.path.join(AGENT_APP, "agents", agent, "tools", tool + ".py")
            imports |= app_imports(parse(tpath), tpath)
        edges |= {("ag_" + agent, c) for c in targets(imports)}
    for mod in ("catalog", "planner", "runner"):
        path = os.path.join(AGENT_APP, "agents", "toolbox", mod + ".py")
        edges |= {("tb_" + mod, c) for c in targets(app_imports(parse(path), path))}
    for src, dst in sorted(edges):
        if src != dst:
            lines.append("    %s ..> %s" % (src, dst))
    lines.append('    class LLMProvider["LLMProvider（见基础设施图）"]')
    for agent, (tree, path) in agent_trees.items():
        text = io.open(path, encoding="utf-8").read()
        if "provider" in text:
            lines.append("    ag_%s ..> LLMProvider" % agent)
    return "\n".join(lines) + "\n"


INFRA_FILES = ["llm/base.py", "llm/budget.py", "llm/providers.py",
               "retrieval/index.py", "retrieval/bm25.py", "retrieval/embed.py",
               "backend.py", "trace.py", "config.py", "agents/support/agent.py"]

# 语法树看不出来的关联：构造参数传进来的依赖。**每一条都会校验两端的类
# 真的存在**，类改名了这里会报错，而不是画出一条悬空的线。
INFRA_LINKS = [
    ("DeadlineProvider", "o--", "LLMProvider", "inner"),
    ("KnowledgeIndex", "o--", "EmbeddingProvider", "embedder"),
    ("KnowledgeIndex", "o--", "VectorCache", "cache"),
    ("KnowledgeBase", "o--", "BackendClient", "backend"),
    ("KnowledgeBase", "o--", "Settings", "settings"),
]


def agent_infra_diagram():
    classes = {}
    for relpath in INFRA_FILES:
        path = os.path.join(AGENT_APP, *relpath.split("/"))
        for node in parse(path).body:
            if isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
                classes[node.name] = (node, relpath)
    lines = ["classDiagram", "    direction TB"]
    for name, (node, relpath) in classes.items():
        bases = [b.id for b in node.bases if isinstance(b, ast.Name)]
        exc = "Exception" in bases
        lines.append('    class %s["%s（%s）"] {' % (name, name, relpath))
        if exc:
            lines.append("        <<exception>>")
        elif any(isinstance(n, ast.FunctionDef) and n.name == "complete"
                 and n.body and isinstance(n.body[0], ast.Raise) for n in node.body):
            lines.append("        <<abstract>>")
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                    not item.name.startswith("_") or item.name == "__init__"):
                lines.append("        +%s%s" % (item.name, func_sig(item)))
        lines.append("    }")
    lines.append("    class Exception")
    for name, (node, _p) in classes.items():
        for base in node.bases:
            if isinstance(base, ast.Name) and (base.id in classes or base.id == "Exception"):
                lines.append("    %s <|-- %s" % (base.id, name))
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Assign) and isinstance(sub.value, ast.Call)
                    and isinstance(sub.value.func, ast.Name)
                    and sub.value.func.id in classes and sub.value.func.id != name):
                target = sub.targets[0]
                label = target.attr if isinstance(target, ast.Attribute) else ""
                lines.append("    %s *-- %s : %s" % (name, sub.value.func.id, label))
    for src, arrow, dst, label in INFRA_LINKS:
        if src not in classes or dst not in classes:
            raise SystemExit("INFRA_LINKS 里的 %s → %s 找不到对应的类，"
                             "代码里的类改名了？" % (src, dst))
        lines.append("    %s %s %s : %s" % (src, arrow, dst, label))
    seen, out = set(), []
    for line in lines:                  # 同一对类的同一种关系只画一条
        if " : " in line or "<|--" in line:
            if line in seen:
                continue
            seen.add(line)
        out.append(line)
    return "\n".join(out) + "\n"


# ============================================================== 系统结构图

def counts(routes, metadata):
    total, writes = catalog_counts()
    return {
        "server": len(routes),
        "agent": len(agent_routes()),
        "tables": len(metadata.tables),
        "fks": len(fk_edges(metadata)),
        "subagents": len(subagents()),
        "support_tools": len(tool_modules("support")),
        "catalog": total, "writes": writes,
    }


def web_versions():
    pkg = json.load(io.open(os.path.join(ROOT, "web", "package.json"), encoding="utf-8"))
    deps = dict(pkg.get("dependencies", {}), **pkg.get("devDependencies", {}))
    major = lambda k: re.sub(r"[^\d.]", "", deps.get(k, "")).split(".")[0]  # noqa: E731
    return major("react"), major("vite")


def system_diagram(n):
    react, vite = web_versions()
    modules = "、".join(MODULE_ORDER)
    return "\n".join([
        "flowchart TB",
        '    browser(["浏览器"])',
        '    subgraph web["前端站点 :5173（开发）"]',
        '        spa["React %s + Vite %s<br/>原生 History API 路由<br/>构建产物由业务后端托管"]' % (react, vite),
        "    end",
        '    subgraph server["业务后端 :8080 · FastAPI + SQLAlchemy 2.0"]',
        '        api["%d 个接口 · %d 张表 · 9 个功能模块<br/>鉴权、权限、状态机、金额规则的唯一出口"]' % (n["server"], n["tables"]),
        '        mods["%s"]' % modules,
        "        api --- mods",
        "    end",
        '    db[("SQLite（开发）/ MySQL 8（部署）<br/>%d 张表 · %d 个外键")]' % (n["tables"], n["fks"]),
        '    subgraph agent["智能体服务 :8000 · FastAPI"]',
        '        sup["Supervisor 总调度 · %d 个接口"]' % n["agent"],
        '        subs["%d 个子智能体<br/>客服 %d 个工具模块 · 工具目录 %d 个工具（写操作 %d 个）"]'
        % (n["subagents"], n["support_tools"], n["catalog"], n["writes"]),
        "        sup --> subs",
        "    end",
        '    llm[/"大模型服务（OpenAI 兼容协议）<br/>允许缺席：不可用时退回规则结论"/]',
        "    browser --> spa",
        '    spa -- "REST / JSON" --> api',
        "    api --> db",
        '    subs -- "HTTP（智能体没有数据库连接）" --> api',
        '    subs -. "HTTP，可缺席" .-> llm',
    ]) + "\n"


# ============================================================== 汇总

def build_all():
    app = load_server()
    metadata = load_metadata()
    from app.models import Base
    entity_classes = {m.class_ for m in Base.registry.mappers}
    routes = server_routes(app)
    n = counts(routes, metadata)

    diagrams = {"system": system_diagram(n)}
    for module in MODULE_ORDER:
        diagrams[MODULE_IDS[module]] = module_diagram(
            module, routes, metadata, entity_classes)
    diagrams["class_agent_dispatch"] = agent_dispatch_diagram()
    diagrams["class_agent_infra"] = agent_infra_diagram()
    diagrams["er_overview"] = er_diagram(metadata, columns=False)
    diagrams["er"] = er_diagram(metadata)
    return diagrams, n


def placements(n):
    """每份文档里放哪几张图、图题是什么。图号按模板的「章-序号」编。"""
    outline = [("system", "图 2-1　系统结构图")]
    for i, module in enumerate(MODULE_ORDER, 1):
        outline.append((MODULE_IDS[module], "图 3-%d　%s模块类图" % (i, module)))
    outline += [
        ("class_agent_dispatch", "图 3-10　智能体服务类图（一）：调度、子智能体与工具箱"),
        ("class_agent_infra", "图 3-11　智能体服务类图（二）：大模型、检索与后端客户端"),
        ("er_overview", "图 4-1　数据库 E-R 总览图（%d 张表、%d 个外键）"
         % (n["tables"], n["fks"])),
        ("er", "图 4-2　数据库 E-R 图（含主键、外键、唯一键列）"),
    ]
    return {
        OUTLINE: outline,
        DB_DOC: [("er", "图 2-1　实体关系图（由模型生成：%d 张表、%d 个外键）"
                  % (n["tables"], n["fks"]))],
    }


def block(name, source, caption):
    return "\n".join([BEGIN % name, "", "```mermaid", source.rstrip("\n"), "```",
                      "", "**%s**" % caption, "", END % name])


def apply_blocks(text, wanted, sources):
    """把文档里各图的标记段换成最新的。标记段不存在就报错——
    图放在哪一节是写文档的人定的，脚本不替人找位置。"""
    problems = []
    for name, caption in wanted:
        begin = re.search(r"<!-- DIAGRAM:%s:BEGIN[^>]*-->" % name, text)
        stop = text.find(END % name)
        if begin is None or stop < 0:
            problems.append("找不到「%s」的标记段（%s … %s）" % (name, BEGIN % name, END % name))
            continue
        text = text[:begin.start()] + block(name, sources[name], caption) \
            + text[stop + len(END % name):]
    return text, problems


def read_manifest():
    if not os.path.exists(MANIFEST):
        return {}
    return json.load(io.open(MANIFEST, encoding="utf-8"))


def find_mmdc():
    env = os.environ.get("MMDC")
    if env:
        return env
    local = os.path.join(ROOT, "scripts", "node_modules", ".bin",
                         "mmdc.cmd" if os.name == "nt" else "mmdc")
    if os.path.exists(local):
        return local
    return shutil.which("mmdc")


# mermaid-cli 12（及其依赖 puppeteer-core）在 package.json 里写的是
# `"node": ">=22.13.0"`。
MIN_NODE = (22, 13)


def node_problem():
    """渲染前先看 PATH 上的 node 够不够新。够就返回空串。

    **为什么要先查**：不查的话，Node 16 上的报错是

        ReferenceError: ReadableStream is not defined
            at .../puppeteer-core/lib/puppeteer/api/ScreenRecording.js

    看不出和 Node 版本有关系。更糟的是它发生在**已经改写了 .mmd 源之后**——
    工作区被留在「源改了、图没出、manifest 没动」的半截状态，
    接着 `--check` 报一串过期，很容易被当成代码的问题去查。
    2026-09-28 在一台只有 Node 16 的机器上正是这么绕了一圈。

    `mmdc.cmd` 用的是 PATH 上的第一个 node，所以这里查的也是它。
    """
    node = shutil.which("node")
    if not node:
        return "找不到 node。渲染要 Node ≥ %d.%d。" % MIN_NODE
    try:
        out = subprocess.run([node, "--version"], capture_output=True,
                             text=True, timeout=30).stdout.strip()
        major, minor = (int(x) for x in out.lstrip("v").split(".")[:2])
    except Exception:                                         # noqa: BLE001
        return "问不出 node 的版本（%s）。" % node
    if (major, minor) >= MIN_NODE:
        return ""
    return ("mermaid-cli 要 Node ≥ %d.%d，PATH 上的是 %s（%s）。\n"
            "  不必替换系统里那份，装一个便携版放到 PATH 最前面再跑即可，例如：\n"
            "    set PATH=C:\\Users\\<你>\\tools\\node-v24.21.0-win-x64;%%PATH%%\n"
            "  只想更新文档里的 Mermaid 块、不出图，可以加 --no-render。"
            % (MIN_NODE + (out, node)))


def find_browser():
    env = os.environ.get("PUPPETEER_EXECUTABLE_PATH")
    if env:
        return env
    for path in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                 r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                 r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                 "/usr/bin/google-chrome", "/usr/bin/chromium",
                 "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"):
        if os.path.exists(path):
            return path
    return None


MERMAID_CONFIG = {
    "theme": "neutral",
    "useMaxWidth": False,
    "themeVariables": {"fontFamily": "Microsoft YaHei, PingFang SC, sans-serif"},
    # useMaxWidth 默认为真：图会被缩到 800px 的视口里，再大的图渲出来
    # 都是 1568px 宽，字小到在 Word 里看不清。关掉，按自然尺寸出图。
    "flowchart": {"htmlLabels": True, "curve": "basis", "wrappingWidth": 420,
                  "useMaxWidth": False},
    "er": {"layoutDirection": "TB", "minEntityWidth": 90, "useMaxWidth": False},
    "class": {"hideEmptyMembersBox": True, "useMaxWidth": False},
}


def scale_of(name):
    """PNG 相对自然尺寸的放大倍数。E-R 图本身就上万像素，不再放大。"""
    if name.startswith("er"):
        return "1"
    return "3" if name == "system" else "1.5"


def render_key(name, source):
    """PNG 的指纹：源 + 渲染配置。只记源的话，改了主题或缩放不会触发重渲。"""
    return sha(source + json.dumps(MERMAID_CONFIG, sort_keys=True) + scale_of(name))


def render(name, source_path, png_path):
    mmdc = find_mmdc()
    if not mmdc:
        raise SystemExit("找不到 mmdc。装一个：cd scripts && npm install"
                         "（设 PUPPETEER_SKIP_DOWNLOAD=1 可免下 Chromium），"
                         "或设环境变量 MMDC 指向它；只想更新文档可加 --no-render")
    tmp = tempfile.mkdtemp()
    config = os.path.join(tmp, "mermaid.json")
    json.dump(MERMAID_CONFIG, io.open(config, "w", encoding="utf-8"))
    base = [mmdc, "-i", source_path, "-c", config, "-b", "white", "-q"]
    browser = find_browser()
    if browser:
        pptr = os.path.join(tmp, "puppeteer.json")
        json.dump({"executablePath": browser, "args": ["--no-sandbox"]},
                  io.open(pptr, "w", encoding="utf-8"))
        base += ["-p", pptr]

    def run(extra):
        done = subprocess.run(base + extra, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              shell=(os.name == "nt"))
        if done.returncode != 0:
            shutil.rmtree(tmp, ignore_errors=True)
            raise SystemExit("渲染 %s 失败：\n%s"
                             % (name, (done.stderr or done.stdout)[-1500:]))

    # 两遍：先出 SVG 读自然尺寸，再按尺寸出 PNG。
    # 类图的渲染器不认 useMaxWidth，直接出 PNG 会被压进 800px 的视口，
    # 再大的图都是 1568px 宽；给 --size 才能按自然尺寸出。
    svg = os.path.join(tmp, "out.svg")
    run(["-o", svg])
    head = io.open(svg, encoding="utf-8").read(4000)
    box = re.search(r'viewBox="[-\d.]+ [-\d.]+ ([\d.]+) ([\d.]+)"', head)
    size = max(float(box.group(1)), float(box.group(2))) if box else 2000
    run(["-o", png_path, "--size", str(int(size * float(scale_of(name))))])
    shutil.rmtree(tmp, ignore_errors=True)
    if not os.path.exists(png_path):
        raise SystemExit("渲染 %s 失败：没有产出 PNG" % name)


def main():
    parser = argparse.ArgumentParser(description="从代码生成概要设计的图")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--no-render", action="store_true")
    args = parser.parse_args()

    diagrams, n = build_all()
    manifest = read_manifest()
    docs = placements(n)

    if args.check:
        problems = []
        for name, source in diagrams.items():
            path = os.path.join(FIG_DIR, name + ".mmd")
            if not os.path.exists(path) or io.open(path, encoding="utf-8").read() != source:
                problems.append("%s.mmd 与代码对不上" % name)
            if manifest.get(name) != render_key(name, source):
                problems.append("%s.png 不是由当前的源渲染的" % name)
            if not os.path.exists(os.path.join(FIG_DIR, name + ".png")):
                problems.append("%s.png 不存在" % name)
        for doc, wanted in docs.items():
            text = io.open(doc, encoding="utf-8").read()
            fresh, missing = apply_blocks(text, wanted, diagrams)
            problems += ["%s：%s" % (os.path.basename(doc), m) for m in missing]
            if fresh != text:
                problems.append("%s 里的图过期了" % os.path.basename(doc))
        if problems:
            for p in problems:
                print("  ! %s" % p)
            print("重新生成：python scripts/gen_diagrams.py")
            return 1
        print("%d 张图与代码一致。" % len(diagrams))
        return 0

    # 要出图的话，先确认 Node 够新，再动任何文件——见 node_problem()
    to_render = [name for name, source in diagrams.items()
                 if manifest.get(name) != render_key(name, source)
                 or not os.path.exists(os.path.join(FIG_DIR, name + ".png"))]
    if to_render and not args.no_render:
        problem = node_problem()
        if problem:
            print("有 %d 张图要重新渲染（%s），但渲染环境不满足：\n  %s"
                  % (len(to_render), "、".join(to_render), problem))
            print("\n**什么文件都没有改动**，工作区保持原样。")
            return 2

    os.makedirs(FIG_DIR, exist_ok=True)
    rendered = []
    for name, source in diagrams.items():
        path = os.path.join(FIG_DIR, name + ".mmd")
        png = os.path.join(FIG_DIR, name + ".png")
        io.open(path, "w", encoding="utf-8", newline="\n").write(source)
        if args.no_render:
            continue
        if manifest.get(name) != render_key(name, source) or not os.path.exists(png):
            render(name, path, png)
            manifest[name] = render_key(name, source)
            rendered.append(name)
    for stale in set(manifest) - set(diagrams):
        manifest.pop(stale)
    json.dump(manifest, io.open(MANIFEST, "w", encoding="utf-8", newline="\n"),
              ensure_ascii=False, indent=2, sort_keys=True)

    for doc, wanted in docs.items():
        text = io.open(doc, encoding="utf-8").read()
        fresh, missing = apply_blocks(text, wanted, diagrams)
        if missing:
            for m in missing:
                print("  ! %s：%s" % (os.path.basename(doc), m))
            return 1
        if fresh != text:
            io.open(doc, "w", encoding="utf-8", newline="\n").write(fresh)
    print("%d 张图：重新渲染 %d 张（%s）；两份文档里的图已更新。"
          % (len(diagrams), len(rendered), "、".join(rendered) or "无"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
