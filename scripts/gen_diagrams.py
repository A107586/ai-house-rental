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
| 系统结构图（分层架构图，HTML） | 各模块的接口数与表数、子智能体、工具数等全部现数；各模块写的业务见 `MODULE_BUSINESS` |

模块归属用 `scripts/db_meta.py` 的 `MODULES` 与 `PATH_MODULES`——
和接口清单、数据库设计书是同一份，不另维护。

## 产物

- `docs/图/<名字>.mmd` —— Mermaid 源，GitHub 上直接能看
  （系统结构图例外，源是 `system.html`，用浏览器截图，原因见该节注释）
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
ARCH_DOC = os.path.join(ROOT, "docs", "技术架构说明.md")
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
    for _m, _p, fn in mine:
        u = analyze(fn, entity_classes)
        router_uses.functions |= u.functions
        router_uses.classes |= u.classes
        router_uses.entities |= u.entities

    # 1) HTTP 层。只写处理函数、请求体类型与 HTTP 方法，**不写路径**：
    #    路径在各模块的接口说明表与附录 A 里都有；写进来的话最长一行七十多个
    #    字符，整张图被撑宽，印到 A4 上字只有两磅。
    api = "Api"
    lines.append('    class %s["%s · HTTP 接口（%d 个）"] {' % (api, module, len(mine)))
    lines.append("        <<router>>")
    for method, _path, fn in mine:
        params = [d.__name__ for d in dto_classes(fn)]
        lines.append("        +%s(%s) %s" % (fn.__name__, ", ".join(params), method))
    lines.append("    }")

    # 2) 服务。本模块的服务（主场）各一框、列全部公开函数；
    #    **借用别的模块的服务合成一框**，只列本模块真正调到的函数——
    #    原先每个借用的模块单独一框，房源那张图右边竖着叠了七个框。
    services = {}
    for short, name in sorted(router_uses.functions):
        services.setdefault(short, set()).add(name)
    service_classes = set(router_uses.classes)
    for short, home_module in SERVICE_HOME.items():
        if home_module == module:
            mod = sys.modules.get("app." + short) or __import__(
                "app." + short, fromlist=["_"])
            functions, classes = public_members(mod)
            services.setdefault(short, set()).update(functions)
            service_classes |= {(short, c) for c in classes}

    home = sorted(s for s in services if SERVICE_HOME[s] == module)
    borrowed = sorted(s for s in services if SERVICE_HOME[s] != module)
    service_uses = Uses()
    for short in home:
        mod = sys.modules["app." + short]
        names = [n for n in public_members(mod)[0] if n in services[short]]
        if not names:
            continue
        lines.append('    class m_%s["%s.py"] {' % (short, short))
        lines.append("        <<service>>")
        for name in names:
            func = getattr(mod, name)
            lines.append("        +%s%s" % (name, signature(func)))
            u = analyze(func, entity_classes)
            service_uses.entities |= {(short, e) for e in u.entities}
        lines.append("    }")
        lines.append("    %s ..> m_%s" % (api, short))

    borrowed_classes = sorted((s, c) for s, c in service_classes
                              if SERVICE_HOME[s] != module)
    if borrowed or borrowed_classes:
        lines.append('    class m_shared["借用其他模块的服务"] {')
        lines.append("        <<service>>")
        for short in borrowed:
            mod = sys.modules["app." + short]
            for name in [n for n in public_members(mod)[0] if n in services[short]]:
                lines.append("        +%s.%s()" % (short, name))
        for short, name in borrowed_classes:
            lines.append("        +%s.%s «class»" % (short, name))
        lines.append("    }")
        lines.append("    %s ..> m_shared" % api)

    class_ids = set()
    home_classes = sorted((s, c) for s, c in service_classes if SERVICE_HOME[s] == module)
    for short, name in home_classes:
        cls = getattr(sys.modules["app." + short], name)
        class_ids.add(name)
        lines.append('    class %s["%s（%s.py）"] {' % (name, name, short))
        methods = [n for n, v in vars(cls).items()
                   if inspect.isfunction(v) and (not n.startswith("_") or n == "__init__")]
        for n in methods:
            lines.append("        +%s%s" % (n, signature(getattr(cls, n))))
        lines.append("    }")
    for short, name in home_classes:
        cls = getattr(sys.modules["app." + short], name)
        for base in cls.__bases__:
            if base.__name__ in class_ids:
                lines.append("    %s <|-- %s" % (base.__name__, name))
        if (short, name) in router_uses.classes:
            lines.append("    %s ..> %s" % (api, name))
        elif not any(b.__name__ in class_ids for b in cls.__bases__):
            lines.append("    m_%s ..> %s" % (short, name))

    # 3) 请求 DTO 不再单独画框：类型名已经写在 HTTP 层每个函数的括号里，
    #    字段见 5.3 与代码。六个 DTO 框竖着叠，是房源那张图高度的一半。

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
        if entity in shown_entities and short in home:
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


