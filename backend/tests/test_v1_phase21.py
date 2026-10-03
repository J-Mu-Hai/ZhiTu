"""规划智能体重构 V1 — P2.1:战略判断优先、停止问卷式追问。

回归案例来自一次真实失败样本(`agent-audit.json`):AI 已判断出「用户怕的是投入
不能兑现」,却仍连续五轮换着问法追问,并把「人工整你」当成用户事实。

全程用 `FakeReasoner`,不联网。覆盖:战略判断优先、零问题、候选方向、同焦点/总量
守卫、元对话不进事实、节点只在有实质变化时更新、discussionCount 只统计真实讨论、
审计可导出。
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
)
from backend.core.config import settings
from backend.db.models import AgentAuditEvent
from backend.tests.conftest import FakeReasoner


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


async def _export(client, account):
    r = await client.get(
        f"/api/workspaces/{account.workspace_id}/agent/v1/audit-export?format=json",
        headers=account.headers,
    )
    assert r.status_code == 200, r.text
    return json.loads(r.text)


def _assessment(**overrides):
    base = {"strategic_thesis": "你真正面对的是「投入能否兑现价值」。", "response_mode": "none"}
    base.update(overrides)
    return V1AssessmentDraft(**base)


async def _start_v1(client, account, reasoner, key="t"):
    await _turn(client, account, f"{key}-open")


@pytest.mark.asyncio
async def test_v1_thesis_first_and_zero_question(
    app_client: httpx.AsyncClient, make_account, monkeypatch, use_reasoner
) -> None:
    monkeypatch.setattr(settings, "planning_v1", True)
    monkeypatch.setattr(settings, "agent_audit_export", True)
    account = await make_account(workspace_title="python 学习计划")
    reasoner = use_reasoner(FakeReasoner(reply="先给判断。"))
    await _start_v1(app_client, account, reasoner)

    # 首轮:战略判断 + 2 个关键维度 + 1 个关键问题。
    reasoner.v1_assessment = _assessment(
        key_dimensions=(
            V1KeyDimension(key="true_intent", judgment="可能是为了兑现价值", why_it_matters="决定路线"),
            V1KeyDimension(key="key_conflict", judgment="不是学不会,是怕白学", why_it_matters="决定是否值得投入"),
        ),
        critical_question="你更接近已有方向、只是不确定 Python 值不值得投入,还是方向也没想清楚?",
        response_mode="ask",
        focus_key="true_intent",
        focus_reason="它决定整条路线。",
    )
    await _send(app_client, account, "我想学 Python,但担心学了没用", "t1")
    view = await _reasoning(app_client, account)
    assert "兑现" in (view["v1StrategicThesis"] or "")
    assert view["v1Question"] == "你更接近已有方向、只是不确定 Python 值不值得投入,还是方向也没想清楚?"
    assert view["v1FocusKey"] == "true_intent"
    questions = await _questions(app_client, account)
    assert questions["true_intent"]["v1Analysis"]["judgment"] == "可能是为了兑现价值"
    assert questions["key_conflict"]["v1Analysis"]["judgment"] == "不是学不会,是怕白学"

    # 没有真正分叉时允许零问题。
    reasoner.v1_assessment = _assessment(
        key_dimensions=(V1KeyDimension(key="value_assessment", judgment="值得做,但先做最小闭环"),),
        response_mode="none",
    )
    await _send(app_client, account, "我是学生,主要想用在课程和作业上", "t2")
    view = await _reasoning(app_client, account)
    assert view["v1Question"] is None, "没有真正的战略分叉就不该提问"


@pytest.mark.asyncio
async def test_v1_low_info_forces_candidate_directions(
    app_client: httpx.AsyncClient, make_account, monkeypatch, use_reasoner
) -> None:
    monkeypatch.setattr(settings, "planning_v1", True)
    monkeypatch.setattr(settings, "agent_audit_export", True)
    account = await make_account(workspace_title="候选方向")
    reasoner = use_reasoner(FakeReasoner(reply="先给判断。"))
    await _start_v1(app_client, account, reasoner)

    reasoner.v1_assessment = _assessment(
        critical_question="你想用 Python 做什么?",
        response_mode="ask",
        focus_key="true_intent",
    )
    await _send(app_client, account, "我想学 Python", "l1")
    reasoner.v1_assessment = _assessment(
        critical_question="你日常主要做什么?",
        response_mode="ask",
        focus_key="current_state",
    )
    await _send(app_client, account, "不知道", "l2")
    # 第二次「不知道」:服务端必须强制候选方向,不再提问。
    reasoner.v1_assessment = _assessment(
        critical_question="那你具体被什么困扰?",
        response_mode="ask",
        focus_key="key_conflict",
        candidate_directions=(
            V1CandidateDirection(key="research", title="科研 / AI 基础", reason="能接上课程与实验", path="数据处理 → 实验"),
            V1CandidateDirection(key="project", title="独立项目能力", reason="成果最容易验证", path="小工具 → 展示"),
            V1CandidateDirection(key="efficiency", title="通用效率能力", reason="立刻可用", path="自动化 → 日常事务"),
        ),
    )
    await _send(app_client, account, "不知道", "l3")
    view = await _reasoning(app_client, account)
    assert view["v1Question"] is None, "连续低信息后不得继续追问"
    assert view["v1CandidateDirections"] and len(view["v1CandidateDirections"]) == 3
    exported = await _export(app_client, account)
    types = [event["eventType"] for event in exported["events"]]
    assert "candidate_directions_offered" in types
    # 候选方向仍可选择
    select = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/agent/v1/direction/select?key=project",
        headers=account.headers,
    )
    assert select.status_code == 200, select.text
    assert select.json()["reasoning"]["v1SelectedDirection"] == "project"


@pytest.mark.asyncio
async def test_v1_question_budget_and_no_repeat_focus(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession, monkeypatch, use_reasoner
) -> None:
    monkeypatch.setattr(settings, "planning_v1", True)
    monkeypatch.setattr(settings, "agent_audit_export", True)
    account = await make_account(workspace_title="追问预算")
    reasoner = use_reasoner(FakeReasoner(reply="先给判断。"))
    await _start_v1(app_client, account, reasoner)
    focuses = ["true_intent", "current_state", "key_conflict", "goal_definition"]
    for index, focus in enumerate(focuses):
        reasoner.v1_assessment = _assessment(
            critical_question=f"第 {index} 个问题?",
            response_mode="ask",
            focus_key=focus,
        )
        await _send(app_client, account, f"这是我第 {index} 条有实质内容的回答", f"b{index}")

    events = await _audit_events(db, account)
    asked = [event for event in events if event.event_type == "global_question_asked"]
    assert len(asked) == 3, "阶段一全局追问预算最多 3 次"
    asked_focus = [event.focus_key for event in asked]
    assert len(set(asked_focus)) == len(asked_focus), "同一焦点不得连续重复提问"

    # 同一焦点连续两次也要被拦下。
    account2 = await make_account(workspace_title="同焦点", email="same@example.com")
    reasoner.v1_assessment = _assessment(
        critical_question="第一个问题?", response_mode="ask", focus_key="true_intent"
    )
    await _start_v1(app_client, account2, reasoner, key="s")
    await _send(app_client, account2, "我想学 Python 做数据分析", "s1")
    reasoner.v1_assessment = _assessment(
        critical_question="换一种问法再问一次?", response_mode="ask", focus_key="true_intent"
    )
    await _send(app_client, account2, "我还是想做数据分析项目", "s2")
    events2 = [
        event for event in await _audit_events(db, account2) if event.event_type == "global_question_asked"
    ]
    assert len(events2) == 1, "同一 focusKey 连续提问最多一次"


@pytest.mark.asyncio
async def test_v1_meta_conversation_is_not_a_fact(
    app_client: httpx.AsyncClient, make_account, monkeypatch, use_reasoner
) -> None:
    monkeypatch.setattr(settings, "planning_v1", True)
    monkeypatch.setattr(settings, "agent_audit_export", True)
    account = await make_account(workspace_title="元对话")
    reasoner = use_reasoner(FakeReasoner(reply="先给判断。"))
    await _start_v1(app_client, account, reasoner)

    reasoner.v1_assessment = _assessment(
        node_updates=(
            V1NodeUpdate(
                node_key="current_state",
                judgment="身份未知",
                known_facts=("用户走神了", "人工整你", "用户是学生"),
            ),
        ),
        response_mode="none",
    )
    await _send(app_client, account, "你走神了", "m1")
    questions = await _questions(app_client, account)
    facts = questions["current_state"]["v1Analysis"]["knownFacts"]
    assert "用户走神了" not in facts and "人工整你" not in facts
    assert "用户是学生" not in facts, "元对话轮次不写事实"

    # 有实质内容 + 战略事实时才写入。
    reasoner.v1_assessment = _assessment(
        node_updates=(
            V1NodeUpdate(node_key="current_state", judgment="学生", known_facts=("用户是学生",)),
        ),
        response_mode="none",
    )
    await _send(app_client, account, "我是学生,在学校", "m2")
    questions = await _questions(app_client, account)
    assert "用户是学生" in questions["current_state"]["v1Analysis"]["knownFacts"]


@pytest.mark.asyncio
async def test_v1_node_updates_only_on_real_change_and_discussion_count(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession, monkeypatch, use_reasoner
) -> None:
    monkeypatch.setattr(settings, "planning_v1", True)
    monkeypatch.setattr(settings, "agent_audit_export", True)
    account = await make_account(workspace_title="节点更新")
    reasoner = use_reasoner(FakeReasoner(reply="先给判断。"))
    await _start_v1(app_client, account, reasoner)

    update = V1NodeUpdate(node_key="true_intent", judgment="为了兑现价值", known_facts=("用户是学生",))
    reasoner.v1_assessment = _assessment(node_updates=(update,), focus_key="true_intent")
    await _send(app_client, account, "我是学生,想提升技能", "u1")
    events = [
        event for event in await _audit_events(db, account) if event.event_type == "node_analysis_updated"
    ]
    assert len(events) == 1

    # 同一份内容再来一次:不应再写、不应再发事件。
    await _send(app_client, account, "我确实是学生", "u2")
    events = [
        event for event in await _audit_events(db, account) if event.event_type == "node_analysis_updated"
    ]
    assert len(events) == 1, "内容未变不应每轮机械重写"

    # 回答紫色问题:只有该节点 discussionCount +1。
    questions = await _questions(app_client, account)
    before = questions["true_intent"]["v1Analysis"]["discussionCount"]
    reasoner.v1_assessment = _assessment(
        node_updates=(V1NodeUpdate(node_key="true_intent", judgment="为了在科研里用上"),),
        focus_key="true_intent",
    )
    answer = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/questions/{questions['true_intent']['id']}/answer",
        json={"selectedOptionIds": [], "customInput": "我想用在科研数据分析上", "clientAnswerId": "disc-answer-1"},
        headers=account.headers,
    )
    assert answer.status_code == 200, answer.text
    questions = await _questions(app_client, account)
    assert questions["true_intent"]["v1Analysis"]["discussionCount"] == before + 1
    key_conflict_analysis = questions["key_conflict"]["v1Analysis"]
    assert not key_conflict_analysis or key_conflict_analysis.get("discussionCount", 0) == 0, "全局对话不虚增其他节点"


@pytest.mark.asyncio
async def test_v1_audit_records_classification_and_question_context(
    app_client: httpx.AsyncClient, make_account, monkeypatch, use_reasoner
) -> None:
    monkeypatch.setattr(settings, "planning_v1", True)
    monkeypatch.setattr(settings, "agent_audit_export", True)
    account = await make_account(workspace_title="审计补全")
    reasoner = use_reasoner(FakeReasoner(reply="先给判断。"))
    await _start_v1(app_client, account, reasoner)
    reasoner.v1_assessment = _assessment(
        critical_question="你想用 Python 做什么?",
        response_mode="ask",
        focus_key="true_intent",
        focus_reason="它决定整条路线。",
    )
    await _send(app_client, account, "我想学 Python", "audit-1")
    exported = await _export(app_client, account)
    events = exported["events"]
    received = next(e for e in events if e["eventType"] == "user_message_received")
    assert received["payload"]["classification"] in {"strategic_fact", "user_preference"}
    asked = next(e for e in events if e["eventType"] == "global_question_asked")
    assert asked["focus"] and asked["focus"]["key"] == "true_intent"
    assert asked["focus"]["reason"] == "它决定整条路线。"
    assert asked["stageBefore"] == "initial_thinking"
    assert asked["stageAfter"] == "goal_reframe"
    assert any(e["eventType"] == "strategic_thesis_generated" for e in events)


@pytest.mark.asyncio
async def test_v1_real_failure_fixture_does_not_loop(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession, monkeypatch, use_reasoner
) -> None:
    """等价于真实失败样本:模型一直换着问法追问,服务端必须把循环封死。"""
    monkeypatch.setattr(settings, "planning_v1", True)
    monkeypatch.setattr(settings, "agent_audit_export", True)
    account = await make_account(workspace_title="python学习计划")
    reasoner = use_reasoner(FakeReasoner(reply="先给判断。"))
    await _start_v1(app_client, account, reasoner)

    user_turns = [
        "因为想要提升自己的技能，得到技能，担心学了之后没有用",
        "不知道",
        "我是学生",
        "你走神了",
        "人工整你",
    ]
    focuses = ["true_intent", "true_intent", "current_state", "key_conflict", "key_conflict"]
    for index, (text, focus) in enumerate(zip(user_turns, focuses, strict=True)):
        reasoner.v1_assessment = _assessment(
            critical_question=f"那你具体是什么情况({index})?",
            response_mode="ask",
            focus_key=focus,
            candidate_directions=(
                V1CandidateDirection(key="research", title="科研 / AI 基础"),
                V1CandidateDirection(key="project", title="独立项目能力"),
                V1CandidateDirection(key="efficiency", title="通用效率能力"),
            ),
        )
        await _send(app_client, account, text, f"fixture-{index}")

    events = await _audit_events(db, account)
    asked = [event for event in events if event.event_type == "global_question_asked"]
    assert len(asked) <= 3, "不得再出现五轮追问循环"
    # 元对话没有进入任何节点事实。
    questions = await _questions(app_client, account)
    for question in questions.values():
        facts = (question["v1Analysis"] or {}).get("knownFacts", [])
        assert all("走神" not in fact and "人工整" not in fact for fact in facts)
    assert (await _reasoning(app_client, account))["v1Question"] is None
