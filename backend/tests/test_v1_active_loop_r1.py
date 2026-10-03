"""V1 主动规划循环 — R1:OpenJiuwen 强制运行时与工作回合生命周期。

规格:`docs/17-OPENJIUWEN-ACTIVE-PLANNING-LOOP-REFACTOR.md` 第 3.2 / 4.1 / 9 / 12 节。

覆盖:

1. V1 必须真实使用 OpenJiuwen —— 来源不是 openjiuwen(例如 `AGENT_REASONER=auto`
   静默落到直连模型)时**准确失败 + 可重试**,绝不把直连结果当成规划成功;
2. 失败后 `retry` 能真正重跑(复用同阶段语义),而不是永久卡死;
3. 每个工作回合持久化 turn_id / deadline / source,结束后 deadline 清空;
4. 真实运行时超时(asyncio)转成可重试失败,审计写 `v1_step_timed_out`;
5. 进程崩溃留下的 `running` 在下次进入空间时按 deadline 恢复成可重试失败;
6. 弃用的 `advance(...)` 转发到唯一编排入口。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agent.runtime.base import ReasoningResult, V1AssessmentDraft, V1KeyDimension
from backend.core.config import settings
from backend.db.base import utcnow
from backend.db.models import AgentAuditEvent, GoalReasoningSession
from backend.db.models.enums import ModelSource
from backend.tests.conftest import FakeReasoner


async def _turn(client, account, key, trigger="space_entered"):
    r = await client.post(
        f"/api/workspaces/{account.workspace_id}/agent/turn",
        json={"trigger": trigger, "idempotencyKey": key},
        headers=account.headers,
    )
    assert r.status_code == 200, r.text
    return r.json()


async def _audit_events(db, account):
    rows = await db.scalars(
        select(AgentAuditEvent)
        .where(AgentAuditEvent.workspace_id == uuid.UUID(account.workspace_id))
        .order_by(AgentAuditEvent.sequence.asc())
    )
    return list(rows)


async def _session(db, account):
    return await db.scalar(
        select(GoalReasoningSession).where(
            GoalReasoningSession.workspace_id == uuid.UUID(account.workspace_id)
        )
    )


def _assessment(**overrides):
    base = {
        "strategic_thesis": "整体判断。",
        "focus_key": "true_intent",
        "key_dimensions": (
            V1KeyDimension(key="key_conflict", judgment="卡在目标不清。", why_it_matters="决定下一步"),
        ),
    }
    base.update(overrides)
    return V1AssessmentDraft(**base)


@pytest.mark.asyncio
async def test_v1_requires_openjiuwen_and_fails_accurately(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession, monkeypatch, use_reasoner
) -> None:
    """`AGENT_REASONER=auto` 落到直连模型时,V1 必须准确失败,不得静默降级。"""
    monkeypatch.setattr(settings, "planning_v1", True)
    monkeypatch.setattr(settings, "v1_require_openjiuwen", True)
    account = await make_account(workspace_title="我想学 Python")
    # 来源不是 openjiuwen:模拟 auto 静默落到 direct_llm。
    reasoner = use_reasoner(
        FakeReasoner(reply="直连模型的回答。", v1_assessment=_assessment(), v1_source_kind="direct_llm")
    )

    body = await _turn(app_client, account, "r1-source")
    view = body["reasoning"]
    assert view["v1Status"] == "failed", "来源不合规必须准确失败"
    assert "OpenJiuwen" in (view["v1Error"] or "")
    assert view["v1RequireOpenjiuwen"] is True
    # **没有当成规划成功**:没有战略、没有时间线、没有把直连结果写进去。
    assert not view["v1Strategy"]
    assert view["v01Timeline"] == []
    # 来源不合规时不重复调模型。
    assert len(reasoner.calls) <= 1

    events = await _audit_events(db, account)
    required = [e for e in events if e.event_type == "v1_openjiuwen_required"]
    assert required, "来源不合规要留审计"
    assert required[0].error_code == "V1_OPENJIUWEN_REQUIRED"
    assert required[0].source == "direct_llm"
    assert required[0].validation_status == "failed"


@pytest.mark.asyncio
async def test_v1_retry_reruns_after_failure(
    app_client: httpx.AsyncClient, make_account, monkeypatch, use_reasoner
) -> None:
    """失败后 `retry` 必须真正重跑同一阶段,而不是永久卡在失败态。"""
    monkeypatch.setattr(settings, "planning_v1", True)
    monkeypatch.setattr(settings, "v1_require_openjiuwen", True)
    account = await make_account(workspace_title="重试")
    # 第一轮模型没给出可用判断 -> 失败;之后换成可用判断 -> retry 必须真正重跑。
    reasoner = use_reasoner(
        FakeReasoner(reply="这次什么都没给出。", v1_assessment=None, v1_source_kind="test")
    )
    first = await _turn(app_client, account, "r1-retry-1")
    assert first["reasoning"]["v1Status"] == "failed"
    assert len(reasoner.calls) == 1

    reasoner.v1_assessment = _assessment()
    body = await _turn(app_client, account, "r1-retry-2", trigger="retry")
    assert body["reasoning"]["v1Status"] != "failed", "重试要能恢复"
    assert len(reasoner.calls) == 2, "重试要真正重跑模型"


@pytest.mark.asyncio
async def test_v1_turn_lifecycle_recorded(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession, monkeypatch, use_reasoner
) -> None:
    """回合结束后留下 turn_id / source,deadline 清空。"""
    monkeypatch.setattr(settings, "planning_v1", True)
    account = await make_account(workspace_title="生命周期")
    use_reasoner(FakeReasoner(reply="判断。", v1_assessment=_assessment(), v1_source_kind="test"))

    await _turn(app_client, account, "r1-life")
    session = await _session(db, account)
    assert session.v1_turn_id
    assert session.v1_turn_source == "test"
    assert session.v1_turn_started_at is not None
    assert session.v1_turn_deadline_at is None, "回合结束就不再“正在思考”"
    assert session.v1_status == "idle"


class _SlowReasoner:
    """真实运行时超时:永远比超时窗口慢。"""

    v1_source_kind = "test"

    def __init__(self, seconds: float) -> None:
        self._seconds = seconds
        self.calls = 0

    async def reason(self, turn):
        self.calls += 1
        await asyncio.sleep(self._seconds)
        return ReasoningResult(
            reply="太慢了。", source=ModelSource.DIRECT_LLM, request_id="slow", prompt_version="t"
        )


@pytest.mark.asyncio
async def test_v1_runtime_timeout_becomes_retryable(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession, monkeypatch, use_reasoner
) -> None:
    monkeypatch.setattr(settings, "planning_v1", True)
    monkeypatch.setattr(settings, "v1_agent_turn_timeout_seconds", 1)
    account = await make_account(workspace_title="超时")
    use_reasoner(_SlowReasoner(seconds=2))

    body = await _turn(app_client, account, "r1-runtime-timeout")
    view = body["reasoning"]
    assert view["v1Status"] == "failed"
    assert "秒" in (view["v1Error"] or "")

    events = await _audit_events(db, account)
    timeouts = [e for e in events if e.event_type == "v1_step_timed_out"]
    assert timeouts, "真实超时也要留痕"


@pytest.mark.asyncio
async def test_v1_deadline_recovery_after_restart(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession, monkeypatch, use_reasoner
) -> None:
    """崩溃留下的 running(deadline 已过)在下次进入空间时恢复成可重试失败。"""
    monkeypatch.setattr(settings, "planning_v1", True)
    account = await make_account(workspace_title="恢复")
    use_reasoner(FakeReasoner(reply="判断。", v1_assessment=_assessment(), v1_source_kind="test"))
    await _turn(app_client, account, "r1-recover-1")

    session = await _session(db, account)
    session.v1_status = "running"
    session.v1_turn_id = "deadbeef"
    session.v1_turn_source = "openjiuwen"
    session.v1_turn_stage = "goal_reframe"
    session.v1_turn_deadline_at = utcnow() - timedelta(seconds=5)
    await db.commit()

    body = await _turn(app_client, account, "r1-recover-2")
    assert body["reasoning"]["v1Status"] == "failed"
    assert "超时" in (body["reasoning"]["v1Error"] or "")

    events = await _audit_events(db, account)
    timeouts = [e for e in events if e.event_type == "v1_step_timed_out"]
    assert timeouts
    assert timeouts[-1].source == "openjiuwen"
    assert (timeouts[-1].payload_json or {}).get("turnId") == "deadbeef"


@pytest.mark.asyncio
async def test_deprecated_advance_delegates_to_orchestrator(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession, monkeypatch
) -> None:
    """弃用的 `advance(...)` 不再自己决定阶段,而是转发到唯一编排入口。"""
    monkeypatch.setattr(settings, "planning_v1", True)
    account = await make_account(workspace_title="兼容")

    from backend.core.security import CurrentUser
    from backend.db.session import SessionLocal
    from backend.services import reasoning_service
    from backend.services.context import load_workspace_context
    from backend.services.v1_service import V1_INITIAL_THINKING, advance

    async with SessionLocal() as other:
        user = CurrentUser(
            user_id=uuid.UUID(account.id),
            session_id=uuid.UUID(account.session_id),
            timezone="Asia/Shanghai",
            token_version=1,
        )
        ctx = await load_workspace_context(other, user, uuid.UUID(account.workspace_id))
        # 直接建会话,模拟进入空间。
        session = await reasoning_service.get_or_create_session(other, ctx)
        assert session is not None
        assert session.v1_stage == V1_INITIAL_THINKING
        result = await advance(other, ctx, None, session, trace=None)
        assert result is not None
        assert session.v1_stage == V1_INITIAL_THINKING
