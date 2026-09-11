# -*- coding: utf-8 -*-
"""把抽出来的文案渲染成一份**可以直接改**的清单。

## 表格要怎么排才真的能用

按「界面 → 用途」两层分组，而不是按文件名排。理由：改文案的人是
**对着屏幕**改的——他想的是「登录页那句副标题太长」，
不是「AuthPage.jsx 第 175 行」。行号照给，但它是给我改的时候用的，
不是给他找的时候用的。

每行带一个 `#序号`。回写脚本认的是这个序号，不是文本匹配——
同一句话在四个地方出现时，文本匹配分不清他改的是哪一处；
而他要是顺手把「原文」那一栏也动了，文本匹配就直接失效了。

## 三类东西分开放

- **能随便改的**：绝大多数。
- **受约束的**：像「整租」「中山区」这种既显示在界面上、
  又是后端筛选取值的，改了筛选当场失效。单独一节，标红说明。
- **带变量的**：`共 ${n} 套` 这种。可以改，但 `${...}` 必须原样保留，
  少一个大括号就是白屏。
"""

import io
import json
import re
from collections import OrderedDict

# 界面的排列顺序。**按用户走到它的先后**，不按字母——
# 改文案的人多半从首页开始往下捋。
ORDER = [
    "全站骨架（顶栏、首页、列表页、页脚）", "首页大图轮播", "首页分栏", "公告栏",
    "筛选与排序", "已生效条件", "房源卡片", "房源详情",
    "地图找房（离线区域图）", "地图找房（高德）", "房源对比",
    "登录 / 注册 / 找回密码", "AuthDialog", "ForgotPasswordForm",
    "AI 助手对话", "助手思考中", "与房东咨询", "SupportPanel",
    "个人中心", "账号设置", "写评价", "合同卡片", "合同详情",
    "签约", "退租", "举报", "图片上传", "BecomeLandlord",
    "工作台外壳", "房东工作台", "委托中介", "工单结案",
    "中介工作台", "AgentRoster", "AgentQueue",
    "管理后台", "RoleRequestPanel", "FlagPanel", "LogPanel",
    "ConfigPanel", "ReportPanel", "HouseReportPanel", "RepairDataPanel",
    "LandlordRoster", "SavedSearchPanel", "RecommendStrip", "VerifyBadge",
    "RecordDetail", "RatioCell", "ChatAvatar", "HousePhoto",
    "接口层（网络错误提示）", "ErrorBoundary", "main",
]

# 用途在一个界面内部的排列顺序：**先排改了最显眼的**
PURPOSE_ORDER = [
    "页面主标题", "标题", "区块标题", "小标题", "选项／页签名", "按钮文案",
    "输入框占位", "表单项名称", "下拉选项", "字段名", "表头",
    "错误提示", "成功提示", "二次确认弹窗", "空态提示",
    "说明文字", "辅助说明", "表单说明", "段落说明", "导航项说明",
    "状态徽标", "标签", "可删条件", "悬停提示",
    "无障碍标签（读屏用，界面上看不见）", "界面文字",
]


def cell(text):
    """让一段文字能安全地待在表格里。"""
    return (text.replace("|", "\\|")
                .replace("\n", " ⏎ ")
                .replace("\r", ""))


