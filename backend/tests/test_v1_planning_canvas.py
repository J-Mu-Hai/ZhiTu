"""规划智能体重构 V1 — P1:阶段一固定容器的确定性验收。

固定案例:新建一个启用 V1 的空间,用户输入“我想学 Python”。

覆盖:
1. 初始:只有根目标与干净画布;`v1Stage=initial_thinking`;零 reasoning 节点;
2. 提交目标:一段整体判断 + **一个**全局关键问题;**不交代内部实现**;
3. 固定容器是**真实 `PlanNode`**:根 + 3 组 + 10 个分析容器 = 14;
4. 三组直接挂在根下、各带 5 个固定分析容器(第三组为空战略容器);
5. 容器 `purpose=information`(不排期、不计完成度)、`origin=ai`;
6. 容器是**可直接进入的真实节点**:能被既有节点接口读取 / 编辑;
7. 全程不写时间线、不生成 proposal、不生成 reasoning 节点;
8. 老空间 / 未开启 V1 时 `v1Stage` 为 None,行为与以前完全一样。
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.config import settings
from backend.db.models import PlanNode, Proposal, ReasoningNode
from backend.services import v1_service


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
async def test_v1_fixed_containers_are_real_plan_nodes(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession, monkeypatch
) -> None:
    # V1 默认关闭(既有空间不变);这条确定性 Demo 显式打开。
    monkeypatch.setattr(settings, "planning_v1", True)
    monkeypatch.setattr(settings, "v01_planning", False)
    account = await make_account(workspace_title="我想学 Python")

    # ---- 1. 初始:干净画布,只有根目标 ----
    body = await _turn(app_client, account, "v1-1")
    view = body["reasoning"]
    assert view["v1Stage"] == "initial_thinking"
    assert view["nodes"] == [], "初始画布不能有任何 reasoning 节点"
    assert await _plan_node_count(db, account) == 1, "阶段一初始不能写业务节点"

    # ---- 2. 提交目标:整体判断 + 一个全局问题,不交代内部实现 ----
    send = await _send(app_client, account, "我想学 Python", "v1-ans-1")
    reply = send["assistantMessage"]["content"]
    assert "Python" in reply
    assert v1_service.GLOBAL_QUESTION in reply, reply
    assert "容器" not in reply and "三组" not in reply, reply

    view = await _reasoning(app_client, account)
    assert view["v1Stage"] == "goal_reframe"
    assert view["v1Question"] == v1_service.GLOBAL_QUESTION
    assert view["v1Judgment"]
    # 没有旧的问题地图/时间线。
    assert view["nodes"] == []
    assert view["v01Timeline"] == []

    # ---- 3. 固定容器是真实 PlanNode:根 + 3 组 + 10 分析 = 14 ----
    plan = await _plan(app_client, account)
    nodes = plan["nodes"]
    assert len(nodes) == 14, [node["title"] for node in nodes]
    root = _root(nodes)
    groups = [node for node in nodes if node["parentId"] == root["id"]]
    assert {node["title"] for node in groups} == {"目标重构", "问题结构", "战略路径"}
    by_title = {node["title"]: node for node in groups}
    assert all(node["nodeType"] == "capability" for node in groups)
    assert all(node["origin"] == "ai" for node in groups)
    assert all(node["purpose"] == "information" for node in groups)

    by_parent: dict[str | None, list[dict]] = {}
    for node in nodes:
        by_parent.setdefault(node["parentId"], []).append(node)

    # 前两组各挂 5 个固定分析容器;第三组是空的战略容器。
    assert len(by_parent[by_title["目标重构"]["id"]]) == 5
    assert len(by_parent[by_title["问题结构"]["id"]]) == 5
    assert by_title["战略路径"]["id"] not in by_parent
    assert {node["title"] for node in by_parent[by_title["目标重构"]["id"]]} == {
        "你现在在哪",
        "你真正想要什么",
        "这件事值得做吗",
        "真正卡你的是什么",
        "最后到底要做到什么",
    }
    # 分析容器带“待验证”的暂定判断,且都是信息用途。
    for node in by_parent[by_title["目标重构"]["id"]] + by_parent[by_title["问题结构"]["id"]]:
        assert node["purpose"] == "information"
        assert "（待验证）" in (node["description"] or "")

    # ---- 4. 没有提案、没有 reasoning 节点 ----
    proposals = await db.scalar(
        select(func.count())
        .select_from(Proposal)
        .where(Proposal.workspace_id == uuid.UUID(account.workspace_id))
    )
    assert proposals == 0, "P1 不允许生成任何提案"
    assert view["sessionId"]
    reasoning_count = await db.scalar(
        select(func.count())
        .select_from(ReasoningNode)
        .where(ReasoningNode.session_id == uuid.UUID(view["sessionId"]))
    )
    assert reasoning_count == 0, "V1 固定容器是 PlanNode,不是 reasoning 节点"

    # ---- 5. 固定容器是**可直接进入**的真实节点:分组有子节点,且能像普通节点一样编辑 ----
    group_id = by_title["目标重构"]["id"]
    edit = await app_client.patch(
        f"/api/workspaces/{account.workspace_id}/nodes/{group_id}",
        json={"description": "（待验证）我把这条判断写进了这个节点的正文。"},
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
