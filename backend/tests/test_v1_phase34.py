"""规划智能体重构 V1 — P3:粗时间架构 → 详细时间线的定向验收。

固定案例:V1 空间把战略走到确认,再由模型给出 3–6 个阶段的粗时间架构,用户确认后
写入正式阶段节点。全程用 `FakeReasoner`,不联网。

覆盖:
1. 战略确认后生成**待确认**的粗时间架构草案(不写正式计划);
2. 用户确认提案后才写入阶段节点,并进入周/日执行状态;
3. 模型输出不合格时不写半成品、可重试;
4. P3 之前(战略未确认)拒绝生成时间架构。
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agent.runtime.base import (
    V1AssessmentDraft,
    V1NodeUpdate,
    V1StrategyDraft,
    V1TimelineDraft,
    V1TimelinePhaseDraft,
)
from backend.core.config import settings
from backend.db.models import PlanNode
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


async def _plan_node_count(db, account):
    return int(
        await db.scalar(
            select(func.count())
            .select_from(PlanNode)
            .where(PlanNode.workspace_id == uuid.UUID(account.workspace_id))
        )
        or 0
    )


def _assessment(**overrides):
    base = {"global_assessment": "整体判断。", "question": ""}
    base.update(overrides)
    return V1AssessmentDraft(**base)


async def _drive_to_strategy_confirmed(client, account, reasoner):
    """四维讨论完成并确认目标后，才形成可供确认的战略。"""
    reasoner.v1_assessment = _assessment()
    await _turn(client, account, "p3-open")
    # 某些守卫用例已先以“无模型判断”的默认 FakeReasoner 打开会话。那一轮不会
    # 产生待答问题；先用一条消息让服务端落下固定的第一维，再开始四项计数。
    if (await _reasoning(client, account))["v1ActualPendingQuestionCount"] == 0:
        await _send(client, account, "我开始梳理目标。", "p3-bootstrap")
    for index, (key, answer) in enumerate((
        ("true_intent", "我想做出一个能展示的数据分析项目。"),
        ("current_state", "我会一点 Python 基础，也能查资料。"),
        ("hard_constraints", "每天只有一小时，预计六周。"),
        ("goal_definition", "能独立演示并写进简历才算完成。"),
    ), start=1):
        reasoner.v1_assessment = _assessment(
            node_updates=(V1NodeUpdate(node_key=key, judgment=answer),),
        )
        await _send(client, account, answer, f"p3-{index}")
    reasoner.v1_strategy = V1StrategyDraft(
        main_line="先用最小项目闭环补齐 pandas。",
        parallel_line="并行看一点统计基础。",
        defer_or_avoid="暂不系统学算法。",
        risk_control="每两周复盘一次。",
        tradeoff="先要能展示的成果。",
    )
    confirmed = await client.post(
        f"/api/workspaces/{account.workspace_id}/agent/v1/goal/confirm",
        headers=account.headers,
    )
    assert confirmed.status_code == 200, confirmed.text


def _timeline_draft():
    return V1TimelineDraft(
        summary="基础闭环 → 最小项目 → 展示复盘",
        phases=(
            V1TimelinePhaseDraft(
                title="基础闭环",
                goal="补齐最小数据处理能力",
                deliverable="能跑通一个清洗脚本",
                completion_criteria="脚本可复现",
                start_week=1,
                end_week=2,
            ),
            V1TimelinePhaseDraft(
                title="最小分析项目",
                goal="做出一个能展示的分析",
                deliverable="一份分析报告",
                completion_criteria="结论可复述",
                start_week=3,
                end_week=4,
                depends_on="基础闭环",
            ),
            V1TimelinePhaseDraft(
                title="展示与复盘",
                goal="整理并暴露缺口",
                deliverable="一次展示",
                completion_criteria="能讲清下一步",
                start_week=5,
                end_week=6,
                depends_on="最小分析项目",
            ),
        ),
    )


@pytest.mark.asyncio
async def test_v1_coarse_timeline_requires_confirmation(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession, monkeypatch, use_reasoner
) -> None:
    monkeypatch.setattr(settings, "planning_v1", True)
    account = await make_account(workspace_title="我想学 Python")
    reasoner = use_reasoner(FakeReasoner(reply="先记下战略。"))
    await _drive_to_strategy_confirmed(app_client, account, reasoner)
    before = await _plan_node_count(db, account)
    assert before == 4, "分组是画布节点,还没有阶段"

    reasoner.v1_timeline = _timeline_draft()
    confirm = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/agent/v1/strategy/confirm",
        headers=account.headers,
    )
    assert confirm.status_code == 200, confirm.text
    assert confirm.json()["reasoning"]["v1Stage"] == "timeline_alignment"
    body = await _align_timeline(app_client, account)
    assert body["v1Stage"] == "coarse_timeline_review"
    assert len(body["v01Timeline"]) == 3
    assert all(item["status"] == "draft" for item in body["v01Timeline"])
    assert body["v01TimelineProposalId"], "应生成待确认提案"
    # 确认前不写正式阶段。
    assert await _plan_node_count(db, account) == before

    proposal_id = body["v01TimelineProposalId"]
    write = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/proposals/{proposal_id}/confirm",
        json={"idempotencyKey": "p3-confirm-timeline"},
        headers=account.headers,
    )
    assert write.status_code == 200, write.text
    after = await _reasoning(app_client, account)
    assert after["v1Stage"] == "weekly_execution"
    assert all(item["status"] == "planned" for item in after["v01Timeline"])
    assert await _plan_node_count(db, account) == before + 3, "确认后才写入 3 个阶段"
    # P2.4:时间线确认后**自动准备本周计划提案**,不需要用户再手动触发。
    auto_weekly = await _open_proposal(app_client, account)
    assert auto_weekly["status"] in {"validated", "pending_confirmation"}


@pytest.mark.asyncio
async def test_v1_coarse_timeline_invalid_and_guard(
    app_client: httpx.AsyncClient, make_account, monkeypatch, use_reasoner
) -> None:
    monkeypatch.setattr(settings, "planning_v1", True)
    account = await make_account(workspace_title="我想学 Python")
    reasoner = use_reasoner(FakeReasoner(reply="先记下战略。"))

    # 还没确认战略:拒绝生成时间架构。
    await _turn(app_client, account, "p3-guard-open")
    early = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/agent/v1/strategy/confirm",
        headers=account.headers,
    )
    assert early.status_code == 400, early.text

    await _drive_to_strategy_confirmed(app_client, account, reasoner)
    # 模型没给合法时间架构:确认战略成功,但时间架构可重试、不写计划。
    reasoner.v1_timeline = None
    confirm = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/agent/v1/strategy/confirm",
        headers=account.headers,
    )
    assert confirm.status_code == 200, confirm.text
    await _align_timeline(app_client, account)
    view = await _reasoning(app_client, account)
    assert view["v1Status"] == "failed"
    assert view["v01Timeline"] == []


async def _align_timeline(client, account, *, accepted=True, answer=""):
    """深度对话:战略确认后先对齐时间节奏,才生成粗时间线。"""
    response = await client.post(
        f"/api/workspaces/{account.workspace_id}/agent/v1/timeline/align",
        params={"accepted": str(accepted).lower(), "answer": answer},
        headers=account.headers,
    )
    assert response.status_code == 200, response.text
    return response.json()["reasoning"]


async def _open_proposal(client, account):
    response = await client.get(
        f"/api/workspaces/{account.workspace_id}/proposals", headers=account.headers
    )
    assert response.status_code == 200, response.text
    for proposal in response.json():
        if proposal["status"] in {"validated", "pending_confirmation"}:
            return proposal
    raise AssertionError(f"没有待确认的提案: {[p['status'] for p in response.json()]}")


async def _confirm(client, account, proposal_id, key):
    response = await client.post(
        f"/api/workspaces/{account.workspace_id}/proposals/{proposal_id}/confirm",
        json={"idempotencyKey": key},
        headers=account.headers,
    )
    assert response.status_code == 200, response.text


async def _plan(client, account):
    response = await client.get(
        f"/api/workspaces/{account.workspace_id}/plan", headers=account.headers
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _confirm_timeline(client, account, reasoner):
    await _drive_to_strategy_confirmed(client, account, reasoner)
    reasoner.v1_timeline = _timeline_draft()
    confirm = await client.post(
        f"/api/workspaces/{account.workspace_id}/agent/v1/strategy/confirm",
        headers=account.headers,
    )
    assert confirm.status_code == 200, confirm.text
    assert confirm.json()["reasoning"]["v1Stage"] == "timeline_alignment"
    aligned = await _align_timeline(client, account)
    proposal_id = aligned["v01TimelineProposalId"]
    await _confirm(client, account, proposal_id, "p4-timeline")
    assert (await _reasoning(client, account))["v1Stage"] == "weekly_execution"


@pytest.mark.asyncio
async def test_v1_weekly_daily_feedback_and_review(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession, monkeypatch, use_reasoner
) -> None:
    monkeypatch.setattr(settings, "planning_v1", True)
    account = await make_account(workspace_title="我想学 Python")
    reasoner = use_reasoner(FakeReasoner(reply="先记下战略。"))
    await _confirm_timeline(app_client, account, reasoner)

    # ---- 本周计划 + 下周预览 ----
    weekly = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/agent/v1/weekly/generate",
        headers=account.headers,
    )
    assert weekly.status_code == 200, weekly.text
    await _confirm(app_client, account, (await _open_proposal(app_client, account))["id"], "p4-weekly")
    plan = await _plan(app_client, account)
    weeks = [n for n in plan["nodes"] if str(n.get("title", "")).startswith("本周计划")]
    assert weeks, [n["title"] for n in plan["nodes"]]

    # ---- 日计划:少量工作日工作块,不均摊七天 ----
    daily = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/agent/v1/daily/generate",
        headers=account.headers,
    )
    assert daily.status_code == 200, daily.text
    await _confirm(app_client, account, (await _open_proposal(app_client, account))["id"], "p4-daily")
    plan = await _plan(app_client, account)
    days = [n for n in plan["nodes"] if str(n.get("title", "")).startswith("日计划")]
    assert 0 < len(days) <= 3, [n["title"] for n in plan["nodes"]]

    # ---- 执行反馈:本周完成率 < 60% -> 进入重规划 ----
    current_week = weeks[0]
    week_children = [n for n in plan["nodes"] if n.get("parentId") == current_week["id"]]
    assert week_children
    for child in week_children:
        response = await app_client.post(
            f"/api/workspaces/{account.workspace_id}/agent/v1/feedback",
            json={"nodeId": child["id"], "outcome": "missed"},
            headers=account.headers,
        )
        assert response.status_code == 200, response.text
    assert (await _reasoning(app_client, account))["v1Stage"] == "replanning"

    # ---- 周末回顾入口 -> 未来重规划草案 -> 确认 -> 回到执行 ----
    review = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/agent/v1/review",
        headers=account.headers,
    )
    assert review.status_code == 200, review.text
    replan = await _open_proposal(app_client, account)
    await _confirm(app_client, account, replan["id"], "p4-replan")
    after = await _reasoning(app_client, account)
    assert after["v1Stage"] == "weekly_execution"
    assert after["v01Timeline"] and all(item["status"] == "planned" for item in after["v01Timeline"])
