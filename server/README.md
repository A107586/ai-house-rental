# 业务后端（server）

房源、预约、用户的增删改查接口。Python + FastAPI + SQLAlchemy。

**职责边界**：只做数据操作与业务校验，不含任何大模型逻辑。
智能体服务通过这里的接口取数，不直接连数据库。

## 数据库

本地开发默认 **SQLite**（`server/rental.db`），零安装、无密码，
组员拉下代码就能跑。部署到阿里云时只改环境变量，业务代码不动：

```
DATABASE_URL=mysql+pymysql://user:password@host:3306/rental?charset=utf8mb4
```

## 表结构要点

落实《需求分析说明书》3.1 节的数据安全与数据质量要求：

| 要求 | 落实方式 |
|---|---|
| R-01 密码不得明文 | `users.password_hash`，BCrypt 或 PBKDF2 加盐哈希 |
| R-02 敏感信息脱敏 | 所有接口返回的手机号中间四位掩码 |
| R-03 表间真外键 | `houses.landlord_id`、`appointments.house_id` 均为外键 |
| R-04 金额用 Numeric | `rent_price`、`deposit` 为 `Numeric(10,2)` |
| R-06 房源关联房东实体 | 不再只存房东姓名字符串 |

另外 `appointments.viewing_time` 用 `DateTime` 而非 `Date`——
AI-06 要处理「这周六下午两点」，只存日期就丢掉了时刻，也做不了时段冲突检测。

## 快速开始

```
pip install fastapi uvicorn pydantic sqlalchemy
python seed.py                                   # 生成 108 套演示房源
python -m uvicorn app.main:app --reload --port 8080
```

接口文档：<http://localhost:8080/docs>

## 前端托管

若 `web/dist` 存在（跑过 `cd web && npm run build`），本服务会把它挂载到 `/`，
访问 <http://localhost:8080> 就是完整网站，不需要单独起前端服务。

两个实现要点：

1. **挂载必须写在文件最末尾。** 挂到 `/` 会接管所有未匹配路径，
   放在 API 路由之前会把 `/api/*` 请求也吞掉。
2. **根路径的 JSON 提示只在没有构建产物时注册。** 显式路由优先级高于挂载，
   无条件注册会把静态首页整个盖住，访问根路径永远只看到那段 JSON。

演示账号：`admin` / `admin123`，`tenant1` / `test123`

## 测试

```
python -m unittest discover -s tests -v
```

29 个用例，跑在内存数据库上，不会动开发用的 `rental.db`。

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/auth/login` | 登录 |
| GET | `/api/houses` | 房源列表，支持区域/户型/租价/面积/关键词筛选与排序分页 |
| GET | `/api/houses/{house_no}` | 房源详情，同时累加浏览量 |
| GET | `/api/districts` | 各区可租房源数量，供前端筛选器使用 |
| GET | `/api/appointments` | 看房预约列表，智能体用它做冲突检测 |
| POST | `/api/appointments` | 提交预约，服务端独立做时段冲突校验 |
| POST | `/api/chat-messages` | 保存智能体对话记录 |

## 开发约定

- **服务端必须独立做冲突校验。** 智能体那边已经查过一遍时段，但并发场景下
  会失效，也可能有人绕过智能体直接调接口。`POST /api/appointments`
  里的重复校验不能省。
- **`to_dict()` 绝不能输出 `password_hash`。** 已有测试锁死这一点。
- **演示数据要覆盖所有筛选组合。** `seed.py` 按「每个区 × 每种户型至少 3 套」
  系统化生成——早先用纯随机，60 套房撒在 6 区 × 6 户型上出现了空组合，
  演示时一句「中山区两室一厅」直接查不到结果。

## 邮件通知（可选）

系统默认只发站内信。配置以下环境变量后，账单、合同、工单三类通知会
同时发一封邮件——**不是所有站内信都发**，每条都推用户很快会把整个域名
拉黑，真正要紧的那封反而看不见了。

```
SMTP_HOST=smtp.example.com
SMTP_PORT=465
SMTP_USER=your@example.com
SMTP_PASSWORD=授权码
SMTP_FROM=your@example.com
```

不配就跳过，不报错也不重试——项目开箱即用不该要求先去申请一个邮箱。
邮件发送失败**不会影响业务**：合同已经生效、账单已经生成，不能因为
邮件服务器连不上就把整个请求回滚。

短信通道没有实现（需要实名认证的服务商账号），但 `app/notifier.py`
的通道接口已经留好，接上只是加一个类。
