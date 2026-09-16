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
| 数据结构 | [05-DATA-MODEL.md](docs/05-DATA-MODEL.md)、[shared/schemas/](shared/schemas/) |
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
| 可复用 UI | `apps/web/src/app/components/`、`apps/mobile/lib/widgets/` |
| 业务逻辑(前端) | `apps/web/src/app/features/`、`apps/mobile/lib/` |
| HTTP 接口 | [backend/api/routes/](backend/api/routes/) |
| Agent 编排 / 提示词 / 工具 | [backend/agent/](backend/agent/) |
| 共享数据结构 | [shared/schemas/](shared/schemas/) |
| 一次性原型 | [prototypes/](prototypes/) —— **不接入生产** |

## 密钥

只走 `.env`。任何形式的 key、token、连接串都不进仓库。
