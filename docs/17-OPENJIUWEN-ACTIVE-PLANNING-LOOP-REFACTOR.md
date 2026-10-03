# OpenJiuwen 主动规划循环重构说明

## 1. 本轮目标

本轮不是继续增加问题节点、候选方向或单独的按钮。目标是把 V1 从“若干 API/按钮拼接的状态机”重构为一个由 **OpenJiuwen Workflow** 驱动的主动规划循环。

用户进入一个已有目标的空间后，智能体应自行开始工作：先形成整体判断，必要时只停下来等待一个能改变战略的回答；一旦战略足够明确，自动准备粗时间线。用户负责回答、纠正和确认会写入正式计划的提案，而不是负责推动流程到下一步。

产品闭环只有两个主要产物：

1. **第一目标：粗时间架构。** 得到战略后，在时间线中呈现“哪个时间段做什么、成果是什么、先后关系如何”的 3–6 个阶段草案。
2. **第二目标：执行与重规划。** 用户要求细化后，将粗时间线落到月/周/日；每周根据执行结果分析偏差，只调整未来并形成可确认的重规划提案。

已有的分析约束、数据模型、提案确认、版本校验、事务写入与审计脱敏是安全边界，必须保留，不能为了“主动”而绕开。

---

## 2. 现状核对与问题定义

截至本规格编写时，代码已经有 `advance_v1_workflow(...)`，也已经尝试在 `INITIAL_THINKING` 时调用 assessment、在战略确认后生成粗时间线。但它还不能视为完整的主动循环，原因如下：

- `AGENT_REASONER=auto` 仍可从 OpenJiuwen 静默降级为 `direct_llm`；因此 V1 并不保证由 OpenJiuwen 执行。
- 同一份 V1 服务仍存在历史 `advance(...)` 路径与多个单独端点，流程语义容易分散；不能从任何状态可靠回答“现在在等谁、下一步会自动做什么”。
- `problem_structure`、候选方向、战略草案与时间线生成仍可能依赖额外 CTA；用户会看到“正在思考”或 `idle`，却没有实际运行中的工作或可理解的阻塞理由。
- 审计能够记录很多事件，但尚未成为主动循环的唯一事实来源：无法用一次导出证明每一轮都从哪个状态进入、为何停下、何时自动推进。

本轮必须解决的是编排与运行时责任划分，而非继续扩大提示词或渲染更多节点。

---

## 3. 核心设计：程序守边界，OpenJiuwen 负责主动决策

### 3.1 职责划分

```text
浏览器事件 / 定时器 / 服务恢复
        ↓
V1 Workflow Orchestrator（唯一阶段推进入口）
        ↓
OpenJiuwen Workflow（受限结构化判断、是否追问、战略/时间架构建议）
        ↓
Schema 解析 + 服务端硬校验
        ↓
Proposal / 用户确认 / version 校验 / transaction
        ↓
数据库、时间线投影、审计事件、前端状态
```

**OpenJiuwen 应负责：**

- 根据上下文形成高层战略判断；
- 判断能否合理暂定、何时必须询问一个关键问题；
- 产出候选方向、战略草案、粗时间架构、月/周/日细化建议和重规划建议；
- 使用受限 JSON 契约表达结果。

**程序必须继续负责：**

- 当前阶段是否允许某一类输出；
- 问题预算、关键问题数量、禁止执行细节过早出现；
- 不允许模型直接写数据库、创建任意节点或提交正式计划；
- `proposal → 用户确认 → version 校验 → transaction`；
- 幂等、并发锁、超时、恢复、审计与脱敏；
- 已完成历史不可被重规划改写。

### 3.2 V1 必须显式要求 OpenJiuwen

新增 V1 专用配置，建议如下：

```dotenv
PLANNING_V1=true
V1_REQUIRE_OPENJIUWEN=true
V1_AGENT_TURN_TIMEOUT_SECONDS=45
```

规则：

