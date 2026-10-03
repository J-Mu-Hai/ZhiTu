"""agent trace step sequence

Revision ID: a1b2c3d4e5f6
Revises: 9b7c1a2d3e4f
Create Date: 2026-10-03 13:00:00.000000

给 `reasoning_states` 增加 `trace_steps`:`[{"step": "...", "at": "..."}]` 的**真实**
状态转移序列,由 `agent_trace_service.mark_step` 在每次跨过执行边界时追加。

存量行没有轨迹 -> 回填空数组;读取侧把它当成"无状态序列",不伪造。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

import backend.db.base


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = '9b7c1a2d3e4f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('reasoning_states', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('trace_steps', backend.db.base.JsonDict(), nullable=True)
        )

    op.execute("UPDATE reasoning_states SET trace_steps = '[]' WHERE trace_steps IS NULL")

    with op.batch_alter_table('reasoning_states', schema=None) as batch_op:
        batch_op.alter_column(
            'trace_steps', existing_type=backend.db.base.JsonDict(), nullable=False
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('reasoning_states', schema=None) as batch_op:
        batch_op.drop_column('trace_steps')
