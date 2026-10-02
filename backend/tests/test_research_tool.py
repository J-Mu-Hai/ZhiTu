"""受控真实公开研究(Tavily)的配置、限额、失败降级与来源治理。

**所有测试都用 mock provider**,绝不发真实网络请求(conftest 的 autouse 守卫也会拦真实
HTTP)。密钥用假值,并断言它不出现在任何返回/记录里。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from backend.agent.runtime.base import ReasoningResult, ToolRequest
from backend.core.config import settings
from backend.db.base import utcnow
from backend.db.models import ResearchCache, ToolCallRecord
from backend.db.models.enums import ModelSource, ResearchCacheStatus, ToolCallStatus
from backend.db.session import SessionLocal
from backend.services import agent_tools, research_service

FAKE_KEY = "tvly-this-is-a-fake-key-123456"


@pytest.fixture(autouse=True)
def _clear_research_cache():
    research_service.clear_cache()
    yield
    research_service.clear_cache()


def _enable(monkeypatch: pytest.MonkeyPatch, **overrides: object) -> None:
    monkeypatch.setattr(settings, "research_enabled", True)
    monkeypatch.setattr(settings, "research_provider", "tavily")
    monkeypatch.setattr(settings, "tavily_api_key", FAKE_KEY)
    monkeypatch.setattr(settings, "research_max_results", 3)
    for key, value in overrides.items():
        monkeypatch.setattr(settings, key, value)


def _mock_search(monkeypatch: pytest.MonkeyPatch, results: list[dict], calls: list[str] | None = None):
    async def _fake(query: str) -> list[dict]:
        if calls is not None:
            calls.append(query)
        return results

    monkeypatch.setattr(research_service, "_fetch_tavily", _fake)


# ---------------------------------------------------------------------------------
# 配置:默认不发请求
# ---------------------------------------------------------------------------------
async def test_default_does_not_research_or_call_provider(db, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    _mock_search(monkeypatch, [{"title": "x", "url": "https://x.test", "content": "y"}], calls)
    outcome = await research_service.search(db, "某大学保研政策")
    assert outcome.configured is False
    assert outcome.sources == ()
    assert calls == [], "默认配置下绝不应该发请求"
    assert outcome.reason == "RESEARCH_ENABLED_FALSE"


async def test_missing_key_is_honest_not_fabricated(db, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "research_enabled", True)
    monkeypatch.setattr(settings, "research_provider", "tavily")
    monkeypatch.setattr(settings, "tavily_api_key", "")
    outcome = await research_service.search(db, "某比赛 报名时间")
    assert outcome.configured is False
    assert outcome.reason == "TAVILY_API_KEY_MISSING"
    assert "未" in research_service.REASON_NOTE[outcome.reason] or "没有" in research_service.REASON_NOTE[outcome.reason]


# ---------------------------------------------------------------------------------
# 真实检索(mock provider)与来源治理
# ---------------------------------------------------------------------------------
async def test_configured_search_returns_sources_with_provenance(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable(monkeypatch)
    _mock_search(
        monkeypatch,
        [
            {"title": "官方通知", "url": "https://example.edu.cn/notice", "content": "报名截止 2026-03-15。"},
            {"title": "无标题", "url": "", "content": "缺 url 的应被丢掉"},
        ],
    )
    outcome = await research_service.search(db, "  某大学   保研政策  ")
    assert outcome.configured is True
    assert outcome.reason == "ok"
    assert len(outcome.sources) == 1
    source = outcome.sources[0]
    assert source.title == "官方通知"
    assert source.url == "https://example.edu.cn/notice"
    assert source.accessed_at  # 访问时间
    assert "2026-03-15" in source.excerpt
    # 规范化后的查询作为缓存键。
    assert outcome.query == "  某大学   保研政策  "


async def test_same_query_is_cached_and_does_not_call_again(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable(monkeypatch)
    calls: list[str] = []
    _mock_search(monkeypatch, [{"title": "t", "url": "https://a.test", "content": "c"}], calls)
    first = await research_service.search(db, "公开课程 报名")
    second = await research_service.search(db, "  公开课程   报名 ")
    assert first.cached is False
    assert second.cached is True
    assert len(calls) == 1, "相同规范化查询第二次应命中缓存,不再发请求"


async def test_domain_whitelist_filters_sources(db, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch, research_allowed_domains="edu.cn")
    _mock_search(
        monkeypatch,
        [
            {"title": "学校", "url": "https://x.edu.cn/a", "content": "ok"},
            {"title": "别处", "url": "https://spam.test/b", "content": "no"},
        ],
    )
    outcome = await research_service.search(db, "政策")
    assert [s.url for s in outcome.sources] == ["https://x.edu.cn/a"]


# ---------------------------------------------------------------------------------
# 失败 / 限额:必须明确“没有完成”
# ---------------------------------------------------------------------------------
async def test_provider_failure_is_reported_not_fabricated(db, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)

    async def _boom(query: str) -> list[dict]:
        raise httpx.TimeoutException("timeout")

    monkeypatch.setattr(research_service, "_fetch_tavily", _boom)
    outcome = await research_service.search(db, "公开岗位要求")
    assert outcome.configured is False
    assert outcome.sources == ()
    assert outcome.reason == "RESEARCH_FAILED"
    assert "失败" in research_service.REASON_NOTE[outcome.reason]


async def test_daily_limit_blocks_before_calling(db, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch, research_max_calls_per_day=1)
    calls: list[str] = []
    _mock_search(monkeypatch, [{"title": "t", "url": "https://a.test", "content": "c"}], calls)

    first = await research_service.search(db, "第一政策")
    assert first.configured is True
    assert len(calls) == 1
    second = await research_service.search(db, "第二政策")
    assert second.configured is False
    assert second.reason == "DAILY_LIMIT_REACHED"
    assert len(calls) == 1, "额度用完后不允许再发请求"


async def test_real_attempt_consumes_quota_even_on_failure(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable(monkeypatch, research_max_calls_per_day=1)

    async def _boom(query: str) -> list[dict]:
        raise httpx.TimeoutException("timeout")

    monkeypatch.setattr(research_service, "_fetch_tavily", _boom)
    failed = await research_service.search(db, "会超时的查询")
    assert failed.reason == "RESEARCH_FAILED"
    _mock_search(monkeypatch, [{"title": "t", "url": "https://a.test", "content": "c"}])
    blocked = await research_service.search(db, "另一个查询")
    assert blocked.reason == "DAILY_LIMIT_REACHED"


async def test_cache_hit_does_not_consume_quota(db, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch, research_max_calls_per_day=1)
    calls: list[str] = []
    _mock_search(monkeypatch, [{"title": "t", "url": "https://a.test", "content": "c"}], calls)
    await research_service.search(db, "同一个查询")
    cached = await research_service.search(db, "同一个查询")
    assert cached.cached is True
    other = await research_service.search(db, "换个查询")
    assert other.reason == "DAILY_LIMIT_REACHED"
    assert len(calls) == 1


async def test_cache_is_cross_session(db, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    calls: list[str] = []
    _mock_search(monkeypatch, [{"title": "t", "url": "https://a.test", "content": "c"}], calls)
    await research_service.search(db, "跨会话查询")
    async with SessionLocal() as other_session:
        outcome = await research_service.search(other_session, "跨会话查询")
    assert outcome.cached is True
    assert len(calls) == 1


async def test_same_query_concurrent_only_one_fetch(db, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    calls: list[str] = []

    async def _slow(query: str) -> list[dict]:
        calls.append(query)
        await asyncio.sleep(0.3)
        return [{"title": "t", "url": "https://a.test", "content": "c"}]

    monkeypatch.setattr(research_service, "_fetch_tavily", _slow)
    results = await asyncio.gather(
        research_service.search(db, "并发查询"),
        research_service.search(db, "并发查询"),
    )
    assert len(calls) == 1, "同一 query 并发只允许一次真实出网"
    assert any(result.sources for result in results)


async def test_valid_lease_blocks_second_request_from_fetching(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable(monkeypatch)
    calls: list[str] = []
    _mock_search(monkeypatch, [{"title": "t", "url": "https://a.test", "content": "c"}], calls)
    key = research_service.cache_key_for("tavily", research_service.normalize_query("租约查询"))
    async with SessionLocal() as session:
        session.add(
            ResearchCache(
                cache_key=key,
                provider="tavily",
                query="租约查询",
                status=ResearchCacheStatus.FETCHING,
                sources=[],
                lease_expires_at=utcnow() + timedelta(seconds=60),
            )
        )
        await session.commit()

    outcome = await research_service.search(db, "租约查询")
    assert outcome.reason == "IN_PROGRESS"
    assert calls == [], "租约有效期间第二个请求不得出网"


async def test_expired_lease_can_be_taken_over(db, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    calls: list[str] = []
    _mock_search(monkeypatch, [{"title": "t", "url": "https://a.test", "content": "c"}], calls)
    key = research_service.cache_key_for("tavily", research_service.normalize_query("过期租约"))
    async with SessionLocal() as session:
        session.add(
            ResearchCache(
                cache_key=key,
                provider="tavily",
                query="过期租约",
                status=ResearchCacheStatus.FETCHING,
                sources=[],
                lease_expires_at=utcnow() - timedelta(seconds=60),
            )
        )
        await session.commit()

    outcome = await research_service.search(db, "过期租约")
    assert outcome.configured is True
    assert len(calls) == 1


async def test_concurrent_daily_quota_never_exceeds_limit(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable(monkeypatch, research_max_calls_per_day=3)
    calls: list[str] = []
    _mock_search(monkeypatch, [{"title": "t", "url": "https://a.test", "content": "c"}], calls)

    results = await asyncio.gather(
        *[research_service.search(db, f"并发额度查询{i}") for i in range(6)]
    )
    allowed = [r for r in results if r.configured and r.sources]
    limited = [r for r in results if r.reason == "DAILY_LIMIT_REACHED"]
    assert len(allowed) == 3, f"并发下应恰好放行 3 次,实际 {len(allowed)}"
    assert len(limited) == 3
    assert len(calls) == 3, "被拒的请求不得出网"


async def test_query_with_private_marker_is_rejected() -> None:
    with pytest.raises(agent_tools.ToolRejected):
        agent_tools._sanitize_research_query("0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b")
    with pytest.raises(agent_tools.ToolRejected):
        agent_tools._sanitize_research_query("token sk-abcdefghijklmnop")


def test_pii_and_private_targets_are_rejected_before_any_request() -> None:
    for bad in (
        "联系我 alice@example.com",
        "手机号 13812345678",
        "身份证 11010119900307123X",
        "卡号 6222020200112233445",
        "内网 192.168.1.10 服务怎么用",
        "http://localhost:8000/private",
        "company.internal 的接口",
    ):
        with pytest.raises(agent_tools.ToolRejected):
            agent_tools._sanitize_research_query(bad)
    # 多行 = 整段对话,不是关键词。
    with pytest.raises(agent_tools.ToolRejected):
        agent_tools._sanitize_research_query("第一行\n第二行")


def test_domain_allowlist_is_boundary_safe() -> None:
    assert research_service.domain_allowed("gov.cn", ("gov.cn",)) is True
    assert research_service.domain_allowed("www.gov.cn", ("gov.cn",)) is True
    assert research_service.domain_allowed("gov.cn.evil.example", ("gov.cn",)) is False
    assert research_service.domain_allowed("evil-gov.cn", ("gov.cn",)) is False
    assert research_service.domain_allowed("anything.test", ()) is True
    assert research_service._hostname_of("https://www.gov.cn:8443/a?q=1") == "www.gov.cn"


async def test_allowlisted_domain_cannot_be_bypassed(db, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch, research_allowed_domains="gov.cn")
    _mock_search(
        monkeypatch,
        [
            {"title": "恶意", "url": "https://gov.cn.evil.example/x", "content": "no"},
            {"title": "合法", "url": "https://www.gov.cn/x", "content": "ok"},
        ],
    )
    outcome = await research_service.search(db, "公开政策")
    assert [s.url for s in outcome.sources] == ["https://www.gov.cn/x"]


# ---------------------------------------------------------------------------------
# 通过工具循环:每轮一次 + 密钥不外泄 + 记录可追踪
# ---------------------------------------------------------------------------------
@dataclass
class SequenceReasoner:
    results: tuple[ReasoningResult, ...]
    calls: list = field(default_factory=list)
    _index: int = 0

    async def reason(self, turn):
        self.calls.append(turn)
        result = self.results[self._index] if self._index < len(self.results) else ReasoningResult(reply="没轮次了。", source=ModelSource.DIRECT_LLM)
        self._index += 1
        return result


async def _send(client: httpx.AsyncClient, account, content: str) -> dict:
    response = await client.post(
        f"/api/workspaces/{account.workspace_id}/messages",
        json={"content": content, "clientMessageId": content},
        headers=account.headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_only_one_real_research_per_turn_and_key_never_leaks(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable(monkeypatch, research_max_calls_per_turn=1)
    calls: list[str] = []
    _mock_search(
        monkeypatch,
        [{"title": "来源", "url": "https://example.edu.cn/policy", "content": "公开内容"}],
        calls,
    )
    account = await make_account()
    reasoner = SequenceReasoner(
        results=(
            ReasoningResult(
                reply="我查两次公开信息。",
                source=ModelSource.DIRECT_LLM,
                tool_requests=(
                    ToolRequest(id="t1", name="research_public", arguments={"query": "第一次查询"}),
                    ToolRequest(id="t2", name="research_public", arguments={"query": "第二次查询"}),
                ),
            ),
            ReasoningResult(reply="只允许一次,我说明一下。", source=ModelSource.DIRECT_LLM, stop_reason="insufficient_evidence"),
        )
    )
    use_reasoner(reasoner)
    await _send(app_client, account, "帮我查一下")

    exchanges = reasoner.calls[1].tool_exchanges
    assert len(exchanges) == 2
    assert exchanges[0].status == "ok"
    assert exchanges[0].summary.get("configured") is True
    assert exchanges[1].status == "rejected", "每轮只允许一次真实检索"
    assert len(calls) == 1, "第二次研究不允许真正发请求"

    # 密钥不出现在工具摘要、参数或工具记录里。
    assert FAKE_KEY not in str(exchanges)
    await db.rollback()
    records = list((await db.execute(select(ToolCallRecord))).scalars())
    assert records
    assert all(FAKE_KEY not in str(record.sanitized_arguments) for record in records)
    assert all(FAKE_KEY not in str(record.result_summary) for record in records)
    # 来源可追踪:成功那条记录里带 URL。
    ok_records = [r for r in records if r.status is ToolCallStatus.OK]
    assert ok_records and "example.edu.cn" in str(ok_records[0].result_summary)


# ---------------------------------------------------------------------------------
# 6B-1.1:新鲜 success 不被抢租约 / 配置安全 / 交错
# ---------------------------------------------------------------------------------
def test_insecure_lease_config_is_rejected() -> None:
    from backend.services import research_service as rs

    original_timeout = settings.research_timeout_seconds
    original_lease = settings.research_lease_seconds
    try:
        settings.research_timeout_seconds = 10
        settings.research_lease_seconds = 5
        assert rs.lease_config_error() == "lease_not_greater_than_timeout"
        settings.research_lease_seconds = 11
        assert rs.lease_config_error() == "lease_buffer_too_small"
        settings.research_lease_seconds = 12
        assert rs.lease_config_error() is None
    finally:
        settings.research_timeout_seconds = original_timeout
        settings.research_lease_seconds = original_lease


async def test_insecure_lease_config_does_not_go_online(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable(monkeypatch, research_timeout_seconds=10, research_lease_seconds=5)
    calls: list[str] = []
    _mock_search(monkeypatch, [{"title": "t", "url": "https://a.test", "content": "c"}], calls)
    outcome = await research_service.search(db, "配置不安全")
    assert outcome.configured is False
    assert outcome.reason == "RESEARCH_LEASE_INVALID"
    assert calls == []


async def test_fresh_success_cannot_be_stolen_by_lease(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable(monkeypatch)
    key = research_service.cache_key_for("tavily", research_service.normalize_query("新鲜缓存"))
    async with SessionLocal() as session:
        session.add(
            ResearchCache(
                cache_key=key,
                provider="tavily",
                query="新鲜缓存",
                status=ResearchCacheStatus.SUCCESS,
                sources=[{"title": "t", "url": "https://a.test", "excerpt": "e", "accessedAt": "now"}],
                expires_at=utcnow() + timedelta(hours=1),
            )
        )
        await session.commit()

    async with SessionLocal() as session:
        acquired = await research_service._acquire_lease(
            session, key=key, query="新鲜缓存", now=utcnow()
        )
    assert acquired is False, "新鲜 success 不能被抢租约"
    async with SessionLocal() as session:
        row = await session.scalar(select(ResearchCache).where(ResearchCache.cache_key == key))
    assert row is not None and row.status is ResearchCacheStatus.SUCCESS


async def test_expired_success_can_be_taken_over(db, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    calls: list[str] = []
    _mock_search(monkeypatch, [{"title": "t", "url": "https://a.test", "content": "c"}], calls)
    key = research_service.cache_key_for("tavily", research_service.normalize_query("过期缓存"))
    async with SessionLocal() as session:
        session.add(
            ResearchCache(
                cache_key=key,
                provider="tavily",
                query="过期缓存",
                status=ResearchCacheStatus.SUCCESS,
                sources=[],
                expires_at=utcnow() - timedelta(seconds=10),
            )
        )
        await session.commit()

    outcome = await research_service.search(db, "过期缓存")
    assert outcome.configured is True
    assert len(calls) == 1


async def test_failed_cache_can_be_retried(db, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    calls: list[str] = []
    _mock_search(monkeypatch, [{"title": "t", "url": "https://a.test", "content": "c"}], calls)
    key = research_service.cache_key_for("tavily", research_service.normalize_query("失败重试"))
    async with SessionLocal() as session:
        session.add(
            ResearchCache(
                cache_key=key,
                provider="tavily",
                query="失败重试",
                status=ResearchCacheStatus.FAILED,
                sources=[],
            )
        )
        await session.commit()

    outcome = await research_service.search(db, "失败重试")
    assert outcome.configured is True
    assert len(calls) == 1


async def test_interleaving_does_not_let_stale_reader_overwrite_fresh_cache(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 读到过期 success;B 在 A 抢租约前完成查询并写入新鲜 success;
    A 必须读到 B 的结果、不得再次出网、不得把 success 改成 fetching。"""
    _enable(monkeypatch)
    calls: list[str] = []
    _mock_search(monkeypatch, [{"title": "t", "url": "https://a.test", "content": "c"}], calls)
    key = research_service.cache_key_for("tavily", research_service.normalize_query("交错查询"))
    async with SessionLocal() as session:
        session.add(
            ResearchCache(
                cache_key=key,
                provider="tavily",
                query="交错查询",
                status=ResearchCacheStatus.SUCCESS,
                sources=[],
                expires_at=utcnow() - timedelta(seconds=10),
            )
        )
        await session.commit()

    real_acquire = research_service._acquire_lease
    state = {"n": 0}

    async def interleaving(session, *, key, query, now):
        state["n"] += 1
        if state["n"] == 1:
            # A 已读到过期缓存,尚未抢租约;B 此时完成一次真实查询。
            await research_service.search(db, "交错查询")
        return await real_acquire(session, key=key, query=query, now=now)

    monkeypatch.setattr(research_service, "_acquire_lease", interleaving)

    outcome_a = await research_service.search(db, "交错查询")
    assert outcome_a.cached is True, "A 应读到 B 写入的新鲜缓存"
    assert len(calls) == 1, "只允许 B 出网一次,A 不得重复调用 provider"

    async with SessionLocal() as session:
        row = await session.scalar(select(ResearchCache).where(ResearchCache.cache_key == key))
    assert row is not None
    assert row.status is ResearchCacheStatus.SUCCESS
    assert row.expires_at is not None and row.expires_at > utcnow(), "新鲜 success 必须保留"
