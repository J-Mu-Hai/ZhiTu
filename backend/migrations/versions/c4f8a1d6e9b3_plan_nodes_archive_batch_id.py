"""plan_nodes archive_batch_id

Revision ID: c4f8a1d6e9b3
Revises: b7d41c9f2a68
Create Date: 2026-09-27 03:40:12.884019

恢复"哪一下把它带走的"这件事原来认的是 `deleted_at` 相等。那是**一条关于时间精度的
默认性质**,不是写下来的约束 —— 精度一变(列类型、驱动、某处截到秒),两次归档就会
合并成一批,症状是"恢复了一项,另一次特意收起来的也跟着回来了"。这一列把那个约定
换成显式的号。

**回填是这次迁移唯一有魔法的一步**,目标是"一一对应":同一个 `deleted_at` 的行拿到
同一个新号,不同 `deleted_at` 的行绝不会拿到同一个号。做完之后新列上的数据与旧语义
完全等价,代码里不需要留"新列空着就退回时间戳"那条岔路 —— 两条真相是这类改动最常见
的收尾方式,而这个功能的出错代价正好是静默的。

回填按**主键逐行**更新,不按 `deleted_at` 回写:那样要依赖时间戳在 SQL 里往返比较
仍然相等(两个后端的类型、时区、微秒处理各不一样),而按 id 更新不依赖任何比较。
"""
import uuid
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# Custom column types (UtcDateTime, JsonDict, ...) live in backend.db.base. Alembic
# renders them as fully-qualified names, so this import must be present in every
# generated migration or upgrade() fails with NameError at run time.
import backend.db.base


# revision identifiers, used by Alembic.
revision: str = 'c4f8a1d6e9b3'
down_revision: Union[str, Sequence[str], None] = 'b7d41c9f2a68'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('plan_nodes', schema=None) as batch_op:
        # 可空、无 server_default —— 活着的节点没有批次可言,NULL 就是"没有"。
        batch_op.add_column(sa.Column('archive_batch_id', sa.Uuid(), nullable=True))

    bind = op.get_bind()
    # 用裸 SQL 取,拿到的 id 在 SQLite 上是 32 位十六进制字符串、在 PostgreSQL 上
    # 已经是 UUID 对象;下面统一转成 UUID 再用 `sa.Uuid()` 类型的参数传回去,
    # 由 SQLAlchemy 按各自后端转换。
    rows = bind.execute(
        sa.text("SELECT id, deleted_at FROM plan_nodes WHERE deleted_at IS NOT NULL")
    ).fetchall()
    batch_of: dict[object, uuid.UUID] = {}
    update = sa.text("UPDATE plan_nodes SET archive_batch_id = :batch WHERE id = :node").bindparams(
        sa.bindparam("batch", type_=sa.Uuid()), sa.bindparam("node", type_=sa.Uuid())
    )
    for node_id, deleted_at in rows:
        batch = batch_of.get(deleted_at)
        if batch is None:
            batch = batch_of[deleted_at] = uuid.uuid4()
        bind.execute(
            update,
            {"batch": batch, "node": node_id if isinstance(node_id, uuid.UUID) else uuid.UUID(str(node_id))},
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('plan_nodes', schema=None) as batch_op:
        batch_op.drop_column('archive_batch_id')
