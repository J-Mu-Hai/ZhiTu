"""规划智能体重构 V1 — Stage 1 信息分层与焦点画布收口(P2.2)。

回归来源:`agent-audit (1).json` —— 十个分析维度被渲染成十个 `canvas_question`、
同一候选方向重复选了五次、`currentSnapshot.strategicThesis` 为 null。

覆盖:内部十维 vs 可见节点、真实待回答问题计数、候选方向闭环、进入问题结构的条件、
审计快照修复。
"""

from __future__ import annotations

import json
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
    V1StrategyDraft,
)
from backend.core.config import settings
from backend.db.models import AgentAuditEvent, PlanNode
from backend.tests.conftest import FakeReasoner

ALL_KEYS = {
    "current_state",
    "true_intent",
    "value_assessment",
    "key_conflict",
    "goal_definition",
    "hard_constraints",
    "controllable_factors",
    "key_levers",
    "major_risks",
    "external_conditions",
}
CORE_KEYS = {"true_intent", "key_conflict", "goal_definition"}
PROBLEM_KEYS = {"hard_constraints", "controllable_factors", "key_levers", "major_risks", "external_conditions"}


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


async def _questions(client, account):
    r = await client.get(
        f"/api/workspaces/{account.workspace_id}/questions?includeDecided=true",
        headers=account.headers,
    )
    assert r.status_code == 200, r.text
    return {q["v1Key"]: q for q in r.json()["questions"] if q.get("v1Key")}


async def _audit_events(db, account):
    rows = await db.scalars(
        select(AgentAuditEvent)
        .where(AgentAuditEvent.workspace_id == uuid.UUID(account.workspace_id))
        .order_by(AgentAuditEvent.sequence.asc())
    )
    return list(rows)


async def _export(client, account, fmt="json"):
    r = await client.get(
        f"/api/workspaces/{account.workspace_id}/agent/v1/audit-export?format={fmt}",
        headers=account.headers,
    )
    assert r.status_code == 200, r.text
    return r.text


def _directions():
    return (
        V1CandidateDirection(key="automation_tool", title="自动化小工具", reason="成本最低", path="重复小事 → 脚本"),
        V1CandidateDirection(key="data_analysis", title="数据小分析", reason="成果明确", path="数据 → 结论"),
        V1CandidateDirection(key="small_app", title="小应用", reason="有趣但周期长", path="原型 → 功能"),
    )


def _assessment(**overrides):
    base = {"strategic_thesis": "真正的问题是没有可验证的产出物。", "response_mode": "offer_options"}
    base.update(overrides)
    return V1AssessmentDraft(**base)


async def _start(client, account, reasoner, key="s"):
    monkeypatch_key = f"{key}-open"
    await _turn(client, account, monkeypatch_key)
    reasoner.v1_assessment = _assessment(
        key_dimensions=(
            V1KeyDimension(key="true_intent", judgment="想要能做出东西", why_it_matters="决定路径"),
        ),
        node_updates=(
            V1NodeUpdate(node_key="true_intent", judgment="想要能做出东西。"),
            V1NodeUpdate(node_key="key_conflict", judgment="卡在缺少具体目标。"),
            V1NodeUpdate(node_key="goal_definition", judgment="先做出一个能运行的小东西。"),
        ),
        candidate_directions=_directions(),
        focus_key="true_intent",
        focus_reason="它决定整条路线。",
    )
    await _send(client, account, "我想提升能力,做些有趣的东西,担心学不会", f"{key}-1")


@pytest.mark.asyncio
async def test_v1_visibility_and_pending_question_count(
    app_client: httpx.AsyncClient, make_account, monkeypatch, use_reasoner
) -> None:
    monkeypatch.setattr(settings, "planning_v1", True)
    account = await make_account(workspace_title="信息分层")
    reasoner = use_reasoner(FakeReasoner(reply="先给判断。"))
    await _start(app_client, account, reasoner)

    view = await _reasoning(app_client, account)
    assert view["v1Stage"] == "goal_reframe"
    assert set(view["v1VisibleAnalysisKeys"]) == CORE_KEYS
    assert view["v1HiddenAnalysisCount"] == 7, "其余七个内部维度默认隐藏"
    assert view["v1ActualPendingQuestionCount"] == 0, "分析维度不是待回答问题"

    questions = await _questions(app_client, account)
    assert set(questions) == ALL_KEYS, "内部十维都在(持久化/审计/讨论仍保留)"
    for key, question in questions.items():
        assert question["v1RequiresResponse"] is False, "固定维度从不要求回答"
        assert question["v1Title"], "每个维度有展示标题"
        assert question["v1Visible"] is (key in CORE_KEYS)
    # goal_reframe 分组下默认只可见三个核心维度。
    group_id = next(iter(questions.values()))["sourceNodeId"]
    group_visible = {
        q["v1Key"] for q in questions.values() if q["sourceNodeId"] == group_id and q["v1Visible"]
    }
    assert all(key not in group_visible for key in ("current_state", "value_assessment"))