# ---------------------------------------------------------------- 系统结构图
#
# **这一张不用 Mermaid。** 评审的意见是原先那张图「太笼统」——九个业务模块
# 一句话带过，看不出系统做什么。要把每个模块、每个业务都摆出来，就得是
# 一张分层的架构图，而 Mermaid 的两种布局都给不了：flowchart 的自动布局
# 会把同一层的框折成两列、把交易链的顺序打乱；block-beta 能排网格，
# 却强制每一行一样高、嵌套块的标题跨不了列。
#
# 所以这张写成 HTML（CSS 网格排版），再用同一个本机浏览器截成 PNG。
# 框里的**业务是人写的归纳**（下面几张表），接口数、表数、工具数等
# **全部是生成时现数的**——和其他图一样，代码一变 `--check` 就报过期。

# 每个业务模块写的「有哪些业务」，与《概要设计说明书》第 3 章
# 各模块「功能定义」表一一对应——改了那张表记得改这里。
MODULE_BUSINESS = {
    "房源": ["发布与审核上架", "多条件检索 · 地图找房", "房源核验 · 费用透明",
             "相册与图片说明", "保存搜索 · 上新提醒", "举报处理 · 租金建议"],
    "看房预约": ["提交预约 · 时段冲突检测", "确认 / 拒绝 / 取消",
                 "委托期间转受托中介", "结果站内通知"],
    "合同与账单": ["签约 · AI 合同审阅", "整期账单排期", "缴费 · 支付回调（幂等）",
                   "退租与押金结算", "续租 · 催缴提醒"],
    "报修与维修": ["报修（AI 判分类与紧急度）", "指派与进度通知", "结案登记：人 · 物 · 工时",
                   "维修人员与物料档案", "维修统计 · 超时判定"],
    "账号与权限": ["注册登录 · 令牌吊销", "一号多身份", "升级房东 / 中介审核",
                   "实名与脱敏", "找回密码"],
    "互动与消息": ["收藏 · 浏览足迹", "租后评价（三个对象）", "站内消息 · 邮件提醒",
                   "平台公告", "租客与房东 / 中介会话"],
    "中介": ["经纪人资质审核", "房源委托（双向确认）", "带看记录与异议",
             "成交计佣", "中介工作台 · 目录"],
    "智能客服与知识": ["FAQ 知识库维护", "问答日志与反馈", "转人工工单", "多轮对话记录"],
    "平台治理": ["管理后台 · 经营统计", "操作日志", "内容审核复核队列", "系统配置"],
}
# 主干交易链与支撑业务的分法同第 3 章：主干是
# 房源 → 预约 → 合同 → 账单 → 缴费 → 评价，其余都挂在这条链上。
CHAIN = ["房源", "看房预约", "合同与账单", "报修与维修"]
SUPPORT = ["账号与权限", "互动与消息", "中介", "智能客服与知识", "平台治理"]

ROLES = [
    ("租客", ["找房 · 约看房", "签约 · 缴费", "报修 · 评价", "问智能助手"]),
    ("房东", ["发布与管理房源", "处理预约 · 签约收租", "维修派单 · 催缴", "委托中介"]),
    ("中介", ["资质认证", "受托房源带看", "跟进租客 · 会话", "佣金结算"]),
    ("管理员", ["房源与资质审核", "内容治理 · 操作日志", "系统配置", "客服知识 · 转人工"]),
]

# 前端的视图。前端是哈希路由（`web/src/useHashRoute.js`），视图名即 `#/名字`。
PAGES = [
    ("首页 / 找房列表", "#/ · #/list", ["条件筛选", "地图找房", "猜你喜欢"]),
    ("房源详情", "#/house", ["相册 · 费用明细", "评价 · 房东信息", "约看 · 咨询"]),
    ("工作台（按角色）", "#/work", ["个人中心", "房东 / 中介工作台", "管理后台"]),
    ("智能助手浮窗", "各页通用", ["多轮对话", "确认卡片", "拍照报修"]),
    ("登录注册", "#/auth", ["注册 · 登录", "找回密码"]),
]

