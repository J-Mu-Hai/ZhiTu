# 现状 →《规划智能体与 NodeSpace 架构规范 v1.1》映射,与本批实施方案

- 规范:`E:\来自C盘下载\知途_Planning_Intelligence_与_NodeSpace_架构规范.md`,**版本 1.1**
  (2026-09-27,§14 版本记录第一条),据其 §11"实施入口:增量映射,不从头重建"。
- 审计方式:四路**只读**审计(AI 运行时与上下文 / 提案与写入 / 数据模型与排期 / 前端),
  全部结论都带 `文件:行号`。本文只写映射与方案,**动手前请先看第 4 节的"本批不实现什么"**。
- 写作日期:2026-09-27。基线提交:`3741870`。

---

## 1. 一句话结论

规范 §11 说的"接手时逐项核实并复用"那批东西(节点身份、NodeSpace 导航、关系编辑、布局保存、
归档恢复、正文自动保存与版本冲突、提案确认、排期与执行)**在代码里是真的存在的,而且是通的**。
本批**不需要**重建其中任何一件。

缺的是一件事,而它正好压在闭环的第一环上:

> **模型现在看不见节点正文,也看不见祖先、关系、子节点和"用户正盯着哪个节点"。**
> 它拿到的是整个空间的一张扁平节点表(handle / type / title / deadline / estimate / status,
> 最多 80 条)。`TurnContext` 里**已经算出了** `current_view` 与 `context_node_title`,
> 但渲染提示词的那一步从来不读它们 —— 值算出来了,没人用。

所以本批的重心不是"加智能体模块",而是**把已有的事实接进已有的工作流**,再把分析结果
按规范 §2.3 存下来、按 §2.3 的失效规则标过期。

---

## 2. 逐项映射

### 2.1 已经存在、直接复用(**不动**)

