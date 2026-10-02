# 阶段 7：目标推理智能体 P0 实施计划

## 1. 目的与完成定义

本计划落实《目标推理智能体设计规格》的 P0，不把现有系统继续修成“用户发一句、AI 回一句”的被动规划器，而是形成以下最小闭环：

```text
创建/首次进入根目标
  → 自动战略探索与问题地图
  → 选择一个当前焦点并说明理由
  → 用户在画布回答、选择、展开或暂缓
  → 增量更新地图与焦点
  → 形成待确认的战略草案
  → 用户确认战略
  → 使用既有提案机制进入阶段 / 周规划
```

完成后，“学习 Python”首次进入时不应只问兴趣并停住：系统应先给出目标用途、能力基础、最小能力、真实应用、时间/资源、验证方式等 4–8 个有决策价值的维度，说明当前优先问哪一个、为什么；确认战略后才请求执行排期所需的时间与能力条件。

这是一轮最后的核心认知编排改造；它不授权自动写入计划、自动排期、替用户决定战略，也不改变既有提案确认链路。

## 2. 现有能力与不可破坏的边界

| 可复用能力 | 在阶段 7 中的作用 |
| --- | --- |
| `plan_nodes`、planning level、proposal 校验/确认/版本事务 | 已确认战略后的阶段、月、周、日规划仍只走此链路 |
| `agent_questions` 与 CanvasQuestionNode | 每轮的高价值用户问题；需能关联问题地图节点 |
| `reasoning_states` 与 `tool_call_records` | 单个 turn 的可审计结论、工具记录和预算；不是长期问题地图本身 |
| `research_public`、来源 citations | 对公开客观信息提供证据；不能代替用户价值取舍 |
| 当前 `messages` / ConversationPanel | 面向用户的解释、战略草案和变更历史 |
| NodeSpace / React Flow | 承载问题地图、焦点、状态、用户选中和递归深入 |

必须保持：

- 所有 `plan_nodes`、依赖、关系、排期写入仍经 `proposal → 用户确认 → 版本校验 → 事务`；
- 推理地图不是任务树，不进入排期、任务统计、依赖或执行记录；
- 用户编辑的标题/描述不能被 Agent 静默覆盖；
- 不暴露或存储模型隐藏思维链，只保存可审阅的结论、假设、依据和选择理由；
- 自动进入空间必须幂等，不能每次进入重复生成一棵地图。

## 3. 最小目标架构

### 3.1 两层对象，不混写

`PlanNode` 继续代表已确认或待确认后将要执行的工作；新增持久化 `ReasoningNode`（具体表名可按项目命名风格确定）代表战略探索中的决策维度、问题、风险、资源或路线。根目标仍可引用既有 `PlanNode`，但首轮问题地图的 4–8 个一级节点不应伪装成任务。

建议新增三类持久化对象：

1. `goal_reasoning_sessions`
   - `workspace_id`、`root_plan_node_id`、当前 `phase`、`turn_action`、`focus_reasoning_node_id`；
   - `map_version`、最近输入/结构版本、首次探索时间、最后评估时间；
   - 自动进入的幂等键/完成状态，以及失败可重试状态。
2. `reasoning_nodes`
   - `session_id`、`parent_id`、可选 `linked_plan_node_id`；
   - `title`、`summary`、`node_type`、`status`、`next_action`；
   - 可解释的 `importance`、`uncertainty`、`urgency`、`impact`、`confidence`；
   - `rationale`、`assumptions`、`evidence`、`user_description`、`agent_summary`、`source`；
   - 创建/更新时间与乐观版本或 map version。
3. 必要时的 `reasoning_node_links`
   - 仅表达推理层的 `depends_on` / `influences`，不可复用业务依赖和业务关系表。

`AgentQuestion` 应可选关联 `reasoning_node_id`。问题回答后，服务端以此定位需重评的地图节点，而不是靠文本猜测。

### 3.2 状态机

会话阶段：

```text
strategic_exploration
→ strategic_convergence
→ awaiting_strategy_confirmation
→ execution_planning
→ monitoring
```

