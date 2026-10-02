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

## AgentQuestion（问题节点 MVP，2026-10-01 加）

AI 在“确有必要”时向用户提的一个问题。`agent_questions`。

```
id
workspace_id
source_node_id         -- 从哪个节点聊出来的（可空；节点归档后仍是原 id）
source_message_id      -- 产出这个问题的助手消息（可空）
question               -- 简洁的一句话
why_now                -- 为什么现在问
response_mode          -- single_select / multi_select / free_text / mixed
options (JSON)         -- 0–5 个 {id, label}，选项是加速器不是限制
allow_custom_input
status                 -- pending / answered / investigating / resolved / archived
answer (JSON)          -- {selectedOptionIds, customInput}
answer_client_id       -- 回答的幂等键
events (JSON)          -- 用户动作审计（answered / skipped / deferred）
answered_at
created_at / updated_at
```

四条边界，都有代码注释对应：

1. **它不是计划节点。** 问题不写 `plan_nodes`，不参与排期、依赖、任务统计，也不出现在
   画布的计划树里（把这些边界做成结构性的，而不是在每个查询里加例外）。
2. **回答不等于同意计划变更。** 回答只是“用户输入事实”；回答之后的后续对话可能产生
   `actions`，而那些仍然是一份要用户确认的提案，走 `proposal -> confirm -> 事务写入`。
   回答接口本身**不写** `plan_nodes` / `node_relations` / 日程。
3. **答案先落库，再调模型。** 状态：`pending -> answered -> investigating -> resolved`，
   或 `pending -> archived`（跳过）。模型失败时停在 `investigating`，答案与状态刷新后读得回。
4. **身份：** `answer_client_id` 让重复点击幂等；`client_message_id`
   （`question-answer:{id}`）让后续那一轮不会被跑两次。

## PlanningLevel(规划层级,2026-10-02 加)

`plan_nodes.planning_level`,**可空**。与 `NodeType`(这是什么)和 `NodePurpose`(要不要排期)
都正交,回答“这个节点在哪一层”。

```
strategy  战略
phase     阶段
month     月
week      周
day       日
(null)    未指定 —— 存量节点与不需要层级的节点
```

五条边界:

1. **可空 + 不回填。** 旧节点全部是 NULL,行为与加这一列之前完全一样。
2. **不用标题 / description / node_type 推断层级** —— 层级只能被明确写入、明确校验。
3. **父子只能从粗到细**(允许跳级);策略可以挂在根或另一个策略下,不能挂在周/日下。
4. **`information` 节点不能带层级** —— 它不进排期。
5. **它不等于“已排期”** —— 只表达语义层级,具体哪天做仍由排期器算。

战略确认复用提案确认:只有一行 `planning_level='strategy'` 的节点被写入
`plan_nodes`("用户确认了提案"或用户手工建),才算“已确认战略”。服务端靠“存在这样
一行”判断能否下钻到月/周/日,没有另一张状态表。

## ReasoningState / ToolCallRecord(有界工具循环,2026-10-02 加)

服务端控制的推理状态与只读工具调用记录。**只存可审计摘要,不存原始 CoT。**

```text
reasoning_states
  id / workspace_id / source_message_id? / context_node_id? / parent_id?
  question_id?                 -- 可空的 Question Node 引用(不改 agent_questions 表)
  question                     -- 当前要解决的简短问题
  importance / uncertainty     -- 0–5 有界整数
  facts_summary (JSON)         -- [{text, source: user|system|tool|model_inference|assumption, at}]
  assumptions / open_questions (JSON)
  conclusion                   -- 简短结论,可空
  next_action                  -- read_tool / ask_user / synthesize / stop
  status                       -- unexplored / exploring / blocked / resolved / abandoned
  tool_calls_used / model_calls_used

tool_call_records
  reasoning_state_id / sequence / tool_name
  sanitized_arguments (JSON)   -- 只留白名单参数,句柄不是真实 UUID
  status                       -- ok / error / rejected
  result_summary (JSON)? / error_summary?
  created_at / finished_at
```

三类边界:

1. **只存摘要。** 完整工具结果不进库也不进提示词;超长会被截断并标注 `truncated`。
2. **所有事实带 source。** user / system / tool / model_inference / assumption 在 state
   里可区分;分析层的 `known`/`assumptions` 不受影响。
3. **可恢复。** 用户消息与已回答问题先落库;循环中途模型/工具失败时 state 置 `blocked`,
   已写工具记录与事实保留。预算(每轮最多 3 次模型调用、4 次工具调用)也记在 state 上。

循环只把**终止那一轮**的 `actions` 交给既有 `proposal_service` —— 中间轮的动作不生成提案。

## Important

该 Schema 当前不是最终版本。

如果 UI / Agent / Mobile 的实际探索证明模型不合理,允许修改。

但修改以后:

1. 更新本文档
2. 更新 CURRENT-DESIGN
3. 通知团队
4. 迁移当前代码
5. 所有人使用新版本
