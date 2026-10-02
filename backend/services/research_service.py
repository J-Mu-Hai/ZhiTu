"""受控的真实公开研究(Tavily)。**默认不联网。**

## 它解决什么

有些未知不是用户私有的,而是可公开验证、会随时间变化的事实(学校政策、申请截止、
比赛规则、公开课程/岗位要求)。把这类问题直接问用户,是把本可以自己查清的负担推回去。
`research_public` 工具在**显式配置**后可以真实检索,并把来源作为“可确认的信息依据”
交给模型。

## 四条硬边界

1. **默认关闭。** `RESEARCH_ENABLED=false` 或 `RESEARCH_PROVIDER=none` 时**不发任何
   请求**,返回“未配置”。
2. **密钥不出门。** `TAVILY_API_KEY` 只用于请求头/体,绝不进返回摘要、工具记录、
   日志或界面。
3. **只读、不写业务数据。** 研究结果只进工具摘要(供模型判断与引用);要写图谱/排期/
   战略,仍然走 `proposal -> 用户确认`。
4. **失败如实说。** 超时、HTTP 错误、没有结果、额度用完都返回明确的“没有完成公开
   研究”,绝不编造来源或结论。

## 缓存与额度的边界(如实说明)

- 缓存是**进程内 TTL 缓存**(`_CACHE`)。当前服务单 worker,够用;多 worker 时各进程
  各有一份,`RESEARCH_MAX_CALLS_PER_DAY` 的统计也可能被放大。要跨进程,需要把缓存与
  计数换成数据库表 —— 本轮未做。
- 每日额度按 `ToolCallRecord` 里 `research_public` 成功调用数统计(全局,按
  `RESEARCH_TIMEZONE` 的自然日)。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.config import settings
from backend.db.base import utcnow
from backend.db.models import ToolCallRecord
from backend.db.models.enums import ToolCallStatus

logger = logging.getLogger(__name__)

TAVILY_ENDPOINT = "https://api.tavily.com/search"
#: 单条摘录的最多字符数。来源只需要“够判断”,不需要整页。
MAX_EXCERPT_CHARS = 400
MAX_TITLE_CHARS = 200
MAX_URL_CHARS = 500

#: 进程内 TTL 缓存:规范化查询 -> (过期时刻 monotonic, 来源)。
_CACHE: dict[str, tuple[float, tuple[ResearchSource, ...]]] = {}


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


def normalize_query(query: str) -> str:
    return " ".join(query.strip().lower().split())


def clear_cache() -> None:
    """测试用:清空进程内缓存。"""
    _CACHE.clear()


def provider_ready() -> tuple[bool, str]:
    """提供商是否可用。返回 (就绪, 不可用原因)。**原因里不含密钥。**"""
    if not settings.research_enabled:
        return False, "RESEARCH_ENABLED_FALSE"
    provider = (settings.research_provider or "none").strip().lower()
    if provider in ("", "none", "off", "disabled"):
        return False, "RESEARCH_PROVIDER_NONE"
    if provider != "tavily":
        return False, "RESEARCH_PROVIDER_UNSUPPORTED"
    if not (settings.tavily_api_key or "").strip():
        return False, "TAVILY_API_KEY_MISSING"
    return True, "ok"


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


def _allowed_domains() -> tuple[str, ...]:
    return tuple(
        part.strip().lower()
        for part in (settings.research_allowed_domains or "").split(",")
        if part.strip()
    )


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
        if allowed and not any(domain in url.lower() for domain in allowed):
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


async def _daily_calls(db: AsyncSession) -> int:
    """今天(按 `research_timezone` 的自然日)已经成功调用了几次真实研究。**全局。**"""
    try:
        tz = ZoneInfo(settings.research_timezone)
    except Exception:
        tz = ZoneInfo("Asia/Shanghai")
    now = utcnow().astimezone(tz)
    start_local = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = start_local.astimezone(UTC)
    used = await db.scalar(
        select(func.count())
        .select_from(ToolCallRecord)
        .where(
            ToolCallRecord.tool_name == "research_public",
            ToolCallRecord.status == ToolCallStatus.OK,
            ToolCallRecord.created_at >= start_utc,
        )
    )
    return int(used or 0)


async def search(db: AsyncSession, query: str) -> ResearchOutcome:
    """执行一次受控研究。**未配置/失败/超额时 `sources` 为空。**"""
    ready, reason = provider_ready()
    if not ready:
        return ResearchOutcome(configured=False, reason=reason, query=query)

    key = normalize_query(query)
    cached = _CACHE.get(key)
    now = time.monotonic()
    if cached is not None and cached[0] > now:
        return ResearchOutcome(configured=True, reason="cached", query=query, sources=cached[1], cached=True)

    if await _daily_calls(db) >= settings.research_max_calls_per_day:
        return ResearchOutcome(configured=False, reason="DAILY_LIMIT_REACHED", query=query)

    try:
        raw = await _fetch_tavily(query)
    except Exception as exc:
        logger.warning("公开研究调用失败: %s", type(exc).__name__)
        return ResearchOutcome(configured=False, reason="RESEARCH_FAILED", query=query)

    sources = _to_sources(raw)
    if not sources:
        return ResearchOutcome(configured=False, reason="NO_RESULTS", query=query)

    _CACHE[key] = (now + settings.research_cache_ttl_seconds, sources)
    return ResearchOutcome(configured=True, reason="ok", query=query, sources=sources)


#: 给调用方(agent_tools)用的诚实文案。**绝不含密钥。**
REASON_NOTE: dict[str, str] = {
    "RESEARCH_ENABLED_FALSE": "公开研究未配置(总开关 RESEARCH_ENABLED=false);我没有联网查过。",
    "RESEARCH_PROVIDER_NONE": "公开研究工具未配置(RESEARCH_PROVIDER=none);我没有联网查过。",
    "RESEARCH_PROVIDER_UNSUPPORTED": "配置的研究提供方本版本没有适配器;我没有联网查过。",
    "TAVILY_API_KEY_MISSING": "公开研究缺少 API Key;我没有联网查过。",
    "DAILY_LIMIT_REACHED": "今天的公开研究额度已经用完;我没有联网查过。",
    "RESEARCH_FAILED": "公开研究调用失败或超时;我没有拿到来源,不要编造。",
    "NO_RESULTS": "这次公开研究没有返回可用来源;不要编造。",
}


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


__all__ = [
    "MAX_EXCERPT_CHARS",
    "REASON_NOTE",
    "TAVILY_ENDPOINT",
    "ResearchOutcome",
    "ResearchSource",
    "clear_cache",
    "normalize_query",
    "provider_ready",
    "search",
    "sources_to_summary",
]
