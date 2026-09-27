# Data Model

> **Status: EXPERIMENTAL**
>
> 当前模型允许修改。
> 任意时间团队只维护一个 Current Version。

> **读这一页之前先知道它在哪一层(2026-09-27 补)。**
>
> 下面那些字段名（`progress`、`metadata`、`startDate`…）与 `parent/support/conflict`
> 那组边类型，是**早期草稿**，代码里从来不是这样。真实模型只有一处权威：
> `backend/db/models/`（表与列）+ `backend/contracts/`（对外的类型），
> 由 `python -m backend.contracts.schema` 生成 `shared/schemas/domain.schema.json`。
> 本页保留草稿原样是为了留下"当初想的是什么"，所以**不要**拿它当迁移依据；
> 新表按下面 `NodeAnalysis` 一节的写法追加（写清它是什么、不参与什么）。

## Core Concept

```
Growth State
├── Goal
├── Node
├── Edge
├── Timeline
├── Execution
├── Memory
└── Growth Asset
```

## PlanNode

Current Draft:

```
id
title
description
type
status
priority
startDate
endDate
progress
metadata
```

## Node Type

Possible values:

```
goal
capability
stage
task
milestone
```

## PlanEdge

```
id
source
target
type
```

## Edge Type

Possible:

```
parent
dependency
support
conflict
```

## PlanAction

Possible:

```
CREATE_NODE
UPDATE_NODE
DELETE_NODE

CREATE_EDGE
DELETE_EDGE

UPDATE_TIME
UPDATE_PRIORITY
UPDATE_STATUS
```

## NodeAnalysis

AI 对节点内容做出的**判断**。`node_analyses`，2026-09-27 加。

```
id
owner_id
workspace_id
scope_root_id          -- 这一轮允许看的范围起点
focus_node_id          -- 讨论的是哪个节点
input_versions (JSON)  -- 当时读到的每个节点的 content_version + 结构版本
conversation_id
message_id             -- 它挂在哪条助手回复上
prompt_version / model_source / model_name

known[]                -- 用户陈述过的事实
unknowns[]             -- 还缺什么
evidence[]             -- 依据（每条自带来源与时间）
assumptions[]          -- 模型自己假设的
diagnosis[]            -- 判断
strategy_options[]     -- 可选的走法
risks[]                -- 风险
confidence_note
coverage_note          -- 只读到了范围的一部分时，读到了多少

created_at
```

三条边界，都在代码里有对应注释，改这张表之前先读它们：

1. **`append-only`。** 像 `plan_revisions` 一样只增不改：重新分析一次不会抹掉上一次——
   用户常问的就是"它上次为什么那么说"，那需要上一次的原话还在。
2. **没有 `stale` 列。** 过期是**读的时候现算的**：拿 `input_versions` 跟库里的现状比。
   落一列标记就得指望每次用户编辑都记得去写它，漏一次就是旧分析冒充最新。
3. **它不是约束，也不是正文。** 这张表里的每一栏都只是判断；要变成计划必须走提案与确认
   （规范 §2.3）。所以它**不参与**排期、不参与容量计算、不产生 `PlanRevision`。
   规则兜底（`rule_fallback`）与"模型不可用"的那两档**不落行**——一段不是模型给的判断
   不能冒充判断；读取时 `model_source` 会如实说明这一条是谁给的。

## Important

该 Schema 当前不是最终版本。

如果 UI / Agent / Mobile 的实际探索证明模型不合理,允许修改。

但修改以后:

1. 更新本文档
2. 更新 CURRENT-DESIGN
3. 通知团队
4. 迁移当前代码
5. 所有人使用新版本
