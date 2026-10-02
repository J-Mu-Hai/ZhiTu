"""公开研究的跨进程缓存与每日额度。

## 为什么必须是数据库,而不是进程内字典

服务可能跑多个 worker。进程内缓存与"先 count 再请求"在单进程下看着没问题,多进程会:
重复出网、重复计费、并轻微超过每日上限。这两张表把状态放到**所有 worker 都看得见的
地方**,并用条件更新/唯一约束把"抢租约"和"预占额度"做成原子操作。

## 两张表

- `research_cache`:按稳定 hash 的 cache key 一行。`fetching` 表示某个 worker 正在
  出网(带一个会过期的 lease);`success` 表示 TTL 内的可用结果;`failed` 可被下一次
  接管重试。**不存原始用户对话**,只存规范化且已隐私过滤的安全 query。
- `research_daily_quota`:`(provider, local_day)` 唯一一行,`reserved` 是已经预占的
  真实出网次数。达到上限后条件更新不匹配,后续请求被拒 —— 不需要也不允许先读后写。
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.db.base import (
    Base,
    JsonDict,
    TimestampMixin,
    UtcDateTime,
    UuidPk,
    enum_type,
)
from backend.db.models.enums import ResearchCacheStatus


class ResearchCache(UuidPk, TimestampMixin, Base):
    __tablename__ = "research_cache"

    #: 稳定 hash(provider + 规范化 query + 白名单 + 结果数)。**不是原始文本。**
    cache_key: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    #: 规范化、已隐私过滤的安全 query(便于排查;不是原始用户消息)。
    query: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[ResearchCacheStatus] = mapped_column(
        enum_type(ResearchCacheStatus, "research_cache_status"), nullable=False
    )
    #: 成功时的来源列表(已截断的摘要)。
    sources: Mapped[list] = mapped_column(JsonDict, default=list, nullable=False)
    #: 正在出网的租约到期时间;到点后可被其他 worker 接管。
    lease_expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    #: 成功缓存的 TTL 到期时间;失败/进行中为 NULL。
    expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    __table_args__ = (
        UniqueConstraint("cache_key", name="uq_research_cache_cache_key"),
        Index("ix_research_cache_status_expires_at", "status", "expires_at"),
    )


class ResearchDailyQuota(UuidPk, TimestampMixin, Base):
    __tablename__ = "research_daily_quota"

    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    #: `RESEARCH_TIMEZONE` 下的自然日。
    local_day: Mapped[date] = mapped_column(Date, nullable=False)
    #: 已预占的真实出网次数。
    reserved: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    #: **刻意不挂 workspace_id**:研究额度是全局的,不能因为某个空间被删而回退额度。
    __table_args__ = (
        UniqueConstraint(
            "provider", "local_day", name="uq_research_daily_quota_provider_local_day"
        ),
    )
