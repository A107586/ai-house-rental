# 基于 AI 智能体的房屋租赁系统

大连理工大学 2026 年秋季学期实习实训课程设计 · 选题三 · 5 人组

在传统房屋租赁管理系统的基础上，引入 **AI 智能体（Agent）** 层，实现自然语言找房、
合同智能审阅、智能客服与报修工单自动分派，打造信息透明、操作简便的在线租赁平台。

---

## 目录结构

```
├── server/          业务后端（Python + FastAPI + SQLAlchemy）      ← 当前主线
├── agent/           AI 智能体服务（Python + FastAPI）              ← 当前主线
├── web/             【待建】React 前端
├── docs/            设计文档、评审材料、测试说明
├── start-all.ps1    一键启动两个服务
├── back/ front/     早期 SpringBoot + Vue 方案，暂留作参考
├── db/              早期 MySQL 建库脚本与初始化工具
└── LICENSE          MIT
```

## 技术栈

| 层 | 技术 |
|---|---|
| 前端 | React · Axios |
| 后端 | Python · FastAPI · SQLAlchemy |
| 智能体 | Python · FastAPI · DeepSeek API |
| 数据库 | 开发用 SQLite，部署用 MySQL |

前后端分离，智能体作为独立微服务通过 HTTP 与业务后端通信。

## 核心业务模块

房屋信息 · 户主管理 · 租客管理 · 看房申请 · 租赁合同 · 收租信息 · 收租统计 · 报修信息 · 维修数据 · 公告管理

## AI 智能体功能（本项目自研部分）

- **自然语言找房** — 「中山区两室一厅，3000 以内，地铁 10 分钟」→ 解析为结构化查询
- **租赁合同智能审阅** — 抽取关键条款，标注对租客不利的条目
- **智能客服** — RAG 检索房源库与常见问答
- **报修工单自动分派** — 从文字描述判断故障类型与紧急程度，路由给对应维修工
- **数据分析报告** — 租金走势与房源情况自动生成报告

## 关键时间节点（2026）

| 日期 | 事项 |
|---|---|
| 9/8 | 需求分析评审 |
| 9/11 | 原型评审 |
| 9/24 | 数据库评审 + 概要设计评审 |
| **10/16** | **代码终版提交** |
| 10/19–10/23 | 黑盒测试与 bug 修复 |
| 10/26–10/30 | 答辩、文档与演示录屏 |

## 快速开始

```
pip install fastapi uvicorn pydantic sqlalchemy
cd server && python seed.py
powershell -ExecutionPolicy Bypass -File .\start-all.ps1
```

| 服务 | 接口文档 |
|---|---|
| 业务后端 | <http://localhost:8080/docs> |
| 智能体服务 | <http://localhost:8000/docs> |

**怎么测试见 [docs/测试说明.md](docs/测试说明.md)。**

## 当前进度

| 模块 | 状态 |
|---|---|
| 业务后端：房源、预约、登录 | ✅ 可用，29 个测试 |
| AI-01 自然语言找房 | ✅ 可用 |
| AI-06 智能预约看房 | ✅ 可用 |
| AI-02/03/04/05 | ⬜ 待开发 |
| React 前端 | ⬜ 待开发 |

共 **121 个自动化测试**，全部不依赖网络、数据库与大模型。

## 致谢 / Attribution

本项目的 CRUD 基础框架改造自开源项目
[zongjixiaoai66/HouseRentalSystem](https://github.com/zongjixiaoai66/HouseRentalSystem)，
原项目以 MIT 协议发布，`LICENSE` 文件与原版权声明已按协议要求保留。
原项目说明与功能文档存放于 `docs/upstream/`。

AI 智能体层、以及在此基础上的改造与扩展为本组自研。