| 规范要求 | 代码现状 | 证据 |
|---|---|---|
| 一个主控工作流 + 能力模块(§6.1) | 一条链:`POST /workspaces/{id}/messages` → `conversation_service.submit_turn` → `turn_context.build_turn_context` → `reasoner.reason` | `api/routes/workspaces.py:167-222`、`services/conversation_service.py:191-279`、`services/turn_context.py:75-127` |
| agent 只提案不写库(§7.1、约束文件) | **结构性成立**:`backend/agent/` 全目录无 session/commit/AsyncSession,只 import 枚举与 config;真正落库在 `services/proposal_service` | `agent/runtime/*`、`services/proposal_service.py:233-266` |
| 模型输出受契约约束(§7.2) | 硬 JSON 契约:`reply` 必需,缺失即 `MODEL_OUTPUT_INVALID`;`brief` 闭集清洗;`actions` 过 pydantic 判别联合 | `agent/runtime/response.py:162-167`、`:252-318`、`contracts/proposal.py:138-177`、`services/proposal_validation.py:344-350` |
| op 白名单,不许模型任意写(§7.2) | 三档:`ACCEPTED_OPS` / `OP_NOT_YET_AVAILABLE`(排期类)/ `UNKNOWN_OP_TYPE`;悬空引用 `DANGLING_PROPOSAL_REF` | `contracts/proposal.py:152-177`、`proposal_validation.py:55,331-341` |
| 提案 → 用户确认 → 原子写入(§7.1) | 锁 → 版本比对 → 状态 CAS → 幂等台账 → 写入 → revision → DomainEvent → 一次 commit | `services/proposal_service.py:394-491`、`db/locking.py:33` |
| 确认时检查基准版本(§7.2) | `base_revision_version` ↔ `workspace.current_revision_version`,不符 409 `STALE_BASE_REVISION` | `db/models/proposal.py:57`、`proposal_service.py:398-410`、`api/errors.py:184-194` |
| 确认幂等(§7.2) | 两层:`proposal_decisions` 唯一键 + 重放比对 `content_hash` | `db/models/proposal.py:155-157`、`proposal_service.py:447-460,493-502,778-794` |
| 版本账本(§10) | `plan_revisions` 一次写事务一行、带 snapshot/diff;只有两个写入点 | `db/models/plan.py:237-274`、`node_service.py:1244`、`proposal_service.py:660` |
| 正文乐观锁(§2.1) | `content_version` 是**前置条件不是可写字段**;只有 `description`/`acceptance_criteria` 推进版本 | `contracts/plan.py:62,292-301`、`api/routes/plan.py:131-135`、`node_service.py:141,394-404` |
| NodeSpace = 节点为根的局部视图、ID 不变(§3.1) | 前端 `spaceId` / `currentSpaceId` / `canvasKey`;进入=设局部根,不复制节点 | `features/growth/provider.tsx:221,242,260,411` |
| 三个动作分开(§3.2) | 单击=开详情;`.node-enter`=进空间;面包屑=返回 | `components/growth/PathView.tsx:906,116-127`、`features/growth/selectors.ts:13-17` |
| 视口记忆与恢复(§3.2) | `viewports[scopeId]` 进后端 `PUT /layout`,每层只 fit 一次 | `provider.tsx:275,647-651`、`PathView.tsx:569-581`、`tests/layout.spec.ts:326` |
| 归属/关系/时间三种结构分开(§1.2) | `parent_id` 邻接表 + `dependencies`(参与排期)+ `node_relations`(不参与) | `db/models/plan.py:54,152,199` |
| 时间数据三分、任务跨天是场次不是复制节点(§9.0) | 1 个 `plan_nodes` + N 行 `scheduled_sessions`;执行记录 append-only | `db/models/schedule.py:46,204`、`plan.py:1-13` |
| 全局容量(§5.2) | 用户级 `UserCapacityProfile`(周预算/安全系数/单日上限),三档回落 | `db/models/user.py:165`、`services/schedule_service.py:344-362` |
| Today 按人聚合(§9.0) | 按 `user_id` 聚合全部活动空间,排除墓碑/软删/归档 | `services/execution_service.py:343-422` |
| 排期是确定性业务能力、LLM 不能"用嘴排"(§5.2) | `scheduler/` 是纯函数叶子包(AST 扫过,禁 sqlalchemy/httpx/"今天") | `scheduler/__init__.py:10-16`、`tests/unit/scheduler/test_purity.py` |
| 不确定就追问、一次最多两个(§6.3) | 提示词强制;规则兜底只问 `missing` 里的 | `prompts/planning.py:55-65,147`、`agent/runtime/rule_fallback.py:33-40` |
| "这是我猜的"要公开(§6.3) | `brief.assumptions.audit` 逐条记 `{field,value,source:user_stated\|model_assumed,at,messageId}`,且 `model_assumed` 永不写驱动排期的列 | `services/brief_service.py:137-165,226-230` |
| 模型来源与降级如实标注(§12.14) | `source`/`degraded`/`degraded_reason` 从 reasoner 一路到库到前端徽标 | `agent/runtime/__init__.py:58-83`、`db/models/enums.py:127-148`、`lib/backend.ts:1218-1259` |

### 2.2 需要扩展(骨架在,没接上)

