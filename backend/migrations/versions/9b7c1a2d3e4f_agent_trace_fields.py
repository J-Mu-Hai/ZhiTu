"""agent trace fields on reasoning_states

Revision ID: 9b7c1a2d3e4f
Revises: 8a1c2f0d4b55
Create Date: 2026-10-03 12:00:00.000000

给 `reasoning_states` 增加运行轨迹字段(阶段 9):

- `trigger`            触发来源(闭集,与 AgentTurnTrigger 同名字面量)
- `current_step`       真实执行步骤(AgentTraceStep)
- `attempt`            第几次尝试
- `terminal_code`      终态错误码
- `safe_summary`       **脱敏后的**可读摘要(绝不写模型/用户原文)
- `started_at`         本轮开始时刻
- `last_progress_at`   最后一次心跳时刻

`current_step` / `attempt` 是在**加列之后**回填存量行、再收紧成 NOT NULL 的:直接
`server_default` 会让 `create_all`(模型侧没有 server_default)与迁移产物出现漂移,
`test_migration_matches_models` 会照实报出来。回填方案对 SQLite 与 PostgreSQL 都成立。

注意:这与 `enums.py` 里那条"新增枚举**成员**不需要迁移"的说明不冲突 —— 这里新增的
是一个**新的枚举列**,必须建列。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# Custom column types (UtcDateTime, JsonDict, ...) live in backend.db.base. Alembic
# renders them as fully-qualified names, so this import must be present in every
# generated migration or upgrade() fails with NameError at run time.
import backend.db.base


# revision identifiers, used by Alembic.
revision: str = '9b7c1a2d3e4f'
down_revision: Union[str, Sequence[str], None] = '8a1c2f0d4b55'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TRACE_STEP = sa.Enum(
    'queued',
    'resolving_context',
    'waiting_model',
    'running_tool',
    'validating_output',
    'persisting',
    'completed',
    'failed',
    'timed_out',
    'cancelled',
    name='agent_trace_step',
    native_enum=False,
    length=32,
)


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('reasoning_states', schema=None) as batch_op:
        batch_op.add_column(sa.Column('trigger', sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column('current_step', _TRACE_STEP, nullable=True))
        batch_op.add_column(sa.Column('attempt', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('terminal_code', sa.String(length=48), nullable=True))
        batch_op.add_column(sa.Column('safe_summary', sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column('started_at', backend.db.base.UtcDateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                'last_progress_at', backend.db.base.UtcDateTime(timezone=True), nullable=True
            )
        )

    # 存量行没有轨迹。给一个**不伪造历史细节**的占位值,只用于满足 NOT NULL;
    # 读取侧另有 `started_at IS NULL -> unavailable` 的判定(见 agent_trace_service)。
    op.execute("UPDATE reasoning_states SET current_step = 'queued' WHERE current_step IS NULL")
    op.execute("UPDATE reasoning_states SET attempt = 1 WHERE attempt IS NULL")

    with op.batch_alter_table('reasoning_states', schema=None) as batch_op:
        batch_op.alter_column('current_step', existing_type=_TRACE_STEP, nullable=False)
        batch_op.alter_column('attempt', existing_type=sa.Integer(), nullable=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('reasoning_states', schema=None) as batch_op:
        batch_op.drop_column('last_progress_at')
        batch_op.drop_column('started_at')
        batch_op.drop_column('safe_summary')
        batch_op.drop_column('terminal_code')
        batch_op.drop_column('attempt')
        batch_op.drop_column('current_step')
        batch_op.drop_column('trigger')