COMMON_SERVICES = [
    ("鉴权与权限", "auth · deps · permissions"),
    ("内容审核", "moderation"),
    ("操作日志 · 限流", "oplog · ratelimit"),
    ("消息通知", "leasing.notify · notifier"),
    ("系统配置", "config_store"),
    ("文件上传", "uploads"),
    ("支付网关（模拟）", "gateway"),
    ("地理编码", "map_service"),
]

SUBAGENT_SHORT = {
    "property_finder": "找房", "contract": "合同审阅", "support": "智能客服",
    "repair": "报修分析", "report": "经营报告", "booking": "预约",
    "recommend": "推荐", "describe": "房源描述", "viewing": "带看小结",
    "handoff": "转人工",
}

SYSTEM_CSS = """
* { box-sizing: border-box; }
body { margin: 0; background: #fff; color: #1d2733;
       font-family: "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", sans-serif; }
.wrap { width: 1880px; padding: 20px 24px 24px; }
.row { display: grid; grid-template-columns: 64px 1fr; gap: 12px; }
.tag { background: #1f4e79; color: #fff; border-radius: 6px; font-weight: 700;
       font-size: 19px; letter-spacing: 8px; writing-mode: vertical-rl;
       display: flex; align-items: center; justify-content: center; }
.band { border: 2px solid #a9bfd6; border-radius: 8px; background: #f3f7fb; padding: 10px 12px 12px; }
.title { font-weight: 700; font-size: 17px; color: #1f4e79; margin: 0 0 8px; }
.title small { font-weight: 400; color: #5a6b7d; font-size: 14px; margin-left: 8px; }
.grid { display: grid; gap: 10px; }
.card { background: #fff; border: 1.5px solid #86a3c3; border-radius: 6px; padding: 8px 10px; }
.card h4 { margin: 0 0 4px; font-size: 16px; }
.card h4 .meta { font-weight: 400; color: #5a6b7d; font-size: 13px; margin-left: 6px; }
.card .route { color: #7a8a9b; font-size: 12.5px; font-style: italic; margin-bottom: 3px; }
.card ul { margin: 0; padding-left: 17px; font-size: 14px; line-height: 1.5; }
.chain { display: grid; grid-template-columns: 1fr 26px 1fr 26px 1fr 26px 1fr; align-items: stretch; }
.chain .to { display: flex; align-items: center; justify-content: center;
             color: #1f4e79; font-size: 24px; font-weight: 700; }
.chain .card { border-color: #1f4e79; border-width: 2px; }
.sub { font-weight: 700; font-size: 14.5px; color: #34506e; margin: 10px 0 6px; }
.chip { background: #fff; border: 1.5px solid #9fb3c8; border-radius: 5px; padding: 6px 8px;
        font-size: 14px; text-align: center; }
.chip i { display: block; color: #6b7c8e; font-size: 12px; font-style: normal; margin-top: 1px; }
.chip b { display: block; font-size: 15px; margin-bottom: 2px; }
.arrows > div:last-child { justify-content: center; white-space: nowrap; }
.mid { display: grid; grid-template-columns: 1fr 56px 440px; }
.hlink { display: flex; flex-direction: column; align-items: center; justify-content: center;
         color: #1f4e79; font-size: 13px; text-align: center; line-height: 1.35; }
.hlink b { font-size: 26px; line-height: 1; }
.arrows { display: grid; grid-template-columns: 64px 1fr 56px 440px; gap: 0 12px; margin: 4px 0; }
.arrows > div { display: flex; align-items: center; justify-content: center; gap: 8px;
                color: #1f4e79; font-size: 14px; height: 36px; }
.arrows b { font-size: 22px; }
.dash { border: 2px dashed #9aa9b8; background: #fafbfc; }
.agent .card, .agent .chip { border-color: #7d9a7a; }
.agent.band { background: #f3f8f2; border-color: #a9c4a6; }
.agent .title { color: #2f5d2b; }
.note { color: #6b7c8e; font-size: 12.5px; margin-top: 6px; }
"""


