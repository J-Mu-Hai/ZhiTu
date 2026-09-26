"""plan_nodes purged_at

Revision ID: b7d41c9f2a68
Revises: eab5fc18adde
Create Date: 2026-09-27 02:35:11.204317

**手写的,不是 autogenerate 出来的。** 原因值得记一笔:本机那个开发库还停在
`8b3ec72e4d24`(上一个提交的那次迁移**没有应用**到这个库上),autogenerate 会直接
拒绝干活("Target database is not up to date")。把那个库升上去是另一件事 ——
它是用户真实数据所在的库,不该为了生成一行 DDL 被动它。

这一列是"这一次删除不可恢复"的标记:归档与彻底删除都打 `deleted_at`,区别只在这里。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# Custom column types (UtcDateTime, JsonDict, ...) live in backend.db.base. Alembic
# renders them as fully-qualified names, so this import must be present in every
# generated migration or upgrade() fails with NameError at run time.
import backend.db.base


# revision identifiers, used by Alembic.
revision: str = 'b7d41c9f2a68'
down_revision: Union[str, Sequence[str], None] = 'eab5fc18adde'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('plan_nodes', schema=None) as batch_op:
        # 可空、没有 server_default —— 和 `deleted_at` 同一形状。"没彻底删过"这件事
        # 由 NULL 表达,不需要一个常量默认值。
        batch_op.add_column(sa.Column('purged_at', backend.db.base.UtcDateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('plan_nodes', schema=None) as batch_op:
        batch_op.drop_column('purged_at')