单轮动作：`ask_user | analyze | expand | confirm | pause | complete | revisit`。阶段与动作必须分开；每轮只有一个主要焦点，复杂目标最多同时呈现三个相互独立的高价值问题。

优先级是可解释启发式，不伪装为客观精确概率：

```text
priority = 0.30 × importance
         + 0.25 × uncertainty
         + 0.25 × impact
         + 0.20 × urgency
```

分值主要服务于排序；用户可见的是“为什么先处理它”的简短理由。

### 3.3 自动触发协议

新增或扩展为显式 Agent turn 接口，而不是借用空用户消息：

```text
POST /workspaces/{workspaceId}/agent/turn
{
  trigger: "space_entered" | "user_message" | "node_selected" |
           "question_answered" | "strategy_confirmation" | "progress_update",
  selectedNodeId?: string,
  reasoningNodeId?: string,
  message?: string,
  idempotencyKey: string,
  contextVersion?: number
}
```

`space_entered` 只在未探索、根目标内容/版本显著变化、或用户明确重试时创建新自动 turn；存在活动问题、正在运行或已完成且输入未变时返回当前地图/焦点，绝不重复建节点。前端进入空间显示可取消的“正在梳理问题”状态，最终以服务端结果为准。

## 4. 分五步实施

### 步骤 1：数据契约、迁移与服务骨架

- 先阅读当前 Agent turn、`PlanNode`、`AgentQuestion`、`ReasoningState`、proposal、NodeSpace 和现有迁移；
- 新增阶段/动作/推理节点状态等闭集 enum、模型、Alembic migration、Pydantic contract；
- 建立 session、地图节点、链接和可选 question 关联；创建合适的唯一约束与索引；
- 设计当前 map 读取接口，返回 session、节点、链接、焦点和面向用户的焦点解释；
- 不在本步骤接入模型自动写入，也不修改 `plan_nodes` 语义。

验收：迁移 `upgrade head/current`、模型一致性、跨空间隔离、删除 workspace 级联、用户描述与 Agent 摘要字段分离。

### 步骤 2：首次进入的幂等战略探索

- 实现 `space_entered` 触发与幂等记录；前端真实调用它，`enterSpace` 不再只是设置 `spaceId`；
- 复用现有 turn context、工具循环、研究工具和消息落库，新增受契约约束的“推理地图操作”；
- 首轮生成 4–8 个一级决策维度节点、风险/假设/可解释评分、一个焦点和最多三个高价值问题；
- 严格校验操作数量、父节点、会话归属、合法 enum、分数范围、用户字段不可覆盖；失败、超时或输出不合法时不写半成品，返回可重试状态；
- “学习 Python”首轮必须形成用途、基础、最小能力、真实应用、时间/资源、能力验证等决策维度，不能默认 AI 路线，也不能直接生成周一到周日任务。

验收：首次进入自动生成；连续进入无重复；失败安全重试；无 Key 的降级明确且不写半成品；真实模型/脚本 reasoner 都可验证。

### 步骤 3：地图画布、焦点与节点交互

- 在现有 React Flow 中增加稳定的 `reasoning` 节点/边渲染类型，和业务 `growth` 节点、虚拟 question 节点清楚分开；
- 呈现状态、重要度层级、来源/假设标记、焦点高亮和简短理由；暂缓节点低强调但不消失；
- 节点详情显示 `user_description`（用户可编辑）与 `agent_summary`（Agent 维护）分区；
- 用户可点击“讨论 / 自动分析 / 展开 / 暂缓 / 标记完成 / 作为新空间深入”；操作通过显式 Agent turn，不靠前端拼状态；
- 保留画布稳定性修复：无闪烁、无重复 fit、无业务关系污染、遵守 `prefers-reduced-motion`。

验收：根目标下有持久化问题地图；选中节点改变焦点但不重建全图；用户编辑不被后续 turn 覆盖；自动进入/节点选中不重复生成。

### 步骤 4：回答后的重评与战略收敛