- 仅 V1 新空间受 `V1_REQUIRE_OPENJIUWEN` 约束；老空间、V0.1 和常规对话保留原有 `AGENT_REASONER` 行为。
- V1 运行前验证当前 reasoner 的真实类型/来源为 `openjiuwen`；不能只看环境变量。
- OpenJiuwen 包、模型 Key、Workflow 初始化或调用不可用时，V1 进入 `failed_retryable`，返回准确原因和“重试”入口。
- 禁止 V1 自动改走 `direct_llm`、`rule` 或 `script` 并当作真实规划成功。测试可显式注入 Fake/OpenJiuwen fixture，但响应与审计必须标明测试来源。
- UI 应显示“AI 规划 · OpenJiuwen”；若未满足条件，显示“OpenJiuwen 未就绪，未开始规划”，而非无限“正在思考”。

---

## 4. 唯一主动循环

### 4.1 事件和状态闭集

所有入口都必须调用同一个 `advance_v1_workflow(...)`，不允许 endpoint 自行决定下一阶段：

```text
space_entered
user_message
canvas_question_answered
candidate_direction_selected
goal_definition_confirmed
strategy_confirmed
timeline_proposal_confirmed
execution_feedback
weekly_review_due
retry
recovery_after_restart
```

工作流响应状态必须是闭集：

```text
running
awaiting_user_answer
awaiting_user_confirmation
completed
failed_retryable
```

禁止 V1 出现下列不透明情况：

- `idle`，但没有 `pending question`、`proposal`、`nextAction` 或可读阻塞原因；
- `running` 已过 deadline 仍继续显示“正在思考”；
- 用户选完方向后仍可重复写入同一选择；
- 已确认战略后还需要用户猜测要点击哪个入口才会生成时间线。

### 4.2 循环不变量

每一轮结束前，Orchestrator 必须满足恰好一项：

| 结果 | 必须存在的内容 |
| --- | --- |
| `running` | `turn_id`、`started_at`、`deadline_at`、当前动作说明 |
| `awaiting_user_answer` | 一个且仅一个待答关键问题，或 2–3 个候选方向 |
| `awaiting_user_confirmation` | 一份明确的战略/时间线/计划 proposal 与对应确认动作 |
| `completed` | 已进入稳定执行阶段，无待处理动作 |
| `failed_retryable` | 闭集错误码、用户可读原因、无副作用的重试动作 |

`nextAction` 不是前端猜测，而是由 Orchestrator 根据 session、proposal 和 pending interaction 计算并持久化/响应。

---

## 5. 用户进入空间后的完整体验

### 5.1 创建或进入一个有根目标的 V1 空间

```text
用户创建「30 天内做一个 Python 数据分析小工具」
  ↓
space_entered
  ↓
Orchestrator 锁定本空间的一次 turn，记录 workspace_entered
  ↓
OpenJiuwen 读取：根目标、意图、已确认事实、已有节点、已确认提案、上轮审计摘要
  ↓
整体判断（2–4 句）+ 最多 3 个关键维度
  ↓
无战略分叉：做暂定并进入下一步
有战略分叉：一个关键问题 或 2–3 个候选方向
```

前端在 OpenJiuwen 正在工作时显示紧凑、可解释的状态，例如“正在形成整体判断（最多约 45 秒）”。它不是静态占位文案；由服务端 `running + deadline_at` 驱动。

首轮不能生成十个问题卡、任务、具体课程、每天安排或时间线。

### 5.2 阶段一：判断、讨论、战略

内部依旧保留既有分析节点和约束：目标重构、问题结构、战略路径；用户默认只看到 `true_intent`、`key_conflict`、`goal_definition` 的摘要卡与当前焦点，其他维度默认折叠为内部分析容器。

OpenJiuwen 每轮遵守：

