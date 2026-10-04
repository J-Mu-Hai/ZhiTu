"""V1 窄契约**战略合成回合**:解析 + 编排。

背景:通用 `v1_strategy` 回合自由度高,真实 DeepSeek-chat 会把白话写进
`keyDimensions`/`candidateDirections`,始终不产出四个战略结构字段。这里单开一个
只输出四条结构 + 取舍的回合,四条齐全才接受。
"""

from __future__ import annotations

import httpx
import pytest

from backend.agent.runtime.base import (
    V1AssessmentDraft,
    V1NodeUpdate,
    V1StrategyDraft,
)
from backend.agent.runtime.response import parse_v1_strategy
from backend.core.config import settings
from backend.tests.conftest import FakeReasoner


def test_parse_requires_all_four_structures() -> None:
    ok = parse_v1_strategy(
        {
            "mainLine": "先用最小项目闭环",
            "parallelLine": "并行看一点统计",
            "deferOrAvoid": "暂不系统学算法",
            "riskControl": "每两周复盘",
            "tradeoff": "先要能展示的成果",
        }
    )
    assert ok is not None
    assert ok.main_line == "先用最小项目闭环"
    assert ok.tradeoff == "先要能展示的成果"

    # 缺任一结构 -> 整份拒绝,不落半成品。
    assert (
        parse_v1_strategy(
            {
                "mainLine": "a",
                "parallelLine": "b",
                "deferOrAvoid": "c",
            }
        )
        is None
    )
    assert parse_v1_strategy({}) is None


def test_parse_accepts_snake_case_aliases() -> None:
    draft = parse_v1_strategy(
        {
            "main_line": "主线",
            "parallel_line": "并行",
            "defer_or_avoid": "暂缓",
            "risk_control": "风控",
            "strategy_tradeoff": "取舍",
        }
    )
    assert draft is not None
    assert (draft.main_line, draft.parallel_line, draft.defer_or_avoid, draft.risk_control) == (
        "主线",
        "并行",
        "暂缓",
        "风控",
    )
    assert draft.tradeoff == "取舍"


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


def _assessment(**overrides):
    base = {"strategic_thesis": "整体判断。", "question": ""}
    base.update(overrides)
    return V1AssessmentDraft(**base)


async def _establish_analyses(client, account, reasoner):
    """让四条战略成形维度全部有判断:goal_definition/key_conflict/hard_constraints/major_risks。"""
    reasoner.v1_assessment = _assessment(
        node_updates=(
            V1NodeUpdate(node_key="goal_definition", judgment="30 天做出可展示的项目。"),
            V1NodeUpdate(node_key="key_conflict", judgment="目标太大、反馈太慢。"),
            V1NodeUpdate(node_key="hard_constraints", judgment="每天只有 1 小时。"),
        )
    )
    await _turn(client, account, "syn-open")
    reasoner.v1_assessment = _assessment(
        node_updates=(V1NodeUpdate(node_key="major_risks", judgment="容易只看不做。"),)
    )
    await _send(client, account, "我还担心自己会只看教程不动手", "syn-1")


@pytest.mark.asyncio
async def test_goal_confirm_synthesizes_strategy_via_narrow_turn(
    app_client: httpx.AsyncClient, make_account, monkeypatch, use_reasoner
) -> None:
    monkeypatch.setattr(settings, "planning_v1", True)
    account = await make_account(workspace_title="战略合成")
    reasoner = use_reasoner(FakeReasoner(reply="记下。", v1_source_kind="test"))
    await _establish_analyses(app_client, account, reasoner)

    # 目标确认:通用回合没给结构,只剩兜底 CTA。
    reasoner.v1_assessment = _assessment()
    confirm = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/agent/v1/goal/confirm",
        headers=account.headers,
    )
    assert confirm.status_code == 200, confirm.text
    assert confirm.json()["reasoning"]["v1Stage"] == "problem_structure"

    # 兜底 CTA 触发窄契约战略合成回合。
    reasoner.v1_assessment = _assessment()
    reasoner.v1_strategy = V1StrategyDraft(
        main_line="先用最小项目闭环补齐 pandas",
        parallel_line="并行看一点统计基础",
        defer_or_avoid="暂不系统学算法",
        risk_control="每两周做一次复盘",
        tradeoff="先要能展示的成果",
    )
    cont = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/agent/v1/strategy/continue",
        headers=account.headers,
    )
    assert cont.status_code == 200, cont.text
    view = cont.json()["reasoning"]
    assert view["v1Stage"] == "strategy_draft"
    assert view["v1Strategy"]["mainLine"] == "先用最小项目闭环补齐 pandas"
    assert view["v1WorkflowNext"] == "confirm_strategy"
    assert view["v1Status"] != "failed"


@pytest.mark.asyncio
async def test_synthesis_contract_violation_is_retryable(
    app_client: httpx.AsyncClient, make_account, monkeypatch, use_reasoner
) -> None:
    monkeypatch.setattr(settings, "planning_v1", True)
    account = await make_account(workspace_title="契约违规")
    reasoner = use_reasoner(FakeReasoner(reply="记下。", v1_source_kind="test"))
    await _establish_analyses(app_client, account, reasoner)

    reasoner.v1_assessment = _assessment()
    await app_client.post(
        f"/api/workspaces/{account.workspace_id}/agent/v1/goal/confirm",
        headers=account.headers,
    )
    reasoner.v1_assessment = _assessment()
    reasoner.v1_strategy = None  # 模型只给白话、不给四条结构
    cont = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/agent/v1/strategy/continue",
        headers=account.headers,
    )
    assert cont.status_code == 200, cont.text
    view = cont.json()["reasoning"]
    assert view["v1Status"] == "failed"
    assert "四条战略结构" in (view["v1Error"] or "")
    assert not view["v1Strategy"], "不落半成品战略"