| 规范要求 | 现状 | 差在哪 |
|---|---|---|
| 上下文解析:当前正文(§2.1、§6.3) | `plan_nodes.description` 在库里,`PlanNodeView` 里**没有**这个字段 | `agent/runtime/base.py:66-80`、`prompts/planning.py:244-252` |
| 祖先目标与约束继承(§3.1) | 只送**整个空间一份** planning_brief;没有按子树/祖先聚合 | `turn_context.py:89`、`db/models/workspace.py:73` |
| 关系与子节点(§3.1) | `load_nodes` 只 `select(PlanNode)`,不查 dependencies / node_relations | `turn_context.py:48-53` |
| scope / 焦点节点(§3.1、§6.2) | `TurnContext.current_view`、`context_node_title` **已算出但从不渲染**;`messages.context_node_id` 只落库 | `turn_context.py:124-125` vs `response.py:128-138`;`db/models/conversation.py:103-105` |
| 必要历史(§6.3 近期执行记录) | 只有复盘路径把偏差渲染成一条"伪装用户消息" | `services/review_service.py:284-290,349-374` |
| 未知项(§2.3) | 只有 3 个固定槽位(deadline / weekly_available_minutes / current_level) | `agent/runtime/base.py:51-62`、`brief_service.py:221` |
| 分析结果落库(§2.3) | **没有这张表** | 全仓无 `NodeAnalysis`/`node_analyses` |
| 分析失效(§2.3) | 今天唯一"内容变了"的信号 = 一次正文保存产生新 `plan_revisions` 行;没有过期标记、没有入口 | `docs/10-NEXT-BATCH-SCOPE.md` §8 第一条 |
| 影响预览(§8) | 只有删除节点的 `archive_impact`(挂在对话框上);提案侧只有 delete 的 `affected_children`,且**含根自身**(契约注释说不含) | `node_service.py:547-582`、`proposal_validation.py:534,548`、`contracts/proposal.py:213` |
| `Proposal` 的 workload/conflict 检查(§7.2) | 两列**存在但从无写入者** | `db/models/proposal.py:71-72` |

### 2.3 需要数据库迁移

**只有一件**:新增 `node_analyses`(§2.3)。其余全部落在现有表上:

- 执行/实际投入 → `execution_records`(已有,字段够:actual_minutes / completion_ratio / result / delay_reason / user_feedback)
- AI 引发的计划变更 → 现有 `proposals` / `proposal_items` / `plan_revisions`(trigger 已有 `execution_deviation`)
- 时间容量与已排场次 → `user_capacity_profiles` / `scheduled_sessions`(只读)

### 2.4 明确"不存在"的东西(别把它们当已有的用)

| 规范条目 | 事实 |
|---|---|
| `NodeAnalysis` | 没有表、没有接口、没有 UI。`docs/09-...md:283-284` 只是设计意向 |
| Notes / Materials(用户长文本) | 没有表。节点长文本目前只能落 `plan_nodes.description` |
| Checklist | 没有表,`docs/10` §3 把它列为"纳入但未完成" |
| Constraint / Evidence 实体 | **没有**。约束只是 `planning_briefs.constraints` 的 JSON 数组,接口再投影成 `list[str]`;`scheduler/errors.py:50` 的 `BindingConstraint` 是**排期内部错误码,不是实体** |
| 短描述/摘要字段(§2.1) | `plan_nodes` 没有 summary/short_description,也没有 `maturity` |
| 工具调用 / 检索 / 证据引用(§6.3) | 全仓零 `tool_call`;模型回复不带"我依据哪条数据" |
| 画布镜头协议(§9.2) | 零。唯一原语是用户手点的「聚焦所选」`fitView({nodes:[id]})` |
| 提案预览接口(§7.2) | 只有 list / confirm / reject 三个;**没有 preview**,逐条接受也不支持 |
| 对话/提案/重排的端到端用例 | **零覆盖**。只有"发一句 + 刷新还在" |

---

## 3. 本批只做这一个闭环

> 节点内容 → AI 理解与分析 → 必要追问 → 局部规划提案 → 用户确认 → 可靠更新

分五步,每步都能单独验证;前四步之间没有"做完一半就不可用"的中间态。

### 步骤 A:让模型真的看得见(上下文解析器)

- `TurnContext` 补上:焦点节点与其**祖先链**的正文、当前 scope 的**直属子节点**、与焦点相关的
  **关系边**(前置/关联分行渲染)、焦点节点上的**排期与执行摘要**(只读)。
- **正文不是全空间都带**:只给"焦点 + 祖先 + 直属子节点"带正文全文,范围外节点只带
  title/type/status/deadline/estimate。这就是规范 §3.1 的"有范围",也顺手挡住了
  "全空间 80 个节点 × 1000 字"的提示词膨胀。
- 把已经算出、但从来没被渲染的 `current_view` / `context_node_title` **接进模板** ——
  这一条是"模型知道用户正盯着哪个节点"的全部实现。
