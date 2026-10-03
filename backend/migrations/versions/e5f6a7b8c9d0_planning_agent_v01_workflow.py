"""planning agent v0.1 workflow

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-10-03 21:30:00.000000

规划智能体 V0.1:程序控制的三阶段工作流状态。

- `goal_reasoning_sessions.workflow_stage`:DISCOVERY / TIMELINE_DRAFT /
  TIMELINE_REVIEW / WEEKLY_EXECUTION / REPLANNING。**可空** —— 老 workspace 保持
  为 NULL,行为与加列之前完全一样。
- `goal_reasoning_sessions.discovery`:阶段一的发现状态(问题/回答/补充次数/模板)。
- `goal_reasoning_sessions.timeline_proposal_id`:时间线确认提案 id。

三列都可空,纯加法;不修改、不删除任何既有行。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from backend.db.base import JsonDict


# revision identifiers, used by Alembic.
revision: str = 'e5f6a7b8c9d0'
down_revision: Union[str, Sequence[str], None] = 'd4e5f6a7b8c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('goal_reasoning_sessions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('workflow_stage', sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column('discovery', JsonDict(), nullable=True))
        batch_op.add_column(sa.Column('timeline_proposal_id', sa.Uuid(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('goal_reasoning_sessions', schema=None) as batch_op:
        batch_op.drop_column('timeline_proposal_id')
        batch_op.drop_column('discovery')
        batch_op.drop_column('workflow_stage')
