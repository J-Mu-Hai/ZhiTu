"""AI 提出的问题节点。

## 为什么单独一张表,而不是塞进 `plan_nodes`

问题节点**不是计划节点**。它不参与排期、不能当依赖端点、不进任务统计,也不该出现在
画布的计划树里 —— 把它做成一种 `PlanNode` 的 `purpose`,上面每一条都要在排期、依赖、
统计、画布投影里各加一个例外;而漏掉任何一处,用户看到的就是一个"混进计划里的问题框"。

单独一张表之后这些边界是**结构性的**:排期只读 `plan_nodes`,问题表根本不在它的查询里。

## 为什么答案和状态在同一行

用户答一个问题只会有一个答案。把它拆成 `question_answers` 一对多,带来的是
"同一个问题为什么有三条回答""哪条才算"这类没有产品含义的状态。一行原地更新,
配合 `answer_client_id` 去重,双击回答天然幂等。

## `events` 记的是用户的动作

`answered` / `skipped` / `deferred` 各追加一条。它是**审计**,不是状态本身 ——
状态在 `status` 列上,`events` 只回答"他当时做了什么"。`deferred` 之后状态仍是
`pending`(稍后回答),这个区别只有 `events` 记得住。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db.base import Base, JsonDict, TimestampMixin, UtcDateTime, UuidPk, enum_type
from backend.db.models.enums import QuestionResponseMode, QuestionStatus


class AgentQuestion(UuidPk, TimestampMixin, Base):
    __tablename__ = "agent_questions"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    #: 问题是从哪个节点聊出来的。**可空** —— 用户没选节点时也可以提问。
    source_node_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("plan_nodes.id", ondelete="SET NULL")
    )
    #: 产出这个问题的助手消息。可空:消息被清掉后问题仍要可审计(见模块 docstring)。
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL")
    )

    question: Mapped[str] = mapped_column(Text, nullable=False)
    #: "为什么现在问这个"。与 question 分开:一个是问什么,一个是问的理由。
    why_now: Mapped[str] = mapped_column(Text, default="", nullable=False)
    response_mode: Mapped[QuestionResponseMode] = mapped_column(
        enum_type(QuestionResponseMode, "question_response_mode"), nullable=False
    )
    #: 0–5 个选项,每项 `{"id": ..., "label": ...}`。服务端校验过的形状。
    options: Mapped[list] = mapped_column(JsonDict, default=list, nullable=False)
    allow_custom_input: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    status: Mapped[QuestionStatus] = mapped_column(
        enum_type(QuestionStatus, "question_status"),
        default=QuestionStatus.PENDING,
        nullable=False,
    )
    #: 用户答案。形状由 `question_service` 规范化后写入:
    #: `{"selectedOptionIds": [...], "customInput": "..."}`
    answer: Mapped[dict | None] = mapped_column(JsonDict)
    #: 幂等键:同一份答案重试时不再跑一次后续处理。
    answer_client_id: Mapped[str | None] = mapped_column(String(64))
    answered_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    #: 用户动作审计(answered / skipped / deferred)。只整体读写,不按内容查询。
    events: Mapped[list] = mapped_column(JsonDict, default=list, nullable=False)

    workspace: Mapped[Workspace] = relationship()  # noqa: F821

    __table_args__ = (
        # 读取 active questions 是按空间 + 状态 + 时间排的,这个索引正好覆盖它。
        Index(
            "ix_agent_questions_workspace_id_status_created_at",
            "workspace_id",
            "status",
            "created_at",
        ),
        Index("ix_agent_questions_source_node_id", "source_node_id"),
        Index("ix_agent_questions_source_message_id", "source_message_id"),
    )
