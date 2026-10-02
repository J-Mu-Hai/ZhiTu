"""领域枚举的唯一定义处。

模型层与 API 契约层都从这里取,避免出现"数据库一套、接口一套"的漂移。
取值一律小写,与前端现有字面量一致(例如 GrowthNode.status 的 pending|doing|completed)。
"""

from __future__ import annotations

from enum import StrEnum


class WorkspaceStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class BriefStatus(StrEnum):
    DRAFT = "draft"
    CONFIRMED = "confirmed"
    SUPERSEDED = "superseded"


class NodeType(StrEnum):
    GOAL = "goal"
    CAPABILITY = "capability"
    STAGE = "stage"
    TASK = "task"
    MILESTONE = "milestone"


class PlanningLevel(StrEnum):
    """一个节点处于哪一层规划。**与 `NodeType` / `NodePurpose` 都正交。**

    - `NodeType` 回答"这是什么"(goal/task/stage/…);
    - `NodePurpose` 回答"要不要排期";
    - `PlanningLevel` 回答"它在哪一层"(战略 / 阶段 / 月 / 周 / 日)。

    ## 为什么单独一列,而不用 title 或 node_type 推断

    "三月重点"与"本月重点"可以挂在任何 `node_type` 上,而标题文本不能当语义
    (一个叫"本周"的阶段和一个叫"本周"的周计划长得一样)。规划层级需要被**明确
    写入、明确校验**,不能靠猜。

    ## 存量兼容

    这一列**可空**。旧节点全部是 `None`(unspecified),不回填、不重写。没有层级的
    节点行为与加这一列之前完全一样。

    ## 它不等于"已排期"

    它只表达语义层级,不进入排期算法。具体哪天做仍由排期器按 `estimate_minutes` /
    `deadline` / 容量算出。
    """

    STRATEGY = "strategy"
    PHASE = "phase"
    MONTH = "month"
    WEEK = "week"
    DAY = "day"


#: 从粗到细的规划层级顺序。**层级顺序只在这里定义一处** —— 校验(父粗子细)、
#: 确认战略的判定、以及前端标签全部读它。
PLANNING_LEVEL_ORDER: tuple[PlanningLevel, ...] = (
    PlanningLevel.STRATEGY,
    PlanningLevel.PHASE,
    PlanningLevel.MONTH,
    PlanningLevel.WEEK,
    PlanningLevel.DAY,
)

#: 层级 -> 粗细排名(越小越粗)。禁止一个较细的层级当较粗层级的父节点。
PLANNING_LEVEL_RANK: dict[PlanningLevel, int] = {
    level: index for index, level in enumerate(PLANNING_LEVEL_ORDER)
}


class NodePurpose(StrEnum):
    """这个节点是**用来排期的**,还是**只用来记事**的。

    **与 `NodeType` 正交,不是它的第六个成员。** 规范 §2.5 把这件事说得很清楚:
    "信息用途"与 Goal/Project/Task 的规划层级是不同维度。一个 `task` 可以是待办,
    也可以是一份"我知道了这个事实"的记录 —— 前者要占日历,后者不能。

    为什么必须单独一列而不是只靠 `node_type` 推断:`capability` 今天既被用来表示
    "我要练出这个能力"(要排期),也被用来表示"我了解到的情况"(不要排期)。**同一个
    类型值承载两种相反的排期语义**,靠类型推断就一定要在某个地方写一张猜的表,
    而那张表没法回答"用户到底想要哪一种"。让用户建的时候明说,问题就消失了。

    排期语义(§5.2、§4.1):
    - `PLANNING`:参与排期。要有工时才有得排,可以被依赖。
    - `INFORMATION`:信息主题。**不需要工时、完成勾选或截止日期**,不进排期预览、
      不计入完成度,也不能作为硬排期依赖的端点。

    默认 `PLANNING` —— 存量节点在加这一列之前全都按可排期对待,默认值必须保持
    它们的行为不变(见迁移 docstring:"升级后没有任何节点的排期行为发生变化")。
    """

    PLANNING = "planning"
    INFORMATION = "information"


class NodeStatus(StrEnum):
    PENDING = "pending"
    DOING = "doing"
    COMPLETED = "completed"
    ARCHIVED = "archived"


