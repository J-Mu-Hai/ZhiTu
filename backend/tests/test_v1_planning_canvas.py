"""规划智能体重构 V1 — P1/P2:阶段一固定框架的确定性验收。

固定案例:新建一个启用 V1 的空间,用户输入“我想学 Python”。

结构:三个分组是画布节点(PlanNode),分组下的固定项是**紫色画布问题节点**
(AgentQuestion),不进主画布、进入分组才看到。

覆盖:
1. 初始:只有根目标与干净画布;`v1Stage=initial_thinking`;零 reasoning 节点;
2. 提交目标:模型的整体判断 + 一个全局问题;不交代内部实现;
3. 三个分组是真实 `PlanNode`(根 + 3 组 = 4),各带 5 个画布问题节点;
4. 问题节点是 `canvas_question`、挂在对应分组下、带模型的可审阅判断;
5. 没有提案 / 时间线 / reasoning 节点;
6. 分组仍能像普通节点一样编辑;
7. 老空间 / 未开启 V1 时 `v1Stage` 为 None,行为与以前完全一样。
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agent.runtime.base import V1AssessmentDraft, V1NodeUpdate
from backend.core.config import settings
from backend.db.models import PlanNode, Proposal, ReasoningNode
from backend.tests.conftest import FakeReasoner


async def _turn(client: httpx.AsyncClient, account, key: str) -> dict:
    response = await client.post(
        f"/api/workspaces/{account.workspace_id}/agent/turn",
        json={"trigger": "space_entered", "idempotencyKey": key},
        headers=account.headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _send(client: httpx.AsyncClient, account, text: str, key: str) -> dict:
    response = await client.post(
        f"/api/workspaces/{account.workspace_id}/messages",
        json={"content": text, "clientMessageId": key},
        headers=account.headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _reasoning(client: httpx.AsyncClient, account) -> dict:
    response = await client.get(
        f"/api/workspaces/{account.workspace_id}/reasoning", headers=account.headers
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _plan(client: httpx.AsyncClient, account) -> dict:
    response = await client.get(
        f"/api/workspaces/{account.workspace_id}/plan", headers=account.headers
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _questions(client: httpx.AsyncClient, account) -> list[dict]:
    response = await client.get(
        f"/api/workspaces/{account.workspace_id}/questions?includeDecided=true",
        headers=account.headers,
    )
    assert response.status_code == 200, response.text
    return response.json()["questions"]


async def _plan_node_count(db: AsyncSession, account) -> int:
    return int(
        await db.scalar(
            select(func.count())
            .select_from(PlanNode)
            .where(PlanNode.workspace_id == uuid.UUID(account.workspace_id))
        )
        or 0
    )


def _root(nodes: list[dict]) -> dict:
    return next(node for node in nodes if node.get("parentId") in (None, ""))


@pytest.mark.asyncio
async def test_v1_fixed_groups_and_question_nodes(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession, monkeypatch, use_reasoner
) -> None:
    monkeypatch.setattr(settings, "planning_v1", True)
    monkeypatch.setattr(settings, "v01_planning", False)
    account = await make_account(workspace_title="我想学 Python")
    use_reasoner(
        FakeReasoner(
            reply="Python 是手段而不是成果。30 天后你想拿出什么具体成果?",
            v1_assessment=V1AssessmentDraft(
                global_assessment="Python 是手段而不是成果。",
                node_updates=(
                    V1NodeUpdate(
                        node_key="true_intent",
                        judgment="真实诉求还不明确。",
                        known_facts=("你写下的目标是:我想学 Python",),
                    ),
                ),
                focus_key="true_intent",
                focus_reason="它最影响路线。",
                question="30 天后你想拿出什么具体成果?",
            ),
        )
    )

    # ---- 1. 初始:干净画布,只有根目标 ----
    body = await _turn(app_client, account, "v1-1")
    view = body["reasoning"]
    assert view["v1Stage"] == "initial_thinking"
    assert view["nodes"] == [], "初始画布不能有任何 reasoning 节点"
    assert await _plan_node_count(db, account) == 1, "阶段一初始不能写业务节点"

    # ---- 2. 提交目标:模型的整体判断 + 一个全局问题,不交代内部实现 ----
    send = await _send(app_client, account, "我想学 Python", "v1-ans-1")
    reply = send["assistantMessage"]["content"]
    assert "Python" in reply
    assert "30 天后" in reply, reply
    assert "容器" not in reply and "三组" not in reply, reply

    view = await _reasoning(app_client, account)
    assert view["v1Stage"] == "goal_reframe"
    assert view["v1Question"] == "30 天后你想拿出什么具体成果?"
    assert view["v1Judgment"]
    assert view["nodes"] == []
    assert view["v01Timeline"] == []

    # ---- 3. 三个分组是真实 PlanNode:根 + 3 组 = 4 ----
    plan = await _plan(app_client, account)
    nodes = plan["nodes"]
    assert len(nodes) == 4, [node["title"] for node in nodes]
    root = _root(nodes)
    groups = [node for node in nodes if node["parentId"] == root["id"]]
    assert {node["title"] for node in groups} == {"目标重构", "问题结构", "战略路径"}
    by_title = {node["title"]: node for node in groups}
    assert all(node["nodeType"] == "capability" for node in groups)
    assert all(node["origin"] == "ai" for node in groups)
    assert all(node["purpose"] == "information" for node in groups)

    # ---- 4. 固定项是紫色画布问题节点,挂在对应分组下 ----
    questions = await _questions(app_client, account)
    assert len(questions) == 10, [q["question"] for q in questions]
    assert all(q["presentation"] == "canvas_question" for q in questions)
    by_source: dict[str, list[dict]] = {}
    for question in questions:
        by_source.setdefault(question["sourceNodeId"], []).append(question)
    assert len(by_source[by_title["目标重构"]["id"]]) == 5
    assert len(by_source[by_title["问题结构"]["id"]]) == 5
    intent = next(q for q in questions if q["v1Key"] == "true_intent")
    assert intent["v1Analysis"]["judgment"] == "真实诉求还不明确。"
    assert intent["v1Analysis"]["knownFacts"] == ["你写下的目标是:我想学 Python"]

    # ---- 5. 没有提案、没有 reasoning 节点 ----
    assert (
        await db.scalar(
            select(func.count())
            .select_from(Proposal)
            .where(Proposal.workspace_id == uuid.UUID(account.workspace_id))
        )
        == 0
    )
    assert view["sessionId"]
    assert (
        await db.scalar(
            select(func.count())
            .select_from(ReasoningNode)
            .where(ReasoningNode.session_id == uuid.UUID(view["sessionId"]))
        )
        == 0
    )

    # ---- 6. 分组仍能像普通节点一样编辑 ----
    group_id = by_title["目标重构"]["id"]
    edit = await app_client.patch(
        f"/api/workspaces/{account.workspace_id}/nodes/{group_id}",
        json={"description": "我把这条判断写进了这个节点的正文。"},
        headers=account.headers,
    )
    assert edit.status_code == 200, edit.text
    updated = await _plan(app_client, account)
    edited = next(node for node in updated["nodes"] if node["id"] == group_id)
    assert "写进了这个节点" in (edited["description"] or "")


@pytest.mark.asyncio
async def test_v1_disabled_leaves_spaces_untouched(
    app_client: httpx.AsyncClient, make_account, monkeypatch
) -> None:
    """未开启 V1 时,新空间 `v1Stage` 为 None,不进 V1 画布。"""
    monkeypatch.setattr(settings, "planning_v1", False)
    monkeypatch.setattr(settings, "v01_planning", False)
    account = await make_account(workspace_title="学点别的")

    body = await _turn(app_client, account, "plain-1")
    view = body["reasoning"]
    assert view["v1Stage"] is None
    assert view["nodes"] == []