def _esc(text):
    import html
    return html.escape(text, quote=False)


def _card(title, items, meta="", route="", extra_cls=""):
    parts = ['<div class="card %s">' % extra_cls,
             "<h4>%s%s</h4>" % (_esc(title),
                                '<span class="meta">%s</span>' % _esc(meta) if meta else "")]
    if route:
        parts.append('<div class="route">%s</div>' % _esc(route))
    parts.append("<ul>%s</ul>" % "".join("<li>%s</li>" % _esc(i) for i in items))
    parts.append("</div>")
    return "".join(parts)


def system_diagram(n, per_module):
    """分层的系统结构图（HTML）：用户 → 表示 → 服务（业务后端 | 智能体）→ 数据与外部。

    每个业务模块一个框，框里写有哪些业务、几个接口、几张表。
    """
    missing = [m for m in MODULE_ORDER if m not in MODULE_BUSINESS]
    if sorted(CHAIN + SUPPORT) != sorted(MODULE_ORDER):
        missing.append("CHAIN + SUPPORT 与 MODULE_ORDER 不一致")
    missing += ["子智能体 %s" % a for a in subagents() if a not in SUBAGENT_SHORT]
    if missing:
        raise SystemExit("系统结构图缺登记：%s（补到 gen_diagrams.py 的"
                         " MODULE_BUSINESS / CHAIN / SUPPORT / SUBAGENT_SHORT）"
                         % "、".join(missing))
    react, vite = web_versions()

    def module_card(module, cls=""):
        return _card(module, MODULE_BUSINESS[module],
                     meta="%d 接口 · %d 表" % (per_module.get(module, 0),
                                               len(MODULES[module])),
                     extra_cls=cls)

    roles = "".join(_card(r, items) for r, items in ROLES)
    pages = "".join(_card(p, items, route=route) for p, route, items in PAGES)
    chain = '<div class="to">→</div>'.join(module_card(m) for m in CHAIN)
    support = "".join(module_card(m) for m in SUPPORT)
    common = "".join('<div class="chip">%s<i>%s</i></div>' % (_esc(a), _esc(b))
                     for a, b in COMMON_SERVICES)
    def req_order(agent):               # 按需求编号排：AI-01 … AI-08，J-05 殿后
        code = SUBAGENT_REQ.get(agent, "").split(" ")[0]
        prefix, _, number = code.partition("-")
        return (prefix != "AI", int(number) if number.isdigit() else 99, agent)

    subs = "".join('<div class="chip">%s<i>%s</i></div>'
                   % (_esc(SUBAGENT_SHORT[a]), _esc(SUBAGENT_REQ.get(a, "").split(" ")[0]))
                   for a in sorted(subagents(), key=req_order))

    def base_chip(title, *lines):
        return '<div class="chip"><b>%s</b>%s</div>' % (
            _esc(title), "".join("<i>%s</i>" % _esc(x) for x in lines))

    html = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<!-- 本文件由 scripts/gen_diagrams.py 生成，不要手改 -->
<title>系统结构图</title><style>%(css)s</style></head>
<body><div class="wrap">

<div class="row"><div class="tag">用户层</div><div class="band">
  <div class="title">四种角色<small>同一个账号可兼租客与房东；中介接单须资质过审</small></div>
  <div class="grid" style="grid-template-columns: repeat(4, 1fr)">%(roles)s</div>
</div></div>

<div class="arrows"><div></div><div><b>▼</b>浏览器访问</div><div></div><div></div></div>

<div class="row"><div class="tag">表示层</div><div class="band">
  <div class="title">前端站点 :5173<small>React %(react)s + Vite %(vite)s · 哈希路由 · 构建产物由业务后端托管</small></div>
  <div class="grid" style="grid-template-columns: repeat(5, 1fr)">%(pages)s</div>
</div></div>

<div class="arrows"><div></div>
  <div><b>▼</b>REST /api：页面上的全部业务请求</div><div></div>
  <div><b>▼</b>/agent：对话与 AI 功能</div></div>

