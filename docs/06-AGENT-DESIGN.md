# Agent Design

> 面向负责 openJiuwen 编排层的人。

## Agent Responsibility

Agent 负责:

- Understand User
- Goal Analysis
- Planning
- Conflict Analysis
- Replanning
- Memory
- Execution Analysis
- Proactive Intervention

## Agent Should NOT

Agent 不应该:

- 直接操作前端 UI
- 决定组件坐标
- 保存 React 状态
- 返回不可解析的大段计划文本作为唯一结果

## Input

```
User Message
      +
   Context
├── currentObject
├── currentView
├── currentPlan
├── executionState
├── userProfile
└── memory
```

`currentObject` / `currentView` 来自用户的选中行为,见
[03-GROWTH-SPACE.md](03-GROWTH-SPACE.md) 第 6 节。

## Output

```
AgentResponse
├── reply       (自然语言回复)
└── PlanAction[]  (对 Growth State 的结构化变更)
```

PlanAction 的取值见 [05-DATA-MODEL.md](05-DATA-MODEL.md)。

## Example

**User:**

> 这学期课程很多,我想把科研项目推迟。

**Agent:**

reply:

> 可以。不过推迟以后可能与期末阶段冲突……

actions:

```
UPDATE_TIME(research_project)
UPDATE_PRIORITY(...)
```

## Replan

```
Execution State
      ↓
Conflict Detection
      ↓
   Reasoning
      ↓
   Proposal
      ↓
User Confirmation
      ↓
  PlanAction
      ↓
 Growth State
```

## 待补全

- [ ] 多 Agent 的拆分时机 —— **不要为了"我们用了 Multi-Agent"强行 Multi-Agent**。
      MVP 先只做 Main Agent;等 Planning 复杂度真的上来再拆 Planner,
      行为分析复杂了再拆 Analyzer,主动陪伴独立了再拆 Coach。
- [ ] 提示词的组织与版本管理([../backend/agent/prompts/](../backend/agent/prompts/))
- [ ] 工具清单与权限边界([../backend/agent/tools/](../backend/agent/tools/))
- [ ] 记忆机制:短期会话 / 长期成长记忆([../backend/agent/memory/](../backend/agent/memory/))
- [ ] 主动介入的触发规则(对应 [01-PRODUCT-DESIGN.md](01-PRODUCT-DESIGN.md) 第 4 节)
- [ ] openJiuwen 的具体接入方式
