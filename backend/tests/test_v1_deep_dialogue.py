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
    V1TimelineAlignmentDraft,
)
from backend.agent.runtime.response import parse_v1_timeline_alignment
from backend.core.config import settings
from backend.db.models import AgentAuditEvent
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


async def _drive_to_strategy_formed(client, account, reasoner):
    reasoner.v1_assessment = _assessment()
    await _turn(client, account, "dd-open")
    reasoner.v1_assessment = _assessment(
        node_updates=(
            V1NodeUpdate(node_key="goal_definition", judgment="30 天做出可展示项目。"),
            V1NodeUpdate(node_key="key_conflict", judgment="目标太大、反馈太慢。"),
            V1NodeUpdate(node_key="hard_constraints", judgment="每天只有 1 小时。"),
        )
    )
    await _send(client, account, "我想做出一个能展示的项目", "dd-1")
    reasoner.v1_assessment = _assessment(
        strategy_ready=True,
        node_updates=(
            V1NodeUpdate(node_key="major_risks", judgment="容易只学不做。"),
            V1NodeUpdate(node_key="main_line", judgment="先用最小项目闭环。"),
            V1NodeUpdate(node_key="parallel_line", judgment="并行看一点统计。"),
        ),
        strategy_tradeoff="先要能展示的成果。",
    )
    await _send(client, account, "可以", "dd-2")
    reasoner.v1_assessment = _assessment(
        strategy_ready=True,
        node_updates=(
            V1NodeUpdate(node_key="defer_or_avoid", judgment="暂不系统学算法。"),
            V1NodeUpdate(node_key="risk_control", judgment="每两周复盘。"),
        ),
    )
    await _send(client, account, "继续", "dd-3")


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
async def test_strategy_confirm_enters_alignment_then_generates_timeline(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession, monkeypatch, use_reasoner
) -> None:
    monkeypatch.setattr(settings, "planning_v1", True)
    account = await make_account(workspace_title="时间架构共创")
    reasoner = use_reasoner(FakeReasoner(reply="记下战略。", v1_source_kind="test"))
    await _drive_to_strategy_formed(app_client, account, reasoner)

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
    await _drive_to_strategy_formed(app_client, account, reasoner)
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
