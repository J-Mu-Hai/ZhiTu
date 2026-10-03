"""strategic intake and temporal architecture

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-10-03 18:00:00.000000

阶段 11:Strategic Intake → 时间架构。

- `agent_questions.presentation`:区分 `conversation_intake`(只在对话区)与
  `canvas_question`(画布 Question Node)。旧行回填 `canvas_question`。
- `goal_reasoning_sessions.intake_questions_asked`:intake 已问过几个关键问题(最多 5)。
- `goal_reasoning_sessions.dates_calibrated`:时间架构里的日期是否已校准。
- `reasoning_nodes.timeframe_kind / start_week / end_week / start_date / end_date`:
  阶段的结构化时间范围。没有日期时用相对周,不伪造日历日期。

NOT NULL 列一律「先加可空 -> 回填 -> 收紧」:直接用 `server_default` 会让
`create_all`(模型侧没有 server_default)与迁移产物出现漂移。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, Sequence[str], None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_PRESENTATION = sa.Enum(
    'conversation_intake',
    'canvas_question',
    name='question_presentation',
    native_enum=False,
    length=32,
)


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('agent_questions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('presentation', _PRESENTATION, nullable=True))
    op.execute(
        "UPDATE agent_questions SET presentation = 'canvas_question' WHERE presentation IS NULL"
    )
    with op.batch_alter_table('agent_questions', schema=None) as batch_op:
        batch_op.alter_column('presentation', existing_type=_PRESENTATION, nullable=False)

    with op.batch_alter_table('goal_reasoning_sessions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('intake_questions_asked', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('dates_calibrated', sa.Boolean(), nullable=True))
    op.execute(
        "UPDATE goal_reasoning_sessions SET intake_questions_asked = 0 "
        "WHERE intake_questions_asked IS NULL"
    )
    op.execute(
        "UPDATE goal_reasoning_sessions SET dates_calibrated = 0 "
        "WHERE dates_calibrated IS NULL"
    )
    with op.batch_alter_table('goal_reasoning_sessions', schema=None) as batch_op:
        batch_op.alter_column(
            'intake_questions_asked', existing_type=sa.Integer(), nullable=False
        )
        batch_op.alter_column('dates_calibrated', existing_type=sa.Boolean(), nullable=False)

    with op.batch_alter_table('reasoning_nodes', schema=None) as batch_op:
        batch_op.add_column(sa.Column('timeframe_kind', sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column('start_week', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('end_week', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('start_date', sa.Date(), nullable=True))
        batch_op.add_column(sa.Column('end_date', sa.Date(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('reasoning_nodes', schema=None) as batch_op:
        batch_op.drop_column('end_date')
        batch_op.drop_column('start_date')
        batch_op.drop_column('end_week')
        batch_op.drop_column('start_week')
        batch_op.drop_column('timeframe_kind')

    with op.batch_alter_table('goal_reasoning_sessions', schema=None) as batch_op:
        batch_op.drop_column('dates_calibrated')
        batch_op.drop_column('intake_questions_asked')

    with op.batch_alter_table('agent_questions', schema=None) as batch_op:
        batch_op.drop_column('presentation')
