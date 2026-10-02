"""6B-3:受测试环境控制的 mock 公开研究 provider。

生产只支持真实 Tavily。mock 有三道闸(`RESEARCH_PROVIDER=mock` +
`RESEARCH_MOCK_ENABLED=true` + `APP_ENV != production`),这里逐条验证它们真的拦得住,
并验证 mock 能稳定产出成功 / 缓存 / 超时 / 失败 / 空结果 —— 且**从不发真实网络请求**。
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.config import settings
from backend.services import research_service


def _use_mock(monkeypatch: pytest.MonkeyPatch, **overrides: object) -> None:
    monkeypatch.setattr(settings, "research_enabled", True)
    monkeypatch.setattr(settings, "research_provider", "mock")
    monkeypatch.setattr(settings, "research_mock_enabled", True)
    monkeypatch.setattr(settings, "app_env", "development")
    for key, value in overrides.items():
        monkeypatch.setattr(settings, key, value)


@pytest.fixture(autouse=True)
def _no_real_tavily(monkeypatch: pytest.MonkeyPatch) -> None:
    """证明 mock 路径不会碰真实 Tavily 适配器。"""

    async def _boom(query: str) -> list[dict]:
        raise AssertionError("mock provider 不允许调用真实 Tavily")

    monkeypatch.setattr(research_service, "_fetch_tavily", _boom)


def test_provider_ready_gates_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "research_enabled", True)
    monkeypatch.setattr(settings, "research_provider", "mock")
    monkeypatch.setattr(settings, "research_mock_enabled", False)
    monkeypatch.setattr(settings, "app_env", "development")
    assert research_service.provider_ready() == (False, "RESEARCH_MOCK_DISABLED")

    monkeypatch.setattr(settings, "research_mock_enabled", True)
    monkeypatch.setattr(settings, "app_env", "production")
    assert research_service.provider_ready() == (False, "RESEARCH_MOCK_IN_PRODUCTION")

    monkeypatch.setattr(settings, "app_env", "development")
    assert research_service.provider_ready() == (True, "ok")


async def test_mock_is_disabled_without_the_explicit_flag(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_mock(monkeypatch, research_mock_enabled=False)
    outcome = await research_service.search(db, "某公开政策")
    assert outcome.configured is False
    assert outcome.reason == "RESEARCH_MOCK_DISABLED"
    assert outcome.sources == ()


async def test_mock_is_refused_in_production(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_mock(monkeypatch, app_env="production")
    outcome = await research_service.search(db, "某公开政策")
    assert outcome.configured is False
    assert outcome.reason == "RESEARCH_MOCK_IN_PRODUCTION"
    assert outcome.sources == ()


async def test_mock_returns_sources_and_caches(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_mock(monkeypatch)
    first = await research_service.search(db, "公开政策测试")
    assert first.configured is True
    assert first.reason == "ok"
    assert len(first.sources) == 2
    assert first.sources[0].url.startswith("https://example.edu.cn/")
    assert first.cached is False

    second = await research_service.search(db, "公开政策测试")
    assert second.cached is True
    assert [s.url for s in second.sources] == [s.url for s in first.sources]


async def test_mock_can_simulate_timeout_failure_and_empty(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_mock(monkeypatch)
    timeout = await research_service.search(db, "某政策 __timeout__")
    assert timeout.reason == "RESEARCH_FAILED"
    assert timeout.error_kind == "timeout"
    assert research_service.outcome_status(timeout) == "timeout"

    failed = await research_service.search(db, "某政策 __fail__")
    assert failed.reason == "RESEARCH_FAILED"
    assert research_service.outcome_status(failed) == "failed"

    empty = await research_service.search(db, "某政策 __empty__")
    assert empty.reason == "NO_RESULTS"
    assert empty.sources == ()


async def test_mock_citations_are_server_derived_and_key_free(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_mock(monkeypatch)
    outcome = await research_service.search(db, "公开政策测试")
    record = research_service.research_record(outcome)
    assert record["status"] == "success"
    citation = record["citations"][0]
    assert citation["sourceId"]
    assert citation["domain"] == "example.edu.cn"
    assert citation["provider"] == "tavily"
    # 记录里不含搜索关键词。
    assert "公开政策测试" not in str(record)
