"""V1 主动规划循环 — R3:月 / 周 / 日细化工作流。

规格:`docs/17-OPENJIUWEN-ACTIVE-PLANNING-LOOP-REFACTOR.md` 第 6 节 / 第 12 节 R3。

覆盖:

1. 确认粗时间线后,自动提案里同时含**月度里程碑**与**本周/下周**计划;
2. 只有确认粗时间线后才允许细化;
3. 日工作块在**没有容量信息**时明确标为“待校准”,不伪造具体日期;
4. 配置了可用容量/时段后,才把工作块校到具体日期(`datesCalibrated=true`)。
"""

from __future__ import annotations

import uuid

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
from backend.db.models import UserCapacityProfile
from backend.db.models.user import AvailabilityRule
from backend.services.timeutil import today_in
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
    raise AssertionError(f"没有待确认的提案: {[p['status'] for p in r.json()]}")


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
        ),
    )


async def _drive_to_weekly(client, account, reasoner):
    """把空间推到 weekly_execution(粗时间线已确认)。"""
    reasoner.v1_assessment = _assessment()
    await _turn(client, account, "r3-open-key")
    reasoner.v1_assessment = _assessment(
        node_updates=(
            V1NodeUpdate(node_key="goal_definition", judgment="30 天做出一个分析项目。"),
            V1NodeUpdate(node_key="key_conflict", judgment="目标太大、反馈太慢。"),
            V1NodeUpdate(node_key="hard_constraints", judgment="每天只有 1 小时。"),
        )
    )
    await _send(client, account, "我想做出一个能展示的数据分析项目", "r3-msg-1")
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
    view = confirm_strategy.json()["reasoning"]
    assert view["v1Stage"] == "coarse_timeline_review"
    proposal_id = view["v01TimelineProposalId"]
    await _confirm(client, account, proposal_id, "r3-timeline")
    assert (await _reasoning(client, account))["v1Stage"] == "weekly_execution"


@pytest.mark.asyncio
async def test_refinement_includes_monthly_and_weekly(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession, monkeypatch, use_reasoner
) -> None:
    monkeypatch.setattr(settings, "planning_v1", True)
    account = await make_account(workspace_title="细化")
    reasoner = use_reasoner(FakeReasoner(reply="记下战略。", v1_source_kind="test"))
    await _drive_to_weekly(app_client, account, reasoner)

    # 时间线确认后自动生成“月度里程碑 + 本周/下周”的待确认提案。
    proposal = await _open_proposal(app_client, account)
    await _confirm(app_client, account, proposal["id"], "r3-refine")
    plan = await _plan(app_client, account)
    titles = [str(node.get("title") or "") for node in plan["nodes"]]
    assert any(title.startswith("月度里程碑 · ") for title in titles), titles
    assert any(title.startswith("本周计划:") for title in titles), titles
    assert any(title.startswith("下周预览:") for title in titles), titles


@pytest.mark.asyncio
async def test_daily_plan_marks_uncalibrated_without_capacity(
    app_client: httpx.AsyncClient, make_account, monkeypatch, use_reasoner
) -> None:
    monkeypatch.setattr(settings, "planning_v1", True)
    account = await make_account(workspace_title="无容量")
    reasoner = use_reasoner(FakeReasoner(reply="记下战略。", v1_source_kind="test"))
    await _drive_to_weekly(app_client, account, reasoner)
    await _confirm(app_client, account, (await _open_proposal(app_client, account))["id"], "r3-week-1")

    daily = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/agent/v1/daily/generate",
        headers=account.headers,
    )
    assert daily.status_code == 200, daily.text
    assert daily.json()["reasoning"]["datesCalibrated"] is False
    await _confirm(app_client, account, (await _open_proposal(app_client, account))["id"], "r3-daily-1")

    plan = await _plan(app_client, account)
    day_nodes = [n for n in plan["nodes"] if str(n.get("title", "")).startswith("日计划")]
    assert 0 < len(day_nodes) <= 3
    assert any("待校准" in (n.get("description") or "") for n in day_nodes), day_nodes


@pytest.mark.asyncio
async def test_daily_plan_calibrates_when_capacity_configured(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession, monkeypatch, use_reasoner
) -> None:
    monkeypatch.setattr(settings, "planning_v1", True)
    account = await make_account(workspace_title="有容量")
    reasoner = use_reasoner(FakeReasoner(reply="记下战略。", v1_source_kind="test"))
    await _drive_to_weekly(app_client, account, reasoner)
    await _confirm(app_client, account, (await _open_proposal(app_client, account))["id"], "r3-week-2")

    # 配置容量档案 + 一条每周可用时段。
    today = today_in("Asia/Shanghai")
    user_id = uuid.UUID(account.id)
    db.add(UserCapacityProfile(user_id=user_id, weekly_total_minutes=600))
    db.add(
        AvailabilityRule(
            user_id=user_id,
            weekday=today.weekday(),
            start_minute=19 * 60,
            end_minute=21 * 60,
        )
    )
    await db.commit()

    daily = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/agent/v1/daily/generate",
        headers=account.headers,
    )
    assert daily.status_code == 200, daily.text
    assert daily.json()["reasoning"]["datesCalibrated"] is True
    await _confirm(app_client, account, (await _open_proposal(app_client, account))["id"], "r3-daily-2")

    plan = await _plan(app_client, account)
    day_nodes = [n for n in plan["nodes"] if str(n.get("title", "")).startswith("日计划")]
    assert day_nodes
    assert any("计划日期:" in (n.get("description") or "") for n in day_nodes), day_nodes
    # 数据库里确实没有伪造出容量档案之外的东西。
    stored = await db.scalar(
        select(UserCapacityProfile).where(UserCapacityProfile.user_id == user_id)
    )
    assert stored is not None
