"""planning agent v1 openjiuwen turn lifecycle (R1)

Revision ID: b4c5d6e7f8a9
Revises: f2a3b4c5d6e7
Create Date: 2026-10-06 10:00:00.000000

规划智能体重构 V1(主动循环 R1):把每个 OpenJiuwen 工作回合持久化下来,
让“正在思考”有明确的 deadline、来源与幂等语义,并能从崩溃 / 重启中恢复成
可重试失败,而不是永久 loading。**纯加法、全部可空**,老空间 / V0.1 行为不变。

- `v1_turn_id`:本回合唯一标识。
- `v1_turn_stage`:进入时的阶段快照。
- `v1_turn_trigger`:触发来源。
- `v1_turn_started_at` / `v1_turn_deadline_at`:开始与截止时刻。
- `v1_turn_attempt`:尝试次数。
- `v1_turn_source`:真实模型来源(`openjiuwen`;测试 fixture 为 `test`)。
- `v1_turn_idempotency_key`:幂等键。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# Custom column types (UtcDateTime, JsonDict, ...) live in backend.db.base. Alembic
# renders them as fully-qualified names, so this import must be present in every
# generated migration or upgrade() fails with NameError at run time.
import backend.db.base


# revision identifiers, used by Alembic.
revision: str = 'b4c5d6e7f8a9'
down_revision: Union[str, Sequence[str], None] = 'f2a3b4c5d6e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('goal_reasoning_sessions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('v1_turn_id', sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column('v1_turn_stage', sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column('v1_turn_trigger', sa.String(length=32), nullable=True))
        batch_op.add_column(
            sa.Column('v1_turn_started_at', backend.db.base.UtcDateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column('v1_turn_deadline_at', backend.db.base.UtcDateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(sa.Column('v1_turn_attempt', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('v1_turn_source', sa.String(length=16), nullable=True))
        batch_op.add_column(
            sa.Column('v1_turn_idempotency_key', sa.String(length=64), nullable=True)
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('goal_reasoning_sessions', schema=None) as batch_op:
        batch_op.drop_column('v1_turn_idempotency_key')
        batch_op.drop_column('v1_turn_source')
        batch_op.drop_column('v1_turn_attempt')
        batch_op.drop_column('v1_turn_deadline_at')
        batch_op.drop_column('v1_turn_started_at')
        batch_op.drop_column('v1_turn_trigger')
        batch_op.drop_column('v1_turn_stage')
        batch_op.drop_column('v1_turn_id')