- 先给战略判断，再决定是否提问；
- 全局关键追问总数最多 3；同一焦点连续追问最多 1；
- 用户低信息回复两次时，给候选方向或暂定综合，不继续盘问；
- 用户选择候选方向后，自动重新评估并更新 `goal_definition`，不展开更多问题；
- 工具、课程、IDE、资料、每天几点等执行细节，在阶段一禁止出现；
- 每轮最多更新 3 个允许的分析维度；未知 key 丢弃；用户事实、AI 假设和公开依据必须区分。

阶段一的完成条件不是“10 个节点都回答了”，而是已有：

1. 可确认的 `goal_definition`；
2. 至少一个 `key_conflict`；
3. 足以决定取舍的约束/杠杆/风险判断；
4. 一份战略草案（主线、并行线、暂缓项、风险控制、取舍）。

满足后，Orchestrator 自动进入 `awaiting_user_confirmation(strategy)`；用户确认战略前不写正式阶段节点。

### 5.3 第一目标：确认战略后自动形成粗时间架构

用户点击“确认战略”后，不应再看到“继续形成战略”或额外的生成按钮。

```text
strategy_confirmed
  ↓
Orchestrator 自动启动 OpenJiuwen 粗时间架构回合
  ↓
产出 3–6 个阶段：范围、目标、可交付成果、完成标准、依赖、风险缓冲
  ↓
写入 timeline proposal（不是正式 PlanNode）
  ↓
前端自动切换或聚焦到时间线视图
  ↓
awaiting_user_confirmation(timeline)
```

时间线显示规则：

- 有截止日期：使用真实日期区间、月与里程碑；
- 无截止日期：相对周是唯一真值（第 1–2 周、约第 3–5 周），禁止伪造日期；
- 阶段必须绘制为主轴内的区间条及终点里程碑，不得仅显示在时间线轴上方的卡片；
- 用户可以调整顺序、范围或缓冲；系统解释影响并生成新版草案，不静默改写；
- 确认前一律是 `draft`，确认后才按既有 proposal/transaction 写入正式阶段计划。

这一步就是“想清楚”结束的验收标志。

---

## 6. 第二目标：从粗时间架构到月、周、日

### 6.1 何时细化

用户确认粗时间线后，工作流进入 `detailed_timeline_draft`。AI 不应一次性生成数月的每日任务；先生成最近可执行窗口：

- 月度/阶段内关键里程碑；
- 当前周计划；
- 下一周预览；
- 当前周少量日工作块。

用户可通过“细化当前阶段”或“生成本周计划”请求进入，自动化只负责准备提案，正式写入继续要求确认。

### 6.2 时间对象的层级

```text
根目标
  → 战略路径
    → 粗时间阶段
      → 月度里程碑 / 阶段关键动作
        → 当前周计划 + 下周预览
          → 当前日工作块
```

每个对象要保留上层关联，支持从日任务回溯到根目标与战略原因。

### 6.3 月、周、日计划约束

- 月度层描述阶段关键动作和里程碑，不重复堆砌每日任务；
- 周计划仅包含当前周和下周预览；
- 日计划最多拆为少量可执行工作块，基于可用时段/容量时使用排期器；没有容量信息时明确标为待校准，不能假装已排入具体日期；
- 所有月/周/日的创建或变更必须生成 proposal，确认后才落库；
- 已完成的历史节点与反馈不得被后续细化或重规划改写。

---

## 7. 每周自动回顾与未来重规划

### 7.1 触发

以下任一事件进入 `weekly_review_due`：

- 当前周末首次进入空间；
- 用户主动点击“本周回顾”；
- 用户提交执行反馈；
- 当前周计划期限结束且仍有活跃任务。

Orchestrator 自动汇总完成度、未完成项、反馈与风险；它可以自动形成回顾判断，但不能自动改写计划。

### 7.2 重规划流程

```text
执行反馈 / 周末
  ↓
OpenJiuwen：偏差是否影响后续阶段？原因是什么？
  ↓
影响轻微：保留计划，给出下周建议
影响显著：创建 replan proposal
  ↓
用户确认
  ↓
只调整未来阶段、未来月/周/日，旧版本归档为可恢复历史
```