class Priority(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class NodeOrigin(StrEnum):
    """节点是用户自己建的还是 AI 提的。用于界面区分,也是排查问题的关键线索。"""

    USER = "user"
    AI = "ai"


class AnalysisFreshness(StrEnum):
    """一条分析的新鲜度。**由输入版本与结构摘要现算,不落库。**

    规范里写的是三档"最新 / 过期 / 需要重新分析"。这里只有两档,因为第三档不是
    另一个状态:它说的是**过期之后该做什么**,而不是数据本身多了一种处境。
    把它做成第三个枚举值,就得额外存一个"用户点过重新分析、但还没做完"的标记
    —— 而那个标记要么靠一次写入去维护、要么靠时间猜,两条都比"界面把 `stale`
    说成「需要重新分析,点这里」"更容易出错。

    所以:`FRESH` 是最新,`STALE` 是过期,是否需要重新分析由界面按 `STALE` 呈现。
    """

    FRESH = "fresh"
    STALE = "stale"


class DependencyType(StrEnum):
    FINISH_TO_START = "finish_to_start"


class NodeRelationType(StrEnum):
    """画布上除"前置"之外的关系类型。

    **与 `DependencyType` 并列,不合并成一个枚举。** 两者看起来都是"节点之间的边",
    差别却是实质性的:

    - `finish_to_start` 是**排期算法的输入** —— 排期器要用它算"这个任务最早能排到哪天";
      这两个类型不参与排期,一条 `influences` 不该让任何任务往后挪。
    - `dependencies` 还有 `lag_days`(前置完成后要等几天),这两个没有。
    - 两者存在**两张表**里,唯一约束的形状也不同:前置按有向的
      `(predecessor, successor)` 去重,`related_to` 是无向的。

    合并的后果是排期代码里开始出现"哪种类型才算前置"的分支,而那是把已经分开的
    两件事重新搅在一起。接口层有一份统一的三类型视图(见 `contracts/plan.py` 的
    `RelationType`),但那是**投影**,不是存储。
    """

    #: 只是相关。**无向** —— A 关联 B 和 B 关联 A 是同一条边,写入前会规范化。
    RELATED_TO = "related_to"
    #: A 影响 B。有向,但**不参与排期**:它表达"这件事做得好不好会影响那件事",
    #: 而不是"必须先做 A"。
    INFLUENCES = "influences"


class ScheduledSessionStatus(StrEnum):
    PLANNED = "planned"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    SKIPPED = "skipped"
    MOVED = "moved"
    CANCELED = "canceled"


#: 已发生、不可再被重排改动的 session 状态。规则"已完成记录不被重排覆盖"依赖这个集合。
FROZEN_SESSION_STATUSES = frozenset(
    {
        ScheduledSessionStatus.DONE,
        ScheduledSessionStatus.SKIPPED,
        ScheduledSessionStatus.CANCELED,
    }
)


class ScheduledSessionOrigin(StrEnum):
    SCHEDULER = "scheduler"
    USER = "user"
    AI = "ai"


class ExecutionResult(StrEnum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    SKIPPED = "skipped"
    FAILED = "failed"


class ConversationKind(StrEnum):
    PRIMARY = "primary"
    SIDE = "side"


class ConversationStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class ModelSource(StrEnum):
    """这次回复到底是谁生成的。

    存在的意义:此前模型调用失败会静默回退到硬编码关键词规则,用户完全无法分辨。
    现在它必须落库,也必须出现在接口响应里。
    """

    OPENJIUWEN = "openjiuwen"
    DIRECT_LLM = "direct_llm"
    RULE_FALLBACK = "rule_fallback"
    UNAVAILABLE = "unavailable"
    #: **测试脚手架,不是产品能力。** 只有 `AGENT_REASONER=script`(那要靠显式设的
    #: `ZHITU_SCRIPTED_ACTIONS` 才启得来,见 `agent/runtime/scripted.py`)会产生它。
    #: 产品里没有任何一条路径能落到这个值上。
    #:
    #: 为什么要单独一个成员,而不是让脚本化的实现借用 `DIRECT_LLM`:那个值会**落库**、
    #: 会出现在接口响应里、会变成对话徽标上的一句话。借用它,隔离栈里那次演示就会在
    #: 库里留下"这一轮是直连模型生成的",而它是脚本 —— 那正是这个枚举存在的全部理由
    #: (见上面那段:静默回退到规则最危险的地方是它看起来像真的)。宁可让它一眼可辨。
    #:
    #: 新增**成员**不需要迁移:`SAEnum(native_enum=False)` 在 SQLAlchemy 2.x 上默认
    #: `create_constraint=False`,列上没有 CHECK 约束(实测 DDL 就是 `VARCHAR(32)`)。
    SCRIPTED = "scripted"


class DegradedReason(StrEnum):
    OPENJIUWEN_NOT_INSTALLED = "OPENJIUWEN_NOT_INSTALLED"
    NO_API_KEY = "NO_API_KEY"
    MODEL_TIMEOUT = "MODEL_TIMEOUT"
    MODEL_OUTPUT_INVALID = "MODEL_OUTPUT_INVALID"
    MODEL_AUTH_FAILED = "MODEL_AUTH_FAILED"
    MODEL_RATE_LIMITED = "MODEL_RATE_LIMITED"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    CIRCUIT_OPEN = "CIRCUIT_OPEN"


class ProposalStatus(StrEnum):
    DRAFT = "draft"
    VALIDATED = "validated"
    PENDING_CONFIRMATION = "pending_confirmation"
    APPLIED = "applied"
    REJECTED = "rejected"
    STALE = "stale"
    FAILED = "failed"
    SUPERSEDED = "superseded"


class ProposalOp(StrEnum):
    CREATE_NODE = "create_node"
    UPDATE_NODE = "update_node"
    DELETE_NODE = "delete_node"
    #: 改写某个节点的**长正文**(`node_notes`),不是 `plan_nodes` 上的那一列。
    #: §7 的矩阵里 Notes 只能提案 —— 所以它必须是一个动作,而不是让
    #: `update_node` 多一个字段(`update_node` 的白名单是 `plan_nodes` 的列)。
    UPDATE_NOTE = "update_note"
    CREATE_DEPENDENCY = "create_dependency"
    DELETE_DEPENDENCY = "delete_dependency"
    #: 连一条「相关」或「影响」边(写 `node_relations`,不参与排期)。
    #:
    #: 与 `CREATE_DEPENDENCY` 分开,是因为它们写的是**两张表**、语义也不同:
    #: 前置会改变排期,这两种只是说明。见 `db/models/enums.py::NodeRelationType`。
    #:
    #: 新增这个**成员**不需要迁移:`proposal_items.op` 是
    #: `enum_type(native_enum=False)`,SQLAlchemy 2.x 默认 `create_constraint=False`,
    #: 列上是一条 `VARCHAR(32)`,没有 CHECK 约束(实测 DDL;
    #: `data/zhitu_dev.db` 里 `proposal_items.op VARCHAR(32) NOT NULL`)。
    #: `node_relations` 表本身也已经在 `eab5fc18adde` 迁移里建好了。
    CREATE_RELATION = "create_relation"
    SCHEDULE_SESSIONS = "schedule_sessions"
    UNSCHEDULE_SESSIONS = "unschedule_sessions"
    MOVE_SESSION = "move_session"
    LOCK_SESSION = "lock_session"
    UPDATE_BRIEF = "update_brief"
    UPDATE_CAPACITY = "update_capacity"


class RevisionTrigger(StrEnum):
    """必须是闭集,否则复盘分析会退化成无法查询的自由字符串。"""

    INITIAL_PLAN = "initial_plan"
    USER_EDIT = "user_edit"
    EXECUTION_DEVIATION = "execution_deviation"
    BRIEF_CHANGE = "brief_change"
    DEADLINE_CHANGE = "deadline_change"
    MANUAL_REPLAN = "manual_replan"


class RevisionActor(StrEnum):
    USER = "user"
    AI = "ai"
    SCHEDULER = "scheduler"
    SYSTEM = "system"


class AvailabilitySource(StrEnum):
    RULE = "rule"
    EXCEPTION = "exception"
    USER_BLOCK = "user_block"


class QuestionResponseMode(StrEnum):
    """用户可以用什么方式回答一个问题。

    选项是**加速器,不是限制** —— 所以 `MIXED` 允许“点一个再加上自己的话”,
    而 `allow_custom_input` 是比 response_mode 更细的一层开关(单/多选也可以
    允许补一句)。
    """

    SINGLE_SELECT = "single_select"
    MULTI_SELECT = "multi_select"
    FREE_TEXT = "free_text"
    #: 既可以选项,也可以自由输入。
    MIXED = "mixed"


class QuestionStatus(StrEnum):
    """问题节点的生命周期。

    `pending -> answered -> investigating -> resolved`,或 `pending -> archived`。
    - `answered`:用户已经答了,但后续处理还没跑完。
    - `investigating`:**“正在处理”**。前端据此显示处理中,避免用户以为答案丢了。
      模型失败时停在这里(答案与状态都已落库,刷新后仍读得回)。
    - `resolved`:后续处理成功,稳定落点/提案已经产生。
    - `archived`:用户跳过、或问题失效不再追问。
    """

    PENDING = "pending"
    ANSWERED = "answered"
    INVESTIGATING = "investigating"
    RESOLVED = "resolved"
    ARCHIVED = "archived"


#: 算是“还需要用户看到/还在处理”的状态。读取接口默认只返回这些。
ACTIVE_QUESTION_STATUSES = frozenset(
    {QuestionStatus.PENDING, QuestionStatus.ANSWERED, QuestionStatus.INVESTIGATING}
)


class QuestionUserAction(StrEnum):
    """记录在问题上的用户动作,用于审计“他答了/跳过了/说稍后”。"""

    ANSWERED = "answered"
    SKIPPED = "skipped"
    DEFERRED = "deferred"


class ReasoningAction(StrEnum):
    """一次 reasoning state 的下一步动作。**服务端据此决定循环怎么走。**"""

    READ_TOOL = "read_tool"
    ASK_USER = "ask_user"
    SYNTHESIZE = "synthesize"
    STOP = "stop"


class ReasoningStatus(StrEnum):
    """reasoning state 的生命周期。

    `blocked` 是“模型/工具失败或预算耗尽”——数据已经保存,可以恢复,但这一轮
    没有得出可应用结论。
    """

    UNEXPLORED = "unexplored"
    EXPLORING = "exploring"
    BLOCKED = "blocked"
    RESOLVED = "resolved"
    ABANDONED = "abandoned"


class ToolCallStatus(StrEnum):
    OK = "ok"
    ERROR = "error"
    #: 参数/权限/未知工具被注册表拒绝 —— 与 `error`(执行时出错)分开。
    REJECTED = "rejected"


class ResearchCacheStatus(StrEnum):
    """研究缓存的跨进程状态。

    - `fetching`:某个 worker 持有租约,正在出网;其他 worker 不得重复出网。
    - `success`:TTL 内的成功结果,命中直接返回。
    - `failed`:失败/无结果;**不当作成功缓存**,可被下一次重新接管。
    """

    FETCHING = "fetching"
    SUCCESS = "success"
    FAILED = "failed"


#: 模型可见事实的来源类型。**所有进入 state / 工具结果 / 分析的内容都带一个。**
PROVENANCE_SOURCES = ("user", "system", "tool", "model_inference", "assumption")


# ---------------------------------------------------------------------------------
# 目标推理地图(阶段 7)。
#
# 这一组枚举属于**推理层**,与 `plan_nodes` 的业务规划层严格分开:它们不会出现在
# 排期、依赖、任务统计或执行记录里,也不与 `NodeType` / `PlanningLevel` 混用。
# ---------------------------------------------------------------------------------
class ReasoningSessionPhase(StrEnum):
    """一次目标推理会话处于哪个阶段。**与单轮动作分开** —— 见 `ReasoningTurnAction`。"""

    STRATEGIC_EXPLORATION = "strategic_exploration"
    STRATEGIC_CONVERGENCE = "strategic_convergence"
    AWAITING_STRATEGY_CONFIRMATION = "awaiting_strategy_confirmation"
    EXECUTION_PLANNING = "execution_planning"
    MONITORING = "monitoring"


class ReasoningTurnAction(StrEnum):
    """一次 Agent turn 的主要动作。阶段与动作必须分开:同一阶段可以有不同的下一步。"""

    ASK_USER = "ask_user"
    ANALYZE = "analyze"
    EXPAND = "expand"
    CONFIRM = "confirm"
    PAUSE = "pause"
    COMPLETE = "complete"
    REVISIT = "revisit"


class ReasoningSessionStatus(StrEnum):
    """自动进入的幂等状态。

    - `idle`:还没探索过;
    - `running`:某次探索正在进行(租约/并发保护);
    - `ready`:最近一次探索成功,当前地图与输入一致;
    - `failed`:最近一次探索失败或输出不合法;可安全重试,不写半成品。
    """

    IDLE = "idle"
    RUNNING = "running"
    READY = "ready"
    FAILED = "failed"


class ReasoningNodeType(StrEnum):
    """推理地图节点的类型。**不是 `NodeType`** —— 它不参与计划。"""

    #: 一个需要用户表明取舍/偏好的决策维度(用途、去向、成功定义…)。
    DIMENSION = "dimension"
    #: 需要用户或研究回答的问题。
    QUESTION = "question"
    #: 风险 / 约束。
    RISK = "risk"
    #: 资源(时间、资金、人脉、信息)。
    RESOURCE = "resource"
    #: 可选的战略路线。
    ROUTE = "route"
    #: 尚未验证的假设。
    ASSUMPTION = "assumption"


class ReasoningNodeStatus(StrEnum):
    UNEXPLORED = "unexplored"
    EXPLORING = "exploring"
    RESOLVED = "resolved"
    PAUSED = "paused"
    ARCHIVED = "archived"


class ReasoningSource(StrEnum):
    """一个推理节点的结论从哪来。**用户说的与研究得来的必须分得开。**"""

    AGENT = "agent"
    USER = "user"
    RESEARCH = "research"


class ReasoningLinkType(StrEnum):
    """推理层的边。**不可复用业务依赖/关系表。**"""

    DEPENDS_ON = "depends_on"
    INFLUENCES = "influences"
