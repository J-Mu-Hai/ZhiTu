# Current Design

Version: 0.1
Updated: 2026-09-16

> 本文是**每天真正要看的文档**,保持 100~200 行。
> 与编号文档冲突时,以本文为准。

## Current Product

大学生成长规划智能体。

## Current Core Loop

```
Conversation
     ↕
Growth Space
     ↕
  Execution
```

## Current Web Demo

已决定:

- React
- Next.js
- React Flow
- Graph / Timeline / Plan
- AI Dock

定位:Think & Plan

## Current Mobile

Flutter

定位:Act & Sense

## Current Backend

FastAPI

Agent:openJiuwen

## Current Data Model

**Status: Experimental**

目前:

```
Growth State
├── Node
├── Edge
├── Timeline
└── Execution
```

## Current Interaction

用户可以:

1. 对话修改计划
2. 直接修改计划

两种方式同等地位,都必须落到同一份 Growth State。

## Current Unresolved Questions

- [ ] Graph 是否允许一个节点属于多个 Goal?
- [ ] Edge 应该有哪些类型?
- [ ] AI 修改计划是否必须用户确认?
- [ ] Timeline 冲突如何表达?
- [ ] Mobile 是否需要完整 Graph?
- [ ] Growth Asset 最终定义是什么?
- [ ] Web 端是否也有 Today?(见 [02-INFORMATION-ARCHITECTURE.md](02-INFORMATION-ARCHITECTURE.md))

## Current Development Stage

Phase 1:**Product Exploration**

## Team Rule

```
Mother Demo
    ↓
Parallel Exploration
    ↓
  Integration
    ↓
 Next Version
```

**每 2~3 天 Integration。**

## 变更记录

| 日期 | 变更 | 原因 |
| --- | --- | --- |
| 2026-09-16 | 建立仓库骨架 | 项目启动 |
| 2026-09-16 | 确立核心模型:目标—规划—执行—感知—调整—成长沉淀 | 产品定义收敛 |
