"""提案、提案条目与确认台账。

提案是"AI 想做但还没做"的完整描述。用户点确认之前,它不产生任何写入。

三处刻意不建外键,都是为了打断建表期的循环引用(PostgreSQL 需要 ALTER TABLE
补约束,而 SQLite 不支持 ALTER TABLE ADD CONSTRAINT):

- Proposal 不存 message_id   -> 关联由 messages.proposal_id 单侧持有。
- Proposal 不存 base_revision_id -> 用 base_revision_version + workspace_id 定位,
  (workspace_id, version) 本身就有唯一约束。
- Proposal 不存 applied_revision_id -> 关联由 plan_revisions.proposal_id 单侧持有。

三处都没有信息损失,只是把同一份事实从两个地方挪到一个地方。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db.base import Base, JsonDict, TimestampMixin, UtcDateTime, UuidPk, enum_type
from backend.db.models.enums import ProposalOp, ProposalStatus, RevisionTrigger


class Proposal(UuidPk, TimestampMixin, Base):
    __tablename__ = "proposals"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL")
    )

    status: Mapped[ProposalStatus] = mapped_column(
        enum_type(ProposalStatus, "proposal_status"),
        default=ProposalStatus.DRAFT,
        nullable=False,
    )

    # 提案是基于哪个计划版本生成的。用户确认时若 workspace.current_revision_version
    # 已经不等于它,说明用户在提案生成后改过计划 —— 必须重新校验或重新生成,
    # 不能直接覆盖用户的改动。
    base_revision_version: Mapped[int] = mapped_column(Integer, nullable=False)

    trigger_type: Mapped[RevisionTrigger] = mapped_column(
        enum_type(RevisionTrigger, "proposal_trigger"), nullable=False
    )

    # 为什么这样安排。给用户看的解释,不是内部日志。
    reasoning: Mapped[str | None] = mapped_column(Text)
    # 每项带 source: user_stated / model_assumed。
    assumptions: Mapped[dict | None] = mapped_column(JsonDict)
    # {add: [...], update: [...], delete: [...]} —— 将要新增/修改/删除什么。
    change_summary: Mapped[dict | None] = mapped_column(JsonDict)
    # {required_minutes, available_minutes, safety_factor_applied, feasible,
    #  deficit_minutes, options: [...]} —— 工作量与冲突的检查结果。
    workload_check: Mapped[dict | None] = mapped_column(JsonDict)
    conflict_check: Mapped[dict | None] = mapped_column(JsonDict)

    # 内容的规范化哈希。用于"同一个幂等键配了不同内容"的检测,以及判断
    # refresh 之后提案是否真的变了。
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    error_code: Mapped[str | None] = mapped_column(String(48))
    error_detail: Mapped[str | None] = mapped_column(String(500))

    expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    decided_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    workspace: Mapped[Workspace] = relationship()  # noqa: F821
    items: Mapped[list[ProposalItem]] = relationship(
        back_populates="proposal", order_by="ProposalItem.ordinal", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_proposals_workspace_id_status_created_at", "workspace_id", "status", "created_at"),
        Index("ix_proposals_user_id", "user_id"),
    )


class ProposalItem(UuidPk, Base):
    """提案里的一条变更。

    刻意做成**行**而不是一个 JSON 大对象:校验需要能精确指出"第 7 条引用了不存在的
    n7",界面需要能高亮到具体某一行。JSON blob 两件事都做不到。
    """

    __tablename__ = "proposal_items"

    proposal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("proposals.id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)

    op: Mapped[ProposalOp] = mapped_column(enum_type(ProposalOp, "proposal_op"), nullable=False)

    # 模型看到的和产出的都是 n1..nK 这样的本地记号,不是真实 UUID。
    # create_node 条目在这里登记自己的记号,后续条目通过它引用新节点。
    local_id: Mapped[str | None] = mapped_column(String(32))
    # 已存在节点的真实 id。新节点在被确认前没有真实 id,所以这里可以为空。
    target_node_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("plan_nodes.id", ondelete="CASCADE")
    )

    payload: Mapped[dict] = mapped_column(JsonDict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)

    proposal: Mapped[Proposal] = relationship(back_populates="items")

    __table_args__ = (
        UniqueConstraint("proposal_id", "ordinal", name="uq_proposal_items_proposal_id_ordinal"),
    )


class ProposalDecision(UuidPk, Base):
    """确认/拒绝的幂等台账。

    用户双击"确认"不应该建两遍节点。做法是:先插这条台账(唯一键 user_id +
    idempotency_key),插入成功才执行;插入冲突说明这个请求已经处理过,
    直接把当时存下来的响应原样返回。

    这也顺带挡住了"同一个幂等键配不同请求体"的滥用。
    """

    __tablename__ = "proposal_decisions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    proposal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("proposals.id", ondelete="CASCADE"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    # 请求体的规范化哈希。
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # 当时返回给客户端的完整响应体,重放时原样返回。
    result: Mapped[dict] = mapped_column(JsonDict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "user_id", "idempotency_key", name="uq_proposal_decisions_user_id_idempotency_key"
        ),
        Index("ix_proposal_decisions_proposal_id", "proposal_id"),
    )
