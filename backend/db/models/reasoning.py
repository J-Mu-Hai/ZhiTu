"""服务端控制的推理状态与工具调用记录。

## 这里存的是**摘要**,不是思维链

`ReasoningState` 只保存可审计的结论性内容:当前子问题、来源化的已知事实、待验证的
假设、仍未解决的问题、一句结论、下一步动作。**绝不保存模型的原始 CoT、隐藏提示词
或完整敏感工具结果** —— 那些东西一旦落库,就会成为下一次提示词的输入,也会成为
无法解释、无法删除的用户数据。

## 为什么单独两张表而不是塞进 messages

一次用户消息可能触发多次模型调用与多次工具调用。把它们压在一条消息上,就没法回答
“这一轮到底查了什么、哪一步失败、预算用在哪”。单独建 `reasoning_states` 与
`tool_call_records` 之后,每个子问题、每次工具调用都是一行,可查、可审计、可恢复。

## 恢复策略

用户消息与已回答问题先落库;循环中途模型或工具失败时,state 置 `blocked`,已经写入的
工具记录与事实摘要保留。下一轮可以基于同一份 state 继续,而不是从头再来。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db.base import (
    Base,
    JsonDict,
    TimestampMixin,
    UtcDateTime,
    UuidPk,
    enum_type,
)
from backend.db.models.enums import ReasoningAction, ReasoningStatus, ToolCallStatus


class ReasoningState(UuidPk, TimestampMixin, Base):
    __tablename__ = "reasoning_states"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    #: 触发这一次推理的用户消息。可空 —— 复盘那条路径没有用户消息。
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL")
    )
    #: 讨论焦点。可空 —— 整空间级的推理没有焦点节点。
    context_node_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("plan_nodes.id", ondelete="SET NULL")
    )
    #: 子问题之间的父子链(一个子问题可能来自另一个)。可空。
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("reasoning_states.id", ondelete="SET NULL")
    )
    #: 与 Question Node 的稳定引用。可空 —— 不是每次推理都要问用户。
    #: 用**可空外键**而不是改 `agent_questions`,不破坏阶段 2 的表。
    question_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_questions.id", ondelete="SET NULL")
    )

    #: 当前要解决的简短问题(一句话)。
    question: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: 0–5 的有界重要性 / 不确定性。**不是分数,不对外展示成置信度。**
    importance: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    uncertainty: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: 来源化事实摘要:`[{"text": ..., "source": "user|system|tool|model_inference|assumption", "at": ...}]`
    facts_summary: Mapped[list] = mapped_column(JsonDict, default=list, nullable=False)
    assumptions: Mapped[list] = mapped_column(JsonDict, default=list, nullable=False)
    open_questions: Mapped[list] = mapped_column(JsonDict, default=list, nullable=False)
    conclusion: Mapped[str | None] = mapped_column(Text)

    next_action: Mapped[ReasoningAction] = mapped_column(
        enum_type(ReasoningAction, "reasoning_action"),
        default=ReasoningAction.STOP,
        nullable=False,
    )
    status: Mapped[ReasoningStatus] = mapped_column(
        enum_type(ReasoningStatus, "reasoning_status"),
        default=ReasoningStatus.UNEXPLORED,
        nullable=False,
    )
    #: 这一条 state 已经用掉的预算。恢复时接着算,不重新给满额。
    tool_calls_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    model_calls_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    workspace: Mapped[Workspace] = relationship()  # noqa: F821
    tool_calls: Mapped[list[ToolCallRecord]] = relationship(
        back_populates="reasoning_state",
        order_by="ToolCallRecord.sequence",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("ix_reasoning_states_workspace_id_status", "workspace_id", "status"),
        Index("ix_reasoning_states_source_message_id", "source_message_id"),
    )


class ToolCallRecord(UuidPk, Base):
    """一次只读工具调用。**只追加,不改。**"""

    __tablename__ = "tool_call_records"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    reasoning_state_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("reasoning_states.id", ondelete="CASCADE"), nullable=False
    )
    #: 同一条 state 内的顺序。
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    #: **已脱敏的参数** —— 只留白名单字段,且手柄不是真实 UUID。
    sanitized_arguments: Mapped[dict] = mapped_column(JsonDict, default=dict, nullable=False)
    status: Mapped[ToolCallStatus] = mapped_column(
        enum_type(ToolCallStatus, "tool_call_status"), nullable=False
    )
    #: 成功时的**摘要**(不是完整结果;长结果会被截断)。
    result_summary: Mapped[dict | None] = mapped_column(JsonDict)
    error_summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    reasoning_state: Mapped[ReasoningState] = relationship(back_populates="tool_calls")

    __table_args__ = (
        UniqueConstraint(
            "reasoning_state_id", "sequence", name="uq_tool_call_records_state_sequence"
        ),
        Index("ix_tool_call_records_workspace_id", "workspace_id"),
    )
