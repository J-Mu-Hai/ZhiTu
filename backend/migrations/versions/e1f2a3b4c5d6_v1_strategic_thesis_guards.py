"""planning agent v1 strategic-thesis guards (P2.1)

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-10-05 16:00:00.000000

规划智能体重构 V1 — P2.1:战略判断优先、停止问卷式追问。**纯加法、全部可空**。

- `goal_reasoning_sessions.v1_strategic_thesis`:当前战略判断(AI 暂定理解)。
- `goal_reasoning_sessions.v1_candidate_directions`:用户答不上来时给出的候选方向。
- `goal_reasoning_sessions.v1_selected_direction`:用户选择的候选方向键。
- `goal_reasoning_sessions.v1_question_budget_used`:阶段一已问问题次数(最多 3)。
- `goal_reasoning_sessions.v1_last_focus_key`:上一次提问的焦点(同一焦点最多问 1 次)。
- `goal_reasoning_sessions.v1_low_info_streak`:连续低信息回答轮数(>=2 必须给候选)。

老空间 / V0.1 这些列都是 NULL,行为不变。时间线 / 周计划 / 日计划一行都不动。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from backend.db.base import JsonDict


# revision identifiers, used by Alembic.
revision: str = 'e1f2a3b4c5d6'
down_revision: Union[str, Sequence[str], None] = 'd0e1f2a3b4c5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('goal_reasoning_sessions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('v1_strategic_thesis', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('v1_candidate_directions', JsonDict(), nullable=True))
        batch_op.add_column(sa.Column('v1_selected_direction', sa.String(length=48), nullable=True))
        batch_op.add_column(sa.Column('v1_question_budget_used', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('v1_last_focus_key', sa.String(length=48), nullable=True))
        batch_op.add_column(sa.Column('v1_low_info_streak', sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('goal_reasoning_sessions', schema=None) as batch_op:
        batch_op.drop_column('v1_low_info_streak')
        batch_op.drop_column('v1_last_focus_key')
        batch_op.drop_column('v1_question_budget_used')
        batch_op.drop_column('v1_selected_direction')
        batch_op.drop_column('v1_candidate_directions')
        batch_op.drop_column('v1_strategic_thesis')
