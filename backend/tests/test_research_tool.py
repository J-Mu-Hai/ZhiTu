"""受控真实公开研究(Tavily)的配置、限额、失败降级与来源治理。

**所有测试都用 mock provider**,绝不发真实网络请求(conftest 的 autouse 守卫也会拦真实
HTTP)。密钥用假值,并断言它不出现在任何返回/记录里。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx
import pytest
from sqlalchemy import select

from backend.agent.runtime.base import ReasoningResult, ToolRequest
from backend.core.config import settings
from backend.db.models import ToolCallRecord
from backend.db.models.enums import ModelSource, ToolCallStatus
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
    _enable(monkeypatch, research_max_calls_per_day=3)
    calls: list[str] = []
    _mock_search(monkeypatch, [{"title": "t", "url": "https://a.test", "content": "c"}], calls)

    async def _used(_db) -> int:
        return 3

    monkeypatch.setattr(research_service, "_daily_calls", _used)
    outcome = await research_service.search(db, "某政策")
    assert outcome.configured is False
    assert outcome.reason == "DAILY_LIMIT_REACHED"
    assert calls == [], "额度用完后不允许再发请求"


async def test_query_with_private_marker_is_rejected() -> None:
    with pytest.raises(agent_tools.ToolRejected):
        agent_tools._sanitize_research_query("0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b")
    with pytest.raises(agent_tools.ToolRejected):
        agent_tools._sanitize_research_query("token sk-abcdefghijklmnop")


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