重规划规则不得改变：

- 已完成阶段、已完成任务、历史反馈永不改写；
- 当前/未来未完成周计划及任务可归档为历史版本，不能物理删除；
- 新版本是唯一活跃版本；
- 每次替换记录原因、前后版本关系和影响范围；
- 所有操作继续走 proposal → confirm → version check → transaction。

---

## 8. OpenJiuwen 输出契约

不要让 OpenJiuwen 返回自由文本后由多个端点猜测下一步。每个回合使用当前阶段专属、闭集的结构化输出。下面是概念契约，字段名可按现有 schema 命名实现。

```json
{
  "decision": "ask | offer_options | provisional_synthesis | strategy_draft | coarse_timeline_draft | monthly_detail_draft | weekly_review | replan_draft",
  "strategicThesis": "2-4 句判断",
  "dimensionUpdates": [{"nodeKey": "key_conflict", "judgment": "..."}],
  "criticalQuestion": {"text": "...", "focusKey": "..."},
  "candidateDirections": [{"key": "...", "title": "...", "reason": "..."}],
  "strategy": {"mainLine": "...", "parallelLine": "...", "defer": "...", "riskControl": "...", "tradeoff": "..."},
  "timeline": {"phases": []},
  "nextAction": "await_answer | await_confirmation | continue_automatically"
}
```

服务端仍须按阶段裁剪：

| 当前阶段 | 可接受决策 | 强制拒绝 |
| --- | --- | --- |
| 初步思考/目标重构 | `ask`、`offer_options`、`provisional_synthesis`、`strategy_draft` | 时间线、月/周/日计划 |
| 问题结构/战略 | `strategy_draft` 或一个关键问题 | 执行细节、正式计划写入 |
| 战略已确认 | `coarse_timeline_draft` | 周/日任务 |
| 粗时间线已确认 | `monthly_detail_draft`、周/日计划提案 | 改写已完成历史 |
| 周回顾 | `weekly_review`、`replan_draft` | 静默变更未来计划 |

解析失败、字段越界、未知节点、模型超时和 OpenJiuwen 不可用必须进入 `failed_retryable`，不落半成品。

---

## 9. 超时、并发和恢复

每个 OpenJiuwen 工作回合必须持久化：

```text
turn_id
workflow_stage
trigger
started_at
deadline_at
attempt
source=openjiuwen
status
idempotency_key
```

- 同一 workspace 同时只能有一个 `running` turn；第二请求读取已有状态，不重复出模型调用；
- 默认 45 秒到期；到期任务转 `failed_retryable`，审计写 `v1_step_timed_out`；
- 刷新页面、服务重启或重新进入空间时，先检查过期 `running`：转换为可重试失败，而不是永久 loading；
- 重试复用同阶段/同幂等语义，不重复创建节点或提案；
- 每个工作流入口及自动推进都记录审计事件，包含 stage before/after、trigger、next action、阻塞原因、模型来源与校验结果；
- 审计不得包含原始提示词、隐藏推理链、API Key、连接串或未脱敏敏感内容。

---

## 10. 前端体验要求

1. **进入空间即看到活动。** 紧凑状态条显示“正在形成整体判断”，有 deadline 后的明确失败/重试，不占用对话主体。
2. **对话区是主要交互。** 全局判断、一个问题、候选方向、战略确认、时间线确认均在右侧完成；不把内部分析维度渲染为十张问卷卡。
3. **画布保持层级和简洁。** 默认根目标、分组、最多 3 个核心判断；其他内部维度折叠；节点正文默认只有标题和一句 judgment，详情放右侧。
4. **时间线是粗时间架构的主呈现。** 战略确认后自动聚焦/切换到时间线，阶段以轴内区间和里程碑出现。
5. **每个等待都可解释。** 明确显示“等待你的回答”“等待确认战略”“等待确认时间线”或“本轮失败，可重试”，禁止仅显示“正在处理”。
6. **不恢复旧噪声。** 不恢复顶部大横幅、React Flow Controls、十个默认问题节点、无意义的诊断提示。