- 同时算出一份 **输入版本快照**:读到的每个节点记 `content_version`,外加
  `workspace.current_revision_version`(结构版本)与 brief 的更新时间。步骤 B 用它判过期。
- **验证**:prompt 渲染断言(含正文、含祖先约束、含焦点节点)+ **一条负面断言**
  (范围外节点的正文不许出现在提示词里)+ 现有 `tests/test_turn_context.py` 全绿。

### 步骤 B:分析落库 + 过期(规范 §2.3)

- **迁移一张表** `node_analyses`:owner/workspace/`scope_root_id`/`focus_node_id`、
  `input_versions`(JSON,按节点记正文版本 + 结构版本)、`known[]`/`unknowns[]`/`evidence[]`/
  `assumptions[]`/`diagnosis[]`/`strategy_options[]`/`risks[]`/`confidence_note`、
  `maturity`(新枚举:想法/基本理解/结构形成/具备执行条件)、来源三件套
  (`model_source`/`prompt_version`/`model_name`)、时间戳。**append-only**(像 `plan_revisions`),
  读取取最新一行。
- **过期是"读的时候算出来的",不落 `stale` 列。** `input_versions` 里任一节点当前的
  `content_version` 不等于记录值 → 返回 `stale: true` + `staleReasons[]`(逐条说清是哪个节点
  的正文改过);结构版本不等 → 保守失效,并在 reason 里**写明这是保守失效**(规范 §2.3 最后
  一条明确允许这么干,但要求不声称做了精确依赖追踪)。
  理由:落一列 `stale` 就必须在每次用户编辑时记得去写它,漏一次就是"旧分析冒充最新";
  派生出来的过期永远不会忘记更新,而且规范要的语义("只标记、不自动覆盖原分析")天然满足。
- **模型返回之后、落库之前,重读一次输入版本**:期间用户改过 → 这次的分析仍存(读取时自然
  显示过期),但**不把 actions 变成提案**,并在回复里说清"这几条是基于旧版本给的,请重新分析"。
  规范 §2.3:"不能直接成为可应用提案"。
- 输出契约扩展:reasoner 的 JSON 里多一个可选的 `analysis` 对象。**模型不给就不落行**,
  不假装有分析;字段容错(与现有 `brief` 的 `clean_value` 同一套路子)。
- **追问收紧为可验证规则**:追问 ≤2 条,且每条必须能对上 `unknowns[]` 里的一项;
  "不重复问已知"在**规则兜底**上是确定性的(它只问 `KnownConditions.missing`),可以直接断言。
- **验证**:契约/服务/迁移三层测试 + **两条反向验证**(去掉重读版本比对 → "旧结果不冒充最新"
  那条必须红;把过期判定改成恒 false → 过期那条必须红)。

### 步骤 C:时间边界(§三,只读,不假装排期)

- 把焦点节点与祖先的**已排场次、计划工时、用户周预算**(`UserCapacityProfile`,沿用现有三档
  回落)读进上下文 —— 这样 AI 说"每周三小时装不下"时是有依据的。
- **不新增任何排期写入**:六个排期类 op 继续留在 `OP_NOT_YET_AVAILABLE`,prompt 明说
  "你没有排期写入能力,不得声称已经排好日子";只有 deadline 没有估时的情形,提示词要求
  说明缺口而不是补一个默认工时。
- **验证**:断言提示词含容量与已排场次;断言排期类 op 仍然被拒(已有机制,补一条钉住)。

### 步骤 D:前端(分析区 + 重新分析入口)

- 详情弹窗里加一块**折叠的「AI 分析」**:**七栏**(已知/还缺什么/依据/假设/判断/可选的走法/风险)
  + 出处与时间 + 只读了一部分的说明 + 可信度那句话 + **过期徽标** + 「根据最新内容重新分析」按钮。
  没有分析时显示服务端给的那句"还没有分析过"。
