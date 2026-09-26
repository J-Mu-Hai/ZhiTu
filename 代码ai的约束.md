# AI Coding Instructions

> 本文件写给 Cursor / Claude Code / Codex 等 Coding Agent,不是给用户看的。

## Before Coding

Always read:

1. [docs/CURRENT-DESIGN.md](docs/CURRENT-DESIGN.md)
2. [docs/00-PROJECT-VISION.md](docs/00-PROJECT-VISION.md)

然后按任务类型读对应文档:

| 任务 | 读 |
| --- | --- |
| 界面 / 交互 | [03-GROWTH-SPACE.md](docs/03-GROWTH-SPACE.md)、[02-INFORMATION-ARCHITECTURE.md](docs/02-INFORMATION-ARCHITECTURE.md) |
| 数据结构 | [05-DATA-MODEL.md](docs/05-DATA-MODEL.md)、[backend/contracts/](backend/contracts/)(契约权威)、[backend/db/](backend/db/)(表) |
| Agent / 提示词 | [06-AGENT-DESIGN.md](docs/06-AGENT-DESIGN.md) |
| 接口 | [04-SYSTEM-ARCHITECTURE.md](docs/04-SYSTEM-ARCHITECTURE.md) |

## Product Core

**This is NOT a Todo List.**

Core loop:

```
Understand → Plan → Execute → Sense → Replan → Grow
```

## Core Interaction

```
Conversation ↔ Growth Space ↔ Execution
```

Conversation 不是独立页面,它是操作 Growth Space 的一种方式。

## Important Architecture Rule

**Graph、Timeline 和 Plan 是同一份 Growth State 的三个视图。**

Do NOT create independent plan states for different views.

## Development Stage

Current stage:**PRODUCT EXPLORATION**

Architecture and schemas may change.

Do not perform large refactors without explaining the impact.

## Coding Rules

- Do not modify unrelated modules.
- Prefer simple implementations during prototype stage.
- Do not introduce infrastructure without necessity.
- **Do not silently change shared schemas.**
- Update documentation when product behavior changes.
- 计划状态的变更必须表达为 `PlanAction`,不要用一段自然语言文本代替。

## Before Large Changes

Explain:

1. What you want to change
2. Why
3. Which modules are affected
4. Whether shared schemas change
5. Migration impact

## 目录归属

| 改动类型 | 落点 |
| --- | --- |
| 页面 / 路由 / 交互 | [apps/web/src/app/](apps/web/src/app/)、[apps/mobile/lib/screens/](apps/mobile/lib/screens/) |
| 可复用 UI | `apps/web/src/components/`、`apps/mobile/lib/widgets/` |
| 业务逻辑(前端) | `apps/web/src/features/`、`apps/mobile/lib/` |
| HTTP 接口 | [backend/api/routes/](backend/api/routes/) —— **只做 HTTP**,不写业务逻辑 |
| 业务逻辑(后端) | [backend/services/](backend/services/) —— **唯一的写入者** |
| 表结构 / 迁移 | [backend/db/models/](backend/db/models/)、[backend/migrations/](backend/migrations/) |
| 排期算法 | [backend/scheduler/](backend/scheduler/) —— **叶子包**,纯函数,不许 import sqlalchemy / httpx / backend.agent / backend.services |
| Agent 编排 / 提示词 | [backend/agent/](backend/agent/) —— **只提案,不写库** |
| 共享数据结构 | 权威在 [backend/contracts/](backend/contracts/)(Pydantic);[shared/schemas/](shared/schemas/) 是**生成物**,改它不生效 |
| 一次性原型 | [prototypes/](prototypes/) —— **不接入生产** |

## 分层是硬约束,不是风格

```
backend/db/        表定义
backend/services/  唯一允许写库的地方
backend/scheduler/ 纯函数排期(叶子包)
backend/agent/     只提案,不写库
backend/api/       只做 HTTP
```

越过任何一条(比如在路由里直接写库、在 scheduler 里 import 服务层、让模型产出 SQL),
代码评审应当直接打回。

**模型的一切输出都只是提案。** 写库由用户在界面上点击确认触发,不能由模型代劳。
模型看不到也产不出真实 UUID —— 它只见 `n1..nK` 句柄,由服务端映射回来。

**数据库两侧都要能跑。** 本地 SQLite、线上 PostgreSQL,同一套模型。`sa.Uuid(as_uuid=True)`、
`sa.Enum(native_enum=False)`、部分唯一索引同时传 `postgresql_where` 与 `sqlite_where`。
"哪一天"绝不由时间戳推导,一律用写入时按用户时区算好的 `Date` 列。

## 密钥

只走 `.env`。任何形式的 key、token、连接串都不进仓库。
