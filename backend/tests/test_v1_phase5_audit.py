"""规划智能体重构 V1 — P5:决策审计记录导出的**固定回放**验收。

全程用 `FakeReasoner`,不联网、不用真实 Key:

    创建 V1 空间 → 初步输入 → 紫色问题回答 → 战略确认
    → 时间线确认 → 周计划确认 → 反馈 40% → 重规划确认 → 导出 JSON / Markdown

断言:事件顺序与阶段迁移可追溯、节点更新可追溯、提案与重规划可追溯、失败/守卫不被
记成成功、导出不含密钥/系统提示词/隐藏思维字段、Markdown 区分事实/假设/待确认、
非 V1 空间不能导出。
"""

from __future__ import annotations

import json

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agent.runtime.base import (
    V1AssessmentDraft,
    V1NodeUpdate,
    V1TimelineDraft,
    V1TimelinePhaseDraft,
)
from backend.core.config import settings
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


async def _questions(client, account):
    r = await client.get(
        f"/api/workspaces/{account.workspace_id}/questions?includeDecided=true",
        headers=account.headers,
    )
    assert r.status_code == 200, r.text
    return {q["v1Key"]: q for q in r.json()["questions"] if q.get("v1Key")}


async def _reasoning(client, account):
    r = await client.get(
        f"/api/workspaces/{account.workspace_id}/reasoning", headers=account.headers
    )
    assert r.status_code == 200, r.text
    return r.json()


async def _plan(client, account):
    r = await client.get(f"/api/workspaces/{account.workspace_id}/plan", headers=account.headers)
    assert r.status_code == 200, r.text
    return r.json()


async def _open_proposal(client, account):
    r = await client.get(
        f"/api/workspaces/{account.workspace_id}/proposals", headers=account.headers
    )
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
    base = {"global_assessment": "整体判断。", "question": "你希望最后拿出什么成果?"}
    base.update(overrides)
    return V1AssessmentDraft(**base)


def _timeline_draft():
    return V1TimelineDraft(
        summary="基础闭环 → 最小项目 → 展示",
        phases=(
            V1TimelinePhaseDraft(title="基础闭环", goal="补齐基础", deliverable="脚本", completion_criteria="可复现", start_week=1, end_week=2),
            V1TimelinePhaseDraft(title="最小项目", goal="做出分析", deliverable="报告", completion_criteria="可复述", start_week=3, end_week=4, depends_on="基础闭环"),
            V1TimelinePhaseDraft(title="展示复盘", goal="整理成果", deliverable="一次展示", completion_criteria="讲清下一步", start_week=5, end_week=6, depends_on="最小项目"),
        ),
    )


async def _export(client, account, fmt):
    r = await client.get(
        f"/api/workspaces/{account.workspace_id}/agent/v1/audit-export?format={fmt}",
        headers=account.headers,
    )
    assert r.status_code == 200, r.text
    return r.text


