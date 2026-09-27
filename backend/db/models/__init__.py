"""模型注册入口。

**导入本模块即保证 Base.metadata 是完整的。** Alembic 的 env.py 与建表测试都依赖这一点;
漏掉任何一个模型模块,autogenerate 都会认为那张表该被删掉。
"""

from backend.db.models.analysis import NodeAnalysis
from backend.db.models.conversation import Conversation, Message
from backend.db.models.enums import (
    FROZEN_SESSION_STATUSES,
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
    Priority,
    ProposalOp,
    ProposalStatus,
    RevisionActor,
    RevisionTrigger,
    ScheduledSessionOrigin,
    ScheduledSessionStatus,
    WorkspaceStatus,
)
from backend.db.models.event import DomainEvent
from backend.db.models.layout import NodePosition, ScopeViewport
from backend.db.models.note import NodeNote
from backend.db.models.plan import Dependency, NodeRelation, PlanNode, PlanRevision
from backend.db.models.proposal import Proposal, ProposalDecision, ProposalItem
from backend.db.models.reminder import ReminderState
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
    "FROZEN_SESSION_STATUSES",
    # 表
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
    "Priority",
    "Proposal",
    "ProposalDecision",
    "ProposalItem",
    "ProposalOp",
    "ProposalStatus",
    "ReminderState",
    "RevisionActor",
    "RevisionTrigger",
    "ScheduleApplication",
    "ScheduledSession",
    "ScheduledSessionOrigin",
    "ScheduledSessionStatus",
    "ScopeViewport",
    "User",
    "UserCapacityProfile",
    "Workspace",
    "WorkspaceStatus",
]
