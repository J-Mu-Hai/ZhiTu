"""用户、登录会话、时间预算与可用时段。

可用时段(capacity / availability)挂在**用户**上,不是空间上 —— 这是"跨空间总投入
不超过个人时间预算"这条规则能成立的前提。若按空间各存一份可用时间,两个空间就会
各自以为自己能占满同一周的晚上。
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
    UniqueConstraint,
)
from sqlalchemy import (
    text as sql_text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from backend.core.security import normalize_email
from backend.db.base import (
    Base,
    TimestampMixin,
    UtcDateTime,
    UuidPk,
    enum_type,
)
from backend.db.models.enums import AvailabilitySource


class User(UuidPk, TimestampMixin, Base):
    __tablename__ = "users"

    # 邮箱一律以小写形式存储,唯一性因此天然大小写不敏感。两件事共同保证它成立:
    #
    #   1. `_normalize_email` 在 ORM 层归一化(正常写入路径);
    #   2. `ck_users_email_is_canonical` 在数据库层拒绝任何非规范形式的值(绕过 ORM 的写入)。
    #
    # 第 2 条不是多余的:只有唯一约束 + ORM 归一化时,一条绕过 ORM 的写入(原生 SQL、
    # bulk insert)可以塞进 `Alice@Example.COM`,它与 `alice@example.com` 不相等,
    # 唯一约束放行 —— 于是"大小写不敏感唯一"就只是应用层的君子协定。实测确认过这个
    # 洞存在。加上 CHECK 之后,任何写入者都无法制造这种逃逸。
    #
    # 这里**不再用** `Index(..., func.lower(email), unique=True)`:那种表达式索引
    # SQLAlchemy 2.0.52 的 SQLite 方言无法反射,autogenerate 会在生成迁移时静默丢掉,
    # `alembic check` 也照样报"无漂移" —— 建出来的库里根本没有这个索引,而 PostgreSQL
    # 上又能看见它,本地与线上静默分叉。CHECK 与 UNIQUE 都直接写在 CREATE TABLE 里,
    # 没有这个盲区。
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    display_name: Mapped[str] = mapped_column(String(80), nullable=False)

    school: Mapped[str | None] = mapped_column(String(120))
    major: Mapped[str | None] = mapped_column(String(120))
    year: Mapped[str | None] = mapped_column(String(32))
    rank: Mapped[int | None] = mapped_column(Integer)
    target_year: Mapped[int | None] = mapped_column(Integer)
    target_goal: Mapped[str | None] = mapped_column(String(500))
    bio: Mapped[str | None] = mapped_column(String(1000))

    # 每个用户一个时区。"哪一天"永远按这个时区算,不由 UTC 时间戳推导。
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai", nullable=False)
    # 改密码后自增,使已发出的会话令牌立即失效。
    token_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    __table_args__ = (
        UniqueConstraint("email", name="uq_users_email"),
        # 把这一列的规范形式完整写死:去掉首尾空白 + 小写。
        #
        # 少了 trim 这一半,`'alice@example.com '` 会满足 CHECK(`lower` 不处理空白),
        # 又与 `'alice@example.com'` 不相等,于是唯一约束照样放行 —— 半个不变量比没有
        # 更糟,因为它让人以为已经安全了。必须与 `_normalize_email` 完全一致。
        #
        # 注意 lower() 在 SQLite 上只处理 ASCII —— 邮箱地址本身是 ASCII(IDN 会先被
        # punycode 化),所以够用。trim() 两端都只去空格,不要往里塞制表符/换行,
        # 那个语法在 SQLite 与 PostgreSQL 上不一致。
        CheckConstraint("email = lower(trim(email))", name="email_is_canonical"),
    )

    @validates("email")
    def _normalize_email(self, _key: str, value: str) -> str:
        """在 ORM 层强制归一化。

        放在模型上而不是服务层:只要走 ORM 写入,就没有哪条代码路径能忘记归一化,
        唯一约束也就不会被大小写绕过。

        归一化规则本身写在 `core/security.normalize_email`,不在这里重写一遍 ——
        上面那条 CHECK 与这个函数必须逐字一致,而"逐字一致"的唯一可行做法是同一份代码。
        """
        if not isinstance(value, str):
            return value
        return normalize_email(value)


class AuthSession(UuidPk, Base):
    """不透明 bearer 令牌的会话记录。

    只存令牌的 SHA-256,不存明文。撤销与轮换都靠这张表 —— 这也是不采用 JWT 的原因:
    JWT 在过期前无法撤销。
    """

    __tablename__ = "auth_sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    # 签发这条会话时,用户的 token_version 是多少。
    #
    # 这一列是 `User.token_version` 那个机制的另一半:光在用户上自增一个整数,
    # 没有任何东西可以拿它来比对,那句"改密码后立即失效"就只是一句注释。
    # 有了它,校验会话时多比一个整数即可 —— 改密码后一条 UPDATE 就让这个用户的
    # 全部会话同时失效,不需要扫 auth_sessions。
    # server_default 在这里不是为了省事,是**唯一能让两端都跑起来的写法**:
    # SQLite 的 ADD COLUMN 不接受"NOT NULL 且无默认值"的列(不论表里有没有数据),
    # PostgreSQL 则接受。只给 Python 侧 default 的话,本地升级直接报错而线上正常 ——
    # 又一个"本地与线上静默分叉"。给上常量默认值,两个后端就都是同一条 ALTER。
    token_version: Mapped[int] = mapped_column(
        Integer, default=0, server_default=sql_text("0"), nullable=False
    )

    issued_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    # 绝对上限。无论怎么滑动续期,超过这个时间必须重新登录。
    absolute_expires_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    # 轮换链:若一个已被轮换掉的令牌被再次使用,整族一并撤销。
    rotated_from_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("auth_sessions.id", ondelete="SET NULL")
    )
    rotated_to_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("auth_sessions.id", ondelete="SET NULL")
    )

    user_agent: Mapped[str | None] = mapped_column(String(400))
    ip_hash: Mapped[str | None] = mapped_column(String(64))

    user: Mapped[User] = relationship()

    __table_args__ = (
        Index("ix_auth_sessions_user_id_expires_at", "user_id", "expires_at"),
    )


class UserCapacityProfile(UuidPk, TimestampMixin, Base):
    """个人时间预算。跨所有空间共享。

    safety_factor 只在这里出现一次。可行性闸门与每日池都用 (weekly_total_minutes ×
    safety_factor),两处各乘一次会报出根本不存在的缺口。
    """

    __tablename__ = "user_capacity_profiles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True
    )

    weekly_total_minutes: Mapped[int] = mapped_column(Integer, default=600, nullable=False)
    safety_factor: Mapped[Decimal] = mapped_column(
        Numeric(3, 2), default=Decimal("0.80"), nullable=False
    )
    daily_max_minutes: Mapped[int | None] = mapped_column(Integer)
    default_buffer_minutes: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    min_session_minutes: Mapped[int] = mapped_column(Integer, default=15, nullable=False)
    max_session_minutes: Mapped[int] = mapped_column(Integer, default=120, nullable=False)
    # 0 = 周一。与 Python date.weekday() 一致。
    week_start_weekday: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    user: Mapped[User] = relationship()

    __table_args__ = (
        CheckConstraint("weekly_total_minutes >= 0", name="weekly_total_minutes_non_negative"),
        CheckConstraint(
            "safety_factor >= 0.50 AND safety_factor <= 1.00", name="safety_factor_range"
        ),
        CheckConstraint("min_session_minutes > 0", name="min_session_minutes_positive"),
        CheckConstraint(
            "max_session_minutes >= min_session_minutes", name="max_session_minutes_gte_min"
        ),
        CheckConstraint(
            "default_buffer_minutes >= 0", name="default_buffer_minutes_non_negative"
        ),
    )


class AvailabilityRule(UuidPk, TimestampMixin, Base):
    """周期性的可用时段,按周几描述。用户没提供过具体时段时这张表是空的。"""

    __tablename__ = "availability_rules"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # 0=周一 .. 6=周日
    weekday: Mapped[int] = mapped_column(Integer, nullable=False)
    # 当日分钟数,0..1440。用分钟整数而不是 TIME,便于与排期算法统一运算。
    start_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    end_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[AvailabilitySource] = mapped_column(
        enum_type(AvailabilitySource, "availability_source"),
        default=AvailabilitySource.RULE,
        nullable=False,
    )
    effective_from: Mapped[date | None] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date)

    user: Mapped[User] = relationship()

    __table_args__ = (
        CheckConstraint("weekday >= 0 AND weekday <= 6", name="weekday_range"),
        CheckConstraint("start_minute >= 0", name="start_minute_non_negative"),
        CheckConstraint("end_minute <= 1440", name="end_minute_within_day"),
        CheckConstraint("start_minute < end_minute", name="start_before_end"),
        Index("ix_availability_rules_user_id_weekday", "user_id", "weekday"),
    )


class AvailabilityException(UuidPk, TimestampMixin, Base):
    """对某个具体日期的覆盖:整天不可用,或当天可用分钟数与常规不同。"""

    __tablename__ = "availability_exceptions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    on_date: Mapped[date] = mapped_column(Date, nullable=False)
    # 覆盖当天的总可用分钟数;is_unavailable 为真时忽略。
    available_minutes: Mapped[int | None] = mapped_column(Integer)
    is_unavailable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    note: Mapped[str | None] = mapped_column(String(200))

    user: Mapped[User] = relationship()

    __table_args__ = (
        UniqueConstraint("user_id", "on_date", name="uq_availability_exceptions_user_id_on_date"),
        CheckConstraint(
            "available_minutes IS NULL OR available_minutes >= 0",
            name="available_minutes_non_negative",
        ),
    )
