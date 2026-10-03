"""planning agent rearchitecture v1: stage-one canvas

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-10-04 10:00:00.000000

规划智能体重构 V1(P1:阶段一画布与节点讨论)。**纯加法、全部可空** ——
老空间 / V0.1 空间的这些列都是 NULL,行为与加列之前完全一样。

- `goal_reasoning_sessions.v1_stage`:V1 阶段一档位(initial_thinking / goal_reframe /
  factor_analysis / strategy_draft)。None = 非 V1。
- `goal_reasoning_sessions.v1_judgment`:首轮整体判断(可审阅结论,不含隐藏思维链)。
- `goal_reasoning_sessions.v1_question`:当前唯一需要回答的全局关键问题。
- `reasoning_nodes.v1_kind`:V1 画布角色(group / analysis / strategy)。
- `reasoning_nodes.v1_key`:V1 固定标识(current_state / true_intent / …)。
- `reasoning_nodes.v1_question`:该分析节点当前唯一待确认的一件事。

这些列都属于 **reasoning 层**;`plan_nodes`、时间线、周/日计划一行都不动。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7b8c9d0e1f2'
down_revision: Union[str, Sequence[str], None] = 'f6a7b8c9d0e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('goal_reasoning_sessions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('v1_stage', sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column('v1_judgment', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('v1_question', sa.Text(), nullable=True))

    with op.batch_alter_table('reasoning_nodes', schema=None) as batch_op:
        batch_op.add_column(sa.Column('v1_kind', sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column('v1_key', sa.String(length=48), nullable=True))
        batch_op.add_column(sa.Column('v1_question', sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('reasoning_nodes', schema=None) as batch_op:
        batch_op.drop_column('v1_question')
        batch_op.drop_column('v1_key')
        batch_op.drop_column('v1_kind')

    with op.batch_alter_table('goal_reasoning_sessions', schema=None) as batch_op:
        batch_op.drop_column('v1_question')
        batch_op.drop_column('v1_judgment')
        batch_op.drop_column('v1_stage')