@pytest.mark.asyncio
async def test_v1_audit_export_replay(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession, monkeypatch, use_reasoner
) -> None:
    monkeypatch.setattr(settings, "planning_v1", True)
    monkeypatch.setattr(settings, "agent_audit_export", True)
    account = await make_account(workspace_title="30 天学习 Python")
    reasoner = use_reasoner(FakeReasoner(reply="先记下目标。"))

    # 1. 进入空间 + 初步输入
    await _turn(app_client, account, "a1")
    reasoner.v1_assessment = _assessment(
        node_updates=(
            V1NodeUpdate(node_key="goal_definition", judgment="30 天做出一个数据分析项目。", known_facts=("用户希望用于科研申请",)),
        ),
        focus_key="goal_definition",
        focus_reason="成果定义决定阶段顺序。",
    )
    await _send(app_client, account, "30 天学习 Python,完成数据分析项目", "a2")

    # 守卫:战略没确认就生成周计划 -> 拒绝(记 failed,不记成功)
    guarded = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/agent/v1/weekly/generate",
        headers=account.headers,
    )
    assert guarded.status_code == 400, guarded.text

    # 2. 回答一个紫色问题
    questions = await _questions(app_client, account)
    reasoner.v1_assessment = _assessment(
        node_updates=(V1NodeUpdate(node_key="current_state", judgment="研一在读,晚上可投入。", known_facts=("晚上有 2 小时",)),),
    )
    answered = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/questions/{questions['current_state']['id']}/answer",
        json={"selectedOptionIds": [], "customInput": "我在读研一,晚上有 2 小时。", "clientAnswerId": "audit-ans-1"},
        headers=account.headers,
    )
    assert answered.status_code == 200, answered.text

    # 3. 战略形成并确认
    reasoner.v1_assessment = _assessment(
        node_updates=(
            V1NodeUpdate(node_key="key_conflict", judgment="目标太大,反馈太慢。"),
            V1NodeUpdate(node_key="hard_constraints", judgment="每天 2 小时。"),
            V1NodeUpdate(node_key="major_risks", judgment="陷入只看不做。"),
        ),
    )
    await _send(app_client, account, "我想先做出一个能展示的东西", "a3")
    reasoner.v1_assessment = _assessment(
        strategy_ready=True,
        node_updates=(
            V1NodeUpdate(node_key="main_line", judgment="先用最小项目闭环补齐 pandas。"),
            V1NodeUpdate(node_key="parallel_line", judgment="并行看一点统计基础。"),
            V1NodeUpdate(node_key="defer_or_avoid", judgment="暂不学算法。"),
        ),
        strategy_tradeoff="先要能展示的成果。",
    )
    await _send(app_client, account, "可以", "a4")
    reasoner.v1_assessment = _assessment(
        strategy_ready=True,
        node_updates=(V1NodeUpdate(node_key="risk_control", judgment="每两周复盘一次。"),),
    )
    await _send(app_client, account, "继续", "a5")

    reasoner.v1_timeline = _timeline_draft()
    confirmed = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/agent/v1/strategy/confirm",
        headers=account.headers,
    )
    assert confirmed.status_code == 200, confirmed.text
    timeline_proposal = confirmed.json()["reasoning"]["v01TimelineProposalId"]
    await _confirm(app_client, account, timeline_proposal, "audit-timeline")
    assert (await _reasoning(app_client, account))["v1Stage"] == "weekly_execution"

    # 4. 周计划
    weekly = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/agent/v1/weekly/generate", headers=account.headers
    )
    assert weekly.status_code == 200, weekly.text
    await _confirm(app_client, account, (await _open_proposal(app_client, account))["id"], "audit-weekly")

    # 5. 反馈 40% -> 重规划
    plan = await _plan(app_client, account)
    weeks = [n for n in plan["nodes"] if str(n.get("title", "")).startswith("本周计划")]
    week_children = [n for n in plan["nodes"] if n.get("parentId") == weeks[0]["id"]]
    assert len(week_children) == 5
    for index, child in enumerate(week_children):
        await app_client.post(
            f"/api/workspaces/{account.workspace_id}/agent/v1/feedback",
            json={"nodeId": child["id"], "outcome": "done" if index < 2 else "missed"},
            headers=account.headers,
        )
    assert (await _reasoning(app_client, account))["v1Stage"] == "replanning"

    # 6. 重规划确认
    review = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/agent/v1/review", headers=account.headers
    )
    assert review.status_code == 200, review.text
    await _confirm(app_client, account, (await _open_proposal(app_client, account))["id"], "audit-replan")
    assert (await _reasoning(app_client, account))["v1Stage"] == "weekly_execution"

    # ---- 导出 JSON ----
    raw_json = await _export(app_client, account, "json")
    data = json.loads(raw_json)
    assert data["exportVersion"] == 1
    events = data["events"]
    assert events, "应有审计事件"
    sequences = [event["sequence"] for event in events]
    assert sequences == sorted(sequences), "sequence 必须单调递增"
    assert len(set(sequences)) == len(sequences), "sequence 不得重复"
    types = {event["eventType"] for event in events}

    # 每次**阶段迁移**都有 before/after(非迁移事件前后相同是正常的)
    transitions = [
        event
        for event in events
        if event["stageBefore"] and event["stageAfter"] and event["stageBefore"] != event["stageAfter"]
    ]
    assert transitions, "至少要有一次阶段迁移带 before/after"
    assert any(
        event["stageBefore"] == "strategy_confirmed_for_timeline"
        and event["stageAfter"] == "coarse_timeline_review"
        for event in transitions
    ), "战略确认 → 时间架构审阅应有迁移"
    assert {"global_assessment_generated", "strategy_confirmed", "coarse_timeline_draft_generated"} <= types
    assert {"timeline_confirmed", "weekly_plan_confirmed", "execution_feedback_recorded", "replan_confirmed"} <= types

    # 节点更新可追溯
    updates = [e for e in events if e["eventType"] == "node_analysis_updated"]
    assert updates and any(e["payload"].get("nodeUpdates") for e in updates)
    node_update = updates[0]["payload"]["nodeUpdates"][0]
    assert node_update["key"] and "afterStatus" in node_update

    # 提案/重规划可追溯
    assert any(e["payload"].get("proposal", {}).get("kind") == "timeline" for e in events)
    assert any(e["payload"].get("proposal", {}).get("kind") == "replan" for e in events)

    # 失败/守卫不被记成成功
    guards = [e for e in events if e["eventType"] == "guard_rejected"]
    assert guards and all(e["validationStatus"] == "failed" for e in guards)
    # 在守卫拒绝之前不得出现“周计划提案已生成”的成功事件
    first_guard = next(e["sequence"] for e in events if e["eventType"] == "guard_rejected")
    weekly_success = [e for e in events if e["eventType"] == "weekly_plan_proposal_created"]
    assert all(e["sequence"] > first_guard for e in weekly_success)

    # 不含密钥 / 系统提示词 / 隐藏思维字段
    lower = raw_json.lower()
    for forbidden in ("sk-", "bearer ", "你是知途的规划智能体", "chainofthought", "chain_of_thought", "hidden"):
        assert forbidden not in lower, forbidden

    # ---- 导出 Markdown ----
    markdown = await _export(app_client, account, "markdown")
    assert "# 规划决策记录" in markdown
    assert "## 决策时间线" in markdown
    assert "用户事实" in markdown
    assert "AI 假设" in markdown
    assert "待确认事项" in markdown
    assert "sk-" not in markdown.lower() and "bearer " not in markdown.lower()


@pytest.mark.asyncio
async def test_v1_audit_export_requires_v1(
    app_client: httpx.AsyncClient, make_account, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "planning_v1", False)
    monkeypatch.setattr(settings, "agent_audit_export", True)
    account = await make_account(workspace_title="普通空间")
    await _turn(app_client, account, "plain")
    response = await app_client.get(
        f"/api/workspaces/{account.workspace_id}/agent/v1/audit-export?format=json",
        headers=account.headers,
    )
    assert response.status_code == 400, response.text


@pytest.mark.asyncio
async def test_v1_audit_export_disabled_by_default(
    app_client: httpx.AsyncClient, make_account, monkeypatch, use_reasoner
) -> None:
    monkeypatch.setattr(settings, "planning_v1", True)
    monkeypatch.setattr(settings, "agent_audit_export", False)
    account = await make_account(workspace_title="未开启导出")
    use_reasoner(FakeReasoner(reply="先记下。"))
    await _turn(app_client, account, "d1")
    response = await app_client.get(
        f"/api/workspaces/{account.workspace_id}/agent/v1/audit-export?format=markdown",
        headers=account.headers,
    )
    assert response.status_code == 400, response.text
