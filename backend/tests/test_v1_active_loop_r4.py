"""V1 主动规划循环 — R4:周回顾与未来重规划闭环。

规格:`docs/17-OPENJIUWEN-ACTIVE-PLANNING-LOOP-REFACTOR.md` 第 7 节 / 第 12 节 R4。

覆盖:

1. 周末首次进入空间**自动发起周回顾**,同一个周末不重复;
2. 完成率过低后,下一次进入空间**自动**准备未来重规划提案;
3. 重规划只调整未来:已完成历史不变,旧未完成周计划归档为可恢复历史;
4. 审计完整(`weekly_review_started` / `replan_proposal_created` / `replan_confirmed`)。
"""

from __future__ import annotations

import uuid
from datetime import date

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agent.runtime.base import (
    V1AssessmentDraft,
    V1NodeUpdate,
    V1TimelineDraft,
    V1TimelinePhaseDraft,
)
from backend.core.config import settings
from backend.db.models import AgentAuditEvent, PlanNode
from backend.tests.conftest import FakeReasoner


async def _turn(client, account, key):
    r = await client.post(
        f"/api/workspaces/{account.workspace_id}/agent/turn",
        json={"trigger": "space_entered", "idempotencyKey": key},
        headers=account.headers,
    )
    assert r.status_code == 200, r.text
    return r.json()["reasoning"]


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


async def _plan(client, account):
    r = await client.get(
        f"/api/workspaces/{account.workspace_id}/plan", headers=account.headers
    )
    assert r.status_code == 200, r.text
    return r.json()


async def _open_proposal(client, account):
    r = await client.get(
        f"/api/workspaces/{account.workspace_id}/proposals", headers=account.headers
    )
    assert r.status_code == 200, r.text
    for proposal in r.json():
        if proposal["status"] in {"validated", "pending_confirmation"}:
            return proposal
    raise AssertionError("没有待确认的提案")


async def _confirm(client, account, proposal_id, key):
    r = await client.post(
        f"/api/workspaces/{account.workspace_id}/proposals/{proposal_id}/confirm",
        json={"idempotencyKey": key},
        headers=account.headers,
    )
    assert r.status_code == 200, r.text


def _assessment(**overrides):
    base = {"strategic_thesis": "整体判断。", "question": ""}
    base.update(overrides)
    return V1AssessmentDraft(**base)


def _timeline_draft():
    return V1TimelineDraft(
        summary="基础 → 项目 → 展示",
        phases=(
            V1TimelinePhaseDraft(
                title="基础",
                goal="补齐最小能力",
                deliverable="跑通脚本",
                completion_criteria="可复现",
                start_week=1,
                end_week=2,
            ),
            V1TimelinePhaseDraft(
                title="项目",
                goal="做出能展示的分析",
                deliverable="一份报告",
                completion_criteria="结论可复述",
                start_week=3,
                end_week=4,
            ),
            V1TimelinePhaseDraft(
                title="展示",
                goal="整理并暴露缺口",
                deliverable="一次展示",
                completion_criteria="能讲清下一步",
                start_week=5,
                end_week=6,
            ),
        ),
    )


async def _drive_to_weekly(client, account, reasoner: FakeReasoner):
    reasoner.v1_assessment = _assessment()
    await _turn(client, account, "r4-open-key")
    reasoner.v1_assessment = _assessment(
        node_updates=(
            V1NodeUpdate(node_key="goal_definition", judgment="30 天做出一个分析项目。"),
            V1NodeUpdate(node_key="key_conflict", judgment="目标太大、反馈太慢。"),
            V1NodeUpdate(node_key="hard_constraints", judgment="每天只有 1 小时。"),
        )
    )
    await _send(client, account, "我想做出一个能展示的数据分析项目", "r4-msg-1")
    reasoner.v1_assessment = _assessment(
        strategy_ready=True,
        node_updates=(
            V1NodeUpdate(node_key="major_risks", judgment="容易只学不做。"),
            V1NodeUpdate(node_key="main_line", judgment="先用最小项目闭环。"),
            V1NodeUpdate(node_key="parallel_line", judgment="并行看一点统计。"),
        ),
        strategy_tradeoff="先要能展示的成果。",
    )
    confirm = await client.post(
        f"/api/workspaces/{account.workspace_id}/agent/v1/goal/confirm",
        headers=account.headers,
    )
    assert confirm.status_code == 200, confirm.text
    reasoner.v1_timeline = _timeline_draft()
    confirm_strategy = await client.post(
        f"/api/workspaces/{account.workspace_id}/agent/v1/strategy/confirm",
        headers=account.headers,
    )
    assert confirm_strategy.status_code == 200, confirm_strategy.text
    assert confirm_strategy.json()["reasoning"]["v1Stage"] == "timeline_alignment"
    align = await client.post(
        f"/api/workspaces/{account.workspace_id}/agent/v1/timeline/align",
        params={"accepted": "true"},
        headers=account.headers,
    )
    assert align.status_code == 200, align.text
    view = align.json()["reasoning"]
    await _confirm(client, account, view["v01TimelineProposalId"], "r4-timeline")
    assert (await _reasoning(client, account))["v1Stage"] == "weekly_execution"
    # 确认“月+周”细化提案,拿到活跃本周计划。
    await _confirm(client, account, (await _open_proposal(client, account))["id"], "r4-refine")


