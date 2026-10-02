"""6B-2:公开研究来源契约与前端可见引用。

这里验证的是一条闭环:**模型请求 `research_public` -> 服务端真实执行/命中缓存 ->
回复响应与消息历史都带上服务端验证过的 citations**。引用永远不来自模型的文字,
失败时如实说\"没有拿到来源\",且搜索关键词与密钥都不外泄。

所有 provider 调用仍走 mock,绝不发真实网络请求(见 conftest 的 autouse 守卫)。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agent.runtime.base import ReasoningResult, ToolRequest
from backend.core.config import settings
from backend.db.models.enums import ModelSource
from backend.services import research_service

FAKE_KEY = "tvly-this-is-a-fake-key-123456"


def _enable(monkeypatch: pytest.MonkeyPatch, **overrides: object) -> None:
    monkeypatch.setattr(settings, "research_enabled", True)
    monkeypatch.setattr(settings, "research_provider", "tavily")
    monkeypatch.setattr(settings, "tavily_api_key", FAKE_KEY)
    monkeypatch.setattr(settings, "research_max_results", 3)
    for key, value in overrides.items():
        monkeypatch.setattr(settings, key, value)


def _mock_search(
    monkeypatch: pytest.MonkeyPatch, results: list[dict], calls: list[str] | None = None
) -> None:
    async def _fake(query: str) -> list[dict]:
        if calls is not None:
            calls.append(query)
        return results

    monkeypatch.setattr(research_service, "_fetch_tavily", _fake)


@dataclass
class SequenceReasoner:
    results: tuple[ReasoningResult, ...]
    calls: list = field(default_factory=list)
    _index: int = 0

    async def reason(self, turn):
        self.calls.append(turn)
        if self._index < len(self.results):
            result = self.results[self._index]
        else:
            result = ReasoningResult(reply="没轮次了。", source=ModelSource.DIRECT_LLM)
        self._index += 1
        return result


def _research_request(query: str) -> tuple[ToolRequest, ...]:
    return (ToolRequest(id="t1", name="research_public", arguments={"query": query}),)


async def _send(client: httpx.AsyncClient, account, content: str) -> dict:
    response = await client.post(
        f"/api/workspaces/{account.workspace_id}/messages",
        json={"content": content, "clientMessageId": content},
        headers=account.headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _history(client: httpx.AsyncClient, account) -> dict:
    response = await client.get(
        f"/api/workspaces/{account.workspace_id}/messages", headers=account.headers
    )
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------------
# 成功 / 缓存:可点击的服务端验证来源
# ---------------------------------------------------------------------------------
async def test_successful_research_surfaces_verified_citations(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    _mock_search(
        monkeypatch,
        [
            {
                "title": "官方通知",
                "url": "https://example.edu.cn/notice",
                "content": "报名截止 2026-03-15。",
            }
        ],
    )
    account = await make_account()
    reasoner = SequenceReasoner(
        results=(
            ReasoningResult(
                reply="我先查一下公开政策。",
                source=ModelSource.DIRECT_LLM,
                tool_requests=_research_request("某大学保研政策"),
            ),
            ReasoningResult(
                reply=(
                    "根据公开来源,报名截止是 2026-03-15。"
                    "(另外 https://model-invented.example/never 也是我编的,不该出现在来源里)"
                ),
                source=ModelSource.DIRECT_LLM,
                stop_reason="ready_to_propose",
            ),
        )
    )
    use_reasoner(reasoner)

    body = await _send(app_client, account, "帮我查一下某大学保研政策")

    research = body["assistantMessage"]["research"]
    assert research is not None
    assert research["status"] == "success"
    assert research["consulted"] is True
    assert len(research["citations"]) == 1
    citation = research["citations"][0]
    assert citation["title"] == "官方通知"
    assert citation["url"] == "https://example.edu.cn/notice"
    assert citation["domain"] == "example.edu.cn"
    assert citation["provider"] == "tavily"
    assert citation["sourceId"]
    assert citation["accessedAt"]
    # **模型自己编的 URL 进不了 citations** —— 引用只从真实 provider 结果来。
    assert "model-invented.example" not in str(research)
    assert "model-invented.example" in body["reply"]
    # 顶层与消息上的引用是同一份。
    assert body["research"] == research
    # 搜索关键词与密钥都不进引用记录。
    assert "某大学保研政策" not in str(research)
    assert FAKE_KEY not in str(body)

    # 刷新后仍然看得见 —— 来源已经落进消息历史。
    history = await _history(app_client, account)
    assistant = [m for m in history["messages"] if m["role"] == "assistant"][-1]
    assert assistant["research"]["status"] == "success"
    assert assistant["research"]["citations"][0]["url"] == citation["url"]


async def test_cached_research_is_marked_cached_and_does_not_go_online(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    calls: list[str] = []
    _mock_search(monkeypatch, [{"title": "t", "url": "https://a.test/x", "content": "c"}], calls)
    # 先真实查一次,把结果写进持久化缓存。
    first = await research_service.search(db, "公开课程报名")
    assert first.cached is False

    account = await make_account()
    reasoner = SequenceReasoner(
        results=(
            ReasoningResult(
                reply="查一下。",
                source=ModelSource.DIRECT_LLM,
                tool_requests=_research_request("公开课程报名"),
            ),
            ReasoningResult(reply="这是缓存的来源。", source=ModelSource.DIRECT_LLM),
        )
    )
    use_reasoner(reasoner)

    body = await _send(app_client, account, "帮我看看")

    research = body["assistantMessage"]["research"]
    assert research["status"] == "cached"
    assert research["citations"][0]["url"] == "https://a.test/x"
    assert len(calls) == 1, "命中缓存不允许再次出网"


async def test_no_research_means_research_is_null(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    account = await make_account()
    reasoner = SequenceReasoner(
        results=(ReasoningResult(reply="好的。", source=ModelSource.DIRECT_LLM),)
    )
    use_reasoner(reasoner)

    body = await _send(app_client, account, "你好")
    assert body["assistantMessage"]["research"] is None
    assert body["research"] is None


# ---------------------------------------------------------------------------------
# 未完成 / 被拦截 / 超限:如实说,绝不假装查过
# ---------------------------------------------------------------------------------
async def test_private_query_is_blocked_without_going_online(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    calls: list[str] = []
    _mock_search(monkeypatch, [{"title": "t", "url": "https://a.test", "content": "c"}], calls)
    account = await make_account()
    reasoner = SequenceReasoner(
        results=(
            ReasoningResult(
                reply="我查一下。",
                source=ModelSource.DIRECT_LLM,
                tool_requests=_research_request("联系我 alice@example.com"),
            ),
            ReasoningResult(
                reply="这个查询包含个人信息,我没有发出去。", source=ModelSource.DIRECT_LLM
            ),
        )
    )
    use_reasoner(reasoner)

    body = await _send(app_client, account, "帮我查")

    research = body["assistantMessage"]["research"]
    assert research["status"] == "blocked"
    assert research["consulted"] is True
    assert research["citations"] == []
    assert calls == [], "隐私查询绝不允许出网"


async def test_second_research_in_turn_is_limited_but_first_sources_are_kept(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch, research_max_calls_per_turn=1)
    calls: list[str] = []
    _mock_search(monkeypatch, [{"title": "s", "url": "https://a.test", "content": "c"}], calls)
    account = await make_account()
    reasoner = SequenceReasoner(
        results=(
            ReasoningResult(
                reply="查两次。",
                source=ModelSource.DIRECT_LLM,
                tool_requests=(
                    ToolRequest(id="t1", name="research_public", arguments={"query": "第一次"}),
                    ToolRequest(id="t2", name="research_public", arguments={"query": "第二次"}),
                ),
            ),
            ReasoningResult(reply="只查了一次。", source=ModelSource.DIRECT_LLM),
        )
    )
    use_reasoner(reasoner)

    body = await _send(app_client, account, "帮我查")

    research = body["assistantMessage"]["research"]
    assert research["status"] == "success", "已有来源不能被后续的限额记录遮掉"
    assert len(research["citations"]) == 1
    assert len(calls) == 1


async def test_provider_timeout_is_reported_as_timeout(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)

    async def _boom(query: str) -> list[dict]:
        raise httpx.TimeoutException("timeout")

    monkeypatch.setattr(research_service, "_fetch_tavily", _boom)
    account = await make_account()
    reasoner = SequenceReasoner(
        results=(
            ReasoningResult(
                reply="查一下。",
                source=ModelSource.DIRECT_LLM,
                tool_requests=_research_request("会超时的查询"),
            ),
            ReasoningResult(reply="超时了,我没拿到来源。", source=ModelSource.DIRECT_LLM),
        )
    )
    use_reasoner(reasoner)

    body = await _send(app_client, account, "帮我查")

    research = body["assistantMessage"]["research"]
    assert research["status"] == "timeout"
    assert research["citations"] == []


async def test_daily_limit_is_reported_as_limited(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch, research_max_calls_per_day=1)
    calls: list[str] = []
    _mock_search(monkeypatch, [{"title": "t", "url": "https://a.test", "content": "c"}], calls)
    # 先用掉今天唯一的额度(直接走服务层,不产生消息)。
    first = await research_service.search(db, "占额度查询")
    assert first.configured is True

    account = await make_account()
    reasoner = SequenceReasoner(
        results=(
            ReasoningResult(
                reply="查一下。",
                source=ModelSource.DIRECT_LLM,
                tool_requests=_research_request("另一个查询"),
            ),
            ReasoningResult(reply="额度用完了。", source=ModelSource.DIRECT_LLM),
        )
    )
    use_reasoner(reasoner)

    body = await _send(app_client, account, "帮我查")

    research = body["assistantMessage"]["research"]
    assert research["status"] == "limited"
    assert research["citations"] == []
    assert len(calls) == 1, "额度用完后不允许再出网"


async def test_provider_error_is_reported_as_failed(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)

    async def _boom(query: str) -> list[dict]:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(research_service, "_fetch_tavily", _boom)
    account = await make_account()
    reasoner = SequenceReasoner(
        results=(
            ReasoningResult(
                reply="查一下。",
                source=ModelSource.DIRECT_LLM,
                tool_requests=_research_request("会报错的查询"),
            ),
            ReasoningResult(reply="没查成,我如实说。", source=ModelSource.DIRECT_LLM),
        )
    )
    use_reasoner(reasoner)

    body = await _send(app_client, account, "帮我查")

    research = body["assistantMessage"]["research"]
    assert research["status"] == "failed"
    assert research["citations"] == []


async def test_unconfigured_research_is_reported_as_unavailable(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "research_enabled", False)
    monkeypatch.setattr(settings, "research_provider", "none")
    account = await make_account()
    reasoner = SequenceReasoner(
        results=(
            ReasoningResult(
                reply="查一下。",
                source=ModelSource.DIRECT_LLM,
                tool_requests=_research_request("某公开政策"),
            ),
            ReasoningResult(reply="我没联网查过。", source=ModelSource.DIRECT_LLM),
        )
    )
    use_reasoner(reasoner)

    body = await _send(app_client, account, "帮我查")

    research = body["assistantMessage"]["research"]
    assert research["status"] == "unavailable"
    assert research["citations"] == []
