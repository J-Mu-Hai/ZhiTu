"""roadmap stage fields

Revision ID: 8a1c2f0d4b55
Revises: 37bb1f4aabe7
Create Date: 2026-10-02 21:40:00.000000

阶段 8:路线优先。`reasoning_nodes` 增加三个**可空**的路线要素列,只对 route / stage
节点有值,其余节点为 NULL。

- 只加可空列,不建默认值、不回填 —— 存量行读出来就是 NULL,行为与之前一致;
- `downgrade()` 把三列删掉,数据可回滚(删的是阶段 8 才写进去的展示字段,不影响
  任何业务计划)。
- `batch_alter_table` 是 SQLite 必须的:它没有原生 `ADD COLUMN` 之外的能力,
  批量模式会重建表;Alembic 在 SQLite 上会正确地复制数据。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# Custom column types live in backend.db.base. Alembic renders them as
# fully-qualified names, so this import must be present or upgrade() fails.
import backend.db.base


revision: str = '8a1c2f0d4b55'
down_revision: Union[str, Sequence[str], None] = '37bb1f4aabe7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('reasoning_nodes') as batch:
        batch.add_column(sa.Column('timeframe', sa.String(length=64), nullable=True))
        batch.add_column(sa.Column('deliverable', sa.Text(), nullable=True))
        batch.add_column(sa.Column('pass_criteria', sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('reasoning_nodes') as batch:
        batch.drop_column('pass_criteria')
        batch.drop_column('deliverable')
        batch.drop_column('timeframe')
