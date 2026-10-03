"""规划智能体 V1 的**决策审计事件**(P5:可导出决策审计记录)。

## 它是什么 / 不是什么

它是用户可理解、可审计、可复现的**决策过程记录**:

    用户输入 → 所处阶段 → AI 可见判断 → 焦点与理由 → 一个关键问题
    → 更新了哪些节点 → 用户事实 / AI 假设 / 公开依据 → 校验/守卫/失败
    → 提案生成与确认 → 状态前后变化

**绝对不是**:模型隐藏思维链、原始系统提示词、API Key/令牌/连接串、未清洗的工具参数、
私密研究查询。写入前一律经过 `audit_service` 的脱敏与白名单清洗。

## 为什么单独一张表

它与 `reasoning_states` / `agent_trace` **不是一回事**:

- `reasoning_states` 是模型循环的工作状态;`agent_trace` 是**脱敏的运行轨迹**;
- 审计事件记录的是**产品决策** —— 阶段迁移、节点更新、提案生成/确认、守卫拒绝。

把它塞进 `domain_events`(节点变更事件)还不够:审计要按"第几轮决策"稳定排序、要能
跨重启导出、要能承载"这轮为什么这么判断"的可审阅摘要。所以单独一张只增不改的表。

## 只服务 V1

只有 `v1_stage is not None` 的空间才写审计事件;老空间 / V0.1 不新增审计噪声。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db.base import Base, JsonDict, TimestampMixin, UtcDateTime, UuidPk


class AgentAuditEvent(UuidPk, TimestampMixin, Base):
    __tablename__ = "agent_audit_events"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    #: V1 会话。可空但推荐 —— 审计几乎都发生在目标推理会话里。
    goal_reasoning_session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("goal_reasoning_sessions.id", ondelete="SET NULL")
    )
    #: 每个空间**单调递增**的稳定序号。导出按它升序,跨重启不变。
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    #: 事件类型。闭集见 `audit_service.AUDIT_EVENT_TYPES`(用 String 而不是枚举,
    #: 新事件类型不必再动数据库)。
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    #: V1 阶段前后。空串表示非阶段迁移类事件。
    stage_before: Mapped[str | None] = mapped_column(String(32))
    stage_after: Mapped[str | None] = mapped_column(String(32))
    #: 触发来源(space_entered / user_message / question_answered / …)。
    trigger: Mapped[str | None] = mapped_column(String(32))
    #: 模型来源(direct_llm / openjiuwen / scripted / rule …)。
    source: Mapped[str | None] = mapped_column(String(32))
    #: 当前焦点容器键与理由(可读)。
    focus_key: Mapped[str | None] = mapped_column(String(48))
    focus_reason: Mapped[str | None] = mapped_column(Text)
    #: 给用户/Codex 看的一句话摘要。
    summary: Mapped[str | None] = mapped_column(Text)
    #: **已清洗**的可审阅载荷。只放结论/事实/假设/提案元数据,不放原文推理。
    payload_json: Mapped[dict | None] = mapped_column(JsonDict)
    #: `ok` / `failed`。失败事件绝不写成功摘要。
    validation_status: Mapped[str | None] = mapped_column(String(16))
    #: 失败时的闭集错误码(可空)。
    error_code: Mapped[str | None] = mapped_column(String(48))

    workspace: Mapped[Workspace] = relationship()  # noqa: F821

    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "sequence", name="uq_agent_audit_events_workspace_id_sequence"
        ),
        Index("ix_agent_audit_events_workspace_id_event_type", "workspace_id", "event_type"),
        Index(
            "ix_agent_audit_events_workspace_id_occurred_at", "workspace_id", "occurred_at"
        ),
    )


__all__ = ["AgentAuditEvent"]
