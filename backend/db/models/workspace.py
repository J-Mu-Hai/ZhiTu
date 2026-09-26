"""成长空间与规划简报。

空间是计划的容器。"Python 学习""保研准备""英语提升"各是一个空间,各有独立的
计划与独立的对话。跨空间唯一共享的是用户的时间预算(见 models/user.py)。
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
from sqlalchemy import (
    text as sql_text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db.base import Base, JsonDict, TimestampMixin, UtcDateTime, UuidPk, enum_type
from backend.db.models.enums import BriefStatus, WorkspaceStatus


class Workspace(UuidPk, TimestampMixin, Base):
    __tablename__ = "workspaces"

    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    intent: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[WorkspaceStatus] = mapped_column(
        enum_type(WorkspaceStatus, "workspace_status"),
        default=WorkspaceStatus.ACTIVE,
        nullable=False,
    )

    # 当前计划版本号。每提交一次变更(用户编辑或确认 AI 提案)自增。
    # 提案过期检测就是比对提案的 base_revision_version 与这个值。
    #
    # 刻意不建 current_revision_id 外键:那会让 workspaces 与 plan_revisions 互相引用,
    # 在 PostgreSQL 里必须用 ALTER TABLE ADD CONSTRAINT 补,而 SQLite 不支持;
    # 按 (workspace_id, version) 查即可,没有额外的表达能力损失。
    current_revision_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai", nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    owner: Mapped[User] = relationship()  # noqa: F821 - 由 models/__init__ 统一注册

    __table_args__ = (
        # 刻意不加 (owner_id, title) 唯一约束:产品允许同名空间。
        Index("ix_workspaces_owner_id_status", "owner_id", "status"),
    )


class PlanningBrief(UuidPk, TimestampMixin, Base):
    """规划所需的已知条件。由对话逐步补齐,每补齐一次产生新版本。

    assumptions 里每一项都带 source:
        user_stated  —— 用户明确说过的
        model_assumed —— AI 的推断,尚未获得确认
    这是"不把猜测保存成已确认事实"的机制保证,不是靠提示词自觉。
    """

    __tablename__ = "planning_briefs"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)

    goal: Mapped[str | None] = mapped_column(Text)
    success_criteria: Mapped[str | None] = mapped_column(Text)
    current_level: Mapped[str | None] = mapped_column(Text)
    deadline: Mapped[date | None] = mapped_column(Date)
    weekly_available_minutes: Mapped[int | None] = mapped_column(Integer)

    constraints: Mapped[dict | None] = mapped_column(JsonDict)
    risks: Mapped[dict | None] = mapped_column(JsonDict)
    assumptions: Mapped[dict | None] = mapped_column(JsonDict)

    status: Mapped[BriefStatus] = mapped_column(
        enum_type(BriefStatus, "brief_status"), default=BriefStatus.DRAFT, nullable=False
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    workspace: Mapped[Workspace] = relationship()

    __table_args__ = (
        UniqueConstraint("workspace_id", "version", name="uq_planning_briefs_workspace_id_version"),
        # 每个空间至多一份"已确认"的简报。
        # 注意:postgresql_where 与 sqlite_where 必须同时给 —— 只给前者会让本地 SQLite
        # 静默失去这个约束,而那正是"以为有唯一性其实没有"这类 bug 的来源。
        Index(
            "uq_planning_briefs_confirmed",
            "workspace_id",
            unique=True,
            postgresql_where=sql_text("status = 'confirmed'"),
            sqlite_where=sql_text("status = 'confirmed'"),
        ),
        CheckConstraint(
            "weekly_available_minutes IS NULL OR weekly_available_minutes >= 0",
            name="weekly_available_minutes_non_negative",
        ),
    )
