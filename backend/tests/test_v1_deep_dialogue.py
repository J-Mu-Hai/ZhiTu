"""V1 深度战略对话 + 时间架构共创。

覆盖:
1. 战略合成后先呈现“战略理解”(audit `strategy_understanding_presented`);
2. `strategy_confirmed` 不直接生成时间线,先进入 `timeline_alignment`
   (audit `timeline_assumptions_presented` / `timeline_alignment_question_asked`);
3. 用户对齐节奏后才生成粗时间线(audit `timeline_alignment_accepted` /
   `timeline_alignment_answered` → `coarse_timeline_draft_generated` →
   `timeline_proposal_created`);
4. 对齐回合契约解析。
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agent.runtime.base import (
    V1AlignmentAssumption,
    V1AssessmentDraft,
    V1NodeUpdate,
    V1StrategyDraft,
    V1TimelineAlignmentDraft,
)
from backend.agent.runtime.response import parse_v1_timeline_alignment
from backend.core.config import settings
from backend.db.models import AgentAuditEvent
from backend.db.models.goal_reasoning import GoalReasoningSession
from backend.db.models.question import AgentQuestion
from backend.tests.conftest import FakeReasoner
from backend.tests.test_v1_phase34 import _timeline_draft


async def _send(client, account, text, key):
    r = await client.post(
        f"/api/workspaces/{account.workspace_id}/messages",
        json={"content": text, "clientMessageId": key},
        headers=account.headers,
    )
    assert r.status_code == 200, r.text
    return r.json()


async def _turn(client, account, key):
    r = await client.post(
        f"/api/workspaces/{account.workspace_id}/agent/turn",
        json={"trigger": "space_entered", "idempotencyKey": key},
        headers=account.headers,
    )
    assert r.status_code == 200, r.text
    return r.json()["reasoning"]


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


async def _drive_to_strategy_formed(client, account, reasoner, db: AsyncSession | None = None):
    """按产品状态机完成四维讨论，再明确确认“已经想清楚”。"""
    reasoner.v1_assessment = _assessment()
    await _turn(client, account, "dd-open")
    for index, (key, answer) in enumerate((
        ("true_intent", "我想做出一个能展示的自动化项目。"),
        ("current_state", "我会一点 Python 基础语法，也能查资料。"),
        ("hard_constraints", "我每天最多投入一小时，预计用六周。"),
        ("goal_definition", "能独立完成、演示并写进简历才算完成。"),
    ), start=1):
        reasoner.v1_assessment = _assessment(
            node_updates=(V1NodeUpdate(node_key=key, judgment=answer),),
        )
        await _send(client, account, answer, f"dd-{index}")

    # 战略只能在四项讨论结束、用户明确确认“想清楚”之后形成。
    reasoner.v1_strategy = V1StrategyDraft(
        main_line="先用最小项目跑通自动化闭环。",
        parallel_line="并行补齐完成项目所需的基础。",
        defer_or_avoid="暂不扩展到复杂算法和大而全课程。",
        risk_control="每周检查一次是否产出可演示成果。",
        tradeoff="优先可展示成果，而非覆盖所有知识点。",
    )
    confirmed = await client.post(
        f"/api/workspaces/{account.workspace_id}/agent/v1/goal/confirm",
        headers=account.headers,
    )
    assert confirmed.status_code == 200, confirmed.text
    if db is not None:
        session = await db.scalar(
            select(GoalReasoningSession).where(
                GoalReasoningSession.workspace_id == uuid.UUID(account.workspace_id)
            )
        )
        assert session is not None
        assert session.v1_question_budget_used == 15
        rows = list(
            await db.scalars(
                select(AgentQuestion).where(
                    AgentQuestion.workspace_id == uuid.UUID(account.workspace_id),
                    AgentQuestion.v1_key.in_((
                        "true_intent", "current_state", "hard_constraints", "goal_definition",
                    )),
                )
            )
        )
        assert {row.v1_key for row in rows if (row.v1_analysis or {}).get("discussionAnswer")} == {
            "true_intent", "current_state", "hard_constraints", "goal_definition",
        }


def test_parse_timeline_alignment_contract() -> None:
    draft = parse_v1_timeline_alignment(
        {
            "summary": "按每周一个闭环推进",
            "totalSpan": "约 4 周",
            "cadence": "每周 1 个可验收小闭环",
            "phaseCount": 4,
            "biggestRisk": "投入不稳定",
            "assumptions": [
                {"text": "用户说每天 1 小时", "source": "user_fact"},
                {"text": "我暂定每周 6 小时", "source": "ai_assumption"},
            ],
            "question": "有没有不可动的截止日期?",
            "options": ["有,时间固定", "没有,可以弹性"],
        }
    )
    assert draft is not None
    assert draft.cadence == "每周 1 个可验收小闭环"
    assert draft.assumptions[0].source == "user_fact"
    assert draft.assumptions[1].source == "ai_assumption"
    assert draft.options == ("有,时间固定", "没有,可以弹性")


@pytest.mark.asyncio
async def test_four_core_discussions_are_a_hard_gate_before_strategy_confirmation(
    app_client: httpx.AsyncClient, make_account, monkeypatch, use_reasoner
) -> None:
    """回归：答完第一题不能直接出现“确认战略”或进入时间线。"""
    monkeypatch.setattr(settings, "planning_v1", True)
    account = await make_account(workspace_title="四维门槛")
    reasoner = use_reasoner(FakeReasoner(reply="先给战略分析。", v1_source_kind="test"))
    reasoner.v1_assessment = _assessment()
    await _turn(app_client, account, "four-open")

    reasoner.v1_assessment = _assessment(
        node_updates=(V1NodeUpdate(node_key="true_intent", judgment="做出可展示的自动化工具。"),),
    )
    await _send(app_client, account, "我要做一个能展示的自动化工具", "four-1")
    view = (await app_client.get(
        f"/api/workspaces/{account.workspace_id}/reasoning", headers=account.headers
    )).json()
    assert view["v1Stage"] == "goal_reframe"
    assert view["v1WorkflowNext"] is None
    assert view["v1FocusKey"] == "current_state"
    assert view["v1ActualPendingQuestionCount"] == 1
    blocked = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/agent/v1/goal/confirm",
        headers=account.headers,
    )
    assert blocked.status_code == 400

    for index, (key, answer) in enumerate((
        ("current_state", "我会基础语法。"),
        ("hard_constraints", "每周稳定投入六小时。"),
        ("goal_definition", "能完成演示并解决真实问题。"),
    ), start=2):
        reasoner.v1_assessment = _assessment(
            node_updates=(V1NodeUpdate(node_key=key, judgment=answer),),
        )
        await _send(app_client, account, answer, f"four-{index}")

    view = (await app_client.get(
        f"/api/workspaces/{account.workspace_id}/reasoning", headers=account.headers
    )).json()
    assert view["v1ActualPendingQuestionCount"] == 0
    assert view["v1WorkflowNext"] == "confirm_goal"
    assert view["v1Strategy"] is None
    assert view["v01Timeline"] == []


@pytest.mark.asyncio
async def test_conversation_acceptance_after_fourth_answer_starts_strategy(
    app_client: httpx.AsyncClient, make_account, monkeypatch, use_reasoner
) -> None:
    """最后一题答完后说“可以”是确认，不得再跑一遍同焦点的模型分析。"""
    monkeypatch.setattr(settings, "planning_v1", True)
    account = await make_account(workspace_title="对话确认推进")
    reasoner = use_reasoner(FakeReasoner(reply="已记录。", v1_source_kind="test"))
    reasoner.v1_assessment = _assessment()
    await _turn(app_client, account, "accept-open")

    for index, (key, answer) in enumerate((
        ("true_intent", "做出一个能展示的自动化工具。"),
        ("current_state", "我会一点 Python 基础语法。"),
        ("hard_constraints", "每周稳定投入六小时。"),
        ("goal_definition", "能独立完成演示并解决真实问题。"),
    ), start=1):
        reasoner.v1_assessment = _assessment(
            node_updates=(V1NodeUpdate(node_key=key, judgment=answer),),
        )
        await _send(app_client, account, answer, f"accept-{index}")

    reasoner.v1_strategy = V1StrategyDraft(
        main_line="先完成一个最小可演示闭环。",
        parallel_line="并行补齐直接需要的基础。",
        defer_or_avoid="暂不追求大而全的课程覆盖。",
        risk_control="每周检查一次是否有可运行产出。",
        tradeoff="优先完成可演示成果。",
    )
    accepted = await _send(app_client, account, "可以", "accept-confirm")
    assert accepted["assistantMessage"]["content"]
    view = (await app_client.get(
        f"/api/workspaces/{account.workspace_id}/reasoning", headers=account.headers
    )).json()
    assert view["v1Stage"] == "strategy_draft"
    assert view["v1Strategy"] and view["v1Strategy"]["mainLine"]
    assert view["v1WorkflowNext"] == "confirm_strategy"


@pytest.mark.asyncio
async def test_strategy_confirm_enters_alignment_then_generates_timeline(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession, monkeypatch, use_reasoner
) -> None:
    monkeypatch.setattr(settings, "planning_v1", True)
    account = await make_account(workspace_title="时间架构共创")
    reasoner = use_reasoner(FakeReasoner(reply="记下战略。", v1_source_kind="test"))
    await _drive_to_strategy_formed(app_client, account, reasoner, db)

    # 有战略理解,且未确认。
    view = (await app_client.get(
        f"/api/workspaces/{account.workspace_id}/reasoning", headers=account.headers
    )).json()
    understanding = view["v1StrategyUnderstanding"]
    assert understanding and understanding["mainLine"]
    assert understanding["confirmed"] is False

    # 确认战略 -> 先进入时间架构共创,不生成时间线。
    reasoner.v1_timeline = _timeline_draft()
    reasoner.v1_timeline_alignment = V1TimelineAlignmentDraft(
        summary="按每周一个可验收小闭环推进。",
        total_span="约 6 周",
        cadence="每周 1 个可验收小闭环",
        phase_count=3,
        biggest_risk="投入不稳定",
        assumptions=(V1AlignmentAssumption(text="用户想 30 天内出成果", source="user_fact"),),
        question="更希望更快见成果,还是更稳打基础?",
        options=("先快后稳", "先稳后快"),
    )
    confirm = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/agent/v1/strategy/confirm",
        headers=account.headers,
    )
    assert confirm.status_code == 200, confirm.text
    view = confirm.json()["reasoning"]
    assert view["v1Stage"] == "timeline_alignment"
    assert view["v01Timeline"] == [], "共创阶段不得生成时间线"
    assert view["v01TimelineProposalId"] is None
    assert view["v1WorkflowNext"] == "confirm_timeline_alignment"
    alignment = view["v1TimelineAlignment"]
    assert alignment and alignment["cadence"]
    assert alignment["question"] == "更希望更快见成果,还是更稳打基础?"

    events = await _events(db, account)
    types = [e.event_type for e in events]
    assert "strategy_understanding_presented" in types
    assert "timeline_assumptions_presented" in types
    assert "timeline_alignment_question_asked" in types
    assert "coarse_timeline_draft_generated" not in types
    assert "timeline_proposal_created" not in types
    # 顺序:假设呈现在前。
    assert types.index("timeline_assumptions_presented") > types.index("strategy_confirmed")

    # 用户对齐节奏 -> 才生成粗时间线。
    align = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/agent/v1/timeline/align",
        params={"answer": "先快后稳", "accepted": "false"},
        headers=account.headers,
    )
    assert align.status_code == 200, align.text
    final = align.json()["reasoning"]
    assert final["v1Stage"] == "coarse_timeline_review"
    assert final["v01TimelineProposalId"]

    types = [e.event_type for e in await _events(db, account)]
    assert "timeline_alignment_answered" in types
    assert types.index("timeline_alignment_answered") < types.index("coarse_timeline_draft_generated")
    assert types.index("coarse_timeline_draft_generated") < types.index("timeline_proposal_created")


@pytest.mark.asyncio
async def test_accepting_default_cadence_records_accepted(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession, monkeypatch, use_reasoner
) -> None:
    monkeypatch.setattr(settings, "planning_v1", True)
    account = await make_account(workspace_title="默认节奏")
    reasoner = use_reasoner(FakeReasoner(reply="记下战略。", v1_source_kind="test"))
    await _drive_to_strategy_formed(app_client, account, reasoner, db)
    reasoner.v1_timeline = _timeline_draft()
    reasoner.v1_timeline_alignment = V1TimelineAlignmentDraft(
        summary="按每周一个闭环推进。", cadence="每周 1 个可验收小闭环", question=""
    )
    await app_client.post(
        f"/api/workspaces/{account.workspace_id}/agent/v1/strategy/confirm", headers=account.headers
    )
    align = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/agent/v1/timeline/align",
        params={"accepted": "true"},
        headers=account.headers,
    )
    assert align.status_code == 200, align.text
    types = [e.event_type for e in await _events(db, account)]
    assert "timeline_alignment_accepted" in types
    assert "timeline_alignment_question_asked" not in types
