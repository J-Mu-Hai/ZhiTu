"""planning agent v0.1 timeline projection

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-10-03 22:30:00.000000

V0.1 时间线投影:`goal_reasoning_sessions.v01_timeline`(JSON,可空)。
它是前端时间轴的**唯一权威来源** —— 结构化阶段(id/title/kind/周次/日期/成果/
完成标准/状态),不从前端从自然语言猜日期。纯加法,不碰任何既有行。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from backend.db.base import JsonDict


# revision identifiers, used by Alembic.
revision: str = 'f6a7b8c9d0e1'
down_revision: Union[str, Sequence[str], None] = 'e5f6a7b8c9d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('goal_reasoning_sessions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('v01_timeline', JsonDict(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('goal_reasoning_sessions', schema=None) as batch_op:
        batch_op.drop_column('v01_timeline')
