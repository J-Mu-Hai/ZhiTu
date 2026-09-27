"""plan_nodes purpose

Revision ID: 6252812243fd
Revises: 35b8423fa3d7
Create Date: 2026-09-27 20:56:36.936140

「这个节点要不要占日历」从今天起是一列。在此之前它没有表示 —— `node_type` 是
`capability` 的节点既可能是"我要练出这个能力"(要排期),也可能是"我了解到的情况"
(不要排期),**同一个类型值承载两种相反的排期语义**。规范 §2.5 要求把信息用途与
规划层级当作两个正交维度,这一列就是那个正交轴。

## 不做任何回填,这是刻意的

`server_default='planning'` 让 SQLite 自己把存量行填满,所以**升级后没有任何节点的
排期行为发生变化** —— 这条要写下来,因为它正是"没做回填"这个选择的可验证后果。
加这一列之前,每个节点都会被排期;加完之后,默认值让它们仍然会被排期。

**也刻意不去猜哪些存量节点是信息主题。** 一个"标题里带'情况''排名'就当信息节点"的
启发式看起来能省掉用户手工分类,实际是把一次**静默的语义迁移**塞进了升级脚本:
判断错了没有任何人会发现,而后果是那个节点从此不再被排期 —— 用户看到的是
"我明明有件事要做,日历上却没有它",且升级脚本已经在几周前跑完了,查无对证。
分类由用户在界面上明说(双击空白处建出来的默认就是信息主题)。

## 升级前先备份

开发库开着 WAL,不能只复制主库文件(会丢掉还在 `-wal` 里的写入)。用在线备份 API:

    python -c "import sqlite3;src=sqlite3.connect('data/zhitu_dev.db');\
dst=sqlite3.connect('data/zhitu_dev.db.bak-<时间戳>');src.backup(dst);dst.close()"

再对一个临时副本演练一次 `upgrade head`,确认无误后才动 `data/zhitu_dev.db`。

## 降级会丢什么

只丢这一次分类(`drop_column`),**不丢任何用户文本** —— 这一列不承载用户写的字,
`title` / `description` / `acceptance_criteria` 一个字节都不碰。代价是降级之后所有
信息主题重新变成可排期节点:它们没有工时、没有子节点,会重新出现在排期缺口里。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# Custom column types (UtcDateTime, JsonDict, ...) live in backend.db.base. Alembic
# renders them as fully-qualified names, so this import must be present in every
# generated migration or upgrade() fails with NameError at run time.
import backend.db.base


# revision identifiers, used by Alembic.
revision: str = '6252812243fd'
down_revision: Union[str, Sequence[str], None] = '35b8423fa3d7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('plan_nodes', schema=None) as batch_op:
        # `server_default` 在这里是**必需**的,不是抄来的习惯:SQLite 的 ADD COLUMN
        # 不接受"NOT NULL 且无默认值"。它同时承担了回填 —— 存量行由数据库填成
        # 'planning',所以下面没有 `op.get_bind()` 那一段。
        batch_op.add_column(
            sa.Column(
                'purpose',
                sa.Enum(
                    'planning',
                    'information',
                    name='node_purpose',
                    native_enum=False,
                    length=32,
                ),
                server_default=sa.text("'planning'"),
                nullable=False,
            )
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('plan_nodes', schema=None) as batch_op:
        batch_op.drop_column('purpose')