- **`maturity`(成熟度)不做。** 步骤 B 落库时按"分析草稿不能冒充正式约束"这条边界把它去掉了:
  一个"想法/基本理解/结构形成/具备执行条件"的枚举看起来像一个**可以据以行动的档位**,
  而它是模型自评的、没有判据、也没人核对过。本节原来那版写着"成熟度",与已交付的契约不一致。
- **徽标在收起状态也要准**,所以组件**一挂载就读一次**(只读最近一条),不是等展开才读 ——
  验收路径"改正文 → 徽标变过期"要能一眼看见,不能要求用户先展开。
- **过期是读的时候算的,界面不许本地推**:正文保存成功只是**触发重读**(`refreshToken`),
  点重新分析之后也是**重读一次**而不是把徽标改成"最新"。那两条都有断言钉着(读次数)。
- 配色沿用现有语义色(过期用杏色、最新用鼠尾草绿),`dark-theme.spec.ts` 的暖白守门已把这块
  加进去(**先点开两层再扫**,理由见那个文件里资料编辑表单那条的前例)。
- 按钮走**同一个工作流**:`POST /workspaces/{id}/nodes/{nid}/analysis/refresh` 内部仍调
  `submit_turn`,只是替用户说了那句"根据最新正文重新分析",并追加一条助手消息 ——
  这样对话与按钮不是两个真相。那句话写在服务层(`analysis_service.REANALYZE_MESSAGE`),
  前端不复制一份。
- **`inputChanged` 的呈现**:对话里说明"它回答时你说的情况已经变了"并**指向节点详情里的
  入口,不给按钮** —— 在对话末尾再放一个,用户看不出它要重新分析的是哪个节点。
- **验证**(Playwright,隔离栈,独立账号):四条行为用例 + 一条正文重读用例 + 一条暖白扫描。
  如实说明这三段的分工:浏览器里能确定造出来的是**降级与前端这一半**(没有记录时不改口、
  徽标只读不问不推、正文保存后重读、重新分析真的在对话里留下两条消息);
  **"改正文 → 服务端判定过期 → 重新分析 → 回到最新"这条完整链路**由
  `backend/tests/test_analysis_staleness.py` / `test_analysis_refresh.py` 用固定 reasoner 钉着 ——
  隔离栈没有模型 key,而**降级不产生分析记录**(这条边界本身是对的),所以浏览器里
  没有一条真记录可过期。这一点不是"没验",是分了两层;哪一层验的什么写在
  `apps/web/tests/node-analysis.spec.ts` 的文件头里。

### 步骤 E:收口

- **修一个缺陷**:AI 提案里改正文(`update_node` 带 `description`)**不推进 `content_version`**
  (`contracts/proposal.py:94-112` 没有这个字段,`proposal_service.py:610-617` 只 `setattr`)。
  后果是"用户手持旧版本 → 静默盖掉一条经用户确认的 AI 改写",正好违反 §7.1"不覆盖原文"。
  按 `BODY_FIELDS` 同一判据推进版本号。
- 更新文档:`05-DATA-MODEL.md` 加新表、`06-AGENT-DESIGN.md` 补分析层,`docs/10` §8 把
  "分析过期"那条划掉,`docs/08` 记本轮验收。

---

## 4. 本批**不实现**什么(写下来免得被读成"做完了")

| 不做 | 为什么 |
|---|---|
| 短描述/摘要字段、AI 改写正文 | 会立刻产生"AI 读哪个"的分叉;规范 §2.1 说它"不是对现有正文的截断规则",本批**不新增第二个正文来源** |
| Notes / Materials 表 | 与闭环无关;现在没有表,也没有界面承诺 |
| Checklist 表与编辑器 | 规范 §11 明说"不强制同时建设完整 Checklist 编辑器";本批只在分析里把它标成**候选**并说明"还没实现,所以只是建议" |
| 独立 Evidence / Constraint 表 | 没有检索能力时它只是一张没人查的空表。本批把证据与约束**作为分析里的结构化条目**(每条带来源与时间)保存,并**如实在界面标注它们来自 AI 分析** |
| 多 Agent / 长期记忆 / 向量库 | 规范 §11 明确排除 |
| 画布镜头协议(§9.2) | 属规范阶段 5;本批**不**做自动聚焦、不做跟随开关 |
| 提案差异预览接口、逐条接受 | 后端没有 preview 接口,逐条接受要算依赖闭包;规范 §7.2 说未实现前不要提供不安全的逐项勾选 |
| 六个排期类 op | 属后续批次;本批只保证"不假装排期" |
| 年月周日的完整界面 | 规范 §9.0 允许分批;本批不动时间线 |
| UI 全面重做 | 用户明确要求不重做 |

