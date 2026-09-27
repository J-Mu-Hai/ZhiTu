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
    CREATE_DEPENDENCY = "create_dependency"
    DELETE_DEPENDENCY = "delete_dependency"
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
