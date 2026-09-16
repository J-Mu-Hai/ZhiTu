# Conversational Growth Space

> 这是整个项目最重要的产品文档,记录本项目的核心创新,请认真维护。

## 1. Definition

Growth Space 是用户长期成长状态的可视化空间。

Conversation 不是独立页面。

Conversation 是操作 Growth Space 的一种方式。

## 2. Interaction Model

```
User
 ↕
Conversation
 ↕
Growth State
 ↕
Direct Manipulation
```

## 3. Views

### Graph View

回答:

> 为什么做?

表现:

```
Goal → Capability → Stage → Task → Dependency
```

### Timeline View

回答:

> 什么时候做?

展示:

- startDate
- endDate
- dependency
- overlap
- conflict

### Plan View

回答:

> 具体做什么?

展示:

```
Long Term → Stage → Month → Week → Today
```

## 4. View Synchronization

所有 View 读取同一份 Growth State。

以 Transformer 为例:

**Graph Node:**

```
Transformer
```

**Timeline:**

```
2026-10-01 → 2026-10-21
```

**Plan:**

```
阶段一
└── Transformer
    ├── Attention
    ├── Encoder
    └── Mini Transformer
```

## 5. Direct Manipulation

用户可以:

- 创建节点
- 删除节点
- 修改节点
- 调整优先级
- 修改时间
- 拖动 Timeline
- 修改依赖关系

所有操作最终转换为 PlanAction。

## 6. Context-aware Conversation

用户选中一个对象:

```
selectedObject = research
```

Conversation 自动获得:

- currentObject
- currentView
- relatedGoal
- relatedTasks

因此:

> 用户点击某个对象本身就是 Context Input。

## 7. Conversation Layer

Conversation 不永久占据 50% 屏幕。

采用可展开 AI Dock。

```
Collapsed: 约 10%
Expanded:  约 35% - 45%
```

Growth Space 始终作为主空间存在。

## 8. Important UI Principle

避免同时显示:

```
Graph Window
Timeline Window
Task Window
Analysis Window
Chat Window
```

采用:

> **One Space, Many Views**

主视图一次聚焦一个主要问题,辅助信息通过 Contextual Layer 临时出现。
