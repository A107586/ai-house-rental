# -*- coding: utf-8 -*-
"""把前端所有面向用户的中文文案抽出来，附上位置与用途。

## 为什么要写个扫描器而不是 grep

grep 会把**注释也抓进来**。这个项目的注释写得很密，而且几乎全是中文，
grep 出来的结果里十句有七句是写给我们自己看的，混在一起没法用。

还有两个坑，正则绕不过去：

- `'https://…'` 里的 `//` 不是注释的开头，按行删 `//` 之后会把字符串
  切成两半，剩下半截再被当成新的字符串开头，后面整片文件都跑偏；
- 模板串 `` `共 ${n} 套` `` 里可以嵌任意表达式，里面还能再套字符串。

所以这里写了个小状态机：逐字符走，知道自己此刻在代码里、在哪种引号里、
还是在注释里。慢一点，但结果可信。

## 输出什么

一份 Markdown，按「界面」分组，每条给：**在哪儿、现在写的什么、干什么用的、
改成什么（留空给人填）**。最后那一列是这份文件存在的理由——
填完之后可以用 apply_copy.py 原样写回代码。
"""

import io
import json
import os
import re
import sys

SRC = "C:/Competition1/rental/web/src"
CJK = re.compile(r"[\u4e00-\u9fff]")

# 这些文件里的中文不是给用户看的，或者改了会坏事
SKIP_FILES = {"useHashRoute.js"}

# 这些字符串是**代码用的键值**，不是文案。改了会直接坏功能。
#
# 判断标准不是「看起来像不像文案」，而是「它有没有被拿去比较或索引」——
# 比如 '整租' 既显示在界面上，又是后端筛选的取值，改了筛选就失效。
# 这类一律标成「受约束」，让人知道不能随便动。
CONSTRAINED = {
    "整租", "合租", "公寓", "中山区", "西岗区", "沙河口区", "甘井子区",
    "旅顺口区", "金州区", "一室", "一室一厅", "两室一厅", "两室两厅",
    "三室一厅", "三室两厅", "朝南", "朝东", "朝西", "朝北", "东南",
    "西南", "东北", "西北", "南北通透", "电梯", "地铁", "近学校",
    "近医院", "有阳台", "可做饭", "有家具", "可养宠物", "采光好",
    "精装", "简装", "毛坯", "集中供暖", "自采暖", "低楼层", "中楼层",
    "高楼层", "紧急", "一般", "不急",
}


def scan(text):
    """逐字符走一遍，吐出 (行号, 类型, 内容)。

    类型：`str` 是引号里的字面量，`jsx` 是标签之间的正文。
    注释整段丢掉。
    """
    out = []
    i, line, n = 0, 1, len(text)
    while i < n:
        ch = text[i]
        if ch == "\n":
            line += 1
            i += 1
            continue

        # ---- 注释 ----
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                if text[i] == "\n":
                    line += 1
                i += 1
            i += 2
            continue

        # ---- 字符串 ----
        if ch in "'\"`":
            quote, start_line, start_pos = ch, line, i
            i += 1
            buf = []
            depth = 0          # 模板串里 ${...} 的嵌套层数
            while i < n:
                c = text[i]
                if c == "\\":
                    buf.append(text[i:i + 2])
                    i += 2
                    continue
                if c == "\n":
                    line += 1
                if quote == "`" and c == "$" and i + 1 < n and text[i + 1] == "{":
                    depth += 1
                    buf.append("${")
                    i += 2
                    continue
                if depth and c == "}":
                    depth -= 1
                    buf.append("}")
                    i += 1
                    continue
                if c == quote and depth == 0:
                    i += 1
                    break
                buf.append(c)
                i += 1
            value = "".join(buf)
            if CJK.search(value):
                # start 是引号的位置：判定用途要看它**前面**是什么
                out.append((start_line, "str", value, start_pos))
            continue

        i += 1

    # ---- JSX 正文：标签之间那些没加引号的字 ----
    #
    # 单独扫一遍。上面那个状态机看不见它们——它们既不在引号里，
    # 也不是注释，就是裸着的字。
    stripped = _strip_comments(text)
    for m in re.finditer(r">([^<>{}]*[\u4e00-\u9fff][^<>{}]*)<", stripped):
        value = " ".join(m.group(1).split())
        if value:
            # 行号要数到**文字本身**的位置，不是那个 `>` 的位置。
            # `>` 常常待在上一行的末尾（JSX 习惯换行写），
            # 按 m.start() 数会少一行——回写时按行号去找就找不着。
            out.append((stripped[:m.start(1)].count("\n") + 1, "jsx", value,
                        m.start()))
    return out


