"""planning agent v1 phase-2 strategic judgment

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-10-04 18:00:00.000000

规划智能体重构 V1 — P2:AI 战略判断、因素筛选与战略路径。**纯加法、全部可空** ——
老空间 / V0.1 空间的这些列都是 NULL,行为与加列之前完全一样。

- `plan_nodes.v1_key`:V1 固定容器标识(current_state / true_intent / main_line / …)。
  模型只能用这个键指涉节点,不能用任意 id/标题。
- `plan_nodes.v1_analysis`:模型对该容器的可审阅判断(结论 / 已知事实 / 假设 / 来源 /
  为什么重要 / 不确定性 / 状态 / 讨论数)。**不存隐藏思维链。**
- `goal_reasoning_sessions.v1_focus_key` / `v1_focus_reason`:当前焦点容器与理由。
- `goal_reasoning_sessions.v1_strategy`:战略路径草案(main_line / parallel_line /
  defer_or_avoid / risk_control / tradeoff / confirmed)。
- `goal_reasoning_sessions.v1_status` / `v1_error`:V1 模型回合状态与可读失败原因。

这些列都属于 reasoning 层与固定分析容器;时间线、任务、正式排期一行都不动。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from backend.db.base import JsonDict


# revision identifiers, used by Alembic.
revision: str = 'b8c9d0e1f2a3'
down_revision: Union[str, Sequence[str], None] = 'a7b8c9d0e1f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('plan_nodes', schema=None) as batch_op:
        batch_op.add_column(sa.Column('v1_key', sa.String(length=48), nullable=True))
        batch_op.add_column(sa.Column('v1_analysis', JsonDict(), nullable=True))

    with op.batch_alter_table('goal_reasoning_sessions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('v1_focus_key', sa.String(length=48), nullable=True))
        batch_op.add_column(sa.Column('v1_focus_reason', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('v1_strategy', JsonDict(), nullable=True))
        batch_op.add_column(sa.Column('v1_status', sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column('v1_error', sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('goal_reasoning_sessions', schema=None) as batch_op:
        batch_op.drop_column('v1_error')
        batch_op.drop_column('v1_status')
        batch_op.drop_column('v1_strategy')
        batch_op.drop_column('v1_focus_reason')
        batch_op.drop_column('v1_focus_key')

    with op.batch_alter_table('plan_nodes', schema=None) as batch_op:
        batch_op.drop_column('v1_analysis')
        batch_op.drop_column('v1_key')
