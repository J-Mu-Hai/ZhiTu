"""node notes and analysis narrative

Revision ID: 665fdb7d8d87
Revises: 6252812243fd
Create Date: 2026-09-27 22:52:07.923871

两件**纯加法**,各自对应 v1.2 的一条要求:

1. `node_notes` 一张新表 —— 节点长正文(「长笔记」)的落点。§2.2 要的是"一个节点
   除了那句几十字的简述,还能承载两万字的正文"。为什么是**新表**而不是
   `plan_nodes` 上加一列,写在 `db/models/note.py` 的模块 docstring 里(三条理由,
   最短的那条:`plan_revisions.snapshot` 每次改计划都把每个节点快照一份)。
2. `node_analyses.narrative` 一列 —— 分析的正文。§2.2 说"分析的摘要可以短,正文
   不能被摘要替代",而在此之前分析只有七个结构化数组,每条还被截到 400 字符。

## 用户文本一个字节都不改写

`node_notes` 是新建的空表;`narrative` 是 nullable 的新列,存量行是 `NULL`(那时
这份分析确实没有正文)。**两个方向都不碰 `description`** —— 那 300 码点上限是靠
服务端的条件规则执行的,不是靠这次迁移把谁的字截短或搬走。存量超过 300 码点的节点
**完全豁免**(上限只落在"新写的说明"上),它们不需要任何迁移动作。

## 升级前先备份

开发库开着 WAL,不能只复制主库文件(会丢掉还在 `-wal` 里的写入)。用在线备份 API:

    python -c "import sqlite3;src=sqlite3.connect('data/zhitu_dev.db');\
dst=sqlite3.connect('data/zhitu_dev.db.bak-<时间戳>');src.backup(dst);dst.close()"

再对一个临时副本演练一次 `upgrade head`,确认无误后才动 `data/zhitu_dev.db`。

## 降级会丢什么

`downgrade()` 里的 `drop_table('node_notes')` **会删掉升级之后写进去的全部笔记**。
分析那一列无所谓(它是可选的正文,`record_analysis` 下次会重新生成),但**用户写的
长正文没有任何上游能重新生成它** —— 那些字只存在于这张表里。所以降级之前**必须先
导出**:

    sqlite3 data/zhitu_dev.db ".mode insert node_notes" \
      "select * from node_notes;" > node_notes.sql

升级方向不丢任何东西:两张表都是加法。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# Custom column types (UtcDateTime, JsonDict, ...) live in backend.db.base. Alembic
# renders them as fully-qualified names, so this import must be present in every
# generated migration or upgrade() fails with NameError at run time.
import backend.db.base


# revision identifiers, used by Alembic.
revision: str = '665fdb7d8d87'
down_revision: Union[str, Sequence[str], None] = '6252812243fd'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # `content_version` / `origin` 都带 `server_default`:与新表一起建的列本来不需要它
    # (没有存量行要填),但**模型侧就是这么声明的**,而 `test_migration_matches_models.py`
    # 逐字比对两侧生成的 DDL —— 少了它,那条用例会红在一句看起来毫无道理的差异上。
    op.create_table('node_notes',
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('node_id', sa.Uuid(), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('content_version', sa.Integer(), server_default=sa.text('1'), nullable=False),
    sa.Column('origin', sa.Enum('user', 'ai', name='node_origin', native_enum=False, length=32), server_default=sa.text("'user'"), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', backend.db.base.UtcDateTime(timezone=True), nullable=False),
    sa.Column('updated_at', backend.db.base.UtcDateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['node_id'], ['plan_nodes.id'], name=op.f('fk_node_notes_node_id_plan_nodes'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_node_notes_workspace_id_workspaces'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_node_notes')),
    sa.UniqueConstraint('node_id', name='uq_node_notes_node_id')
    )
    # 新表上的索引不用 batch 那一套:`batch_alter_table` 是为"SQLite 改不了已有的表"
    # 准备的,而这里没有任何东西要重建。
    op.create_index('ix_node_notes_workspace_id', 'node_notes', ['workspace_id'], unique=False)

    # 分析正文。nullable,不回填:升级之前写下的分析确实没有正文,填一个空串反而
    # 会让"这份分析有没有正文"变成两种表示(空串与 NULL)。
    with op.batch_alter_table('node_analyses', schema=None) as batch_op:
        batch_op.add_column(sa.Column('narrative', sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema。**会删掉全部笔记 —— 先导出,见模块 docstring。**"""
    with op.batch_alter_table('node_analyses', schema=None) as batch_op:
        batch_op.drop_column('narrative')

    op.drop_index('ix_node_notes_workspace_id', table_name='node_notes')
    op.drop_table('node_notes')
