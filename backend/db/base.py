"""声明基类与跨数据库通用列类型。

同一套模型必须同时跑在 SQLite(本地开发/测试)与 PostgreSQL(线上)。凡是两端行为
不同的能力,都在这里收敛成一个类型或一条纪律,而不是散落在各个模型里:

- UUID 主键     -> Uuid(as_uuid=True) + Python 侧 uuid4(),不用 PG 的 UUID 类型,也不用
                   gen_random_uuid()(需要 pgcrypto 扩展)。
- JSON 列       -> JsonDict(JSONB on PG / JSON on SQLite)。**禁止**在主键之外的查询里用
                   JSONB 操作符(@> / ? / GIN 索引);需要按内容过滤就提升为真实列。
- 时间戳        -> UtcDateTime。统一 UTC 存取。关键纪律:**"哪一天"绝不由时间戳推导**,
                   一律使用写入时按用户时区算好的 Date 列(scheduled_date / deadline)。
- 枚举          -> enum_type(),native_enum=False。两端都是 VARCHAR + CHECK,
                   避免 PostgreSQL ENUM 类型的迁移生命周期问题。
- 命名约定      -> MetaData(naming_convention=...)。否则 SQLite 的 batch 迁移会产生
                   无法删除的匿名约束,autogenerate 也会每次抖动。
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, MetaData, Text, TypeDecorator, Uuid
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class JsonDict(TypeDecorator):
    """JSONB on PostgreSQL, plain JSON on SQLite.

    Deliberately a named class rather than a shared type instance: Alembic renders the
    type of every column into the migration file, and a bare with_variant() instance gets
    expanded to its full constructor expression (which then references helpers the
    migration does not import). A class renders as `backend.db.base.JsonDict()`.

    Discipline: never filter on the *contents* of this column with PostgreSQL-only
    operators (@>, ?, GIN). Promote whatever you need to query into a real column.
    """

    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(postgresql.JSONB(astext_type=Text()))
        return dialect.type_descriptor(JSON())


class UtcDateTime(TypeDecorator):
    """始终以带时区的 UTC datetime 存取。

    PostgreSQL 存 TIMESTAMPTZ;SQLite 没有时区类型,所以写入前剥掉 tzinfo、读回时补回 UTC。
    传入 naive datetime 会直接报错 —— 这是刻意的:静默假定本地时区是这类 bug 最常见的来源。
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(
                "UtcDateTime 只接受带时区的 datetime;请用 backend.db.base.utcnow() "
                "或显式 .replace(tzinfo=UTC)"
            )
        utc_value = value.astimezone(UTC)
        if dialect.name == "sqlite":
            return utc_value.replace(tzinfo=None)
        return utc_value

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


def utcnow() -> datetime:
    """唯一允许的"当前时间"来源。"""
    return datetime.now(UTC)


def enum_type(enum_cls: type[enum.Enum], name: str) -> SAEnum:
    """VARCHAR + CHECK,两端一致。

    values_callable 让数据库里存的是枚举的 value(小写),与 API / 前端一致,
    而不是 Python 的成员名。
    """
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=False,
        length=32,
        validate_strings=True,
        values_callable=lambda cls: [member.value for member in cls],
    )


class UuidPk:
    """UUID 主键。对象在 flush 前就已有 id,便于在事务内互相引用。"""

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    """创建/更新时间。全部由 Python 侧生成,不用 server_default=now()(两端语义不同)。"""

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=utcnow, onupdate=utcnow, nullable=False
    )
