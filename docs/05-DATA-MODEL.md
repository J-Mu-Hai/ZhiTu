# Data Model

> **Status: EXPERIMENTAL**
>
> 当前模型允许修改。
> 任意时间团队只维护一个 Current Version。

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

## Important

该 Schema 当前不是最终版本。

如果 UI / Agent / Mobile 的实际探索证明模型不合理,允许修改。

但修改以后:

1. 更新本文档
2. 更新 CURRENT-DESIGN
3. 通知团队
4. 迁移当前代码
5. 所有人使用新版本