- 回答 `AgentQuestion` 或节点讨论后，关联事实、修正假设、更新受影响节点及依赖节点，重算优先级并选择新焦点；
- 助手只说明变化、当前焦点和理由，不每轮重复全图；
- 达到收敛条件后生成 2–4 条差异明确的战略路线，说明收益、成本、风险、适用条件和“不做什么”；
- 战略草案可以给出相对时间窗与阶段成果假设，例如“第 1–2 周 / 第 3–6 周”，但不得据此假装已掌握用户每周容量或排入日历；
- 用户确认某战略时，才通过现有 proposal 生成 `planning_level=strategy` 的业务节点；确认后再把关联写回 `linked_plan_node_id`。

验收：回答会实质改变地图/焦点；战略未确认不会生成执行任务；战略确认仍走现有 proposal 与版本校验。

### 步骤 5：执行桥接、端到端验收与收口

- 仅当已确认战略存在时，提供“细化第一阶段 / 生成阶段计划”的明确入口；
- 执行规划阶段才询问期限、每周可用时间、当前水平等会改变任务规模或排期的变量；
- 基于已确认战略生成阶段、里程碑、月/周计划提案，继续由用户确认写入；
- 执行反馈按影响范围重评局部节点或请求战略复审，不因一次未完成推翻战略；
- 补全脚本化 reasoner、mock research、隔离 E2E、真实模型人工验收说明和部署前检查。

验收：端到端通过“进入目标 → 地图 → 回答 → 战略草案 → 确认 → 阶段细化”；未确认 proposal 时业务图谱不变。

## 5. 关键产品规则

### 5.1 战略和执行的时间边界

| 层级 | AI 可以主动给出 | 必须由用户确认/补充后才做 |
| --- | --- | --- |
| 战略探索 | 决策维度、风险、相对时间窗、阶段成果假设、路线选项 | 价值取舍、成功定义、风险底线 |
| 战略收敛 | 2–4 路线、优先/暂缓项、战略原则 | 选定路线/战略草案 |
| 执行规划 | 基于战略的阶段与里程碑建议 | 截止时间、每周容量、当前水平、具体周/日排期 |

因此“不先问每周投入多少时间”和“战略层明确大致什么时间段完成什么成果”可以同时成立。

### 5.2 非目标

- 不改写用户根目标或用户描述；
- 不自动确认战略或计划；
- 不自动写入日历；
- 不把问题地图节点当作计划任务；
- 不暴露 Chain-of-Thought；
- 不把搜索结果或模型推断伪装为用户事实；
- 不做多 Agent、向量库或后台自治代理。

## 6. 验收场景

1. `学习 Python`：首次进入自动生成地图；优先问用途，给出“先形成可用能力、小项目暴露缺口”的待验证战略假设；不默认 AI、不问每天几小时。
2. `我要保研`：地图区分资格、去向、竞争力、时间窗口、风险；焦点问题说明会怎样改变路线。
3. 用户回答用途/去向：相关节点摘要、状态、分数、焦点发生实质变化。
4. 重复进入：不生成重复一级节点；根目标编辑后才创建新版本探索。
5. 用户手动改描述：后续 Agent turn 保留用户字段。
6. 战略草案：可见路线差异和相对时间窗；未确认时没有业务阶段/周任务。
7. 战略确认：产生既有 proposal，确认后才写 strategy 计划节点；再进入阶段细化。
8. 公开信息：真实/缓存研究显示来源；隐私拦截不出网、不伪造来源。
9. 模型超时、非法结构、并发进入：不写半成品、不重复建图，可重试。

## 7. 建议提交与验证纪律

建议按步骤至少拆为以下可回退提交：

1. `feat(reasoning): add persistent goal reasoning map`
2. `feat(agent): trigger idempotent exploration on space entry`
3. `feat(canvas): render interactive reasoning map and focus`
4. `feat(agent): converge reasoning map into confirmed strategy`
5. `test(agent): cover goal reasoning lifecycle end to end`

每一步运行相关单测；最终必须实际运行：

```text
python -m alembic -c backend/alembic.ini upgrade head
python -m alembic -c backend/alembic.ini current
python -m pytest backend/tests
python -m ruff check backend
npm run contracts:check
npm run typecheck
npm run lint
node scripts/dev/accept-e2e.mjs <本轮 fixture/命令>
git diff --check
git status --short
```

