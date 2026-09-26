"""领域事件。在提交事务的**同一个事务**里写入。

用途:站内提醒的触发依据("用户回来了""连续漏做""阶段完成"),以及复盘分析。
事件表不是审计日志的替代品,它记录的是"发生了什么值得回应的事"。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.db.base import Base, JsonDict, UtcDateTime, UuidPk


class DomainEvent(UuidPk, Base):
    __tablename__ = "domain_events"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE")
    )

    # 例如 workspace_created / proposal_applied / session_completed / plan_revised。
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    ref_type: Mapped[str | None] = mapped_column(String(32))
    ref_id: Mapped[uuid.UUID | None] = mapped_column()
    payload: Mapped[dict | None] = mapped_column(JsonDict)

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)

    __table_args__ = (
        Index("ix_domain_events_user_id_created_at", "user_id", "created_at"),
        Index("ix_domain_events_workspace_id_kind", "workspace_id", "kind"),
    )
