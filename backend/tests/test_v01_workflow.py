"""规划智能体 V0.1:固定案例「30 天学习 Python」的脚本化端到端验收。

这是 V0.1 **唯一必须稳定通过**的 Demo。全程不依赖真实模型:V0.1 是程序控制的
确定性流程,内容由模板产出,状态由服务端状态机迁移。

覆盖:
1. 进入新目标 → DISCOVERY:洞察 + 2–4 个核心问题,零业务节点、零问题实体;
2. 用户回答一次 → 4–6 个第一层节点,进入 TIMELINE_DRAFT;
3. 推进 → 3–6 个阶段的时间线草案 → TIMELINE_REVIEW;
4. 确认(既有 proposal 链路)→ WEEKLY_EXECUTION,计划里出现阶段节点;
5. 推进 → 本周计划 + 下周预览 → 确认 → 出现 week / task 节点,且可追溯;
6. 反馈完成率 < 60% → REPLANNING;
7. 推进 → 未来调整提案 → 确认 → 回到 WEEKLY_EXECUTION,历史不被删。
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.config import settings
from backend.db.models import GoalReasoningSession, PlanNode
from backend.services import v01_service


async def _turn(client: httpx.AsyncClient, account, key: str) -> dict:
    response = await client.post(
        f"/api/workspaces/{account.workspace_id}/agent/turn",
        json={"trigger": "space_entered", "idempotencyKey": key},
        headers=account.headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _answer(client: httpx.AsyncClient, account, text: str, key: str) -> dict:
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


async def _open_proposal(client: httpx.AsyncClient, account) -> dict:
    response = await client.get(
        f"/api/workspaces/{account.workspace_id}/proposals", headers=account.headers
    )
    assert response.status_code == 200, response.text
    for proposal in response.json():
        if proposal["status"] in {"validated", "pending_confirmation"}:
            return proposal
    raise AssertionError("没有待确认的提案")


async def _confirm(client: httpx.AsyncClient, account, proposal_id: str, key: str) -> None:
    response = await client.post(
        f"/api/workspaces/{account.workspace_id}/proposals/{proposal_id}/confirm",
        json={"idempotencyKey": key},
        headers=account.headers,
    )
    assert response.status_code == 200, response.text


def _root(nodes: list[dict]) -> dict:
    return next(node for node in nodes if node.get("parentId") in (None, ""))


@pytest.mark.asyncio
async def test_v01_thirty_day_python_end_to_end(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession, monkeypatch
) -> None:
    # V0.1 默认关闭(保证既有 workspace / 既有测试不变);这条 Demo 显式打开。
    monkeypatch.setattr(settings, "v01_planning", True)
    account = await make_account(workspace_title="30 天学习 Python")

    # ---- 1. DISCOVERY:洞察 + 核心问题,零业务节点 ----
    body = await _turn(app_client, account, "v01-1")
    view = body["reasoning"]
    assert view["workflowStage"] == "discovery"
    assert 2 <= len(view["discoveryQuestions"]) <= 4, view["discoveryQuestions"]
    assert view["nodes"] == []
    assert body["message"] is not None and body["message"]["content"].strip()
    assert len((await _plan(app_client, account))["nodes"]) == 1, "阶段一不能写业务计划"

    # ---- 2. 回答一次 → 4–6 个第一层节点 ----
    await _answer(
        app_client,
        account,
        "我想在 30 天内用 Python 做出一个能展示的数据分析作品。",
        "v01-ans-1",
    )
    view = await _reasoning(app_client, account)
    assert view["workflowStage"] == "timeline_draft"
    stages = [node for node in view["nodes"] if node["nodeType"] == "stage"]
    assert 4 <= len(stages) <= 6, [n["title"] for n in stages]

    # ---- 3. 时间线草案 → TIMELINE_REVIEW ----
    body = await _turn(app_client, account, "v01-2")
    assert body["reasoning"]["workflowStage"] == "timeline_review", body["reasoning"].get("error")
    timeline = await _open_proposal(app_client, account)

    # ---- 4. 确认 → WEEKLY_EXECUTION + 阶段节点落库 ----
    await _confirm(app_client, account, timeline["id"], "v01-confirm-timeline")
    assert (await _reasoning(app_client, account))["workflowStage"] == "weekly_execution"
    plan = await _plan(app_client, account)
    root_id = _root(plan["nodes"])["id"]
    phases = [
        node
        for node in plan["nodes"]
        if node.get("parentId") == root_id and node.get("nodeType") == "stage"
    ]
    assert 4 <= len(phases) <= 6, [n["title"] for n in phases]

    # ---- 5. 本周计划 + 下周预览 ----
    await _turn(app_client, account, "v01-3")
    weekly = await _open_proposal(app_client, account)
    await _confirm(app_client, account, weekly["id"], "v01-confirm-weekly")
    plan = await _plan(app_client, account)
    by_id = {node["id"]: node for node in plan["nodes"]}
    weeks = [
        node for node in plan["nodes"] if str(node.get("title", "")).startswith(("本周计划", "下周预览"))
    ]
    tasks = [node for node in plan["nodes"] if node.get("nodeType") == "task"]
    assert weeks, "没有本周计划/下周预览节点"
    assert tasks, "没有可执行任务节点"
    # 可追溯:task -> week -> phase -> root
    for task in tasks:
        week = by_id[task["parentId"]]
        phase = by_id[week["parentId"]]
        assert by_id[phase["parentId"]]["id"] == root_id

    # ---- 6. 反馈:完成率 < 60% → REPLANNING ----
    for index, task in enumerate(tasks):
        response = await app_client.post(
            f"/api/workspaces/{account.workspace_id}/v01/feedback",
            json={"nodeId": task["id"], "outcome": "done" if index < 2 else "missed"},
            headers=account.headers,
        )
        assert response.status_code == 200, response.text
    assert (await _reasoning(app_client, account))["workflowStage"] == "replanning"

    # ---- 7. 重规划 → 确认 → 回到 WEEKLY_EXECUTION,未来区间后移 ----
    await _turn(app_client, account, "v01-4")
    replan = await _open_proposal(app_client, account)
    await _confirm(app_client, account, replan["id"], "v01-confirm-replan")
    after_view = await _reasoning(app_client, account)
    assert after_view["workflowStage"] == "weekly_execution"
    after = after_view["v01Timeline"]
    assert after and all(item["status"] == "planned" for item in after), [item["status"] for item in after]
    # 重规划提案说的确实是“调整未来阶段”,而不是重写历史。
    assert any(
        "重规划" in str(item.get("payload", {}).get("description", ""))
        for item in replan["items"]
    ), replan["items"]

    await db.rollback()
    sessions = list((await db.execute(select(GoalReasoningSession))).scalars())
    assert len(sessions) == 1
    assert sessions[0].workflow_stage is v01_service.PlanningWorkflowStage.WEEKLY_EXECUTION
    # 历史节点一个都没删。
    nodes = list((await db.execute(select(PlanNode))).scalars())
    assert len(nodes) >= 1 + len(phases) + len(weeks) + len(tasks)