async def _audit_events(db, account):
    rows = await db.scalars(
        select(AgentAuditEvent)
        .where(AgentAuditEvent.workspace_id == uuid.UUID(account.workspace_id))
        .order_by(AgentAuditEvent.sequence.asc())
    )
    return list(rows)


@pytest.mark.asyncio
async def test_weekend_entry_auto_starts_review_once(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession, monkeypatch, use_reasoner
) -> None:
    import backend.services.v1_service as v1_service

    monkeypatch.setattr(settings, "planning_v1", True)
    # 钉在周六,让“周末自动回顾”这条规则可断言,而不是取决于跑测试的钟点。
    saturday = date(2026, 10, 3)
    assert saturday.weekday() == 5
    monkeypatch.setattr(v1_service, "today_in", lambda _tz: saturday)

    account = await make_account(workspace_title="周末回顾")
    reasoner = use_reasoner(FakeReasoner(reply="记下战略。", v1_source_kind="test"))
    await _drive_to_weekly(app_client, account, reasoner)

    # 周末进入空间:自动发起回顾并准备未来重规划草案。
    await _turn(app_client, account, "r4-weekend-1")
    proposal = await _open_proposal(app_client, account)
    assert proposal["status"] in {"validated", "pending_confirmation"}

    # 同一个周末再次进入:不重复发起。
    await _turn(app_client, account, "r4-weekend-2")
    events = await _audit_events(db, account)
    starts = [e for e in events if e.event_type == "weekly_review_started"]
    assert len(starts) == 1, "同一个周末只自动回顾一次"


@pytest.mark.asyncio
async def test_low_completion_auto_replans_future_only(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession, monkeypatch, use_reasoner
) -> None:
    import backend.services.v1_service as v1_service

    monkeypatch.setattr(settings, "planning_v1", True)
    wednesday = date(2026, 10, 7)
    assert wednesday.weekday() == 2
    monkeypatch.setattr(v1_service, "today_in", lambda _tz: wednesday)

    account = await make_account(workspace_title="重规划")
    reasoner = use_reasoner(FakeReasoner(reply="记下战略。", v1_source_kind="test"))
    await _drive_to_weekly(app_client, account, reasoner)

    plan = await _plan(app_client, account)
    week = next(n for n in plan["nodes"] if str(n.get("title", "")).startswith("本周计划"))
    children = [n for n in plan["nodes"] if n.get("parentId") == week["id"]]
    assert len(children) >= 3
    # 完成一个、其余未完成 -> 完成率低于 60%。
    done_task_id = children[0]["id"]
    await app_client.post(
        f"/api/workspaces/{account.workspace_id}/agent/v1/feedback",
        json={"nodeId": done_task_id, "outcome": "done"},
        headers=account.headers,
    )
    for child in children[1:]:
        response = await app_client.post(
            f"/api/workspaces/{account.workspace_id}/agent/v1/feedback",
            json={"nodeId": child["id"], "outcome": "missed"},
            headers=account.headers,
        )
        assert response.status_code == 200, response.text
    assert (await _reasoning(app_client, account))["v1Stage"] == "replanning"

    # 下一次进入空间:自动准备未来重规划提案。
    await _turn(app_client, account, "r4-replan-entry")
    proposal = await _open_proposal(app_client, account)
    await _confirm(app_client, account, proposal["id"], "r4-replan-confirm")

    after = await _reasoning(app_client, account)
    assert after["v1Stage"] == "weekly_execution"
    assert after["v01Timeline"] and all(item["status"] == "planned" for item in after["v01Timeline"])

    # 已完成的历史不变;旧未完成周计划归档为可恢复历史。
    completed = await db.get(PlanNode, uuid.UUID(done_task_id))
    assert completed is not None
    assert completed.status.value == "completed", "已完成任务不得被重规划改写"
    old_week = await db.get(PlanNode, uuid.UUID(week["id"]))
    assert old_week is not None
    assert old_week.status.value == "archived", "旧未完成周计划应归档而非删除"

    events = await _audit_events(db, account)
    types = {event.event_type for event in events}
    assert "replan_proposal_created" in types
    assert "replan_confirmed" in types