@pytest.mark.asyncio
async def test_v1_candidate_selection_closed_loop_and_dedup(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession, monkeypatch, use_reasoner
) -> None:
    monkeypatch.setattr(settings, "planning_v1", True)
    monkeypatch.setattr(settings, "agent_audit_export", True)
    account = await make_account(workspace_title="候选闭环")
    reasoner = use_reasoner(FakeReasoner(reply="先给判断。"))
    await _start(app_client, account, reasoner)

    # 选择候选方向后,模型立即更新战略判断与目标定义。
    reasoner.v1_assessment = _assessment(
        strategic_thesis="你选择的是最低风险的起点:用 Python 解决一件重复小事。",
        node_updates=(
            V1NodeUpdate(node_key="goal_definition", judgment="短周期内独立完成并使用一个自动化小工具。"),
            V1NodeUpdate(node_key="true_intent", judgment="验证 Python 是否值得长期投入。"),
        ),
        response_mode="none",
        focus_key="goal_definition",
    )
    # R2:选定起点后直接自动综合(通用回合 + 窄契约战略合成),不再抛下一道选择题。
    reasoner.v1_strategy = V1StrategyDraft(
        main_line="先用最小项目闭环补齐 pandas",
        parallel_line="并行看一点统计基础",
        defer_or_avoid="暂不系统学算法",
        risk_control="每两周复盘一次",
        tradeoff="先要能展示的成果",
    )
    calls_before = len(reasoner.calls)
    first = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/agent/v1/direction/select?key=automation_tool",
        headers=account.headers,
    )
    assert first.status_code == 200, first.text
    body = first.json()["reasoning"]
    assert body["v1SelectedDirection"] == "automation_tool"
    assert "最低风险" in (body["v1StrategicThesis"] or "")
    questions = await _questions(app_client, account)
    assert questions["goal_definition"]["v1Analysis"]["judgment"] == "短周期内独立完成并使用一个自动化小工具。"
    assert body["v1Question"] is None, "选择方向后不再提问"
    assert body["v1Stage"] == "strategy_draft", "选定后自动综合战略"
    assert len(reasoner.calls) == calls_before + 2, "通用回合 + 窄契约战略合成"

    # 同一方向重复点击:幂等,不再审计、不再调模型。
    for _ in range(4):
        again = await app_client.post(
            f"/api/workspaces/{account.workspace_id}/agent/v1/direction/select?key=automation_tool",
            headers=account.headers,
        )
        assert again.status_code == 200, again.text
    assert len(reasoner.calls) == calls_before + 2, "重复选择不重复调用模型"
    events = await _audit_events(db, account)
    selected = [event for event in events if event.event_type == "candidate_direction_selected"]
    assert len(selected) == 1, "同一方向最多一条选择审计"
    assert selected[0].focus_key == "true_intent"
    assert selected[0].focus_reason
    assert selected[0].payload_json.get("selectedDirection") == "automation_tool"

    # 审计快照修复:strategicThesis / 可见性 / 待回答问题数。
    exported = json.loads(await _export(app_client, account))
    snapshot = exported["currentSnapshot"]
    assert snapshot["strategicThesis"], "currentSnapshot.strategicThesis 不能为 null"
    assert set(snapshot["visibleAnalysisDimensionKeys"]) == CORE_KEYS
    assert snapshot["hiddenAnalysisDimensionCount"] == 7
    assert snapshot["actualPendingQuestionCount"] == 0
    assert snapshot["selectedDirection"] == "automation_tool"
    assert snapshot["focusKey"] == "goal_definition"

    # 不进入时间线、不创建任务。
    assert (await _reasoning(app_client, account))["v01Timeline"] == []


@pytest.mark.asyncio
async def test_v1_goal_confirmation_enters_problem_structure(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession, monkeypatch, use_reasoner
) -> None:
    monkeypatch.setattr(settings, "planning_v1", True)
    monkeypatch.setattr(settings, "agent_audit_export", True)
    account = await make_account(workspace_title="进入问题结构")
    reasoner = use_reasoner(FakeReasoner(reply="先给判断。"))
    await _start(app_client, account, reasoner)
    reasoner.v1_assessment = _assessment(
        strategic_thesis="先定一个小工具目标。",
        node_updates=(V1NodeUpdate(node_key="goal_definition", judgment="完成一个自动化小工具。"),),
        response_mode="none",
        focus_key="goal_definition",
    )
    await app_client.post(
        f"/api/workspaces/{account.workspace_id}/agent/v1/direction/select?key=automation_tool",
        headers=account.headers,
    )
    before = await _reasoning(app_client, account)
    assert before["v1Stage"] == "goal_reframe"
    assert before["v1ActualPendingQuestionCount"] == 0

    confirm = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/agent/v1/goal/confirm",
        headers=account.headers,
    )
    assert confirm.status_code == 200, confirm.text
    after = confirm.json()["reasoning"]
    assert after["v1Stage"] == "problem_structure"
    # R2:主画布**只**投影三个核心维度(+当前焦点);其余内部维度留在数据层与右侧详情。
    assert set(after["v1VisibleAnalysisKeys"]) == CORE_KEYS, "主画布默认只投影三个核心维度"
    assert not (set(after["v1VisibleAnalysisKeys"]) & PROBLEM_KEYS), "不再默认铺开五个因素维度"
    assert after["v01Timeline"] == []
    # 只有三组 + 根,没有时间线阶段或任务。
    plan_nodes = list(
        await db.scalars(
            select(PlanNode).where(PlanNode.workspace_id == uuid.UUID(account.workspace_id))
        )
    )
    assert all(node.node_type.value in {"goal", "capability"} for node in plan_nodes)

    events = await _audit_events(db, account)
    assert any(event.event_type == "goal_definition_confirmed" for event in events)

    markdown = await _export(app_client, account, "markdown")
    assert "用户真正需要回答的问题" in markdown
    assert "内部分析维度" in markdown
