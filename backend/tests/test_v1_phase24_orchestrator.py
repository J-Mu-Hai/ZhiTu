"""规划智能体重构 V1 — 工作流编排器(`advance_v1_workflow`)定向验收。

修复“进入空间只初始化、之后每一步都要手动端点”的零散状态机:

- `space_entered` 必须**实际启动**首轮整体判断(调用模型);
- 任何非终态阶段不得停在无解释的 idle;
- 运行中卡死 45s 后必须明确失败 + 重试,不能无限转圈;
- 审计记录每次自动推进与阻塞原因。

P3/P4 的确认→写入链路不在本轮改动范围(另有测试覆盖)。
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agent.runtime.base import V1AssessmentDraft, V1KeyDimension
from backend.core.config import settings
from backend.db.base import utcnow
from backend.db.models import AgentAuditEvent, GoalReasoningSession
from backend.tests.conftest import FakeReasoner


async def _turn(client, account, key):
    r = await client.post(
        f"/api/workspaces/{account.workspace_id}/agent/turn",
        json={"trigger": "space_entered", "idempotencyKey": key},
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



def _assessment(**overrides):
    base = {
        "strategic_thesis": "你还不知道要用它做什么,但真正的问题是没有可验证的产出。",
        "response_mode": "ask",
        "critical_question": "你更接近已有方向、只是不确定值不值得投入,还是方向也没想清楚?",
        "focus_key": "true_intent",
        "focus_reason": "它决定整条路线。",
        "key_dimensions": (
            V1KeyDimension(key="key_conflict", judgment="卡在缺少具体目标。", why_it_matters="决定下一步"),
        ),
    }
    base.update(overrides)
    return V1AssessmentDraft(**base)


@pytest.mark.asyncio
async def test_v1_space_entered_starts_the_judgment(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession, monkeypatch, use_reasoner
) -> None:
    monkeypatch.setattr(settings, "planning_v1", True)
    account = await make_account(workspace_title="我想学 Python")
    reasoner = use_reasoner(FakeReasoner(reply="先给整体判断。", v1_assessment=_assessment()))

    # 进入空间:不再只初始化,而是实际调用模型做首轮整体判断。
    body = await _turn(app_client, account, "wf-1")
    view = body["reasoning"]
    assert reasoner.calls, "space_entered 必须实际启动首轮整体判断"
    assert view["v1Stage"] == "goal_reframe"
    assert view["v1StrategicThesis"]
    assert view["v1Question"] == "你更接近已有方向、只是不确定值不值得投入,还是方向也没想清楚?"

    # 不是“无解释的 idle”死点。
    assert not (
        view["v1Status"] == "idle"
        and view["v1ActualPendingQuestionCount"] == 0
        and not view["v1Strategy"]
        and not view["v1NextAction"]
    )

    events = await _audit_events(db, account)
    advanced = [event for event in events if event.event_type == "v1_workflow_advanced"]
    assert advanced, "自动推进要留痕"
    assert advanced[0].stage_after == "goal_reframe"


@pytest.mark.asyncio
async def test_v1_running_step_times_out_to_failed(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession, monkeypatch, use_reasoner
) -> None:
    monkeypatch.setattr(settings, "planning_v1", True)
    account = await make_account(workspace_title="超时恢复")
    use_reasoner(FakeReasoner(reply="先给判断。", v1_assessment=_assessment()))
    await _turn(app_client, account, "wf-t1")

    # 模拟“运行中卡死”:status=running,且 90 秒没有推进。
    session = await db.scalar(
        select(GoalReasoningSession).where(
            GoalReasoningSession.workspace_id == uuid.UUID(account.workspace_id)
        )
    )
    assert session is not None
    session.v1_status = "running"
    session.updated_at = utcnow() - timedelta(seconds=90)
    await db.commit()

    body = await _turn(app_client, account, "wf-t2")
    view = body["reasoning"]
    assert view["v1Status"] == "failed", "卡死必须转成明确失败"
    assert "超时" in (view["v1Error"] or "")
    events = await _audit_events(db, account)
    timeout_events = [event for event in events if event.event_type == "v1_step_timed_out"]
    assert timeout_events
    assert timeout_events[0].validation_status == "failed"
    assert timeout_events[0].error_code == "V1_STEP_TIMEOUT"
