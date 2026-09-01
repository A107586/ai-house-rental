# 智能体服务（agent）

房屋租赁系统的 AI 智能体层，Python + FastAPI 独立微服务，
通过 HTTP 与 SpringBoot 业务后端通信。

## 核心设计：规则优先，大模型兜底

```
用户输入
   ↓
Supervisor 意图分类 ──► 规则命中且置信度 ≥ 0.5 ──► 直接路由（零延迟、零 Token）
   │                                                        │
   └─ 置信度不足 ──► 大模型分类 ──► 失败则回退规则结论 ◄─────┘
   ↓
子智能体（房源 / 预约 / 合同 / 客服 / 报修）
   ↓
工具（parse_query、search_properties …）
   ↓
业务后端接口 ──► MySQL
```

**为什么这样设计：**

1. **省 Token** — 中文找房、报修这类表达高度模式化，规则解析既快又准。
   实测内置的 10 条样例语句全部由规则直接解决，一次大模型都没调。
2. **可降级** — 没有 API Key、大模型超时、厂商故障，服务照常工作
   （对应非功能需求 N-07）。
3. **可测试** — 每个工具都是纯函数，脱离大模型就能写单元测试。
   目前 **92 个单元测试，全部不依赖网络、数据库和大模型**。

## 目录结构

```
agent/
├── app/
│   ├── main.py              FastAPI 入口，对外接口
│   ├── config.py            配置，密钥只从环境变量读
│   ├── supervisor.py        总调度：意图分类与路由
│   ├── backend.py           业务后端 HTTP 客户端
│   ├── cn_number.py         中文数字解析（「两千五」→ 2500）
│   ├── llm/
│   │   ├── base.py          LLMProvider 抽象 + JSON 容错提取
│   │   └── providers.py     OpenAI 兼容协议实现
│   └── agents/
│       ├── property_finder/
│       │   ├── agent.py     房源智能体编排
│       │   └── tools/
│       │       ├── parse_query.py        自然语言 → 筛选条件
│       │       └── search_properties.py  条件 → 后端查询
│       └── booking/
│           ├── agent.py     预约智能体编排
│           └── tools/
│               ├── parse_datetime.py     中文相对时间 →「这周六下午两点」
│               └── slots.py              冲突检测与可用时段推荐
├── tests/                   92 个单元测试
├── demo.py                  命令行演示，无需任何外部依赖
└── requirements.txt
```

## 快速开始

### 1. 只想看解析效果（零依赖）

```
python demo.py
python demo.py "中山区两室一厅，租金三千以内"
```

不需要装任何第三方包，不需要数据库，不需要大模型。

### 2. 跑测试

```
python -m unittest discover -s tests -v
```

同样零依赖。**改完代码务必先跑一遍再提交。**

### 3. 启动服务

```
pip install -r requirements.txt
cp .env.example .env        # Windows: copy .env.example .env
uvicorn app.main:app --reload --port 8000
```

访问 <http://localhost:8000/docs> 查看自动生成的接口文档。

`.env` 里的大模型配置**全部留空也能启动**，此时以纯规则模式运行。

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查，返回当前运行模式（不含密钥） |
| POST | `/agent/route` | 只做意图识别，返回决策过程，便于答辩演示 |
| POST | `/agent/search` | AI-01 自然语言找房 |
| POST | `/agent/booking` | AI-06 智能预约看房（需带 `house_no`） |
| POST | `/agent/chat` | 统一入口，先判意图再路由 |

请求体统一为 `{"text": "用户说的话"}`。

## 换大模型厂商

团队选定 **DeepSeek**，`.env` 里填上密钥即可：

```
LLM_PROVIDER=deepseek
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat
LLM_API_KEY=<你的密钥>
```

换成其它厂商只需改 `LLM_BASE_URL` 与 `LLM_MODEL`——通义千问、智谱 GLM、
Kimi、豆包都提供 OpenAI 兼容端点。

若某厂商只有私有协议，在 `app/llm/providers.py` 新增一个 `LLMProvider`
子类即可，智能体代码无需改动。

> ⚠️ **API Key 只能放 `.env`**（已在 `.gitignore` 排除），
> 绝不允许写进代码或提交到仓库（非功能需求 N-05）。

## 开发约定

- **不要把大模型的输出直接当参数用。** `agent.py` 里的 `_sanitize`
  会过白名单、校验类型、丢弃负数——大模型会臆造字段名，这层必须留着。
- **新增关键词时注意强弱之分。** `supervisor.py` 里强特征词才能定意图，
  「怎么」「如何」这类泛问句词只能当弱特征加权。曾经把它们混为一谈，
  导致「今天股市怎么样」被判成客服问答且置信度高到不会转交大模型复核，
  `tests/test_supervisor.py` 里有对应的回归测试。
- **时间解析必须能注入「现在」。** `parse_datetime(text, now=...)` 的 `now`
  参数不是可有可无——若函数内部直接调 `datetime.now()`，测试结果会随运行
  日期漂移，根本没法写确定性断言。
- **带「这/本/下」修饰的星期要按周边界算。** 曾经用「下一次出现再加七天」
  的写法，导致周二说「下周一」算成了两周后。`tests/test_booking.py`
  里有回归测试。
- **后端字段名不要泄漏到解析层。** `parse_query` 输出的是业务语义字段
  （`zujia_min`/`zujia_max`），转成 MyBatis-Plus 的 `_start`/`_end`
  约定这件事只在 `search_properties.build_params` 里做。

## 待实现

| 需求 | 状态 |
|---|---|
| AI-01 自然语言找房 | ✅ 已完成 |
| AI-06 智能预约看房 | ✅ 已完成 |
| AI-03 智能客服问答（RAG） | ⬜ 待开发 |
| AI-04 报修工单智能分派 | ⬜ 待开发 |
| AI-02 合同智能审阅 | ⬜ 待开发 |
| AI-05 经营分析报告 | ⬜ 待开发 |

意图路由已经把这些分支都接好了，新增子智能体时在
`supervisor.INTENT_KEYWORDS` 加关键词、在 `main.py` 的
`/agent/chat` 里加一条分支即可。
