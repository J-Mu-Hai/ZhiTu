"""conversation-first strategic intake

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-10-03 20:00:00.000000

阶段 12:把战略 intake 从 Question Node / 选项问卷改成真正的对话式顾问。

- `goal_reasoning_sessions.pending_intake_message_id`:正在等用户回答的那条 intake
  助手消息。非空 = 这个空间正在等 intake 回答,用户下一条消息就是它的答案。
- `goal_reasoning_sessions.pending_intake_decision`:待回答问题的结构化决策
  (question / decisionScope / whyThisMatters / quickReplies),**不是问题实体**。

两列都可空:老会话没有它们,行为与加列之前完全一样(Boolean/JSON 列缺省即 NULL)。
不再新增 `agent_questions.presentation='conversation_intake'` 的行;老行保留不删,
由读取层明确隔离(见 `reasoning_service._primary_pending_question`)。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from backend.db.base import JsonDict


# revision identifiers, used by Alembic.
revision: str = 'd4e5f6a7b8c9'
down_revision: Union[str, Sequence[str], None] = 'c3d4e5f6a7b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('goal_reasoning_sessions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('pending_intake_message_id', sa.Uuid(), nullable=True))
        batch_op.add_column(sa.Column('pending_intake_decision', JsonDict(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('goal_reasoning_sessions', schema=None) as batch_op:
        batch_op.drop_column('pending_intake_decision')
        batch_op.drop_column('pending_intake_message_id')
