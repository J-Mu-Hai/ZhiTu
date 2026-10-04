"""V1 R2 收口:有解释的战略对话 / 统一的节点状态 / 严格时间架构。

规格:`docs/17-OPENJIUWEN-ACTIVE-PLANNING-LOOP-REFACTOR.md` + 本轮 R2 收口要求。

覆盖:
1. 候选方向必须带 decisionContext / provisionalRecommendation / optionImpact;
2. 不允许“问题 + 选项”连续重复;选定起点后自动综合,不再抛选择题;
3. 画布默认只投影三个核心维度;分析节点状态不再永远 pending;分组状态由子节点汇总;
4. 粗时间架构缺字段时先走窄契约 repair;仍不合格 -> failed_retryable;
5. 合格时间线 -> awaiting_user_confirmation,不显示 idle。
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
    V1NodeUpdate,
    V1StrategyDraft,
    V1TimelineDraft,
    V1TimelinePhaseDraft,
)
from backend.agent.runtime.response import parse_v1_assessment
from backend.core.config import settings
from backend.db.models import AgentAuditEvent
from backend.tests.conftest import FakeReasoner

CORE_KEYS = {"true_intent", "key_conflict", "goal_definition"}


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


async def _events(db, account):
    rows = await db.scalars(
        select(AgentAuditEvent)
        .where(AgentAuditEvent.workspace_id == uuid.UUID(account.workspace_id))
        .order_by(AgentAuditEvent.sequence.asc())
    )
    return list(rows)


def _assessment(**overrides):
    base = {"strategic_thesis": "整体判断。", "question": ""}
    base.update(overrides)
    return V1AssessmentDraft(**base)


def _options(*, directions=None, **overrides):
    return _assessment(
        response_mode="offer_options",
        focus_key="true_intent",
        decision_context="数据从哪来会决定第一周做什么,现在不定就会空转。",
        provisional_recommendation="我倾向“自找公开数据”:动力最强、报告最有话可说。",
        candidate_directions=(
            directions
            if directions is not None
            else (
                V1CandidateDirection(
                    key="public",
                    title="公开数据集",
                    reason="最易拿到",
                    path="数据→结论",
                    impact="第一周直接进 pandas 闭环",
                ),
                V1CandidateDirection(
                    key="self",
                    title="自找问题",
                    reason="动力最强",
                    path="问题→数据",
                    impact="第一周先找数据",
                ),
            )
        ),
        **overrides,
    )


def test_parse_option_explanation_fields() -> None:
    draft = parse_v1_assessment(
        {
            "strategicThesis": "判断。",
            "responseMode": "single_select",
            "decisionContext": "为什么现在定",
            "provisionalRecommendation": "我倾向 A",
            "optionImpact": [{"key": "a", "impact": "选 A 会改变第一周"}],
            "candidateDirections": [{"id": "a", "label": "方向 A"}],
        }
    )
    assert draft is not None
    assert draft.decision_context == "为什么现在定"
    assert draft.provisional_recommendation == "我倾向 A"
    assert draft.candidate_directions[0].impact == "选 A 会改变第一周"


@pytest.mark.asyncio
async def test_options_are_explained_and_not_repeated(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession, monkeypatch, use_reasoner
) -> None:
    monkeypatch.setattr(settings, "planning_v1", True)
    account = await make_account(workspace_title="有解释的选项")
    reasoner = use_reasoner(FakeReasoner(reply="先给判断。", v1_source_kind="test"))

    reasoner.v1_assessment = _options()
    view = await _turn(app_client, account, "r2-opt-1")
    assert view["v1DecisionContext"], "候选方向必须带“为什么现在决定”"
    assert view["v1ProvisionalRecommendation"], "必须先给 AI 倾向"
    assert view["v1CandidateDirections"] and len(view["v1CandidateDirections"]) == 2
    assert all(d.get("impact") for d in view["v1CandidateDirections"]), "每个选项要有选择后果"
    assert view["v1WorkflowNext"] == "select_direction"

    # 连续第二轮又给“问题 + 选项”:必须被压成暂定综合,不能退化成问卷。
    reasoner.v1_assessment = _options(
        critical_question="再选一次?",
        directions=(
            V1CandidateDirection(key="x", title="X"),
            V1CandidateDirection(key="y", title="Y"),
        ),
    )
    await _send(app_client, account, "我还不太确定", "r2-opt-2")
    after = await _reasoning(app_client, account)
    assert after["v1CandidateDirections"] is None, "不得连续抛出第二组裸选项"

    events = await _events(db, account)
    offered = [e for e in events if e.event_type == "candidate_directions_offered"]
    assert len(offered) == 1, "只允许出现一次选项提案"


@pytest.mark.asyncio
async def test_direction_selection_auto_synthesizes(
    app_client: httpx.AsyncClient, make_account, monkeypatch, use_reasoner
) -> None:
    monkeypatch.setattr(settings, "planning_v1", True)
    account = await make_account(workspace_title="选完自动综合")
    reasoner = use_reasoner(FakeReasoner(reply="先给判断。", v1_source_kind="test"))
    reasoner.v1_assessment = _options()
    await _turn(app_client, account, "r2-sel-1")

    reasoner.v1_assessment = _assessment(focus_key="goal_definition")
    reasoner.v1_strategy = V1StrategyDraft(
        main_line="先用公开数据跑最小闭环",
        parallel_line="边做边补 pandas",
        defer_or_avoid="暂不系统学语法",
        risk_control="第 1 周末检查点",
        tradeoff="先要能展示",
    )
    select = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/agent/v1/direction/select?key=public",
        headers=account.headers,
    )
    assert select.status_code == 200, select.text
    view = select.json()["reasoning"]
    assert view["v1Stage"] == "strategy_draft", "选定起点后必须自动综合,不再抛选择题"
    assert view["v1Strategy"]
    assert view["v1CanReselectDirection"] is False, "选定后不再展示候选按钮"


@pytest.mark.asyncio
async def test_analysis_status_unified_and_projection(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession, monkeypatch, use_reasoner
) -> None:
    monkeypatch.setattr(settings, "planning_v1", True)
    account = await make_account(workspace_title="状态统一")
    reasoner = use_reasoner(FakeReasoner(reply="判断。", v1_source_kind="test"))
    reasoner.v1_assessment = _assessment(
        focus_key="goal_definition",
        node_updates=(
            V1NodeUpdate(node_key="goal_definition", judgment="30 天做出可展示项目。", status="resolved"),
            V1NodeUpdate(node_key="key_conflict", judgment="卡在目标太大。"),
            V1NodeUpdate(node_key="true_intent", judgment="想要能展示的成果。"),
        ),
    )
    await _turn(app_client, account, "r2-status-1")

    view = await _reasoning(app_client, account)
    assert set(view["v1VisibleAnalysisKeys"]) == CORE_KEYS, "主画布只投影三个核心维度"

    r = await app_client.get(
        f"/api/workspaces/{account.workspace_id}/questions?includeDecided=true",
        headers=account.headers,
    )
    questions = {q["v1Key"]: q for q in r.json()["questions"] if q.get("v1Key")}
    assert questions["goal_definition"]["status"] == "resolved"
    assert questions["key_conflict"]["status"] in ("investigating", "resolved")
    assert questions["true_intent"]["status"] in ("investigating", "resolved")

    # R2:隐藏用 visibility/internal 投影表达,不把隐藏维度伪装成 archived。
    dims = {d["key"]: d for d in view["v1Dimensions"]}
    assert dims["major_risks"]["visible"] is False
    assert dims["major_risks"]["internal"] is True
    assert dims["goal_definition"]["visible"] is True
    assert dims["goal_definition"]["judgment"] == "30 天做出可展示项目。"
    assert dims["goal_definition"]["questionId"]

    # 分组状态由子节点汇总:已分析的 goal_reframe 分组不得停在 pending。
    r = await app_client.get(f"/api/workspaces/{account.workspace_id}/plan", headers=account.headers)
    groups = {n["title"]: n for n in r.json()["nodes"] if n.get("nodeType") == "capability"}
    assert groups, "应有三个分组"
    assert groups["目标重构"]["status"] in ("doing", "completed")


# ---------------------------------------------------------------------------
# 时间架构严格化
# ---------------------------------------------------------------------------
def _timeline(*, weeks: bool) -> V1TimelineDraft:
    def phase(title: str, index: int) -> V1TimelinePhaseDraft:
        return V1TimelinePhaseDraft(
            title=title,
            goal=f"{title} 的目标",
            deliverable=f"{title} 的成果",
            completion_criteria=f"{title} 的完成标准",
            start_week=(index * 2 - 1) if weeks else None,
            end_week=(index * 2) if weeks else None,
        )

    return V1TimelineDraft(
        summary="基础 → 项目 → 展示",
        phases=(phase("基础", 1), phase("项目", 2), phase("展示", 3)),
    )


async def _drive_to_strategy_confirmed(client, account, reasoner):
    reasoner.v1_assessment = _assessment()
    await _turn(client, account, "r2-tl-open")
    reasoner.v1_assessment = _assessment(
        node_updates=(
            V1NodeUpdate(node_key="goal_definition", judgment="30 天做出可展示项目。"),
            V1NodeUpdate(node_key="key_conflict", judgment="目标太大、反馈太慢。"),
            V1NodeUpdate(node_key="hard_constraints", judgment="每天只有 1 小时。"),
        )
    )
    await _send(client, account, "我想做出一个能展示的项目", "r2-tl-1")
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
    reasoner.v1_assessment = _assessment(
        strategy_ready=True,
        node_updates=(
            V1NodeUpdate(node_key="defer_or_avoid", judgment="暂不系统学算法。"),
            V1NodeUpdate(node_key="risk_control", judgment="每两周复盘。"),
        ),
    )
    cont = await client.post(
        f"/api/workspaces/{account.workspace_id}/agent/v1/strategy/continue",
        headers=account.headers,
    )
    assert cont.status_code == 200, cont.text
    assert cont.json()["reasoning"]["v1Strategy"]


@pytest.mark.asyncio
async def test_timeline_incomplete_triggers_repair_then_succeeds(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession, monkeypatch, use_reasoner
) -> None:
    monkeypatch.setattr(settings, "planning_v1", True)
    account = await make_account(workspace_title="时间线修补")
    reasoner = use_reasoner(FakeReasoner(reply="记下战略。", v1_source_kind="test"))
    await _drive_to_strategy_confirmed(app_client, account, reasoner)

    # 第一次缺相对周 -> 自动 repair 一次 -> 合格。
    reasoner.timeline_queue = [_timeline(weeks=False), _timeline(weeks=True)]
    confirm = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/agent/v1/strategy/confirm",
        headers=account.headers,
    )
    assert confirm.status_code == 200, confirm.text
    assert confirm.json()["reasoning"]["v1Stage"] == "timeline_alignment"
    align = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/agent/v1/timeline/align",
        params={"accepted": "true"},
        headers=account.headers,
    )
    assert align.status_code == 200, align.text
    view = align.json()["reasoning"]
    assert view["v1Stage"] == "coarse_timeline_review"
    assert view["v1Status"] == "awaiting_user_confirmation", "必须是等待确认,不是 idle"
    assert len(view["v01Timeline"]) == 3
    assert all(p["startWeek"] and p["endWeek"] for p in view["v01Timeline"])
    assert all(p["goal"] and p["deliverable"] and p["completionCriteria"] for p in view["v01Timeline"])
    assert view["v01TimelineProposalId"]

    events = await _events(db, account)
    types = [e.event_type for e in events]
    assert "timeline_repair_requested" in types
    assert "coarse_timeline_draft_generated" in types
    assert "timeline_proposal_created" in types


@pytest.mark.asyncio
async def test_timeline_repair_failure_is_retryable(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession, monkeypatch, use_reasoner
) -> None:
    monkeypatch.setattr(settings, "planning_v1", True)
    account = await make_account(workspace_title="修补失败")
    reasoner = use_reasoner(FakeReasoner(reply="记下战略。", v1_source_kind="test"))
    await _drive_to_strategy_confirmed(app_client, account, reasoner)

    reasoner.timeline_queue = [_timeline(weeks=False), _timeline(weeks=False)]
    confirm = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/agent/v1/strategy/confirm",
        headers=account.headers,
    )
    assert confirm.status_code == 200, confirm.text
    align = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/agent/v1/timeline/align",
        params={"accepted": "true"},
        headers=account.headers,
    )
    assert align.status_code == 200, align.text
    view = align.json()["reasoning"]
    assert view["v1Status"] == "failed"
    assert "缺少时间范围或验收标准" in (view["v1Error"] or "")
    assert view["v01Timeline"] == [], "不合格不得创建时间线"
    assert view["v01TimelineProposalId"] is None

    events = await _events(db, account)
    invalid = [e for e in events if e.event_type == "model_output_invalid"]
    assert invalid and invalid[-1].error_code == "MODEL_OUTPUT_INVALID"
    proposals = await app_client.get(
        f"/api/workspaces/{account.workspace_id}/proposals", headers=account.headers
    )
    assert [p for p in proposals.json() if p["status"] in ("validated", "pending_confirmation")] == []
