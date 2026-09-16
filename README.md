# 知途 · Growth Agent

面向个人成长与教育规划场景的长期 AI 智能体系统。

不是 Todo List,也不是 AI 学习计划生成器。系统围绕用户真实的成长过程建立闭环:

```
Understand → Plan → Execute → Sense → Replan → Grow
```

AI 不只帮用户制定计划,还持续理解用户目标、现实约束、执行状态和行为变化,并据此动态调整成长路径。

## 核心模型

```
Conversation
     ↕
Growth Space     ← Graph / Timeline / Plan 是它的三个投影
     ↕
  Execution
```

**One State, Many Views** —— Graph、Timeline、Plan 不是不同计划,它们是同一份 Growth State 的不同投影。

## 四层架构

```
EXPERIENCE      Web (Think & Plan)   Mobile (Act & Sense)
                        ↓                    ↓
GROWTH STATE    Goal / Node / Edge / Time / Execution / Memory / Asset
                        ↕
INTELLIGENCE    openJiuwen — Understand / Plan / Analyze / Replan / Coach
                        ↕
INFRASTRUCTURE  FastAPI / PostgreSQL / LLM API / Server
```

## 双端定位

| | Web | Mobile |
| --- | --- | --- |
| 定位 | **Think & Plan** | **Act & Sense** |
| 负责 | 深度对话、Growth Space、Graph、Timeline、Plan、长期规划、深度分析 | Today、当前任务、AI 陪伴、快速记录、随笔、执行反馈、行为感知、主动提醒 |

## 仓库结构

| 目录 | 说明 |
| --- | --- |
| [docs/](docs/) | **设计文档 —— 人和 AI 的共同上下文。先读 [CURRENT-DESIGN.md](docs/CURRENT-DESIGN.md)** |
| [apps/web/](apps/web/) | Web 端(Next.js + React Flow) |
| [apps/mobile/](apps/mobile/) | 移动端(Flutter) |
| [backend/](backend/) | FastAPI + openJiuwen 编排层 |
| [shared/](shared/) | 共享契约。**`schemas/` 是唯一数据契约源** |
| [prototypes/](prototypes/) | 一次性原型。**验证成功的东西才进入 `apps/`** |
| [scripts/](scripts/) | 开发、数据库、部署脚本 |
| [tests/](tests/) | 跨服务的集成测试 |

## 文档地图

任何人在动代码之前,先读这两个:

- **[docs/CURRENT-DESIGN.md](docs/CURRENT-DESIGN.md)** — 当前有效设计,每天要看的
- **[AGENTS.md](AGENTS.md)** — 给 AI Coding Agent 的指令

其余按主题:

- [00-PROJECT-VISION.md](docs/00-PROJECT-VISION.md) — 我们到底在做什么(项目的"宪法")
- [01-PRODUCT-DESIGN.md](docs/01-PRODUCT-DESIGN.md) — 产品结构与模块
- [02-INFORMATION-ARCHITECTURE.md](docs/02-INFORMATION-ARCHITECTURE.md) — 信息架构与导航
- [03-GROWTH-SPACE.md](docs/03-GROWTH-SPACE.md) — **核心创新,最重要的产品文档**
- [04-SYSTEM-ARCHITECTURE.md](docs/04-SYSTEM-ARCHITECTURE.md) — 四层架构
- [05-DATA-MODEL.md](docs/05-DATA-MODEL.md) — 数据模型(EXPERIMENTAL)
- [06-AGENT-DESIGN.md](docs/06-AGENT-DESIGN.md) — Agent 职责与输入输出
- [07-DEVELOPMENT.md](docs/07-DEVELOPMENT.md) — 环境与开发流程

## 快速开始

```bash
cp .env.example .env     # 填入模型 API Key 等配置
docker compose up -d     # 启动 Postgres / Redis / API
```

分端启动方式见 [07-DEVELOPMENT.md](docs/07-DEVELOPMENT.md)。

## 团队工作方式

```
Mother Demo → Parallel Exploration → Integration → Next Version
```

每 2~3 天 Integration。

先做 Mother Demo,验证一个闭环就够:

> 用户与 AI 对话 → Growth Space 生长 → Graph / Timeline / Plan 三种投影 → 用户直接修改 → AI 理解修改并 Replan

这个闭环成立,项目最难、最有区别度的部分就被证明了。

**探索阶段不要为了"我们用了 Multi-Agent"强行 Multi-Agent。**
架构应该从产品复杂度长出来,而不是从比赛 PPT 长出来。
