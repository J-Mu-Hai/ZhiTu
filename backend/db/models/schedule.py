"""排期场次与执行记录。

ScheduledSession 是"哪天做"的唯一表示。一个任务需要 8 小时就产生 8 场,
但 plan_nodes 里始终只有一行。

关于 start_minute:**允许为空**。用户还没给出具体空闲时段时,我们只排
"这天投入 60 分钟",不凭空指定 19:00 开始。凭空捏造时钟时间会制造大量无意义
的日历抖动,而且用户在预演时会看到一个自己没同意过的时间表。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import (
    text as sql_text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db.base import Base, JsonDict, TimestampMixin, UtcDateTime, UuidPk, enum_type
from backend.db.models.enums import (
    ExecutionResult,
    ScheduledSessionOrigin,
    ScheduledSessionStatus,
)

#: partial index 的谓词:已取消/已移动的场次不占用"同一天同一序号"这个槽位。
_ACTIVE_SLOT_PREDICATE = "status NOT IN ('canceled', 'moved')"


class ScheduledSession(UuidPk, TimestampMixin, Base):
    __tablename__ = "scheduled_sessions"

    # 冗余 user_id:规则"跨所有空间每天的投入不超过个人预算"因此变成一次带索引的
    # 聚合查询,不必 JOIN workspaces。这个冗余的维护点只有一个(排期服务)。
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    node_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("plan_nodes.id", ondelete="CASCADE"), nullable=False
    )
    plan_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("plan_revisions.id", ondelete="SET NULL")
    )

    # 按**用户本地日期**写入,不由 UTC 时间戳推导。
    scheduled_date: Mapped[date] = mapped_column(Date, nullable=False)
    # 当日分钟数。为空表示"只定了哪天、多少分钟,没定时钟时间"。
    start_minute: Mapped[int | None] = mapped_column(Integer)
    end_minute: Mapped[int | None] = mapped_column(Integer)

    planned_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    # 缓冲也计入当天占用。存在行上,是为了让"当天总占用 ≤ 上限"这条不变量
    # 可以随时从数据库直接复核,而不是只能信任算法的内部状态。
    buffer_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    actual_minutes: Mapped[int | None] = mapped_column(Integer)

    # 当天的先后次序。
    seq: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    status: Mapped[ScheduledSessionStatus] = mapped_column(
        enum_type(ScheduledSessionStatus, "scheduled_session_status"),
        default=ScheduledSessionStatus.PLANNED,
        nullable=False,
    )
    # 用户锁定后,重排绝不能自动移动它。
    locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    lock_reason: Mapped[str | None] = mapped_column(String(200))
    origin: Mapped[ScheduledSessionOrigin] = mapped_column(
        enum_type(ScheduledSessionOrigin, "scheduled_session_origin"),
        default=ScheduledSessionOrigin.SCHEDULER,
        nullable=False,
    )

    moved_to_session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("scheduled_sessions.id", ondelete="SET NULL")
    )
    completed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    workspace: Mapped[Workspace] = relationship()  # noqa: F821
    node: Mapped[PlanNode] = relationship()  # noqa: F821

    __table_args__ = (
        CheckConstraint("planned_minutes > 0", name="planned_minutes_positive"),
        CheckConstraint("buffer_minutes >= 0", name="buffer_minutes_non_negative"),
        CheckConstraint(
            "actual_minutes IS NULL OR actual_minutes >= 0", name="actual_minutes_non_negative"
        ),
        CheckConstraint(
            "start_minute IS NULL OR (start_minute >= 0 AND start_minute < 1440)",
            name="start_minute_within_day",
        ),
        CheckConstraint(
            "end_minute IS NULL OR (end_minute > 0 AND end_minute <= 1440)",
            name="end_minute_within_day",
        ),
        CheckConstraint(
            "start_minute IS NULL OR end_minute IS NULL OR start_minute < end_minute",
            name="start_before_end",
        ),
        # 规则(跨空间每日总量)的热路径。
        Index(
            "ix_scheduled_sessions_user_id_scheduled_date_status",
            "user_id",
            "scheduled_date",
            "status",
        ),
        Index(
            "ix_scheduled_sessions_workspace_id_scheduled_date",
            "workspace_id",
            "scheduled_date",
        ),
        Index("ix_scheduled_sessions_node_id", "node_id"),
        # 同一个任务的同一天序号不重复。两端都要给 where,否则本地 SQLite 静默失去约束,
        # 而"重复确认导致重复建场次"正是这个约束要挡的 bug。
        Index(
            "uq_scheduled_sessions_slot",
            "node_id",
            "scheduled_date",
            "seq",
            unique=True,
            postgresql_where=sql_text(_ACTIVE_SLOT_PREDICATE),
            sqlite_where=sql_text(_ACTIVE_SLOT_PREDICATE),
        ),
    )


class ScheduleApplication(UuidPk, Base):
    """「应用这份排期」的幂等台账。

    ## 为什么不能像提案那样靠"看看状态是不是已经应用过"来去重

    提案有一列 `status` 可以做 CAS,而排期没有这样一列 —— "这份排期应用过了吗"
    这个问题在 `scheduled_sessions` 里没有等价物。更糟的是,**重复应用不会自然幂等**:
    结果里那些新场次(`session_id` 为空)第二次会再插一遍,同一个任务在同一天多出
    两场,而界面上看起来只是"今天要做两小时"。

    所以这里必须有一张台账。唯一键 `(user_id, idempotency_key)` 由数据库保证:两个
    并发请求抢着插同一行,只有一个成功;输的那个回读赢家写好的响应原样返回。

    ## 它**不**进 `plan_revisions`

    排期是"哪天做",节点图是"要做什么",两者各自有自己的历史:前者在这张表与
    `scheduled_sessions` 里,后者在 `plan_revisions` 里。把应用排期也记成一次计划版本,
    副作用是所有待确认的提案会在下一次确认时被判成"计划已经变了" —— 而用户只是点了
    一下"应用排期",节点一个都没改。让提案因为一件与它无关的事失效,代价由用户承担。
    """

    __tablename__ = "schedule_applications"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # 用户是从哪个空间点的"应用"。**写入范围不限于这个空间**(见 schedule_service)——
    # 池子是按人算的,只应用一半会让另一半的安排与实际占用对不上。记下它是为了能回答
    # "这次排期是从哪儿发起的"。
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 请求体的规范化哈希。同一个键配不同请求体时必须拦下来(见 IdempotencyKeyReused)。
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 被应用的那个输入的指纹(`scheduler.diff.schedule_version`)。事后排查"用户当时
    #: 点的是哪一版"靠它,不必再去猜。
    schedule_version: Mapped[str] = mapped_column(String(32), nullable=False)
    #: 当时返回给客户端的完整响应体,重放时原样返回。
    result: Mapped[dict] = mapped_column(JsonDict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "idempotency_key",
            name="uq_schedule_applications_user_id_idempotency_key",
        ),
        Index("ix_schedule_applications_workspace_id", "workspace_id"),
    )


class ExecutionRecord(UuidPk, Base):
    """执行反馈。**只追加,永不修改。**

    排期算法把它当作只读输入:已完成的场次不是"可以被重排"的候选项,而是既成事实。
    这条性质由"没有任何代码 UPDATE 这张表"来保证,不是靠算法自觉。
    """

    __tablename__ = "execution_records"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    # 可以为空:用户可能直接对任务报告进度,而不是对某个具体场次。
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("scheduled_sessions.id", ondelete="SET NULL")
    )
    node_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("plan_nodes.id", ondelete="CASCADE"), nullable=False
    )

    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    ended_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    actual_minutes: Mapped[int | None] = mapped_column(Integer)
    # 0.000 ~ 1.000
    completion_ratio: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))

    result: Mapped[ExecutionResult] = mapped_column(
        enum_type(ExecutionResult, "execution_result"), nullable=False
    )
    delay_reason: Mapped[str | None] = mapped_column(Text)
    user_feedback: Mapped[str | None] = mapped_column(Text)

    # 客户端重试去重。
    idempotency_key: Mapped[str | None] = mapped_column(String(64))
    #: 请求体的规范化哈希。同一个键配不同内容时**必须**被拦下来(见
    #: `IdempotencyKeyReused`)—— 否则客户端把所有请求共用一个固定字符串时,用户会
    #: 看到"另一场安排的执行结果"被记录成功,而他自己那次反馈凭空消失。
    content_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "actual_minutes IS NULL OR actual_minutes >= 0", name="actual_minutes_non_negative"
        ),
        CheckConstraint(
            "completion_ratio IS NULL OR (completion_ratio >= 0 AND completion_ratio <= 1)",
            name="completion_ratio_range",
        ),
        Index("ix_execution_records_user_id_created_at", "user_id", "created_at"),
        Index("ix_execution_records_session_id", "session_id"),
        Index("ix_execution_records_node_id", "node_id"),
        Index(
            "uq_execution_records_idempotency",
            "user_id",
            "idempotency_key",
            unique=True,
            postgresql_where=sql_text("idempotency_key IS NOT NULL"),
            sqlite_where=sql_text("idempotency_key IS NOT NULL"),
        ),
    )
