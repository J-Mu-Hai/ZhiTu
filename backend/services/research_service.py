"""受控的真实公开研究(Tavily)。**默认不联网。**

## 它解决什么

有些未知不是用户私有的,而是可公开验证、会随时间变化的事实(学校政策、申请截止、
比赛规则、公开课程/岗位要求)。把这类问题直接问用户,是把本可以自己查清的负担推回去。
`research_public` 工具在**显式配置**后可以真实检索,并把来源作为“可确认的信息依据”。

## 跨进程状态:为什么要数据库

服务可能跑多个 worker。进程内缓存与“先 count 再请求”在单进程下看着没问题,多进程会
重复出网、重复计费、并轻微超过每日上限。所以:

- `research_cache`:按稳定 hash 的 cache key 一行,带 `fetching` 租约;TTL 内的成功结果
  直接命中,不联网、不占额度。
- `research_daily_quota`:`(provider, local_day)` 唯一行,用**条件更新**原子预占额度,
  绝不 `先 SELECT count 再决定`。

## 四条硬边界

1. **默认关闭。** 未配置时**不发任何请求**,返回“未配置”。
2. **密钥不出门。** `TAVILY_API_KEY` 只用于请求,绝不进返回、记录或日志。
3. **只读、不写业务数据。** 研究结果只进工具摘要与缓存;要写图谱/排期/战略仍走确认。
4. **失败如实说。** 超时/HTTP 错误/无结果/额度用完都返回“没有完成公开研究”。

## 额度与租约的边界

- 额度在**真实 HTTP 之前**预占;一旦预占,即便 timeout/429/5xx/解析失败也**不退回**
  (它们可能已经产生提供商费用)。
- 缓存命中、未配置、隐私拦截、同 query 正在拉取都不消耗额度。
- 租约到期后其他 worker 可接管,避免进程崩溃导致永久卡死。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import blake2b
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.config import settings
from backend.db.base import utcnow
from backend.db.models import ResearchCache, ResearchDailyQuota
from backend.db.models.enums import ResearchCacheStatus
from backend.db.session import SessionLocal

logger = logging.getLogger(__name__)

TAVILY_ENDPOINT = "https://api.tavily.com/search"
PROVIDER = "tavily"
#: 测试用的 mock provider。**不是产品能力** —— 只在显式开启且非生产时可用。
MOCK_PROVIDER = "mock"
#: 单条摘录的最多字符数。来源只需要“够判断”,不需要整页。
MAX_EXCERPT_CHARS = 400
MAX_TITLE_CHARS = 200
MAX_URL_CHARS = 500
#: 发现同 query 正在拉取时,最多轮询等待多久(秒)。等不到就返回 `in_progress` 降级。
WAIT_ATTEMPTS = 10
WAIT_DELAY_SECONDS = 0.2


@dataclass(frozen=True, slots=True)
class ResearchSource:
    title: str
    url: str
    accessed_at: str
    excerpt: str


@dataclass(frozen=True, slots=True)
class ResearchOutcome:
    """一次研究的结果。`sources` 为空时表示**没有完成公开研究**。"""

    configured: bool
    reason: str
    query: str
    sources: tuple[ResearchSource, ...] = ()
    cached: bool = False
    #: 失败的类型(`timeout` / `error`)。**不改 `reason`** —— 既有语义保持
    #: `RESEARCH_FAILED`,这里只是让引用契约能把超时与普通错误分开说。
    error_kind: str | None = None


def normalize_query(query: str) -> str:
    return " ".join(query.strip().lower().split())


def _research_tz() -> ZoneInfo:
    try:
        return ZoneInfo(settings.research_timezone)
    except Exception:
        return ZoneInfo("Asia/Shanghai")


def local_day() -> str:
    """当前 `RESEARCH_TIMEZONE` 下的自然日(ISO)。"""
    return utcnow().astimezone(_research_tz()).date().isoformat()


def clear_cache() -> None:
    """测试用:清空进程内可能残留的缓存状态(现在缓存都在数据库,事务回滚即清)。"""
    return None


#: 租约至少要比单次 HTTP 超时多这么多秒,否则“HTTP 还没超时、租约却过期了”会再出网。
LEASE_MIN_BUFFER_SECONDS = 2.0


def lease_config_error() -> str | None:
    """租约/超时组合是否安全。不安全时返回原因,否则 `None`。

    必须 `RESEARCH_LEASE_SECONDS >= RESEARCH_TIMEOUT_SECONDS + 缓冲`:否则一个还没
    超时的出网请求,其租约可能已经过期,第二个 worker 会接管并重复出网。
    """
    if settings.research_lease_seconds <= settings.research_timeout_seconds:
        return "lease_not_greater_than_timeout"
    if settings.research_lease_seconds < settings.research_timeout_seconds + LEASE_MIN_BUFFER_SECONDS:
        return "lease_buffer_too_small"
    return None


def provider_ready() -> tuple[bool, str]:
    """提供商是否可用。返回 (就绪, 不可用原因)。**原因里不含密钥。**

    mock 有三道闸:`RESEARCH_PROVIDER=mock`、`RESEARCH_MOCK_ENABLED=true`、且
    `APP_ENV != production`。任何一道不满足都返回不可用 —— 生产环境**不可能**
    因为一个配错的环境变量而启用 mock。
    """
    if not settings.research_enabled:
        return False, "RESEARCH_ENABLED_FALSE"
    provider = (settings.research_provider or "none").strip().lower()
    if provider in ("", "none", "off", "disabled"):
        return False, "RESEARCH_PROVIDER_NONE"
    if provider == MOCK_PROVIDER:
        if settings.app_env == "production":
            return False, "RESEARCH_MOCK_IN_PRODUCTION"
        if not settings.research_mock_enabled:
            return False, "RESEARCH_MOCK_DISABLED"
        if lease_config_error() is not None:
            return False, "RESEARCH_LEASE_INVALID"
        return True, "ok"
    if provider != PROVIDER:
        return False, "RESEARCH_PROVIDER_UNSUPPORTED"
    if not (settings.tavily_api_key or "").strip():
        return False, "TAVILY_API_KEY_MISSING"
    if lease_config_error() is not None:
        return False, "RESEARCH_LEASE_INVALID"
    return True, "ok"


#: mock provider 的固定来源。用保留的 `example` 域,不指向任何真实站点。
_MOCK_SOURCES: tuple[dict, ...] = (
    {
        "title": "公开政策说明(测试来源)",
        "url": "https://example.edu.cn/mock-policy",
        "content": "这是测试用的公开来源摘要,用于验证引用链路,不对应真实政策。",
    },
    {
        "title": "公开报名时间(测试来源)",
        "url": "https://example.edu.cn/mock-deadline",
        "content": "报名截止时间以测试来源为准。",
    },
)


def _mock_results(query: str) -> list[dict]:
    """**测试用**的确定性公开来源。

    由 query 里的标记决定成功 / 超时 / 失败 / 空 —— 让隔离 E2E 不必真的出网,
    也不必依赖公网波动。不是产品能力:三道闸见 `provider_ready`。
    """
    lowered = query.lower()
    if "__timeout__" in lowered:
        raise httpx.TimeoutException("mock timeout")
    if "__fail__" in lowered:
        raise httpx.ConnectError("mock failure")
    if "__empty__" in lowered:
        return []
    return [dict(item) for item in _MOCK_SOURCES]


async def _fetch_raw(query: str) -> list[dict]:
    """按配置选真实 Tavily 还是测试 mock。**只有这两种。**"""
    provider = (settings.research_provider or "").strip().lower()
    if provider == MOCK_PROVIDER:
        return _mock_results(query)
    return await _fetch_tavily(query)


async def _fetch_tavily(query: str) -> list[dict]:
    """真实的 Tavily 调用。**测试只允许 monkeypatch 这个函数。**"""
    payload = {
        "api_key": settings.tavily_api_key,
        "query": query,
        "max_results": settings.research_max_results,
        "search_depth": "basic",
        "include_answer": False,
    }
    async with httpx.AsyncClient(timeout=settings.research_timeout_seconds) as client:
        response = await client.post(TAVILY_ENDPOINT, json=payload)
        response.raise_for_status()
        data = response.json()
    results = data.get("results") if isinstance(data, dict) else None
    return list(results) if isinstance(results, list) else []


def _hostname_of(url: str) -> str | None:
    """用 URL parser 取 hostname。**不用字符串包含。**"""
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    if not host:
        return None
    return host.rstrip(".").lower()


def _allowed_domains() -> tuple[str, ...]:
    entries: list[str] = []
    for part in (settings.research_allowed_domains or "").split(","):
        raw = part.strip().lower().lstrip(".")
        if not raw:
            continue
        if "://" in raw:
            host = _hostname_of(raw)
            if host:
                entries.append(host)
            continue
        if ":" in raw and raw.count(":") == 1:
            raw = raw.split(":", 1)[0]
        entries.append(raw.rstrip("."))
    return tuple(dict.fromkeys(entries))


def domain_allowed(host: str, allowed: tuple[str, ...]) -> bool:
    """只允许**精确域名**或**点边界子域名**。空白名单 = 不限制。"""
    if not allowed:
        return True
    normalized = host.rstrip(".").lower()
    return any(normalized == domain or normalized.endswith("." + domain) for domain in allowed)


def _to_sources(raw: list[dict]) -> tuple[ResearchSource, ...]:
    allowed = _allowed_domains()
    accessed = utcnow().isoformat()
    sources: list[ResearchSource] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        title = str(item.get("title") or "").strip()
        content = str(item.get("content") or "").strip()
        if not url or not title:
            continue
        host = _hostname_of(url)
        if not host or not domain_allowed(host, allowed):
            continue
        sources.append(
            ResearchSource(
                title=title[:MAX_TITLE_CHARS],
                url=url[:MAX_URL_CHARS],
                accessed_at=accessed,
                excerpt=content[:MAX_EXCERPT_CHARS],
            )
        )
        if len(sources) >= settings.research_max_results:
            break
    return tuple(sources)


# ---------------------------------------------------------------------------------
# 缓存键
# ---------------------------------------------------------------------------------
def cache_key_for(provider: str, normalized_query: str) -> str:
    """稳定 hash:provider + 安全 query + 白名单 + 影响结果的参数。**不是原始文本。**"""
    parts = "|".join(
        [
            provider,
            normalized_query,
            ",".join(sorted(_allowed_domains())),
            str(settings.research_max_results),
        ]
    )
    return blake2b(parts.encode("utf-8"), digest_size=32).hexdigest()


def _sources_to_json(sources: tuple[ResearchSource, ...]) -> list[dict]:
    return [
        {"title": s.title, "url": s.url, "accessedAt": s.accessed_at, "excerpt": s.excerpt}
        for s in sources
    ]


def _sources_from_json(raw: object) -> tuple[ResearchSource, ...]:
    if not isinstance(raw, list):
        return ()
    sources: list[ResearchSource] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "")
        url = str(item.get("url") or "")
        if not title or not url:
            continue
        sources.append(
            ResearchSource(
                title=title,
                url=url,
                accessed_at=str(item.get("accessedAt") or ""),
                excerpt=str(item.get("excerpt") or ""),
            )
        )
    return tuple(sources)


async def _read_cache(session: AsyncSession, key: str) -> ResearchCache | None:
    return await session.scalar(select(ResearchCache).where(ResearchCache.cache_key == key))


async def _acquire_lease(
    session: AsyncSession, *, key: str, query: str, now: datetime
) -> bool:
    """抢租约。成功返回 True(该请求可以出网)。**原子。**"""
    lease_until = now + timedelta(seconds=settings.research_lease_seconds)
    result = await session.execute(
        update(ResearchCache)
        .where(
            ResearchCache.cache_key == key,
            # **只允许这三种可以抢占的状态。** 尤其:新鲜的 success 不能被抢 ——
            # 否则一个读到过期缓存的旧请求会把另一个 worker 刚写入的新结果改成
            # fetching,导致重复出网。
            or_(
                ResearchCache.status == ResearchCacheStatus.FAILED,
                and_(
                    ResearchCache.status == ResearchCacheStatus.SUCCESS,
                    or_(ResearchCache.expires_at.is_(None), ResearchCache.expires_at <= now),
                ),
                and_(
                    ResearchCache.status == ResearchCacheStatus.FETCHING,
                    or_(
                        ResearchCache.lease_expires_at.is_(None),
                        ResearchCache.lease_expires_at <= now,
                    ),
                ),
            ),
        )
        .values(status=ResearchCacheStatus.FETCHING, lease_expires_at=lease_until, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 1:
        await session.commit()
        return True
    # 没有行 -> 插入一个 fetching 行;唯一约束保证只有一个赢家。
    session.add(
        ResearchCache(
            cache_key=key,
            provider=PROVIDER,
            query=query,
            status=ResearchCacheStatus.FETCHING,
            sources=[],
            lease_expires_at=lease_until,
            expires_at=None,
        )
    )
    try:
        await session.commit()
        return True
    except IntegrityError:
        await session.rollback()
        return False


async def _release_lease(session: AsyncSession, key: str) -> None:
    """把租约置成 failed,让下一次可以重试(比如额度用完时)。"""
    now = utcnow()
    await session.execute(
        update(ResearchCache)
        .where(ResearchCache.cache_key == key)
        .values(status=ResearchCacheStatus.FAILED, lease_expires_at=None, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    await session.commit()


async def _mark_success(
    session: AsyncSession, key: str, sources: tuple[ResearchSource, ...]
) -> None:
    now = utcnow()
    await session.execute(
        update(ResearchCache)
        .where(ResearchCache.cache_key == key)
        .values(
            status=ResearchCacheStatus.SUCCESS,
            sources=_sources_to_json(sources),
            lease_expires_at=None,
            expires_at=now + timedelta(seconds=settings.research_cache_ttl_seconds),
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    await session.commit()


async def _mark_failed(session: AsyncSession, key: str) -> None:
    now = utcnow()
    await session.execute(
        update(ResearchCache)
        .where(ResearchCache.cache_key == key)
        .values(
            status=ResearchCacheStatus.FAILED,
            sources=[],
            lease_expires_at=None,
            expires_at=None,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    await session.commit()


async def _reserve_quota(session: AsyncSession, limit: int) -> bool:
    """原子预占一个每日额度。**不做“先 count 再请求”。**

    先条件 UPDATE(行存在且未满才 +1);没有行时靠唯一约束 INSERT;并发插入输的一方
    回滚后重试一次条件 UPDATE。两条路径都不会超过 `limit`。
    """
    day = utcnow().astimezone(_research_tz()).date()
    if limit <= 0:
        return False
    result = await session.execute(
        update(ResearchDailyQuota)
        .where(
            ResearchDailyQuota.provider == PROVIDER,
            ResearchDailyQuota.local_day == day,
            ResearchDailyQuota.reserved < limit,
        )
        .values(reserved=ResearchDailyQuota.reserved + 1)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 1:
        await session.commit()
        return True
    # 这一行存在吗?存在说明已经到上限(条件更新没匹配)。
    exists = await session.scalar(
        select(ResearchDailyQuota.id).where(
            ResearchDailyQuota.provider == PROVIDER, ResearchDailyQuota.local_day == day
        )
    )
    if exists is not None:
        await session.rollback()
        return False
    session.add(ResearchDailyQuota(provider=PROVIDER, local_day=day, reserved=1))
    try:
        await session.commit()
        return True
    except IntegrityError:
        await session.rollback()
        result = await session.execute(
            update(ResearchDailyQuota)
            .where(
                ResearchDailyQuota.provider == PROVIDER,
                ResearchDailyQuota.local_day == day,
                ResearchDailyQuota.reserved < limit,
            )
            .values(reserved=ResearchDailyQuota.reserved + 1)
            .execution_options(synchronize_session=False)
        )
        await session.commit()
        return result.rowcount == 1


def _cached_outcome(row: ResearchCache) -> ResearchOutcome:
    return ResearchOutcome(
        configured=True,
        reason="cached",
        query=row.query,
        sources=_sources_from_json(row.sources),
        cached=True,
    )


async def _wait_for_cache(key: str) -> ResearchOutcome | None:
    """同 query 正在被别的 worker 拉取时,短暂轮询等成功缓存。**不出网、不占额度。**"""
    for _ in range(WAIT_ATTEMPTS):
        await asyncio.sleep(WAIT_DELAY_SECONDS)
        async with SessionLocal() as session:
            row = await _read_cache(session, key)
        if row is None:
            continue
        if row.status is ResearchCacheStatus.SUCCESS and row.expires_at and row.expires_at > utcnow():
            return _cached_outcome(row)
        if row.status is ResearchCacheStatus.FAILED:
            return ResearchOutcome(configured=False, reason="RESEARCH_FAILED", query=row.query)
    return None


async def search(db: AsyncSession, query: str) -> ResearchOutcome:
    """执行一次受控研究。**未配置/失败/超额时 `sources` 为空。**

    `db` 参数保留是为了调用方签名稳定;缓存与额度必须用自己的短事务并**立即提交**,
    否则其他 worker 看不到租约与预留。
    """
    ready, reason = provider_ready()
    if not ready:
        return ResearchOutcome(configured=False, reason=reason, query=query)

    normalized = normalize_query(query)
    key = cache_key_for(PROVIDER, normalized)
    now = utcnow()

    # 1) 读缓存(**短事务,读完就关** —— 不拿着旧快照去抢租约/写入)。
    async with SessionLocal() as session:
        row = await _read_cache(session, key)
    if row is not None and row.status is ResearchCacheStatus.SUCCESS and row.expires_at and row.expires_at > now:
        return _cached_outcome(row)
    if (
        row is not None
        and row.status is ResearchCacheStatus.FETCHING
        and row.lease_expires_at is not None
        and row.lease_expires_at > now
    ):
        waited = await _wait_for_cache(key)
        return waited or ResearchOutcome(configured=False, reason="IN_PROGRESS", query=query)

    # 2) 用**新事务**抢租约(只允许 failed / 过期 success / 过期 fetching)。
    async with SessionLocal() as session:
        if not await _acquire_lease(session, key=key, query=normalized, now=utcnow()):
            # 抢租约失败:先重读当前缓存。若已经是新鲜 success(别的 worker 刚写完),
            # 直接返回缓存 —— **绝不立即再次出网**。
            async with SessionLocal() as fresh:
                latest = await _read_cache(fresh, key)
            if (
                latest is not None
                and latest.status is ResearchCacheStatus.SUCCESS
                and latest.expires_at
                and latest.expires_at > utcnow()
            ):
                return _cached_outcome(latest)
            waited = await _wait_for_cache(key)
            return waited or ResearchOutcome(configured=False, reason="IN_PROGRESS", query=query)

        if not await _reserve_quota(session, settings.research_max_calls_per_day):
            await _release_lease(session, key)
            return ResearchOutcome(configured=False, reason="DAILY_LIMIT_REACHED", query=query)

        # ---- 从这里起可能真的产生费用;后续任何失败都不退回额度。----
        try:
            raw = await _fetch_raw(query)
        except httpx.TimeoutException as exc:
            logger.warning("公开研究调用超时: %s", type(exc).__name__)
            await _mark_failed(session, key)
            return ResearchOutcome(
                configured=False, reason="RESEARCH_FAILED", query=query, error_kind="timeout"
            )
        except Exception as exc:
            logger.warning("公开研究调用失败: %s", type(exc).__name__)
            await _mark_failed(session, key)
            return ResearchOutcome(
                configured=False, reason="RESEARCH_FAILED", query=query, error_kind="error"
            )

        sources = _to_sources(raw)
        if not sources:
            await _mark_failed(session, key)
            return ResearchOutcome(configured=False, reason="NO_RESULTS", query=query)

        await _mark_success(session, key, sources)
        return ResearchOutcome(configured=True, reason="ok", query=query, sources=sources)


def sources_to_summary(outcome: ResearchOutcome) -> dict:
    return {
        "configured": outcome.configured,
        "cached": outcome.cached,
        "query": outcome.query,
        "sources": [
            {
                "title": source.title,
                "url": source.url,
                "accessedAt": source.accessed_at,
                "excerpt": source.excerpt,
            }
            for source in outcome.sources
        ],
    }


# ---------------------------------------------------------------------------------
# 引用契约:给前端看的、**服务端验证过**的公开来源。
#
# 与 `sources_to_summary` 的区别是对象不同:那个进模型的提示词(可能被截断),
# 这个进消息历史与响应。引用只能从**真实 provider 结果**里来 —— 模型无法写、
# 无法改,也不包含搜索关键词本身。
# ---------------------------------------------------------------------------------

#: 没有来源时,按原因归到契约允许的 status。**不把"未配置"说成"查过"。**
_UNAVAILABLE_REASONS = frozenset(
    {
        "RESEARCH_ENABLED_FALSE",
        "RESEARCH_PROVIDER_NONE",
        "RESEARCH_PROVIDER_UNSUPPORTED",
        "TAVILY_API_KEY_MISSING",
        "RESEARCH_LEASE_INVALID",
        "RESEARCH_MOCK_DISABLED",
        "RESEARCH_MOCK_IN_PRODUCTION",
    }
)


def source_id_for(url: str) -> str:
    """一条来源的稳定 id。**由服务端从 URL 派生,模型拿不到也无法伪造。**"""
    return blake2b(f"{PROVIDER}|{url}".encode(), digest_size=8).hexdigest()


def _citation_payload(source: ResearchSource) -> dict:
    return {
        "sourceId": source_id_for(source.url),
        "title": source.title,
        "url": source.url,
        "domain": _hostname_of(source.url) or "",
        "excerpt": source.excerpt or None,
        # 当前 provider 的 basic 深度不返回发布日期;契约允许它为 null。
        "publishedAt": None,
        "accessedAt": source.accessed_at,
        "provider": PROVIDER,
    }


def citations_for(outcome: ResearchOutcome) -> list[dict]:
    """把来源转成前端可点击的引用。**只反映真实 provider 结果。**"""
    return [_citation_payload(source) for source in outcome.sources]


def outcome_status(outcome: ResearchOutcome) -> str:
    """一次研究结果对应的契约 status(闭集,见 contracts/conversation.py)。"""
    if outcome.sources:
        return "cached" if outcome.cached else "success"
    if outcome.reason == "DAILY_LIMIT_REACHED":
        return "limited"
    if outcome.reason in _UNAVAILABLE_REASONS:
        return "unavailable"
    if outcome.error_kind == "timeout":
        return "timeout"
    return "failed"


def research_record(outcome: ResearchOutcome) -> dict:
    """把一次研究结果收敛成**只含服务端验证事实**的引用记录。

    刻意**不带 query** —— 搜索关键词既不该回到提示词,也不该出现在历史响应里。
    """
    return {
        "status": outcome_status(outcome),
        "consulted": True,
        "citations": citations_for(outcome),
    }


def uncited_record(status: str) -> dict:
    """没有 provider 结果时的记录:隐私拦截 / 超每轮上限 / 工具错误。"""
    return {"status": status, "consulted": True, "citations": []}


#: 给调用方(agent_tools)用的诚实文案。**绝不含密钥。**
REASON_NOTE: dict[str, str] = {
    "RESEARCH_ENABLED_FALSE": "公开研究未配置(总开关 RESEARCH_ENABLED=false);我没有联网查过。",
    "RESEARCH_PROVIDER_NONE": "公开研究工具未配置(RESEARCH_PROVIDER=none);我没有联网查过。",
    "RESEARCH_PROVIDER_UNSUPPORTED": "配置的研究提供方本版本没有适配器;我没有联网查过。",
    "TAVILY_API_KEY_MISSING": "公开研究缺少 API Key;我没有联网查过。",
    "DAILY_LIMIT_REACHED": "今天的公开研究额度已经用完;我没有联网查过。",
    "RESEARCH_FAILED": "公开研究调用失败或超时;我没有拿到来源,不要编造。",
    "NO_RESULTS": "这次公开研究没有返回可用来源;不要编造。",
    "IN_PROGRESS": "同一个公开研究正在进行中;我没有重复联网,请稍后再看。",
    "RESEARCH_LEASE_INVALID": "公开研究的租约时长配置不安全(必须大于单次超时并留缓冲);我没有联网查过。",
    "RESEARCH_MOCK_DISABLED": "公开研究的 mock provider 没有显式打开(RESEARCH_MOCK_ENABLED=false);我没有联网查过。",
    "RESEARCH_MOCK_IN_PRODUCTION": "生产环境不允许使用 mock 研究 provider;我没有联网查过。",
}


__all__ = [
    "MAX_EXCERPT_CHARS",
    "MOCK_PROVIDER",
    "PROVIDER",
    "REASON_NOTE",
    "TAVILY_ENDPOINT",
    "ResearchOutcome",
    "ResearchSource",
    "cache_key_for",
    "citations_for",
    "clear_cache",
    "domain_allowed",
    "lease_config_error",
    "local_day",
    "normalize_query",
    "outcome_status",
    "provider_ready",
    "research_record",
    "search",
    "source_id_for",
    "sources_to_summary",
    "uncited_record",
]
