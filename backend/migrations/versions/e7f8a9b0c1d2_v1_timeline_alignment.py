"""planning agent v1 timeline alignment (deep dialogue)

Revision ID: e7f8a9b0c1d2
Revises: d6e7f8a9b0c1
Create Date: 2026-10-05 15:00:00.000000

深度战略对话:战略确认后先进入**时间架构共创**子阶段,把时间假设讲清楚、至多问一个
战略级问题,用户对齐后才生成粗时间线。新增一列可空 JSON,老空间 / V0.1 不变。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from backend.db.base import JsonDict


# revision identifiers, used by Alembic.
revision: str = 'e7f8a9b0c1d2'
down_revision: Union[str, Sequence[str], None] = 'd6e7f8a9b0c1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('goal_reasoning_sessions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('v1_timeline_alignment', JsonDict(), nullable=True))
        batch_op.add_column(sa.Column('v1_strategy_understanding', JsonDict(), nullable=True))
        batch_op.add_column(sa.Column('v1_user_understanding', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('v1_question_example', sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('goal_reasoning_sessions', schema=None) as batch_op:
        batch_op.drop_column('v1_question_example')
        batch_op.drop_column('v1_user_understanding')
        batch_op.drop_column('v1_strategy_understanding')
        batch_op.drop_column('v1_timeline_alignment')
