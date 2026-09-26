# System Architecture

## 四层结构

```
┌──────────────────────────────────────────────┐
│                 EXPERIENCE                   │
│                                              │
│          Web                  Mobile         │
│      Think & Plan           Act & Sense      │
└───────────────────┬──────────────────────────┘
                    │
                    ↓
┌──────────────────────────────────────────────┐
│                GROWTH STATE                  │
│                                              │
│ Goal / Node / Edge / Time / Execution        │
│ Memory / Profile / Growth Asset              │
└───────────────────┬──────────────────────────┘
                    ↕
┌──────────────────────────────────────────────┐
│                 INTELLIGENCE                 │
│                                              │
│                openJiuwen                    │
│                                              │
│ Understand / Plan / Analyze / Replan / Coach │
└───────────────────┬──────────────────────────┘
                    ↕
┌──────────────────────────────────────────────┐
│                INFRASTRUCTURE                │
│                                              │
│ FastAPI / PostgreSQL / LLM API / Server      │
└──────────────────────────────────────────────┘
```

**团队的共同语言就是这四层。** 有人搞不清架构时,给他看这张图。

## 串起四层的主链路

```
                  USER
                   ↓
              Understand
                   ↓
             Growth State
             ↙     ↓     ↘
         Graph Timeline Plan
                   ↕
              Conversation
                   ↕
                Replan
                   ↑
             Execution State
                   ↑
                  App
```

## 落到代码

| 层 | 落点 |
| --- | --- |
| Experience | [apps/web/](../apps/web/)(Think & Plan)、[apps/mobile/](../apps/mobile/)(Act & Sense) |
| Growth State | 契约权威在 [backend/contracts/](../backend/contracts/)(Pydantic),[shared/schemas/](../shared/schemas/) 是**由它生成**的 JSON Schema;持久化在 [backend/db/](../backend/db/) 定义的表里,本地 SQLite / 线上 PostgreSQL 同一套模型 |
| Intelligence | openJiuwen;包装层在 [backend/agent/](../backend/agent/)(可选依赖,未安装时降级为直连模型并在响应里如实标注) |
| Infrastructure | [backend/api/](../backend/api/)、SQLite 或 PostgreSQL。**没有 Redis** —— 登录限流与熔断是进程内实现,每个 worker 各一份 |

## 两个硬约束

1. **Graph / Timeline / Plan 是同一份 Growth State 的三个投影。**
   不允许为不同 View 建各自独立的计划状态。
2. **只有 `backend/agent/` 能调用模型。** `backend/api/` 只做 HTTP。

## 待补全

- [ ] openJiuwen 的部署形态(进程内库 / 独立服务)
- [ ] 哪些 Agent 调用走同步、哪些走队列
- [ ] 对话的流式输出方案
- [ ] 可观测性:日志、追踪、成本统计
