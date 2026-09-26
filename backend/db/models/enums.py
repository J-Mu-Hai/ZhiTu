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


class DependencyType(StrEnum):
    FINISH_TO_START = "finish_to_start"


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
