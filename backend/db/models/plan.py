"""计划节点、依赖关系与计划版本。

这里体现产品最重要的一条建模纪律:**"要做什么"与"哪天做"分开**。

- PlanNode 回答"要做什么",带截止日期(deadline),但**不带**开始/结束的排期。
- ScheduledSession 回答"哪天做",一个 PlanNode 可以对应多个 Session。

所以一个 8 小时的任务永远是**一行** PlanNode + 若干行 ScheduledSession,
不会为了填满日历被复制成 8 个同名任务。

层级只用 parent_id 这一种表示(邻接表)。docs/05-DATA-MODEL.md 里把 parent 也列为
一种 edge,那是两套真相 —— 图和计划视图会因此各说各话,所以不采用。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db.base import Base, JsonDict, TimestampMixin, UtcDateTime, UuidPk, enum_type
from backend.db.models.enums import (
    DependencyType,
    NodeOrigin,
    NodeStatus,
    NodeType,
    Priority,
    RevisionActor,
    RevisionTrigger,
)


class PlanNode(UuidPk, TimestampMixin, Base):
    __tablename__ = "plan_nodes"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    # 自引用。刻意用 RESTRICT 而不是 CASCADE:级联删除会静默抹掉整棵子树,
    # 而删除必须是显式动作并写进计划版本。级联删除由服务层显式收集子树后逐个执行。
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("plan_nodes.id", ondelete="RESTRICT")
    )

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    # 验收标准。用户判断"这片树叶做完了没有"的依据,不是给 AI 看的。
    acceptance_criteria: Mapped[str | None] = mapped_column(Text)

    node_type: Mapped[NodeType] = mapped_column(
        enum_type(NodeType, "node_type"), default=NodeType.TASK, nullable=False
    )
    status: Mapped[NodeStatus] = mapped_column(
        enum_type(NodeStatus, "node_status"), default=NodeStatus.PENDING, nullable=False
    )
    priority: Mapped[Priority] = mapped_column(
        enum_type(Priority, "node_priority"), default=Priority.MEDIUM, nullable=False
    )

    # 预计工时(分钟)。是排期算法的输入,不是排期结果本身。
    estimate_minutes: Mapped[int | None] = mapped_column(Integer)
    deadline: Mapped[date | None] = mapped_column(Date)

    order_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 从父链算出的层级,冗余存储以便一次查询取出某一层的全部节点。
    depth: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    origin: Mapped[NodeOrigin] = mapped_column(
        enum_type(NodeOrigin, "node_origin"), default=NodeOrigin.USER, nullable=False
    )
    # 引入这个节点的计划版本。用于"AI 生成的阶段/任务在哪个版本出现"。
    plan_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("plan_revisions.id", ondelete="SET NULL")
    )

    completed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    # 软删除。历史版本里的快照仍要能引用到它。
    deleted_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    workspace: Mapped[Workspace] = relationship()  # noqa: F821
    parent: Mapped[PlanNode | None] = relationship(
        remote_side="PlanNode.id", back_populates="children"
    )
    children: Mapped[list[PlanNode]] = relationship(back_populates="parent")

    __table_args__ = (
        CheckConstraint(
            "estimate_minutes IS NULL OR estimate_minutes > 0", name="estimate_minutes_positive"
        ),
        CheckConstraint("depth >= 0", name="depth_non_negative"),
        Index("ix_plan_nodes_workspace_id_parent_id", "workspace_id", "parent_id"),
        Index("ix_plan_nodes_workspace_id_status", "workspace_id", "status"),
        Index("ix_plan_nodes_deadline", "deadline"),
    )


class Dependency(UuidPk, TimestampMixin, Base):
    """finish-to-start 前置关系。环由服务层用递归可达性探测拒绝,不靠数据库。"""

    __tablename__ = "dependencies"

    # 冗余 workspace_id:跨空间依赖校验和按空间取图都靠它,避免每次 JOIN plan_nodes。
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    predecessor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("plan_nodes.id", ondelete="CASCADE"), nullable=False
    )
    successor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("plan_nodes.id", ondelete="CASCADE"), nullable=False
    )
    dep_type: Mapped[DependencyType] = mapped_column(
        enum_type(DependencyType, "dependency_type"),
        default=DependencyType.FINISH_TO_START,
        nullable=False,
    )
    lag_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "predecessor_id", "successor_id", name="uq_dependencies_predecessor_id_successor_id"
        ),
        CheckConstraint("predecessor_id <> successor_id", name="no_self_dependency"),
        Index("ix_dependencies_workspace_id_successor_id", "workspace_id", "successor_id"),
    )


class PlanRevision(UuidPk, Base):
    """计划的版本记录。每次变更都产生一条,不覆盖历史。

    snapshot 与 diff 都存:只存 snapshot 的话,"V4 改了什么"每次都要重算;
    只存 diff 的话,回滚与对比需要从头重放。
    """

    __tablename__ = "plan_revisions"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)

    parent_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("plan_revisions.id", ondelete="SET NULL")
    )

    trigger_type: Mapped[RevisionTrigger] = mapped_column(
        enum_type(RevisionTrigger, "revision_trigger"), nullable=False
    )
    trigger_detail: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[RevisionActor] = mapped_column(
        enum_type(RevisionActor, "revision_actor"), nullable=False
    )
    proposal_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("proposals.id", ondelete="SET NULL")
    )

    snapshot: Mapped[dict] = mapped_column(JsonDict, nullable=False)
    diff: Mapped[dict] = mapped_column(JsonDict, nullable=False)

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)

    workspace: Mapped[Workspace] = relationship()  # noqa: F821

    __table_args__ = (
        UniqueConstraint("workspace_id", "version", name="uq_plan_revisions_workspace_id_version"),
        Index("ix_plan_revisions_workspace_id_version", "workspace_id", "version"),
    )
