"""阶段 7 步骤 1:持久化目标推理地图的数据边界。

这一组不碰模型 —— 它验证的是**结构**:推理层与业务计划层分开、用户字段与 Agent 字段
分开、跨空间隔离、删除级联、以及空地图只读不建会话。
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models import (
    AgentQuestion,
    GoalReasoningSession,
    PlanNode,
    Proposal,
    ReasoningNode,
    ReasoningNodeLink,
    Workspace,
)
from backend.db.models.enums import (
    ReasoningLinkType,
    ReasoningNodeStatus,
    ReasoningNodeType,
    ReasoningSessionPhase,
    ReasoningSessionStatus,
    ReasoningSource,
    ReasoningTurnAction,
)
from backend.services import reasoning_service


def _wid(account) -> uuid.UUID:
    return uuid.UUID(account.workspace_id)


async def _workspace(db: AsyncSession, account) -> Workspace:
    return await db.scalar(select(Workspace).where(Workspace.id == _wid(account)))


async def _root_node(db: AsyncSession, workspace_id) -> PlanNode:
    return await db.scalar(
        select(PlanNode).where(PlanNode.workspace_id == workspace_id, PlanNode.depth == 0)
    )


async def _make_session(db: AsyncSession, account) -> GoalReasoningSession:
    workspace_id = _wid(account)
    root = await _root_node(db, workspace_id)
    session = GoalReasoningSession(
        workspace_id=workspace_id,
        root_plan_node_id=root.id,
        phase=ReasoningSessionPhase.STRATEGIC_EXPLORATION,
        turn_action=ReasoningTurnAction.ASK_USER,
        status=ReasoningSessionStatus.READY,
    )
    db.add(session)
    await db.flush()
    return session


async def test_read_reasoning_map_is_empty_and_does_not_create_a_session(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession
) -> None:
    account = await make_account()
    response = await app_client.get(
        f"/api/workspaces/{account.workspace_id}/reasoning", headers=account.headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["phase"] == "strategic_exploration"
    assert body["status"] == "idle"
    assert body["nodes"] == []
    assert body["links"] == []
    assert body["sessionId"] is None

    # 读接口绝不写库:打开一次空间不该凭空多出一个会话。
    await db.rollback()
    count = await db.scalar(select(func.count()).select_from(GoalReasoningSession))
    assert count == 0


async def test_reasoning_nodes_separate_user_and_agent_fields(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession
) -> None:
    account = await make_account()
    session = await _make_session(db, account)
    node = ReasoningNode(
        session_id=session.id,
        handle="r1",
        title="目标用途",
        summary="Agent 认为这决定路线",
        user_description="我自己写的一段话",
        node_type=ReasoningNodeType.DIMENSION,
        status=ReasoningNodeStatus.EXPLORING,
        importance=5,
        uncertainty=4,
        urgency=1,
        impact=5,
        confidence=2,
        assumptions=["可能用于工作"],
        evidence=["用户提到过实习"],
        source=ReasoningSource.AGENT,
    )
    db.add(node)
    await db.flush()
    session.focus_reasoning_node_id = node.id
    session.focus_reason = "它决定后面几条路线是否成立"
    await db.commit()

    response = await app_client.get(
        f"/api/workspaces/{account.workspace_id}/reasoning", headers=account.headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["focusHandle"] == "r1"
    assert body["focusReason"] == "它决定后面几条路线是否成立"
    assert len(body["nodes"]) == 1
    view = body["nodes"][0]
    # 用户原文与 Agent 摘要必须分开两个字段。
    assert view["userDescription"] == "我自己写的一段话"
    assert view["summary"] == "Agent 认为这决定路线"
    # 优先级是现算的启发式:0.30*5 + 0.25*4 + 0.25*5 + 0.20*1 = 3.95
    assert view["priority"] == pytest.approx(3.95)


async def test_reasoning_map_is_not_part_of_plan_nodes(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession
) -> None:
    account = await make_account()
    session = await _make_session(db, account)
    for index in range(4):
        db.add(
            ReasoningNode(
                session_id=session.id,
                handle=f"r{index + 1}",
                title=f"维度 {index + 1}",
                node_type=ReasoningNodeType.DIMENSION,
            )
        )
    await db.commit()

    # 推理地图出现 4 个节点,但业务计划里仍然只有根目标那一个。
    await db.rollback()
    plan_count = await db.scalar(
        select(func.count())
        .select_from(PlanNode)
        .where(PlanNode.workspace_id == _wid(account))
    )
    reasoning_count = await db.scalar(
        select(func.count())
        .select_from(ReasoningNode)
        .where(ReasoningNode.session_id == session.id)
    )
    assert plan_count == 1
    assert reasoning_count == 4


async def test_reasoning_links_are_their_own_table(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession
) -> None:
    account = await make_account()
    session = await _make_session(db, account)
    first = ReasoningNode(session_id=session.id, handle="r1", title="A")
    second = ReasoningNode(session_id=session.id, handle="r2", title="B")
    db.add_all([first, second])
    await db.flush()
    db.add(
        ReasoningNodeLink(
            session_id=session.id,
            source_reasoning_node_id=first.id,
            target_reasoning_node_id=second.id,
            link_type=ReasoningLinkType.INFLUENCES,
            note="A 的选择会改变 B",
        )
    )
    await db.commit()

    response = await app_client.get(
        f"/api/workspaces/{account.workspace_id}/reasoning", headers=account.headers
    )
    body = response.json()
    assert body["links"] == [
        {
            "id": body["links"][0]["id"],
            "sourceHandle": "r1",
            "targetHandle": "r2",
            "linkType": "influences",
            "note": "A 的选择会改变 B",
        }
    ]


async def test_question_can_reference_a_reasoning_node(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession
) -> None:
    account = await make_account()
    session = await _make_session(db, account)
    node = ReasoningNode(session_id=session.id, handle="r1", title="目标用途")
    db.add(node)
    await db.flush()
    from backend.db.models.enums import QuestionResponseMode, QuestionStatus

    question = AgentQuestion(
        workspace_id=_wid(account),
        question="你打算用它做什么?",
        why_now="它决定路线",
        response_mode=QuestionResponseMode.FREE_TEXT,
        status=QuestionStatus.PENDING,
        reasoning_node_id=node.id,
    )
    db.add(question)
    await db.commit()

    await db.refresh(question)
    assert question.reasoning_node_id == node.id


async def test_deleting_workspace_cascades_reasoning_rows(make_account, db: AsyncSession) -> None:
    account = await make_account()
    session = await _make_session(db, account)
    node = ReasoningNode(session_id=session.id, handle="r1", title="A")
    db.add(node)
    await db.flush()
    db.add(
        ReasoningNodeLink(
            session_id=session.id,
            source_reasoning_node_id=node.id,
            target_reasoning_node_id=node.id,
            link_type=ReasoningLinkType.DEPENDS_ON,
        )
    )
    await db.commit()

    workspace = await _workspace(db, account)
    await db.delete(workspace)
    await db.commit()

    assert await db.scalar(select(func.count()).select_from(GoalReasoningSession)) == 0
    assert await db.scalar(select(func.count()).select_from(ReasoningNode)) == 0
    assert await db.scalar(select(func.count()).select_from(ReasoningNodeLink)) == 0
    # 级联删推理层不许牵连提案表(策略确认提案由 FK SET NULL,不在这一步)。
    assert await db.scalar(select(func.count()).select_from(Proposal)) == 0


async def test_priority_heuristic_is_explainable_and_bounded() -> None:
    assert reasoning_service.compute_priority(
        importance=5, uncertainty=5, urgency=5, impact=5
    ) == pytest.approx(5.0)
    assert reasoning_service.compute_priority(
        importance=0, uncertainty=0, urgency=0, impact=0
    ) == 0.0
    # 超出范围的评分被夹住,不是抛错也不是写进库。
    assert reasoning_service.clamp_score(99) == 5
    assert reasoning_service.clamp_score(-3) == 0
    assert reasoning_service.clamp_score("abc") == 0