def main():
    rows = json.load(io.open("copy_rows.json", encoding="utf-8"))
    for i, r in enumerate(rows, start=1):
        r["id"] = i

    constrained = [r for r in rows if r["purpose"].startswith("选项取值")]
    variable = [r for r in rows
                if "${" in r["text"] and r not in constrained]
    plain = [r for r in rows if r not in constrained and r not in variable]

    by_screen = OrderedDict()
    for r in plain:
        by_screen.setdefault(r["screen"], []).append(r)

    out = []
    w = out.append

    w("# 前端文案清单 · 2026-09-12\n")
    w("> 全站面向用户的中文文案，共 **%d 条**（去重后），"
      "覆盖 %d 个界面。\n" % (len(rows), len(by_screen) + 2))
    w("> 注释不在其中——这份表只收**用户看得见的字**。\n")

    w("\n## 怎么用\n")
    w("在「改为」一栏里填新文案，**不用管其他栏**。填完把文件给我，"
      "我按 `#序号` 写回代码。\n")
    w("\n三条注意：\n")
    w("\n1. **`#序号` 不要改**，也不要调整行的顺序——回写靠它定位。"
      "同一句话在几处出现时，「位置」一栏会列全，改一次全部生效；"
      "只想改其中一处的话在「改为」里注明。\n")
    w("2. **`${...}` 必须原样保留。** 那是要被真实数字替换掉的坑位，"
      "少一个大括号，那一屏会白掉。\n")
    w("3. 最后一节「不要改」里的取值同时被后端拿去筛选和入库，"
      "改了功能当场失效——真要改得连数据库一起改，先跟我说。\n")

    w("\n> 关于句号：界面上的按钮、标签、页签、占位符**不带句号**，"
      "成段的说明和对话内容保留标点。这一条我按这个标准过了一遍，"
      "有漏的直接在表里改。\n")

    w("\n---\n")

    screens = [s for s in ORDER if s in by_screen]
    screens += [s for s in by_screen if s not in ORDER]

    for screen in screens:
        items = by_screen[screen]
        w("\n## %s\n" % screen)
        w("\n共 %d 条。\n" % len(items))

        groups = OrderedDict()
        for r in items:
            groups.setdefault(r["purpose"], []).append(r)
        keys = [p for p in PURPOSE_ORDER if p in groups]
        keys += [p for p in groups if p not in PURPOSE_ORDER]

        for purpose in keys:
            w("\n### %s\n" % purpose)
            w("\n| # | 原文 | 位置 | 改为 |")
            w("|--:|---|---|---|")
            for r in groups[purpose]:
                w("| %d | %s | %s | |"
                  % (r["id"], cell(r["text"]), cell("、".join(r["lines"]))))

    if variable:
        w("\n---\n")
        w("\n## 带变量的句子\n")
        w("\n这些句子里的 `${...}` 会在运行时被换成真实的数字或名字。"
          "**改文字可以，`${...}` 必须一个字符不差地留着**——"
          "少一个大括号，那一屏会白掉。\n")
        w("\n| # | 原文 | 界面 | 位置 | 改为 |")
        w("|--:|---|---|---|---|")
        for r in sorted(variable, key=lambda x: (x["screen"], x["id"])):
            w("| %d | `%s` | %s | %s | |"
              % (r["id"], cell(r["text"]), r["screen"],
                 cell("、".join(r["lines"]))))

    if constrained:
        w("\n---\n")
        w("\n## 不要改：这些同时是数据\n")
        w("\n下面这些字**既显示在界面上，也是后端用来筛选和入库的取值**。"
          "比如「整租」不只是一个词，它是 `rent_type` 这一列里存的内容，"
          "也是筛选按钮传给接口的参数。\n")
        w("\n改了会发生什么：筛选按钮点下去一条也搜不出来，"
          "而页面不会报错——**它只是安静地返回空列表**，"
          "看上去像是「真的没有这类房源」。\n")
        w("\n真要改的话得连同数据库里的存量数据一起改，那是一次迁移，"
          "不是一次文案调整。需要的话跟我说。\n")
        # **每张表都要有「改为」这一列，哪怕这一节不该改。**
        #
        # 回写脚本认的是「最后一格」。这一节原来只有四列，于是「位置」
        # 顶到了最后一格的位置上，被当成了填写内容——dry-run 里冒出
        # 74 条根本没人填的「修改」，差点把 `App.jsx:828` 这种字符串
        # 写进界面。
        w("\n| # | 取值 | 用在哪 | 位置 | 改为（要改先问我） |")
        w("|--:|---|---|---|---|")
        for r in sorted(constrained, key=lambda x: (x["screen"], x["id"])):
            w("| %d | %s | %s | %s | |"
              % (r["id"], cell(r["text"]), r["screen"],
                 cell("、".join(r["lines"][:3]))))

    text = "\n".join(out) + "\n"
    path = "C:/Competition1/rental/docs/前端文案清单_2026-09-12.md"
    io.open(path, "w", encoding="utf-8", newline="").write(text)
    print("写入 %s" % path)
    print("  可改 %d 条 / 带变量 %d 条 / 不要改 %d 条"
          % (len(plain), len(variable), len(constrained)))
    print("  %d 行" % len(out))


if __name__ == "__main__":
    main()