<div class="row"><div class="tag">服务层</div><div class="mid">
  <div class="band">
    <div class="title">业务后端 :8080<small>FastAPI + SQLAlchemy 2.0 · %(server)d 个接口 · 9 个业务模块 · 鉴权、状态机、金额规则的唯一出口</small></div>
    <div class="sub">核心交易链</div>
    <div class="chain">%(chain)s</div>
    <div class="sub">支撑业务</div>
    <div class="grid" style="grid-template-columns: repeat(5, 1fr)">%(support)s</div>
    <div class="sub">公共服务（各模块共用）</div>
    <div class="grid" style="grid-template-columns: repeat(8, 1fr)">%(common)s</div>
  </div>
  <div class="hlink"><b>◀</b>HTTP<br/>取数<br/>执行已<br/>确认的<br/>写操作</div>
  <div class="band agent">
    <div class="title">智能体服务 :8000<small>%(agent)d 个接口 · 无数据库连接</small></div>
    %(sup)s
    <div class="sub">%(subagents)d 个子智能体</div>
    <div class="grid" style="grid-template-columns: repeat(3, 1fr)">%(subs)s</div>
    <div class="sub">共用底座</div>
    <div class="grid" style="grid-template-columns: repeat(3, 1fr)">%(tools)s%(rag)s%(llmgw)s</div>
  </div>
</div></div>

<div class="arrows"><div></div>
  <div><b>▼</b>读写（SQLAlchemy） · 地理编码与邮件（可缺席）</div><div></div>
  <div><b>▼</b>OpenAI 兼容协议（可缺席）</div></div>

<div class="row" style="min-height: 150px"><div class="tag" style="letter-spacing: 1px; font-size: 17px">数据与外部</div><div class="mid">
  <div class="grid" style="grid-template-columns: 1.3fr 1fr 1fr 1fr">
    %(db)s%(files)s%(amap)s%(smtp)s
  </div>
  <div></div>
  %(llm)s
</div></div>

</div></body></html>
""" % {
        "css": SYSTEM_CSS, "react": react, "vite": vite,
        "roles": roles, "pages": pages, "chain": chain, "support": support,
        "common": common, "subs": subs,
        "server": n["server"], "agent": n["agent"], "subagents": n["subagents"],
        "sup": _card("Supervisor 总调度", ["判意图、转交子智能体", "规则与大模型融合 · 护栏"]),
        "tools": base_chip("工具箱", "%d 个工具" % n["catalog"],
                           "写操作 %d 个" % n["writes"], "先确认后执行"),
        "rag": base_chip("知识检索", "BM25 + 向量融合", "可答性闸门"),
        "llmgw": base_chip("大模型接入层", "单轮时限 · 熔断", "不可用时降级为规则"),
        "db": _card("关系数据库", ["SQLite（开发）/ MySQL 8（部署）",
                                   "%d 张表 · %d 个外键" % (n["tables"], n["fks"])]),
        "files": _card("文件存储 static/", ["房源相册", "报修照片"]),
        "amap": _card("高德地图（外部）", ["地理编码", "不可达时退回离线示意图"], extra_cls="dash"),
        "smtp": _card("邮件 SMTP（外部）", ["账单 · 合同 · 工单提醒", "未配置时跳过"], extra_cls="dash"),
        "llm": _card("大模型服务（外部）", ["OpenAI 兼容协议", "允许缺席：全部测试在无模型下跑"],
                     extra_cls="dash"),
    }
    return html


# ---------------------------------------------------------------- 模块结构图
#
# 模板每个模块要两张图：「模块结构」（通过图形描述）与「类图」。
# 类图讲代码怎么组织；模块结构图讲**这个模块由哪些功能组成**——
# 左边是模块，右边一条条功能点，每条带上它的说明。
#
# **功能点不在这里另写一份**：直接读《概要设计说明书》第 3 章各模块
# 「x.1 功能定义」那张表。图和表是同一份数据，改了表，`--check` 就提示图过期。

STRUCT_CSS = """
* { box-sizing: border-box; }
body { margin: 0; background: #fff; color: #1d2733;
       font-family: "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", sans-serif; }
.wrap { width: 1180px; padding: 16px; }
.tree { display: flex; align-items: center; }
.root { width: 230px; flex: none; background: #1f4e79; color: #fff; border-radius: 8px;
        padding: 16px 14px; text-align: center; }
