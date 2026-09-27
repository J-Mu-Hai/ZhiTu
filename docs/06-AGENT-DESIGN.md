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

## Analysis（分析层，2026-09-27 加）

回复与动作之外，模型还可以给一份**结构化的判断**。它和 `PlanAction[]` 是两件事：

```
AgentResponse
├── reply          (自然语言回复)
├── PlanAction[]   (要用户确认才生效的结构化变更)
└── analysis       (可选) 对这一轮读到的内容的判断 —— 不改变任何东西
    ├── known / unknowns / evidence / assumptions
    ├── diagnosis / strategy_options / risks
    └── confidence_note
```

**它不是约束。** 这一层存在的唯一理由是让"模型对现状的理解"变成一份**可以挑错的记录**，
而不是藏在回复的一段文字里。三条硬边界，写在 `backend/services/analysis_service.py` 与
`backend/services/conversation_service.py` 的注释里，改动前先读：

1. **要变成计划必须走提案与确认。** 分析里写着"这个任务缺一个时长"不产生任何写入；
   模型必须另提一条 `PlanAction`，由用户点确认。直接拿分析当约束会让模型的猜测
   悄悄变成计划的前提。
2. **过期是读的时候现算的，不是落一个标记。** 每次读都拿"当时读到的输入"跟库里的现状比。
   输入在模型思考期间变过（`input_changed`）时，**这一轮不生成提案**、分析一出生就带着过期。
3. **降级不落行，也不许长得像真的。** 规则兜底与"模型不可用"那两档不产生分析记录；
   已经存在的记录会带上 `model_source`，界面上必须看得出"这一条不是模型的判断"。

**重新分析走的是同一条对话工作流**（`POST /workspaces/{id}/nodes/{nid}/analysis/refresh`
内部仍调 `submit_turn`），只是替用户说了一句"根据最新内容重新分析一下这个节点"。
所以它会在对话里留下两条消息 —— 按钮与对话不是两个真相。那句话常量只有一个来源：
`analysis_service.REANALYZE_MESSAGE`。

**加一栏判断时，改的是三处，不是一处。** `known` / `risks` 这些栏要能真的落进库，得同时
出现在：提示词（`agent/prompts/planning.py`）、解析器（`runtime/response.py` 的
`ANALYSIS_FIELD_ORDER`）、以及**给 SDK 的输出声明**（`runtime/openjiuwen_runtime.py` 的
`OUTPUT_CONFIG`）。漏掉第三处的后果最难查：openJiuwen 那条路上的载荷是**照声明重建**的，
不在声明里的键在到解析器之前就没了 —— **模型给了、库里没有、日志里什么也没有**。
这条是真实模型验收抓出来的（当时漏的正是 `analysis` 这个键本身，整层记录一行都不落）。
现在两张转发表由 `OUTPUT_CONFIG` 现推，`PARSED_PAYLOAD_FIELDS` 是"解析器会读哪些键"的
唯一出处，两端各有测试钉着；加一个键只需改**提示词 + `OUTPUT_CONFIG` + 解析器**。

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
- [x] 提示词的组织与版本管理 —— 在 [../backend/agent/prompts/](../backend/agent/prompts/),
      带 `PROMPT_VERSION`,随每次响应回传,方便把"某个版本的计划不好"追到具体提示词。
- [ ] 工具清单与权限边界。**当前一个工具都没有**:模型只做一次结构化生成,不调工具,
      也就没有工具权限可谈。真要加工具时,红线是"工具只能读数据和提变更,
      **不能执行 SQL、不能直接写库**"。
- [ ] 记忆机制:短期会话 / 长期成长记忆。当前是"最近若干轮原文 + 历史摘要"直接进提示词,
      没有检索、没有独立记忆层。
- [ ] 主动介入的触发规则(对应 [01-PRODUCT-DESIGN.md](01-PRODUCT-DESIGN.md) 第 4 节)。
      站内提醒已实现(只由具体事件触发,不是推送)。
- [x] openJiuwen 的具体接入方式 —— [../backend/agent/runtime/openjiuwen_runtime.py](../backend/agent/runtime/openjiuwen_runtime.py),
      以及 [../backend/agent/README.md](../backend/agent/README.md) 里的三条路与降级规则。
