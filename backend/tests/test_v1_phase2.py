"""规划智能体重构 V1 — P2:AI 战略判断、因素筛选与战略路径的定向验收。

全程用 `FakeReasoner`(不联网):它返回的是 `V1AssessmentDraft` —— 与真实模型经
`response.parse_v1_assessment` 解析后的形状相同。测的是**服务端如何接受、如何写入**,
不是模型本身写得好不好。

覆盖:
1. 首轮:整体判断 + 一个关键问题;固定容器建立;无任务 / 时间线 / proposal;
2. 模型只能更新已存在的固定容器,未知键丢弃、数量夹上限(不制造节点爆炸);
3. 一轮至多一个全局问题;
4. 只更新焦点节点及声明受影响节点;
5. 不合格模型输出不部分落库,且是可重试状态;
6. 模型不可用时不伪造 AI 战略结论;
7. 信息足够时形成 ≤4 个战略子项,但确认前不写计划;
8. 确认战略后只进入 P3 准备状态,不生成时间线。
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
from backend.db.models.enums import DegradedReason
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


async def _plan_node_count(db: AsyncSession, account) -> int:
    return int(
        await db.scalar(
            select(func.count())
            .select_from(PlanNode)
            .where(PlanNode.workspace_id == uuid.UUID(account.workspace_id))
        )
        or 0
    )


def _by_key(plan: dict) -> dict[str, dict]:
    return {node["v1Key"]: node for node in plan["nodes"] if node.get("v1Key")}


def _assessment(**overrides) -> V1AssessmentDraft:
    base = {
        "global_assessment": "整体判断:这件事是手段,不是结果。",
        "question": "你希望最后拿出什么具体成果?",
    }
    base.update(overrides)
    return V1AssessmentDraft(**base)


@pytest.mark.asyncio
async def test_v1_first_turn_is_model_judgment(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession, monkeypatch, use_reasoner
) -> None:
    monkeypatch.setattr(settings, "planning_v1", True)
    monkeypatch.setattr(settings, "v01_planning", False)
    account = await make_account(workspace_title="我想学 Python")
    reasoner = use_reasoner(
        FakeReasoner(
            reply="Python 是手段而不是成果……30 天后你希望拿出什么具体成果?",
            v1_assessment=_assessment(
                global_assessment="Python 是手段而不是成果,四条路要求的最小能力不同。",
                node_updates=(
                    V1NodeUpdate(
                        node_key="true_intent",
                        judgment="真实诉求还不明确,可能为了求职,也可能为了做个作品。",
                        known_facts=("你写下的目标是:我想学 Python",),
                        assumptions=("（AI 假设）可能为了求职",),
                        importance_reason="真实意图决定后面的深度与成果。",
                        uncertainty="high",
                        status="discussing",
                    ),
                ),
                focus_key="true_intent",
                focus_reason="它比其他未知项更能改变路线。",
            ),
        )
    )

    # 初始:干净画布,不调用模型。
    body = await _turn(app_client, account, "v1-1")
    assert body["reasoning"]["v1Stage"] == "initial_thinking"
    assert reasoner.calls == [], "初始思考阶段不该调用模型"
    assert await _plan_node_count(db, account) == 1

    send = await _send(app_client, account, "我想学 Python", "v1-ans-1")
    assert "30 天后" in send["assistantMessage"]["content"]

    view = await _reasoning(app_client, account)
    assert view["v1Stage"] == "goal_reframe"
    assert view["v1FocusKey"] == "true_intent"
    assert view["v1Question"] == "你希望最后拿出什么具体成果?"
    assert "手段" in (view["v1Judgment"] or "")

    plan = await _plan(app_client, account)
    nodes = _by_key(plan)
    assert {"goal_reframe", "problem_structure", "strategy_path"} <= set(nodes)
    assert len(plan["nodes"]) == 14  # 根 + 3 组 + 10 容器
    intent = nodes["true_intent"]
    assert intent["v1Analysis"]["knownFacts"] == ["你写下的目标是:我想学 Python"]
    assert intent["v1Analysis"]["assumptions"] == ["（AI 假设）可能为了求职"]
    assert intent["v1Analysis"]["status"] == "discussing"

    # 没有正式计划写入,也没有 reasoning 节点。
    assert (
        await db.scalar(
            select(func.count())
            .select_from(Proposal)
            .where(Proposal.workspace_id == uuid.UUID(account.workspace_id))
        )
        == 0
    )
    assert view["v01Timeline"] == []
    assert (
        await db.scalar(
            select(func.count())
            .select_from(ReasoningNode)
            .where(ReasoningNode.session_id == uuid.UUID(view["sessionId"]))
        )
        == 0
    )
    # 送给模型的是 V1 回合,且提示词里没有真实 UUID/键以外的实现细节。
    assert reasoner.calls[-1].purpose == "v1_strategy"


@pytest.mark.asyncio
async def test_v1_updates_are_guarded(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession, monkeypatch, use_reasoner
) -> None:
    """未知键丢弃、数量夹上限、只更新声明受影响节点。"""
    monkeypatch.setattr(settings, "planning_v1", True)
    account = await make_account(workspace_title="我想学 Python")
    reasoner = use_reasoner(
        FakeReasoner(
            reply="先记下你现在的起点。",
            v1_assessment=_assessment(
                node_updates=(
                    V1NodeUpdate(node_key="current_state", judgment="起点不清。", status="discussing"),
                    V1NodeUpdate(node_key="true_intent", judgment="意图待定。", status="discussing"),
                    V1NodeUpdate(node_key="hacked_key", judgment="不应该被写入。"),
                    V1NodeUpdate(node_key="key_conflict", judgment="超出数量上限。"),
                ),
            ),
        )
    )
    await _turn(app_client, account, "v1-open")
    await _send(app_client, account, "我想学 Python", "v1-1")
    plan = await _plan(app_client, account)
    nodes = _by_key(plan)
    # 前两个合法 + 第三个是未知键(丢弃);第四个合法但超出上限(丢弃)。
    assert nodes["current_state"]["v1Analysis"]["judgment"] == "起点不清。"
    assert nodes["true_intent"]["v1Analysis"]["judgment"] == "意图待定。"
    assert nodes["key_conflict"]["v1Analysis"] is None
    # 未知键没有变成任何节点。
    assert "hacked_key" not in nodes
    assert len(plan["nodes"]) == 14, "未知键不应制造新节点"

    # 只更新焦点与声明受影响节点。
    reasoner.v1_assessment = _assessment(
        node_updates=(
            V1NodeUpdate(
                node_key="goal_definition",
                judgment="还没有可观察的成果定义。",
                impacted_node_keys=("current_state",),
            ),
            V1NodeUpdate(node_key="current_state", judgment="补充后的起点。"),
            V1NodeUpdate(node_key="major_risks", judgment="风险待识别。"),
        ),
    )
    await _send(app_client, account, "我想做出一个能展示的数据分析项目", "v1-2")
    plan = await _plan(app_client, account)
    nodes = _by_key(plan)
    assert nodes["goal_definition"]["v1Analysis"]["judgment"] == "还没有可观察的成果定义。"
    assert nodes["goal_definition"]["v1Analysis"]["impactedNodeKeys"] == ["current_state"]
    # 上一轮写过、这一轮没有声明的节点不被重写。
    assert nodes["true_intent"]["v1Analysis"]["judgment"] == "意图待定。"


@pytest.mark.asyncio
async def test_v1_invalid_and_unavailable_do_not_write(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession, monkeypatch, use_reasoner
) -> None:
    monkeypatch.setattr(settings, "planning_v1", True)
    account = await make_account(workspace_title="我想学 Python")
    reasoner = use_reasoner(FakeReasoner(reply="模型随口说了一句。", v1_assessment=None))
    await _turn(app_client, account, "v1-bad-open")
    send = await _send(app_client, account, "我想学 Python", "v1-bad")
    # 输出不合格:可重试、不落任何节点判断。
    assert send["assistantMessage"]["degraded"] is True
    view = await _reasoning(app_client, account)
    assert view["v1Status"] == "failed"
    assert "解析" in (view["v1Error"] or "")
    assert view["v1Judgment"] is None
    plan = await _plan(app_client, account)
    assert all(node["v1Analysis"] is None for node in plan["nodes"] if node.get("v1Key"))

    # 模型不可用:同样不伪造结论,但原因要准确(“不可用”,不是“输出不合格”)。
    reasoner.v1_assessment = None
    reasoner.degraded = True
    reasoner.degraded_reason = DegradedReason.MODEL_UNAVAILABLE
    reasoner.retryable = True
    reasoner.reply = "模型暂时不可用,可以再试一次。"
    await _send(app_client, account, "再试一次", "v1-unavailable")
    view = await _reasoning(app_client, account)
    assert view["v1Status"] == "failed"
    assert view["v1Judgment"] is None


@pytest.mark.asyncio
async def test_v1_strategy_draft_and_confirmation(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession, monkeypatch, use_reasoner
) -> None:
    monkeypatch.setattr(settings, "planning_v1", True)
    account = await make_account(workspace_title="我想学 Python")
    reasoner = use_reasoner(FakeReasoner(reply="先记下目标定义与矛盾。"))
    await _turn(app_client, account, "v1-s-open")
    reasoner.v1_assessment = _assessment(
        node_updates=(
            V1NodeUpdate(node_key="goal_definition", judgment="30 天内做出一个能展示的分析项目。"),
            V1NodeUpdate(node_key="key_conflict", judgment="目标太大,反馈太慢。"),
            V1NodeUpdate(node_key="hard_constraints", judgment="每天只有 1 小时。"),
        ),
    )
    await _send(app_client, account, "我想做出一个能展示的数据分析项目", "v1-s1")

    reasoner.v1_assessment = _assessment(
        strategy_ready=True,
        node_updates=(
            V1NodeUpdate(node_key="major_risks", judgment="容易陷入只看不做的教程循环。"),
            V1NodeUpdate(node_key="main_line", judgment="先用最小项目闭环补齐 pandas 与可视化。"),
            V1NodeUpdate(node_key="parallel_line", judgment="并行看一点统计基础,但不挤占主线。"),
        ),
        strategy_tradeoff="先要能展示的成果,而不是先把语法学全。",
    )
    await _send(app_client, account, "我更在意能拿出东西", "v1-s2")

    reasoner.v1_assessment = _assessment(
        strategy_ready=True,
        node_updates=(
            V1NodeUpdate(node_key="defer_or_avoid", judgment="暂不系统学算法与框架。"),
            V1NodeUpdate(node_key="risk_control", judgment="每两周做一次可展示的小复盘。"),
        ),
    )
    await _send(app_client, account, "可以", "v1-s3")

    plan = await _plan(app_client, account)
    nodes = _by_key(plan)
    assert {"main_line", "parallel_line", "defer_or_avoid", "risk_control"} <= set(nodes)
    view = await _reasoning(app_client, account)
    assert view["v1Stage"] == "strategy_draft"
    assert view["v1Strategy"]["mainLine"]
    assert view["v1Strategy"]["confirmed"] is False
    assert view["v01Timeline"] == []
    before = await _plan_node_count(db, account)

    # 确认:只进入 P3 准备状态,不生成时间线/阶段。
    confirm = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/agent/v1/strategy/confirm",
        headers=account.headers,
    )
    assert confirm.status_code == 200, confirm.text
    assert confirm.json()["reasoning"]["v1Stage"] == "strategy_confirmed_for_timeline"
    assert confirm.json()["reasoning"]["v1Strategy"]["confirmed"] is True
    assert confirm.json()["reasoning"]["v01Timeline"] == []
    assert await _plan_node_count(db, account) == before, "确认战略不应新增任何计划节点"
