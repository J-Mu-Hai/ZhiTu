# Current Design

Version: 0.4
Updated: 2026-09-25

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
- 四个成长分类单击进入各自路径
- 子路径以当前分类为中心,内容以“树叶”向下生长
- 树叶可填写名称、类型和说明,并可继续进入下一级空间
- Web 统一采用近黑画布、深灰分层表面和四类成长色，覆盖路径、时间线、任务、对话与个人页面
- 登录与注册是正式 Web 入口；每个本地账户拥有独立的 Growth State 和个人资料
- “我的”支持编辑姓名、学校、专业、年级、排名、目标年份、目标方向和个人介绍

`prototypes/demo3` 已完成体验验证并提升为正式入口 `apps/web`。原 `apps/web` 骨架保存在 `prototypes/web-skeleton` 供架构对照。

当前边界:

- Web 使用按账户隔离的浏览器本地存储，刷新后保留 Growth State、随笔、对话和偏好
- “树叶”是 `GrowthNode` 的界面表达,本次未改变 `shared/schemas/`
- 当前登录是前端本地账户演示；后端身份、数据库与真实 AI 尚未接入

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

1. 单击学业成绩、科研能力、综合经历、个人成长进入专属路径
2. 围绕中心主题添加树叶并继续进入子路径
3. 对话修改计划
4. 直接修改计划

这些方式同等地位,都必须落到同一份 Growth State。

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
| 2026-09-24 | Demo3 提升为正式 Web；加入分类下钻与树叶路径 | 让四类成长维度可以独立、递归地丰富内容 |
| 2026-09-25 | Web 从白色视觉切换为完整深色工作台，并统一时间线与内容页 | 降低空旷感，强化节点、路径和内容层级 |
| 2026-09-25 | 恢复登录／注册入口，加入账户级计划隔离和可编辑个人资料 | 让产品从单人静态 Demo 进入多用户产品结构 |
