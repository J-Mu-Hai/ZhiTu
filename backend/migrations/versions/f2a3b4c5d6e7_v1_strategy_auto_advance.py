"""planning agent v1 strategy auto-advance (P2.3)

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-10-05 20:00:00.000000

规划智能体重构 V1 — P2.3:消除 problem_structure 空转。

新增可空列 `goal_reasoning_sessions.v1_next_action`:当前阶段**显式下一步动作**
(如 `continue_strategy`)。非终态阶段不允许同时处于“idle + 无待回答问题 + 无 CTA +
无战略草案”。纯加法,老空间为 NULL,行为不变。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f2a3b4c5d6e7'
down_revision: Union[str, Sequence[str], None] = 'e1f2a3b4c5d6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('goal_reasoning_sessions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('v1_next_action', sa.String(length=32), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('goal_reasoning_sessions', schema=None) as batch_op:
        batch_op.drop_column('v1_next_action')