---

## 11. 必须保留的不变量

- V1 分析约束与问题预算保持，不放宽成自由问卷；
- OpenJiuwen 不直接写业务表；
- 所有正式计划、时间线、月/周/日任务和重规划均经过 proposal → 用户确认 → 版本校验 → transaction；
- 无截止日期不阻止战略和粗时间架构，只能使用相对周；
- 公开研究仍只是证据，不替代战略判断，不伪造来源；
- 老空间、V0.1 与非 V1 路径不迁移、不重写；
- 审计导出继续有效并能解释自动推进与失败。

---

## 12. 实施拆分与提交

为了保持每个提交可运行，按以下四段完成；禁止一个大提交把运行时、服务、UI 与验收混在一起。

### R1：OpenJiuwen 强制运行时与 turn 生命周期

- V1 专用 `V1_REQUIRE_OPENJIUWEN`、真实 source 校验与准确失败；
- `turn_id/deadline/status`，超时和服务恢复；
- 清除/弃用 V1 旧的被动 `advance(...)` 入口，所有 V1 入口汇入 `advance_v1_workflow(...)`；
- 不改变业务阶段产物。

提交：`feat(agent): require OpenJiuwen and recover v1 workflow turns`

### R2：主动战略到粗时间架构循环

- 实现进入空间自动 Orientation；
- 收束 `goal_reframe → problem_structure → strategy_draft`，禁止无解释 idle；
- 战略确认自动生成粗时间线 proposal；
- 在时间线主轴显示草案并自动聚焦；
- 保持正式写入确认边界。

提交：`feat(agent): drive strategy to coarse timeline with OpenJiuwen`

### R3：月/周/日细化工作流

- 确认粗时间线后生成月度里程碑、当前周/下周和日工作块的提案；
- 将可用容量接入时才排到具体日期；没有容量时明确待校准；
- 保留版本化周计划语义。

提交：`feat(agent): refine confirmed timelines into monthly weekly and daily plans`

### R4：周回顾与重规划闭环

- 周末/反馈自动启动回顾 turn；
- 形成可确认的未来重规划 proposal；
- 保证历史不可变、未来版本化替换与审计完整。

提交：`feat(agent): automate weekly review and future-only replanning`

每段先提交，再进入下一段。若当前工作树有用户未提交内容，先报告并停止，不得混入提交。

---

## 13. 验收场景

使用一个真实 OpenJiuwen、本地新建 V1 空间：

> 我想在 30 天内掌握 Python，并做一个可展示的数据分析小工具。

验收：

1. 创建/进入空间后，不需要先点击按钮，45 秒内出现 OpenJiuwen 的整体判断；来源明确是 OpenJiuwen。
2. 首屏最多一份判断、3 个核心摘要、一个问题或三项候选方向；不出现十个问题卡。
3. 用户选择一个方向后，系统自动重新判断并收束目标，不可重复写同一选择。
4. 最多三次关键澄清后，自动给出战略草案；不存在无解释 `idle` 或无限“正在思考”。
5. 用户确认战略后，自动生成 3–6 个粗时间线阶段并切换/聚焦时间线；阶段均为轴内区间条或里程碑。
6. 用户确认时间线后，才出现月度、当前周/下周和日计划的细化入口；任何正式写入之前均可见 proposal。
7. 提交“本周只完成 40%”后，系统自动形成回顾判断和未来重规划草案；确认后已完成历史不变，旧未完成版本归档，新未来计划唯一活跃。
8. OpenJiuwen 不可用或超过 45 秒时，显示可重试失败，审计中能看到 `source=openjiuwen`、失败码与下一步；绝不静默降级成直连模型。
9. 导出 JSON/Markdown 可以按顺序复原：进入空间 → Orientation → 用户交互 → 战略 → 粗时间线 → 细化 → 回顾/重规划。

只有以上九项均通过，才可认为此次“主动循环”重构完成。
