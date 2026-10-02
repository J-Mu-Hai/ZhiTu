"""模型注册入口。

**导入本模块即保证 Base.metadata 是完整的。** Alembic 的 env.py 与建表测试都依赖这一点;
漏掉任何一个模型模块,autogenerate 都会认为那张表该被删掉。
"""

from backend.db.models.analysis import NodeAnalysis
from backend.db.models.conversation import Conversation, Message
from backend.db.models.enums import (
    ACTIVE_QUESTION_STATUSES,
    FROZEN_SESSION_STATUSES,
    PLANNING_LEVEL_ORDER,
    PLANNING_LEVEL_RANK,
    PROVENANCE_SOURCES,
    AnalysisFreshness,
    AvailabilitySource,
    BriefStatus,
    ConversationKind,
    ConversationStatus,
    DegradedReason,
    DependencyType,
    ExecutionResult,
    MessageRole,
    ModelSource,
    NodeOrigin,
    NodeRelationType,
    NodeStatus,
    NodeType,
    PlanningLevel,
    Priority,
    ProposalOp,
    ProposalStatus,
    QuestionResponseMode,
    QuestionStatus,
    QuestionUserAction,
    ReasoningAction,
    ReasoningStatus,
    ResearchCacheStatus,
    RevisionActor,
    RevisionTrigger,
    ScheduledSessionOrigin,
    ScheduledSessionStatus,
    ToolCallStatus,
    WorkspaceStatus,
)
from backend.db.models.event import DomainEvent
from backend.db.models.layout import NodePosition, ScopeViewport
from backend.db.models.note import NodeNote
from backend.db.models.plan import Dependency, NodeRelation, PlanNode, PlanRevision
from backend.db.models.proposal import Proposal, ProposalDecision, ProposalItem
from backend.db.models.question import AgentQuestion
from backend.db.models.reasoning import ReasoningState, ToolCallRecord
from backend.db.models.reminder import ReminderState
from backend.db.models.research import ResearchCache, ResearchDailyQuota
from backend.db.models.schedule import ExecutionRecord, ScheduleApplication, ScheduledSession
from backend.db.models.user import (
    AuthSession,
    AvailabilityException,
    AvailabilityRule,
    User,
    UserCapacityProfile,
)
from backend.db.models.workspace import PlanningBrief, Workspace

__all__ = [
    # 枚举
    "ACTIVE_QUESTION_STATUSES",
    "FROZEN_SESSION_STATUSES",
    "PLANNING_LEVEL_ORDER",
    "PLANNING_LEVEL_RANK",
    "PROVENANCE_SOURCES",
    # 表
    "AgentQuestion",
    "AnalysisFreshness",
    "AuthSession",
    "AvailabilityException",
    "AvailabilityRule",
    "AvailabilitySource",
    "BriefStatus",
    "Conversation",
    "ConversationKind",
    "ConversationStatus",
    "DegradedReason",
    "Dependency",
    "DependencyType",
    "DomainEvent",
    "ExecutionRecord",
    "ExecutionResult",
    "Message",
    "MessageRole",
    "ModelSource",
    "NodeAnalysis",
    "NodeNote",
    "NodeOrigin",
    "NodePosition",
    "NodeRelation",
    "NodeRelationType",
    "NodeStatus",
    "NodeType",
    "PlanNode",
    "PlanRevision",
    "PlanningBrief",
    "PlanningLevel",
    "Priority",
    "Proposal",
    "ProposalDecision",
    "ProposalItem",
    "ProposalOp",
    "ProposalStatus",
    "QuestionResponseMode",
    "QuestionStatus",
    "QuestionUserAction",
    "ReasoningAction",
    "ReasoningState",
    "ReasoningStatus",
    "ReminderState",
    "ResearchCache",
    "ResearchCacheStatus",
    "ResearchDailyQuota",
    "RevisionActor",
    "RevisionTrigger",
    "ScheduleApplication",
    "ScheduledSession",
    "ScheduledSessionOrigin",
    "ScheduledSessionStatus",
    "ScopeViewport",
    "ToolCallRecord",
    "ToolCallStatus",
    "User",
    "UserCapacityProfile",
    "Workspace",
    "WorkspaceStatus",
]
