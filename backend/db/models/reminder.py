"""站内提醒的"用户已经处理过它"状态。

## 为什么提醒本身不落库、只有状态落库

提醒的**内容**是从既有事实现算出来的:哪个空间是空的、哪一份计划刚生成、哪几天连续
没记录、今天是不是周末。把它当成一张"通知表"存下来的话,每一条都要有人在某个时刻
写进去,而"谁在什么时候写"本身就成了一个会漏的地方 —— 漏掉的那种提醒不会报错,
只会永远不出现。

所以 `reminder_service` 每次都用当前数据重算一遍,这里只存**用户对某条提醒做过的
处置**:关掉、或者晚点再说。键是 `reminder_key`(一个稳定的字符串,把"哪一条提醒"
说清楚,例如 `weekend:2026-W39`),不是行 id —— 提醒没有行,也就没有 id 可用。

## 键里必须带时间片

`weekend:2026-W39` 与 `weekend:2026-W40` 是两条不同的提醒。键里不带时间片的话,
用户这周点掉「周末了,看看这周排了几场」,下周五它就再也不会出现了 —— 而那条提醒
本来是新的一周的、新的信息。

## 一次处置是幂等的

重复关闭同一条提醒不报错(CAS 成"已经关了"就是同一件事),这与其他写路径上的
幂等约定一致:用户点两下不该看到一个 409。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.db.base import Base, TimestampMixin, UtcDateTime, UuidPk


class ReminderState(UuidPk, TimestampMixin, Base):
    """一条提醒被处置过的记录。

    `dismissed_at` 与 `snoozed_until` **可以同时有值**:用户先点了"稍后提醒",一小时后
    它又出现,这次他点了"关掉"。两者共存不是冲突,是两个先后发生的事实。
    """

    __tablename__ = "reminder_states"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    #: 稳定标识,形如 `weekend:2026-W39`、`repeated_skips:<workspace uuid>`。
    #:
    #: 不加长度上限的想象力:最长的形态是 `plan_created:<uuid>:<version>`,约 60 字符;
    #: 给到 160 是留出余量,而不是鼓励往这里塞内容。
    reminder_key: Mapped[str] = mapped_column(String(160), nullable=False)
    #: 这条提醒说的是哪个空间。**可以为空** —— 「用户回归」「周末」这两类跨所有空间。
    #:
    #: 不设成级联删除:空间被删掉之后,那条提醒的处置记录留着没有任何害处,而
    #: 级联删除会让"用户关掉过它"这件事随着空间一起消失。
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workspaces.id", ondelete="SET NULL")
    )
    dismissed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    #: 到这个时刻之前不要再出现。到点之后提醒会**重新出现** —— "稍后"是一个真实的
    #: 承诺,不是一个软化的关闭。
    snoozed_until: Mapped[datetime | None] = mapped_column(UtcDateTime)

    __table_args__ = (
        # 一个用户对同一条提醒只有一行状态。两个并发的"关掉"抢着插同一行时,
        # 只有一个成功,另一个回读赢家那一行 —— 用户看到的都是"关掉了"。
        UniqueConstraint(
            "user_id", "reminder_key", name="uq_reminder_states_user_id_reminder_key"
        ),
        # 热路径:`GET /reminders` 每次都要把这个用户的全部处置读出来。
        Index("ix_reminder_states_user_id", "user_id"),
    )
