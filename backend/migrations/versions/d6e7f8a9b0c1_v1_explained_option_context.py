"""planning agent v1 explained option context (R2 closure)

Revision ID: d6e7f8a9b0c1
Revises: c5d6e7f8a9b0
Create Date: 2026-10-05 09:00:00.000000

R2 收口:把“选项问卷”改为有解释的战略对话。纯加法、全部可空,老空间 / V0.1 行为不变。

- `goal_reasoning_sessions.v1_decision_context`:为什么此刻需要这个决定。
- `goal_reasoning_sessions.v1_provisional_recommendation`:AI 当前倾向与理由。
- `goal_reasoning_sessions.v1_options_streak`:连续“问题 + 候选选项”轮数(最多连续 1 次)。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd6e7f8a9b0c1'
down_revision: Union[str, Sequence[str], None] = 'c5d6e7f8a9b0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('goal_reasoning_sessions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('v1_decision_context', sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column('v1_provisional_recommendation', sa.Text(), nullable=True)
        )
        batch_op.add_column(sa.Column('v1_options_streak', sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('goal_reasoning_sessions', schema=None) as batch_op:
        batch_op.drop_column('v1_options_streak')
        batch_op.drop_column('v1_provisional_recommendation')
        batch_op.drop_column('v1_decision_context')
