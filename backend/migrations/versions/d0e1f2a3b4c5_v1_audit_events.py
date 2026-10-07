"""planning agent v1 audit events (P5)

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-10-05 10:00:00.000000

规划智能体重构 V1 — P5:可导出的决策审计记录。**纯加法**,可 downgrade。

`agent_audit_events` 只增不改:按空间单调递增的 `sequence` 稳定排序,记录用户可理解、
可审计的决策过程(阶段迁移、AI 判断、焦点、节点更新、提案生成/确认、校验/守卫/失败)。

**不存**:API Key / 令牌 / 连接串 / 原始系统提示词 / 隐藏思维链 / 未清洗工具参数。
只服务 V1 空间;老空间 / V0.1 不新增审计噪声。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from backend.db.base import JsonDict


# revision identifiers, used by Alembic.
revision: str = 'd0e1f2a3b4c5'
down_revision: Union[str, Sequence[str], None] = 'c9d0e1f2a3b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'agent_audit_events',
        sa.Column('workspace_id', sa.Uuid(), nullable=False),
        sa.Column('goal_reasoning_session_id', sa.Uuid(), nullable=True),
        sa.Column('sequence', sa.Integer(), nullable=False),
        sa.Column('event_type', sa.String(length=48), nullable=False),
        sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('stage_before', sa.String(length=32), nullable=True),
        sa.Column('stage_after', sa.String(length=32), nullable=True),
        sa.Column('trigger', sa.String(length=32), nullable=True),
        sa.Column('source', sa.String(length=32), nullable=True),
        sa.Column('focus_key', sa.String(length=48), nullable=True),
        sa.Column('focus_reason', sa.Text(), nullable=True),
        sa.Column('summary', sa.Text(), nullable=True),
        sa.Column('payload_json', JsonDict(), nullable=True),
        sa.Column('validation_status', sa.String(length=16), nullable=True),
        sa.Column('error_code', sa.String(length=48), nullable=True),
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ['workspace_id'], ['workspaces.id'], name='fk_agent_audit_events_workspace_id_workspaces',
            ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['goal_reasoning_session_id'], ['goal_reasoning_sessions.id'],
            # 短名是必须的:命名约定拼出来的
            # `fk_agent_audit_events_goal_reasoning_session_id_goal_reasoning_sessions`
            # 有 71 字符,超过 PostgreSQL 的 63 上限(模型侧同名,见 models/audit.py)。
            name='fk_agent_audit_events_goal_reasoning_session_id',
            ondelete='SET NULL',
        ),
        sa.PrimaryKeyConstraint('id', name='pk_agent_audit_events'),
        sa.UniqueConstraint(
            'workspace_id', 'sequence', name='uq_agent_audit_events_workspace_id_sequence'
        ),
    )
    op.create_index(
        'ix_agent_audit_events_workspace_id_event_type',
        'agent_audit_events',
        ['workspace_id', 'event_type'],
    )
    op.create_index(
        'ix_agent_audit_events_workspace_id_occurred_at',
        'agent_audit_events',
        ['workspace_id', 'occurred_at'],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        'ix_agent_audit_events_workspace_id_occurred_at', table_name='agent_audit_events'
    )
    op.drop_index(
        'ix_agent_audit_events_workspace_id_event_type', table_name='agent_audit_events'
    )
    op.drop_table('agent_audit_events')
