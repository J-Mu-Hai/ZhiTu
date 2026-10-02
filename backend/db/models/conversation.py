"""对话与消息。

每个空间有自己的对话,所以对话必须挂在 workspace_id 上,消息不能是一个全局数组。

新建空间时**没有任何会话行**。之前前端在空空间里凭空造出一条保研示例对话,
用户看到的第一句话不是自己说的 —— 那是产品层面最直接的"假装"。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
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
from backend.db.models.enums import (
    ConversationKind,
    ConversationStatus,
    DegradedReason,
    MessageRole,
    ModelSource,
)


class Conversation(UuidPk, TimestampMixin, Base):
    __tablename__ = "conversations"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )

    kind: Mapped[ConversationKind] = mapped_column(
        enum_type(ConversationKind, "conversation_kind"),
        default=ConversationKind.PRIMARY,
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(200), default="主对话", nullable=False)
    status: Mapped[ConversationStatus] = mapped_column(
        enum_type(ConversationStatus, "conversation_status"),
        default=ConversationStatus.ACTIVE,
        nullable=False,
    )
    last_message_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    workspace: Mapped[Workspace] = relationship()  # noqa: F821
    messages: Mapped[list[Message]] = relationship(
        back_populates="conversation", order_by="Message.seq"
    )

    __table_args__ = (
        # 每个空间至多一条活动的主对话。新空间零行 —— 主对话在用户第一次说话时创建,
        # 不在建空间时预置。
        Index(
            "uq_conversations_primary",
            "workspace_id",
            unique=True,
            postgresql_where=sql_text("kind = 'primary' AND status = 'active'"),
            sqlite_where=sql_text("kind = 'primary' AND status = 'active'"),
        ),
        Index("ix_conversations_workspace_id_status", "workspace_id", "status"),
    )


class Message(UuidPk, Base):
    __tablename__ = "messages"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    # 冗余 workspace_id 与 user_id:鉴权与按空间取历史都不必 JOIN conversations。
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    role: Mapped[MessageRole] = mapped_column(
        enum_type(MessageRole, "message_role"), nullable=False
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)

    proposal_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("proposals.id", ondelete="SET NULL")
    )
    context_node_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("plan_nodes.id", ondelete="SET NULL")
    )

    # 这条回复是谁生成的,以及是否降级。模型失败时静默回退到关键词规则的旧行为
    # 之所以危险,就是因为这三列当时不存在 —— 事后无法分辨哪条回复是真模型。
    model_source: Mapped[ModelSource | None] = mapped_column(
        enum_type(ModelSource, "model_source")
    )
    degraded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    degraded_reason: Mapped[DegradedReason | None] = mapped_column(
        enum_type(DegradedReason, "degraded_reason")
    )
    prompt_version: Mapped[str | None] = mapped_column(String(32))
    model_name: Mapped[str | None] = mapped_column(String(80))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    usage: Mapped[dict | None] = mapped_column(JsonDict)

    #: 这条助手回复所依据的**服务端验证过的**公开来源(见 contracts/conversation.py
    #: 的 `ResearchView`)。None = 这一轮没有调用过 `research_public`。
    #: 只存真实 provider 结果派生出的 citations,模型无法写入或伪造。
    research: Mapped[dict | None] = mapped_column(JsonDict)

    # 客户端生成的消息 id,用于重试去重:请求超时后用户再点一次不会产生两条消息。
    client_message_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")

    __table_args__ = (
        UniqueConstraint("conversation_id", "seq", name="uq_messages_conversation_id_seq"),
        Index("ix_messages_workspace_id_created_at", "workspace_id", "created_at"),
        Index(
            "uq_messages_client_message_id",
            "conversation_id",
            "client_message_id",
            unique=True,
            postgresql_where=sql_text("client_message_id IS NOT NULL"),
            sqlite_where=sql_text("client_message_id IS NOT NULL"),
        ),
    )
