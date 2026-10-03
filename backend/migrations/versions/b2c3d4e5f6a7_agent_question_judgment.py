"""agent question strategic judgment

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-10-03 16:00:00.000000

给 `agent_questions` 增加提问前的**战略判断**字段(阶段 10):

- `analysis_summary`  AI 基于已知事实的判断
- `recommendation`    明确推荐
- `decision_impact`   不同选择会怎样改变路线/阶段
- `confidence_note`   可选:哪些是假设、还需确认

只存**可审阅的结论**,不存隐藏思维链、原始 prompt 或模型原文推理过程。旧行没有判断,
回填空字符串 —— 读取侧另有“不足以推荐”的降级展示,不伪造历史判断。

三个 NOT NULL 列用「先加可空 -> 回填 -> 收紧」而不是 `server_default`:后者会让
`create_all`(模型侧没有 server_default)与迁移产物出现漂移,`test_migration_matches_models`
会照实报出来。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('agent_questions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('analysis_summary', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('recommendation', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('decision_impact', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('confidence_note', sa.Text(), nullable=True))

    op.execute("UPDATE agent_questions SET analysis_summary = '' WHERE analysis_summary IS NULL")
    op.execute("UPDATE agent_questions SET recommendation = '' WHERE recommendation IS NULL")
    op.execute("UPDATE agent_questions SET decision_impact = '' WHERE decision_impact IS NULL")

    with op.batch_alter_table('agent_questions', schema=None) as batch_op:
        batch_op.alter_column('analysis_summary', existing_type=sa.Text(), nullable=False)
        batch_op.alter_column('recommendation', existing_type=sa.Text(), nullable=False)
        batch_op.alter_column('decision_impact', existing_type=sa.Text(), nullable=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('agent_questions', schema=None) as batch_op:
        batch_op.drop_column('confidence_note')
        batch_op.drop_column('decision_impact')
        batch_op.drop_column('recommendation')
        batch_op.drop_column('analysis_summary')