---

## 5. 迁移、备份与回滚

- **一次迁移,纯加法**:只新建 `node_analyses`。**不加列、不改列、不删列、不动任何现有行**,
  更不会碰到用户的正文。
- **不需要备份**(与 `c4f8a1d6e9b3` 那一轮同一判断):迁移不修改既有数据。若用户要求,
  可以按在线备份 API(`con.backup`)先取一份 `data/zhitu_dev.db.bak-<时间戳>`。
- **回滚**:`alembic downgrade -1` 只删这张新表;它没有任何外键指向它,现有功能不受影响。
- 双库纪律:纯加法表不含 partial index,但建表仍要保证 SQLite 与 PostgreSQL 同一套模型
  (`test_migration_matches_models.py` 逐列对拍)。
- 落实"改开发库/重启开发后端要用户点头":迁移只跑在**隔离测试库**上;开发库要升级时,
  先说明再动手。

---

## 6. 怎么验收(三层分开报告,不许混)

1. **结构化契约与规则测试**(隔离栈、无模型 key):`PYTHONUTF8=1 python -m pytest backend/tests`,
   新增用例覆盖上下文渲染、输入版本、派生过期、重读版本后不建提案、追问规则。
2. **模拟模型**:用规则兜底与 stub reasoner 跑"追问—分析—提案—确认"的完整链路,
   重点是**确定性断言**(追问条数、不重复问已知、范围外不泄露、过期后不许应用)。
3. **真实模型连续体验**:一个**独立测试账号**,按用户给的脚本连走一遍 ——
   模糊目标 → 补时间限制 → 深入子节点 → 改正文约束 → 重新分析 → 确认局部调整 → 刷新验证。
   检查:理解上下文 / 不重复问已知 / 拆解合理 / 不重复建节点 / 尊重范围 / 按新信息修正建议。
4. **反向验证**:每条关键断言都要有"先让它红"的证据(至少两条,见步骤 B)。

**如实标注**:真实模型需要 (a) 开发后端重启加载本批代码,(b) `.env` 里有可用的 key。
两者任一不成立,就报告"本层未验证 + 当前降级到哪一档",**不用模拟测试冒充真实模型结果**。
自动化用隔离库与 stub,与真实模型验收分开报;模型来源
`openjiuwen / direct_llm / rule_fallback / unavailable` 逐条如实写。

---

## 7. 风险与已知限制

1. **提示词变长**会挤占上下文:本批用"只给焦点/祖先/直属子节点带正文"来限流,并给出
   字数上限;超限时**截断哪一个必须写进回复**,不能静默截断(规范 §2.1 禁止静默搬迁/截断)。
2. **保守失效**会让无关编辑也要求重算(规范 §2.3 明确要求说明这一点,不许声称做了精确依赖追踪)。
3. **规则兜底下的分析是空的**:没有 key 时 `rule_fallback` 只能追问、`actions=()` 恒空 ——
   所以第 1、2 层能验的是**契约、失效与拒绝**,不能验"分析好不好"。
4. **AI 提案改正文的版本号缺陷**(步骤 E)在本批修,但**已经产生的历史提案**不受影响。
5. **端到端零覆盖的那块**(对话/提案/重排)本轮会被新用例部分覆盖,但不会一次补齐 ——
   本批只新增与闭环直接相关的用例,如实报告覆盖率没有变好的地方。
