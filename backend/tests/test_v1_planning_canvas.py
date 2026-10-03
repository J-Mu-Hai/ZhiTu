"""规划智能体重构 V1 — P1:阶段一画布与节点讨论的确定性验收。

固定案例:新建一个启用 V1 的空间,用户输入“我想学 Python”。

覆盖:
1. 初始:只有根目标与干净画布;`v1Stage=initial_thinking`;零 reasoning 节点;
2. 提交目标:一段整体判断 + **一个**全局关键问题;画布出现 3 组 + 10 个固定分析容器;
3. 前两组各挂 5 个固定节点,第三组是空的战略容器(显示“待形成战略路径”);
4. 默认折叠(节点都挂在分组下,不散乱平铺);
5. 点开“你真正想要什么”进入局部讨论;回答后**只更新该节点**;
6. 全程 `plan_nodes` 始终只有根目标 —— 不写任务 / 时间线 / 周计划;
7. 老空间 / 未开启 V1 时 `v1Stage` 为 None,行为与以前完全一样。
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


async def _turn(client: httpx.AsyncClient, account, key: str, **extra) -> dict:
    response = await client.post(
        f"/api/workspaces/{account.workspace_id}/agent/turn",
        json={"trigger": "space_entered", "idempotencyKey": key, **extra},
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


async def _plan_nodes(db: AsyncSession, account) -> list[PlanNode]:
    rows = await db.execute(
        select(PlanNode).where(PlanNode.workspace_id == uuid.UUID(account.workspace_id))
    )
    return list(rows.scalars())


def _by_key(view: dict) -> dict[str, dict]:
    return {node["v1Key"]: node for node in view["nodes"] if node.get("v1Key")}


@pytest.mark.asyncio
async def test_v1_canvas_and_node_discussion(
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
    assert view["v1Question"] is None and view["v1Judgment"] is None
    assert len(await _plan_nodes(db, account)) == 1, "阶段一不能写业务计划"

    # ---- 2. 提交目标:整体判断 + 一个全局问题 + 三组固定画布 ----
    send = await _send(app_client, account, "我想学 Python", "v1-ans-1")
    reply = send["assistantMessage"]["content"]
    assert "Python" in reply
    assert v1_service.GLOBAL_QUESTION in reply, reply
    # 只给一个问题:回复里不同时堆 5 个编号问题。
    assert reply.count("\n1.") == 0 and reply.count("\n2.") == 0

    view = await _reasoning(app_client, account)
    assert view["v1Stage"] == "goal_reframe"
    assert view["v1Question"] == v1_service.GLOBAL_QUESTION
    assert view["v1Judgment"]

    nodes = _by_key(view)
    # 三个一级分组,均连接根目标(parent_handle 为空)。
    groups = [n for n in view["nodes"] if n["v1Kind"] in ("group", "strategy")]
    assert len(groups) == 3, [n["title"] for n in groups]
    assert all(node["parentHandle"] in (None, "") for node in groups)
    assert {node["v1Key"] for node in groups} == {
        "goal_reframe",
        "problem_structure",
        "strategy_path",
    }

    # 前两组各挂 5 个固定分析容器。
    assert set(nodes) >= {
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
    goal_group = nodes["goal_reframe"]
    problem_group = nodes["problem_structure"]
    strategy_node = nodes["strategy_path"]
    assert strategy_node["v1Kind"] == "strategy"
    assert "待形成战略路径" in (strategy_node["summary"] or "")

    children_of = {}
    for node in view["nodes"]:
        if node["v1Kind"] == "analysis":
            children_of.setdefault(node["parentHandle"], []).append(node)
    assert len(children_of[goal_group["handle"]]) == 5
    assert len(children_of[problem_group["handle"]]) == 5
    assert strategy_node["handle"] not in children_of, "战略容器在 P1 必须是空的"

    # 分析节点初始是“待讨论”,判断明确标注“待验证”,不是用户事实。
    for node in children_of[goal_group["handle"]] + children_of[problem_group["handle"]]:
        assert node["status"] == "unexplored"
        assert node["summary"].startswith("（待验证）")
        assert node["v1Question"], node["v1Key"]

    # 全程没有写业务计划或提案。
    assert len(await _plan_nodes(db, account)) == 1
    proposals = await db.scalar(
        select(func.count()).select_from(Proposal).where(
            Proposal.workspace_id == uuid.UUID(account.workspace_id)
        )
    )
    assert proposals == 0, "P1 不允许生成任何提案"

    # ---- 3. 点开“你真正想要什么”进入局部讨论 ----
    intent_node = nodes["true_intent"]
    opened = await _turn(
        app_client,
        account,
        "v1-open",
        trigger="node_selected",
        reasoningHandle=intent_node["handle"],
    )
    assert opened["reasoning"]["focusHandle"] == intent_node["handle"]

    # ---- 4. 回答后只更新该节点 ----
    before = await _reasoning(app_client, account)
    before_current = _by_key(before)["current_state"]
    answer = await _turn(
        app_client,
        account,
        "v1-discuss",
        trigger="user_message",
        reasoningHandle=intent_node["handle"],
        message="我想做出一个能展示的数据分析小项目。",
    )
    after = answer["reasoning"]
    updated = _by_key(after)["true_intent"]
    assert updated["status"] == "resolved"
    assert any("数据分析小项目" in item for item in updated["evidence"])
    assert updated["v1Question"] is None
    assert updated["userDescription"] == "我想做出一个能展示的数据分析小项目。"
    # 其它节点没有被重建或改动。
    assert _by_key(after)["current_state"]["status"] == before_current["status"]
    assert _by_key(after)["current_state"]["handle"] == before_current["handle"]
    assert len(after["nodes"]) == len(before["nodes"])

    # 讨论回复必须说明“没有生成计划”。
    discuss_reply = answer["message"]["content"]
    assert "没有生成" in discuss_reply or "不生成" in discuss_reply

    # 讨论之后的计划里仍然只有根目标。
    plan = await _plan_nodes(db, account)
    assert len(plan) == 1 and plan[0].depth == 0

    # 数据库里也只是 reasoning 节点,且都在根目标下。
    reasoning_count = await db.scalar(
        select(func.count())
        .select_from(ReasoningNode)
        .where(ReasoningNode.session_id == uuid.UUID(before["sessionId"]))
    )
    assert reasoning_count == 13


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
