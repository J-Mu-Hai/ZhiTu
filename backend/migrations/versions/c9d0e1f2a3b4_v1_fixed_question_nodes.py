"""planning agent v1 phase-2 fixed question nodes

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-10-04 21:00:00.000000

规划智能体重构 V1 — P2 修正:阶段一的分组子项是**画布问题节点**(紫色),
不是计划子节点。**纯加法、全部可空** —— 老问题行 / 老空间这些列都是 NULL。

- `agent_questions.v1_key`:V1 固定问题键(current_state / true_intent / main_line / …)。
  模型只能用这个键指涉问题节点,不能用任意 id/标题。
- `agent_questions.v1_analysis`:模型对该问题的可审阅判断(结论 / 已知事实 / 假设 /
  来源 / 为什么重要 / 不确定性 / 状态 / 讨论数)。**不存隐藏思维链。**

分组本身仍是 `plan_nodes`(画布节点),它们不受这张迁移影响。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from backend.db.base import JsonDict


# revision identifiers, used by Alembic.
revision: str = 'c9d0e1f2a3b4'
down_revision: Union[str, Sequence[str], None] = 'b8c9d0e1f2a3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('agent_questions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('v1_key', sa.String(length=48), nullable=True))
        batch_op.add_column(sa.Column('v1_analysis', JsonDict(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('agent_questions', schema=None) as batch_op:
        batch_op.drop_column('v1_analysis')
        batch_op.drop_column('v1_key')