def _strip_comments(text):
    """把注释替换成等长空白，行号才不会错位。"""
    res, i, n = [], 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                res.append(" ")
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                res.append("\n" if text[i] == "\n" else " ")
                i += 1
            res.append("  ")
            i += 2
            continue
        if ch in "'\"`":
            quote = ch
            res.append(ch)
            i += 1
            while i < n:
                if text[i] == "\\":
                    res.append(text[i:i + 2])
                    i += 2
                    continue
                res.append(text[i])
                if text[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        res.append(ch)
        i += 1
    return "".join(res)


# 用途判定。按**上下文**猜，不是按内容猜——
# 「提交」两个字在按钮上和在说明里是两种东西。
PURPOSE_RULES = [
    (re.compile(r"placeholder\s*=\s*$"), "输入框占位"),
    (re.compile(r"aria-label\s*=\s*$"), "无障碍标签（读屏用，界面上看不见）"),
    (re.compile(r"title\s*=\s*$"), "悬停提示"),
    (re.compile(r"(setError|setTop)\s*\(\s*$"), "错误提示"),
    (re.compile(r"(setOk|setDone|setMsg)\s*\(\s*$"), "成功提示"),
    (re.compile(r"confirm\s*\(\s*$"), "二次确认弹窗"),
    (re.compile(r"label\s*[:=]\s*$"), "选项／页签名"),
    (re.compile(r"hint\s*[:=]\s*$"), "辅助说明"),
    (re.compile(r"(desc|note)\s*[:=]\s*$"), "说明文字"),
]

TAG_PURPOSE = [
    ("<button", "按钮文案"),
    ("<h1", "页面主标题"), ("<h2", "标题"), ("<h3", "区块标题"),
    ("<h4", "小标题"), ("<label", "表单项名称"), ("<option", "下拉选项"),
    ("<th", "表头"), ("<dt", "字段名"), ("<summary", "折叠标题"),
]

CLASS_PURPOSE = [
    ("empty", "空态提示"), ("desc", "说明文字"), ("muted-note", "辅助说明"),
    ("section-title", "区块标题"), ("auth-sub", "副标题"),
    ("reply failed", "错误提示"), ("reply created", "成功提示"),
    ("fld-hint", "表单说明"), ("wb-nav-hint", "导航项说明"),
    ("pill", "状态徽标"), ("tag", "标签"), ("chip", "可删条件"),
]


def purpose_of(text_before, line_text, value):
    if value in CONSTRAINED:
        return "选项取值（**改了会影响筛选与入库，慎动**）"
    tail = text_before[-60:]
    for pattern, label in PURPOSE_RULES:
        if pattern.search(tail):
            return label
    for cls, label in CLASS_PURPOSE:
        if cls in line_text:
            return label
    for tag, label in TAG_PURPOSE:
        if tag in line_text:
            return label
    if "${" in value or "{" in value:
        return "带变量的句子"
    if len(value) > 30:
        return "段落说明"
    return "界面文字"


SCREEN = {
    "App.jsx": "全站骨架（顶栏、首页、列表页、页脚）",
    "AuthPage.jsx": "登录 / 注册 / 找回密码",
    "HeroBanner.jsx": "首页大图轮播",
    "HomeSections.jsx": "首页分栏",
    "NoticeBar.jsx": "公告栏",
    "FilterBar.jsx": "筛选与排序",
    "ActiveFilters.jsx": "已生效条件",
    "HouseCard.jsx": "房源卡片",
    "HouseDrawer.jsx": "房源详情",
    "MapFinder.jsx": "地图找房（离线区域图）",
    "AmapFinder.jsx": "地图找房（高德）",
    "MyPanel.jsx": "个人中心",
    "ProfileForm.jsx": "账号设置",
    "Workbench.jsx": "工作台外壳",
    "LandlordPanel.jsx": "房东工作台",
    "AgentPanel.jsx": "中介工作台",
    "AdminPanel.jsx": "管理后台",
    "ChatPanel.jsx": "AI 助手对话",
    "ChatWithLandlord.jsx": "与房东咨询",
    "ReviewForm.jsx": "写评价",
    "ContractCard.jsx": "合同卡片",
    "ContractDetail.jsx": "合同详情",
    "DelegationPanel.jsx": "委托中介",
    "ImageUploader.jsx": "图片上传",
    "ReportDialog.jsx": "举报",
    "SignContractDialog.jsx": "签约",
    "EndLeaseDialog.jsx": "退租",
    "CloseRepairDialog.jsx": "工单结案",
    "CompareBar.jsx": "房源对比",
    "Thinking.jsx": "助手思考中",
    "api.js": "接口层（网络错误提示）",
}


def main():
    rows = []
    for root, _, files in os.walk(SRC):
        for name in sorted(files):
            if not name.endswith((".jsx", ".js")) or name in SKIP_FILES:
                continue
            path = os.path.join(root, name)
            text = io.open(path, encoding="utf-8").read()
            lines = text.splitlines()
            for line_no, kind, value, pos in scan(text):
                line_text = lines[line_no - 1] if line_no <= len(lines) else ""
                # **紧挨着字符串前面**的那一段，不是整行——
                # 一行里有三个字符串时，整行上下文会让三个都判成同一类
                before = text[max(0, pos - 200):pos]
                rows.append({
                    "file": name,
                    "screen": SCREEN.get(name, name.replace(".jsx", "")),
                    "line": line_no,
                    "text": value,
                    "purpose": purpose_of(before, line_text, value),
                })

    # 同一句话在多处出现时合并成一条，但把出现位置都列出来——
    # 改的时候要知道动一处会影响几个地方
    merged = {}
    for r in rows:
        key = (r["screen"], r["text"])
        if key in merged:
            merged[key]["lines"].append("%s:%d" % (r["file"], r["line"]))
        else:
            r["lines"] = ["%s:%d" % (r["file"], r["line"])]
            merged[key] = r

    out = sorted(merged.values(), key=lambda r: (r["screen"], r["lines"][0]))
    io.open("copy_rows.json", "w", encoding="utf-8").write(
        json.dumps(out, ensure_ascii=False, indent=1))
    print("共 %d 条（去重前 %d 条），覆盖 %d 个界面"
          % (len(out), len(rows), len({r["screen"] for r in out})))


if __name__ == "__main__":
    main()