已确认的 Windows 基线环境失败若仍存在，必须先报告完整原始结果，再报告排除它们后的完整回归结果；不得把未运行写成通过。

## 8. 交给 Pi 的实施提示词

```text
你现在执行阶段 7：Goal Reasoning Agent P0。

工作目录：E:\代码\ZhiTu
分支：release/phase-4-strategy-loop
开始前先读取 docs/14-GOAL-REASONING-AGENT-P0-IMPLEMENTATION-PLAN.md 全文；它是本轮产品规格和验收标准。再执行 git status --short、git log --oneline -8，并确认当前 HEAD 与工作树状态。不要依赖旧对话记忆。

注意：`docs/14-GOAL-REASONING-AGENT-P0-IMPLEMENTATION-PLAN.md` 是本次交接刻意创建、但尚未提交的规格文档。不要删除、还原或忽略它；先以独立文档提交 `docs(agent): add goal reasoning P0 implementation plan` 纳入版本控制，再开始代码实现。除此文件外，若工作树存在其他未预期改动，先停下并报告。

目标：实现“进入根目标 → 自动问题地图 → 焦点选择 → 用户回答后更新地图 → 可确认战略 → 已确认战略后分层执行规划”的最小闭环。

先做只读架构核对，再按文档第 4 节的五步连续实现。不要只改提示词；必须实现后端持久化状态机、幂等的 space_entered Agent turn、结构化地图操作、前端地图/焦点交互和战略确认桥接。

必须复用并保持：
- 现有 proposal → 用户确认 → 版本校验 → 事务写入；
- `plan_nodes` 只代表业务计划，推理地图不能混入排期/依赖/统计；
- `agent_questions`、`reasoning_states`、research citations、CanvasQuestionNode；
- 用户编辑不被 Agent 覆盖；
- 默认不联网、没有真实 Key 的测试全部 mock/stub。

核心实现约束：
1. 新增独立持久化 reasoning session/map node（和必要 links）模型，而不是把 4–8 个探索维度伪装成任务；
2. `space_entered` 必须幂等：同一输入/版本重进不重复生成，模型失败不写半成品，可安全重试；
3. 首轮先生成有决策价值的问题地图、风险/假设/焦点理由，再最多问 1 个主要问题（复杂目标最多 3 个独立问题）；
4. 战略层允许相对时间窗和阶段成果假设，但不先问每周投入、不排周一到周日；
5. 用户回答后必须增量更新节点状态、摘要、排序和焦点，不能只是追加一条聊天消息；
6. 只有用户确认战略 proposal 后才生成真实 `planning_level=strategy` 节点；之后才进入阶段/月/周细化；
7. 画布显示 reasoning 节点、状态、焦点、依据与用户操作，且不回归此前修复的闪烁/拖动/关系边边界；
8. 所有正式计划写入继续要求用户确认。

不要做：多 Agent、向量库、自动日历写入、自动确认战略、暴露 CoT、把搜索/推断伪装为用户事实。

测试必须覆盖文档第 6 节场景，至少包括：学习 Python、保研、重复进入幂等、用户回答改变焦点、用户编辑保护、未确认不写计划、确认后才细化、模型失败不写半成品、隐私研究不出网。

按文档第 7 节的五个逻辑提交拆分；在同一次连续任务中完成。每个提交前跑相应测试，最后完整运行迁移、后端测试、ruff、contracts、typecheck、lint、隔离 E2E、diff 检查。真实模型/真实 Tavily 人工验收若未配置 Key，必须明确报告为未验证，不能用 mock 冒充。

不得修改或提交：docs/13-CURRENT-AGENT-RUNTIME-AUDIT.md、.env、真实 Key、data/、logs/、apps/web/.env.local、.next、artifacts。

最终报告必须给出：开始/结束 branch、HEAD、git status；迁移影响；每步实现；每个 commit SHA；完整测试实际结果；真实模型/联网是否验证；已知未完成项。
```
