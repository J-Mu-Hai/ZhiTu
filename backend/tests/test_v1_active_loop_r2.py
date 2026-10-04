"""V1 主动规划循环 — R2:从战略到粗时间架构的自动循环。

规格:`docs/17-OPENJIUWEN-ACTIVE-PLANNING-LOOP-REFACTOR.md` 第 5 节 / 第 12 节 R2。

覆盖:

1. 进入空间即实际启动整体判断(不是只置 `initial_thinking`);
2. `goal_reframe → problem_structure → strategy_draft` 的自动收束;
3. 任何停下都有可解释的下一步(`v1WorkflowNext` / 待答问题),不存在无解释 idle;
4. 战略确认后**自动**生成粗时间线草案并进入 `coarse_timeline_review`,不需要额外按钮;
5. 重复选择同一候选方向幂等,不重复写、不重复调模型。
"""

from __future__ import annotations

import httpx
import pytest

from backend.agent.runtime.base import (
    V1AssessmentDraft,
    V1KeyDimension,
    V1NodeUpdate,
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
    return r.json()["reasoning"]


async def _send(client, account, text, key):
    r = await client.post(
        f"/api/workspaces/{account.workspace_id}/messages",
        json={"content": text, "clientMessageId": key},
        headers=account.headers,
    )
    assert r.status_code == 200, r.text
    return r.json()


def _explained(view: dict) -> bool:
    """可解释:idle 时必须有等待对象 / 明确下一步 / 已成形产物。"""
    if view["v1Status"] in ("running", "failed"):
        return True
    if view.get("v1Question"):
        return True
    if view.get("v1WorkflowNext"):
        return True
    if view.get("v1Strategy") or view.get("v01Timeline") or view.get("v01TimelineProposalId"):
        return True
    return view.get("v1Stage") in (None, "initial_thinking", "weekly_execution")


def _assessment(**overrides):
    base = {
        "strategic_thesis": "整体判断。",
        "response_mode": "ask",
        "question": "",
        "focus_key": "true_intent",
        "key_dimensions": (
            V1KeyDimension(key="key_conflict", judgment="卡在目标不清。", why_it_matters="决定下一步"),
        ),
    }
    base.update(overrides)
    return V1AssessmentDraft(**base)


@pytest.mark.asyncio
async def test_v1_orientation_then_auto_strategy_to_coarse_timeline(
    app_client: httpx.AsyncClient, make_account, monkeypatch, use_reasoner
) -> None:
    monkeypatch.setattr(settings, "planning_v1", True)
    account = await make_account(workspace_title="我想学 Python")
    reasoner = use_reasoner(FakeReasoner(reply="先给整体判断。", v1_source_kind="test"))

    # ---- 1. 进入空间:实际启动整体判断 ----
    reasoner.v1_assessment = _assessment()
    view = await _turn(app_client, account, "r2-open")
    assert reasoner.calls, "进入空间必须实际启动首轮整体判断"
    assert _explained(view), view

    # ---- 2. 目标维度收束:形成可确认的目标定义 ----
    reasoner.v1_assessment = _assessment(
        node_updates=(
            V1NodeUpdate(node_key="goal_definition", judgment="30 天做出一个能展示的分析项目。"),
            V1NodeUpdate(node_key="key_conflict", judgment="目标太大、反馈太慢。"),
            V1NodeUpdate(node_key="hard_constraints", judgment="每天只有 1 小时。"),
        )
    )
    await _send(app_client, account, "我想做出一个能展示的数据分析项目", "r2-1")
    view = await app_client.get(
        f"/api/workspaces/{account.workspace_id}/reasoning", headers=account.headers
    )
    view = view.json()
    assert view["v1Stage"] == "goal_reframe"
    assert _explained(view), view

    # ---- 3. 确认目标定义 -> problem_structure -> 自动收束到 strategy_draft ----
    reasoner.v1_assessment = _assessment(
        strategy_ready=True,
        node_updates=(
            V1NodeUpdate(node_key="major_risks", judgment="容易陷入只学不做的循环。"),
            V1NodeUpdate(node_key="main_line", judgment="先用最小项目闭环补齐 pandas。"),
            V1NodeUpdate(node_key="parallel_line", judgment="并行看一点统计基础。"),
        ),
        strategy_tradeoff="先要能展示的成果。",
    )
    confirm_goal = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/agent/v1/goal/confirm",
        headers=account.headers,
    )
    assert confirm_goal.status_code == 200, confirm_goal.text
    view = confirm_goal.json()["reasoning"]
    assert view["v1Stage"] == "strategy_draft"
    assert view["v1Strategy"], view
    assert view["v1WorkflowNext"] == "confirm_strategy", view
    assert _explained(view), view

    # ---- 4. 确认战略 -> 自动生成粗时间线草案并进入审阅 ----
    from backend.agent.runtime.base import V1TimelineDraft, V1TimelinePhaseDraft

    reasoner.v1_timeline = V1TimelineDraft(
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
    confirm = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/agent/v1/strategy/confirm",
        headers=account.headers,
    )
    assert confirm.status_code == 200, confirm.text
    view = confirm.json()["reasoning"]
    assert view["v1Stage"] == "coarse_timeline_review"
    assert view["v01Timeline"] and all(item["status"] == "draft" for item in view["v01Timeline"])
    assert view["v01TimelineProposalId"], "战略确认后应自动生成待确认时间线"
    assert view["v1WorkflowNext"] == "confirm_timeline"
    assert _explained(view), view

    # 时间线确认**前**不写正式阶段。
    plan = await app_client.get(
        f"/api/workspaces/{account.workspace_id}/plan", headers=account.headers
    )
    assert plan.status_code == 200
    stage_nodes = [n for n in plan.json()["nodes"] if n.get("nodeType") == "stage"]
    assert stage_nodes == [], "确认前不得写入正式阶段"


@pytest.mark.asyncio
async def test_v1_repeated_direction_selection_is_idempotent(
    app_client: httpx.AsyncClient, make_account, monkeypatch, use_reasoner
) -> None:
    monkeypatch.setattr(settings, "planning_v1", True)
    account = await make_account(workspace_title="候选方向")
    from backend.agent.runtime.base import V1CandidateDirection

    reasoner = use_reasoner(
        FakeReasoner(
            reply="给候选方向。",
            v1_source_kind="test",
            v1_assessment=_assessment(
                response_mode="offer_options",
                candidate_directions=(
                    V1CandidateDirection(key="a", title="方向 A", reason="理由 A", path="路径 A"),
                    V1CandidateDirection(key="b", title="方向 B", reason="理由 B", path="路径 B"),
                ),
            ),
        )
    )
    view = await _turn(app_client, account, "r2-dir")
    assert view["v1Stage"] == "goal_reframe"
    assert view["v1WorkflowNext"] == "select_direction"
    calls_after_first = len(reasoner.calls)

    first = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/agent/v1/direction/select?key=a",
        headers=account.headers,
    )
    assert first.status_code == 200, first.text
    calls_after_select = len(reasoner.calls)

    # 同一方向再次选择:幂等,不再跑模型。
    second = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/agent/v1/direction/select?key=a",
        headers=account.headers,
    )
    assert second.status_code == 200, second.text
    assert len(reasoner.calls) == calls_after_select, "重复选择不再调用模型"
    assert calls_after_select >= calls_after_first
    assert second.json()["reasoning"]["v1SelectedDirection"] == "a"
