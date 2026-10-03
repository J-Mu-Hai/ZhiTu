"""规划智能体重构 V1 — P2.3:消除 problem_structure 空转、自动推进战略路径。

回归来源:`agent-audit (2).json` —— `goal_definition_confirmed` 进入 problem_structure
后 `idle`、待回答问题 0、无战略草案、无 CTA;候选方向在 problem_structure 里被反复
切换却始终不推进。

覆盖:自动战略合成、responseMode=none 兜底 CTA、候选方向生命周期、审计事件。
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agent.runtime.base import (
    V1AssessmentDraft,
    V1CandidateDirection,
    V1KeyDimension,
    V1NodeUpdate,
)
from backend.core.config import settings
from backend.db.models import AgentAuditEvent
from backend.tests.conftest import FakeReasoner


async def _turn(client, account, key):
    r = await client.post(
        f"/api/workspaces/{account.workspace_id}/agent/turn",
        json={"trigger": "space_entered", "idempotencyKey": key},
        headers=account.headers,
    )
    assert r.status_code == 200, r.text
    return r.json()


async def _send(client, account, text, key):
    r = await client.post(
        f"/api/workspaces/{account.workspace_id}/messages",
        json={"content": text, "clientMessageId": key},
        headers=account.headers,
    )
    assert r.status_code == 200, r.text
    return r.json()


async def _reasoning(client, account):
    r = await client.get(
        f"/api/workspaces/{account.workspace_id}/reasoning", headers=account.headers
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


def _directions():
    return (
        V1CandidateDirection(key="small_tool", title="小工具", reason="成本低", path="脚本"),
        V1CandidateDirection(key="data_line", title="数据线", reason="成果清晰", path="数据 → 结论"),
    )


def _base_assessment(**overrides):
    base = {
        "strategic_thesis": "真正的问题是没有可验证的产出物。",
        "response_mode": "offer_options",
        "focus_key": "true_intent",
    }
    base.update(overrides)
    return V1AssessmentDraft(**base)


def _strategy_assessment():
    return V1AssessmentDraft(
        strategic_thesis="先走最小工具闭环,再决定是否深入数据方向。",
        strategy_tradeoff="先要一次可运行成果,而不是先补全语法。",
        strategy_ready=True,
        response_mode="ready_for_strategy",
        node_updates=(
            V1NodeUpdate(node_key="main_line", judgment="先用最小项目闭环补齐基础。"),
            V1NodeUpdate(node_key="parallel_line", judgment="并行看一点统计基础,不挤占主线。"),
            V1NodeUpdate(node_key="defer_or_avoid", judgment="暂不系统学算法与框架。"),
        ),
    )


async def _ready_for_goal(client, account, reasoner, key="p"):
    await _turn(client, account, f"{key}-open")
    reasoner.v1_assessment = _base_assessment(
        key_dimensions=(
            V1KeyDimension(key="major_risks", judgment="容易陷入只看不做。", why_it_matters="决定是否设检查点"),
        ),
        node_updates=(
            V1NodeUpdate(node_key="goal_definition", judgment="短周期内完成一个可运行的小工具。"),
            V1NodeUpdate(node_key="key_conflict", judgment="卡在缺少具体目标,而非能力不足。"),
            V1NodeUpdate(node_key="hard_constraints", judgment="每天只有 1 小时。"),
        ),
        candidate_directions=_directions(),
    )
    await _send(client, account, "我想学 Python,但不确定用来做什么", f"{key}-1")
    reasoner.v1_assessment = _base_assessment(
        strategic_thesis="你选择的是最低风险的起点。",
        response_mode="none",
        focus_key="goal_definition",
        node_updates=(V1NodeUpdate(node_key="goal_definition", judgment="完成一个小工具。"),),
    )
    selected = await client.post(
        f"/api/workspaces/{account.workspace_id}/agent/v1/direction/select?key=small_tool",
        headers=account.headers,
    )
    assert selected.status_code == 200, selected.text


@pytest.mark.asyncio
async def test_v1_goal_confirmed_auto_synthesizes_strategy(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession, monkeypatch, use_reasoner
) -> None:
    monkeypatch.setattr(settings, "planning_v1", True)
    account = await make_account(workspace_title="自动推进")
    reasoner = use_reasoner(FakeReasoner(reply="先给判断。"))
    await _ready_for_goal(app_client, account, reasoner)

    # 确认目标定义 -> 自动发起 problem_structure_entered,并形成战略草案。
    reasoner.v1_assessment = _strategy_assessment()
    confirm = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/agent/v1/goal/confirm",
        headers=account.headers,
    )
    assert confirm.status_code == 200, confirm.text
    view = confirm.json()["reasoning"]
    assert view["v1Stage"] == "strategy_draft", view.get("v1NextAction")
    assert view["v1Strategy"] and view["v1Strategy"]["mainLine"]
    assert view["v1NextAction"] is None, "战略已成形,不应再留兜底 CTA"
    assert view["v1ActualPendingQuestionCount"] == 0

    events = await _audit_events(db, account)
    types = [event.event_type for event in events]
    assert "problem_structure_entered" in types
    assert "problem_structure_synthesized" in types
    assert "strategy_draft_generated" in types
    assert "strategy_review_ready" in types
    # 没有“idle 无下一步”的死点:要么有战略草案,要么有显式 CTA。
    idle_dead = (
        view["v1Status"] == "idle"
        and view["v1ActualPendingQuestionCount"] == 0
        and not view["v1Strategy"]
        and not view["v1NextAction"]
    )
    assert not idle_dead


@pytest.mark.asyncio
async def test_v1_response_mode_none_leaves_explicit_cta(
    app_client: httpx.AsyncClient, make_account, monkeypatch, use_reasoner
) -> None:
    monkeypatch.setattr(settings, "planning_v1", True)
    account = await make_account(workspace_title="兜底 CTA")
    reasoner = use_reasoner(FakeReasoner(reply="先给判断。"))
    await _ready_for_goal(app_client, account, reasoner)

    # 模型返回 none 且没给战略:不允许静默 idle,必须给显式 CTA。
    reasoner.v1_assessment = _base_assessment(
        strategic_thesis="还需要把风险与杠杆想清楚。",
        response_mode="none",
    )
    confirm = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/agent/v1/goal/confirm",
        headers=account.headers,
    )
    view = confirm.json()["reasoning"]
    assert view["v1Stage"] == "problem_structure"
    assert view["v1NextAction"] == "continue_strategy", "不能停在无下一步的合法状态"

    # CTA 触发受控分析 -> 形成战略草案。
    reasoner.v1_assessment = _strategy_assessment()
    cont = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/agent/v1/strategy/continue",
        headers=account.headers,
    )
    assert cont.status_code == 200, cont.text
    after = cont.json()["reasoning"]
    assert after["v1Stage"] == "strategy_draft"
    assert after["v1Strategy"] and after["v1Strategy"]["mainLine"]
    assert after["v1NextAction"] is None


@pytest.mark.asyncio
async def test_v1_direction_lifecycle_after_goal_confirmation(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession, monkeypatch, use_reasoner
) -> None:
    monkeypatch.setattr(settings, "planning_v1", True)
    account = await make_account(workspace_title="候选生命周期")
    reasoner = use_reasoner(FakeReasoner(reply="先给判断。"))
    await _ready_for_goal(app_client, account, reasoner)
    reasoner.v1_assessment = _strategy_assessment()
    await app_client.post(
        f"/api/workspaces/{account.workspace_id}/agent/v1/goal/confirm",
        headers=account.headers,
    )

    # 目标确认后:候选不可直接切换,只返回守卫拒绝。
    blocked = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/agent/v1/direction/select?key=data_line",
        headers=account.headers,
    )
    assert blocked.status_code == 400, blocked.text
    events = await _audit_events(db, account)
    assert any(event.event_type == "guard_rejected" for event in events)
    assert (
        len([event for event in events if event.event_type == "candidate_direction_selected"]) == 1
    ), "确认后不得再产生候选选择事件"

    # 只有“重新选择起点”能回到候选方向。
    reopen = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/agent/v1/direction/reopen",
        headers=account.headers,
    )
    assert reopen.status_code == 200, reopen.text
    reopened = reopen.json()["reasoning"]
    assert reopened["v1Stage"] == "goal_reframe"
    assert reopened["v1SelectedDirection"] is None
    assert reopened["v1CanReselectDirection"] is True
    reselect = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/agent/v1/direction/select?key=data_line",
        headers=account.headers,
    )
    assert reselect.status_code == 200, reselect.text
    events = await _audit_events(db, account)
    assert any(event.event_type == "direction_reselection_started" for event in events)