.root b { display: block; font-size: 21px; letter-spacing: 1px; }
.root span { display: block; font-size: 14px; color: #d6e3f0; margin-top: 6px; line-height: 1.45; }
.link { width: 30px; flex: none; border-top: 2.5px solid #1f4e79; }
.branches { flex: 1; display: flex; flex-direction: column; padding-left: 30px; }
.br { position: relative; padding: 5px 0; }
.br::before { content: ""; position: absolute; left: -30px; top: 50%; width: 30px;
              border-top: 2.5px solid #1f4e79; }
.br::after { content: ""; position: absolute; left: -30px; top: 0; bottom: 0;
             border-left: 2.5px solid #1f4e79; }
.br:first-child::after { top: 50%; }
.br:last-child::after { bottom: 50%; }
.leaf { display: flex; background: #f3f7fb; border: 1.5px solid #86a3c3; border-radius: 6px; }
.leaf b { flex: none; width: 190px; padding: 9px 12px; font-size: 17px; color: #1f4e79;
          border-right: 1.5px solid #c3d3e4; display: flex; align-items: center; }
.leaf span { padding: 9px 12px; font-size: 15.5px; line-height: 1.5; }
"""


def _plain(markdown):
    """表格单元格里的 Markdown 去掉：粗体、行内代码。"""
    return re.sub(r"`([^`]*)`", r"\1", markdown.replace("**", "")).strip()


def function_tables():
    """读第 3 章各模块的「功能定义」表：{节号: (模块标题, [(功能点, 说明), ...])}。

    3.1～3.9 的表是「序号 | 功能点 | 详细说明」；3.10 智能体服务的表是
    「序号 | 子智能体 | 职责 | 对应需求」，功能点取「职责」，说明写子智能体名与需求。
    """
    text = io.open(OUTLINE, encoding="utf-8").read()
    sections = {}
    heads = list(re.finditer(r"^### 3\.(\d+) (.+)$", text, flags=re.M))
    for idx, head in enumerate(heads):
        end = heads[idx + 1].start() if idx + 1 < len(heads) else text.index("\n## 4 ")
        body = text[head.start():end]
        number = int(head.group(1))
        anchor = re.search(r"^#### 3\.%d\.1 功能定义\s*$" % number, body, flags=re.M)
        if not anchor:
            raise SystemExit("3.%d 节找不到「3.%d.1 功能定义」" % (number, number))
        rows = []
        for line in body[anchor.end():].split("\n"):
            line = line.strip()
            if not line.startswith("|"):
                if rows:
                    break
                continue
            rows.append([c.strip() for c in line.strip("|").split("|")])
        header, data = rows[0], rows[2:]
        if header[1] == "子智能体":
            points = [(_plain(r[2]), "%s · %s" % (_plain(r[1]), _plain(r[3]))) for r in data]
        else:
            points = [(_plain(r[1]), _plain(r[2])) for r in data]
        sections[number] = (head.group(2).strip(), points)
    return sections


def struct_diagram(title, points, meta):
    leaves = "".join('<div class="br"><div class="leaf"><b>%s</b><span>%s</span></div></div>'
                     % (_esc(name), _esc(desc)) for name, desc in points)
    return """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<!-- 本文件由 scripts/gen_diagrams.py 从《概要设计说明书》的功能定义表生成，不要手改 -->
<title>%(title)s</title><style>%(css)s</style></head>
<body><div class="wrap"><div class="tree">
  <div class="root"><b>%(title)s</b>%(meta)s</div>
  <div class="link"></div>
  <div class="branches">%(leaves)s</div>
</div></div></body></html>
""" % {"title": _esc(title), "css": STRUCT_CSS, "leaves": leaves,
       "meta": "".join(_meta_line(m) for m in meta)}


def _meta_line(text):
    """需求编号一个个包成不折行的块——否则「K-07」会在连字符处断成两行。"""
    if not text.startswith("需求 "):
        return "<span>%s</span>" % _esc(text)
    codes = text[3:].split()
    return "<span>需求 %s</span>" % " ".join(
        '<i style="font-style:normal;white-space:nowrap">%s</i>' % _esc(c) for c in codes)


def struct_diagrams(n, per_module):
    from db_meta import REQUIREMENTS
    tables = function_tables()
    out = {}
    for i, module in enumerate(MODULE_ORDER, 1):
        title, points = tables[i]
        codes = sorted({c for t in MODULES[module] for c in REQUIREMENTS.get(t, [])},
                       key=lambda c: (c.split("-")[0], int(c.split("-")[1])))
        meta = ["%d 个接口 · %d 张表" % (per_module.get(module, 0), len(MODULES[module])),
                "%d 个功能点" % len(points),
                "需求 " + " ".join(codes)]
        name = MODULE_IDS[module].replace("class_", "struct_")
        out[name] = struct_diagram(re.sub(r"（.*?）", "", title), points, meta)
    title, points = tables[len(MODULE_ORDER) + 1]
    out["struct_agent"] = struct_diagram(
        re.sub(r"（.*?）", "", title), points,
        ["%d 个接口 · 无数据库连接" % n["agent"], "%d 个子智能体" % n["subagents"],
         "需求 AI-01～AI-09 N-06～N-08"])
    return out


# ============================================================== 汇总

def build_all():
    app = load_server()
    metadata = load_metadata()
    from app.models import Base
    entity_classes = {m.class_ for m in Base.registry.mappers}
    routes = server_routes(app)
    n = counts(routes, metadata)
    per_module = {}
    for _method, path, _fn in routes:
        module = module_of_path(path)
        per_module[module] = per_module.get(module, 0) + 1

    diagrams = {"system": system_diagram(n, per_module)}
    structs = struct_diagrams(n, per_module)
    for module in MODULE_ORDER:
        struct = MODULE_IDS[module].replace("class_", "struct_")
        diagrams[struct] = structs[struct]
        diagrams[MODULE_IDS[module]] = module_diagram(
            module, routes, metadata, entity_classes)
    diagrams["struct_agent"] = structs["struct_agent"]
    diagrams["class_agent_dispatch"] = agent_dispatch_diagram()
    diagrams["class_agent_infra"] = agent_infra_diagram()
    diagrams["er_overview"] = er_diagram(metadata, columns=False)
    diagrams["er"] = er_diagram(metadata)
    return diagrams, n


def placements(n):
    """每份文档里放哪几张图、图题是什么。图号按模板的「章-序号」编。"""
    # 与模板一样，每个模块先「模块结构图」再「类图」，第 3 章按出现顺序连续编号
    outline = [("system", "图 2-1　系统结构图")]
    fig = 0
    for module in MODULE_ORDER:
        fig += 1
        outline.append((MODULE_IDS[module].replace("class_", "struct_"),
                        "图 3-%d　%s模块结构图" % (fig, module)))
        fig += 1
        outline.append((MODULE_IDS[module], "图 3-%d　%s模块类图" % (fig, module)))
    outline += [
        ("struct_agent", "图 3-%d　智能体服务模块结构图" % (fig + 1)),
        ("class_agent_dispatch", "图 3-%d　智能体服务类图（一）：调度、子智能体与工具箱" % (fig + 2)),
        ("class_agent_infra", "图 3-%d　智能体服务类图（二）：大模型、检索与后端客户端" % (fig + 3)),
        ("er_overview", "图 4-1　数据库 E-R 总览图（%d 张表、%d 个外键）"
         % (n["tables"], n["fks"])),
        ("er", "图 4-2　数据库 E-R 图（含主键、外键、唯一键列）"),
    ]
    return {
        OUTLINE: outline,
        DB_DOC: [("er", "图 2-1　实体关系图（由模型生成：%d 张表、%d 个外键）"
                  % (n["tables"], n["fks"]))],
        # 原先是一张手画的 ASCII 草图，调用方向画错了（只画了浏览器 → 业务后端）
        ARCH_DOC: [("system", "图 1　系统结构图（与《概要设计说明书》图 2-1 同源）")],
    }


# 这几张的源是 HTML 而不是 Mermaid（原因见「系统结构图」「模块结构图」两节）
HTML_DIAGRAMS = {"system", "struct_account", "struct_house", "struct_appointment",
                 "struct_contract", "struct_repair", "struct_interaction",
                 "struct_broker", "struct_governance", "struct_support",
                 "struct_agent"}


def source_ext(name):
    return ".html" if name in HTML_DIAGRAMS else ".mmd"


def block(name, source, caption, doc):
    if name in HTML_DIAGRAMS:
        # 文档里贴 PNG；相对路径按文档所在目录算
        rel = os.path.relpath(os.path.join(FIG_DIR, name + ".png"),
                              os.path.dirname(doc)).replace("\\", "/")
        body = ["![%s](%s)" % (caption.split("　")[-1], rel)]
    else:
        body = ["```mermaid", source.rstrip("\n"), "```"]
    return "\n".join([BEGIN % name, ""] + body + ["", "**%s**" % caption, "", END % name])


def apply_blocks(text, wanted, sources, doc):
    """把文档里各图的标记段换成最新的。标记段不存在就报错——
    图放在哪一节是写文档的人定的，脚本不替人找位置。"""
    problems = []
    for name, caption in wanted:
        begin = re.search(r"<!-- DIAGRAM:%s:BEGIN[^>]*-->" % name, text)
        stop = text.find(END % name)
        if begin is None or stop < 0:
            problems.append("找不到「%s」的标记段（%s … %s）" % (name, BEGIN % name, END % name))
            continue
        text = text[:begin.start()] + block(name, sources[name], caption, doc) \
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
    return "2" if name == "system" else "1.5"          # system 是浏览器的设备像素比


def render_key(name, source):
    """PNG 的指纹：源 + 渲染配置。只记源的话，改了主题或缩放不会触发重渲。"""
    return sha(source + json.dumps(MERMAID_CONFIG, sort_keys=True) + scale_of(name))


def render_html(name, source_path, png_path):
    """HTML 源的图：本机浏览器无头截图，再裁掉四周的空白。

    窗口高度给得很宽裕，截出来底下是一大片白——裁边比「先量页面高度
    再截」少一次往返，而且量高度要跑脚本，无头截图模式跑不了。"""
    from PIL import Image, ImageChops
    browser = find_browser()
    if not browser:
        raise SystemExit("渲染 %s 要用本机的 Edge 或 Chrome，没找到；"
                         "可设环境变量 PUPPETEER_EXECUTABLE_PATH 指向它" % name)
    tmp = tempfile.mkdtemp()
    shot = os.path.join(tmp, "shot.png")
    url = "file:///" + os.path.abspath(source_path).replace("\\", "/")
    subprocess.run([browser, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                    "--force-device-scale-factor=%s" % scale_of(name),
                    "--window-size=1940,3200", "--screenshot=%s" % shot, url],
                   capture_output=True, timeout=120)
    if not os.path.exists(shot):
        shutil.rmtree(tmp, ignore_errors=True)
        raise SystemExit("渲染 %s 失败：浏览器没有产出截图" % name)
    with Image.open(shot) as image:
        image = image.convert("RGB")
        blank = Image.new("RGB", image.size, (255, 255, 255))
        box = ImageChops.difference(image, blank).getbbox()
        pad = 16
        if box:
            box = (max(box[0] - pad, 0), max(box[1] - pad, 0),
                   min(box[2] + pad, image.width), min(box[3] + pad, image.height))
            image = image.crop(box)
        image.save(png_path, optimize=True)
    shutil.rmtree(tmp, ignore_errors=True)


def render(name, source_path, png_path):
    if name in HTML_DIAGRAMS:
        return render_html(name, source_path, png_path)
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
            path = os.path.join(FIG_DIR, name + source_ext(name))
            if not os.path.exists(path) or io.open(path, encoding="utf-8").read() != source:
                problems.append("%s%s 与代码对不上" % (name, source_ext(name)))
            if manifest.get(name) != render_key(name, source):
                problems.append("%s.png 不是由当前的源渲染的" % name)
            if not os.path.exists(os.path.join(FIG_DIR, name + ".png")):
                problems.append("%s.png 不存在" % name)
        for doc, wanted in docs.items():
            text = io.open(doc, encoding="utf-8").read()
            fresh, missing = apply_blocks(text, wanted, diagrams, doc)
            problems +=["%s：%s" % (os.path.basename(doc), m) for m in missing]
            if fresh != text:
                problems.append("%s 里的图过期了" % os.path.basename(doc))
        if problems:
            for p in problems:
                print("  ! %s" % p)
            print("重新生成：python scripts/gen_diagrams.py")
            return 1
        print("%d 张图与代码一致。" % len(diagrams))
        return 0

    os.makedirs(FIG_DIR, exist_ok=True)
    rendered = []
    for name, source in diagrams.items():
        path = os.path.join(FIG_DIR, name + source_ext(name))
        stale_src = os.path.join(FIG_DIR, name + (".mmd" if name in HTML_DIAGRAMS else ".html"))
        if os.path.exists(stale_src):            # 换了源格式，旧的那份删掉
            os.remove(stale_src)
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
        fresh, missing = apply_blocks(text, wanted, diagrams, doc)
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
