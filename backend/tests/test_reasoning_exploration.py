"""阶段 7 步骤 2:首次进入的幂等战略探索。

这一组验证**幂等、失败安全与结构约束**:
- 首次进入生成地图与一个高价值问题;连续进入不重复生成;
- 模型失败/结构不合法时**不写半成品**,可安全重试;
- 战略阶段由服务端拦住排期类问题;
- 根目标内容变化后才重新探索。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agent.runtime.base import (
    QuestionDraft,
    QuestionOptionDraft,
    ReasoningMapDraft,
    ReasoningMapNodeDraft,
    ReasoningResult,
)
from backend.agent.runtime.scripted import ScriptedConfigError, parse_script
from backend.db.models import (
    AgentQuestion,
    GoalReasoningSession,
    PlanNode,
    ReasoningNode,
    Workspace,
)
from backend.db.models.enums import ModelSource, ReasoningSessionStatus


def _map_draft(*, primary: int = 6, question: QuestionDraft | None = None) -> ReasoningMapDraft:
    nodes = tuple(
        ReasoningMapNodeDraft(
            handle=f"r{index}",
            title=f"决策维度 {index}",
            summary=f"关于维度 {index} 的摘要",
            importance=5 - (index % 3),
            uncertainty=4,
            urgency=1,
            impact=5,
            confidence=2,
            rationale=f"维度 {index} 会改变路线",
            assumptions=["一个假设"],
        )
        for index in range(1, primary + 1)
    )
    return ReasoningMapDraft(
        nodes=nodes,
        focus_handle="r1",
        focus_reason="它决定后面几条路线是否成立",
        phase="strategic_exploration",
        turn_action="ask_user",
    )


@dataclass
class MapReasoner:
    """可编程的假模型:返回带 `reasoning_map` 的结果,并记录调用次数。"""

    drafts: tuple[ReasoningMapDraft | None, ...] = ()
    degraded: bool = False
    questions: tuple[QuestionDraft, ...] = ()
    calls: list = field(default_factory=list)
    _index: int = 0

    async def reason(self, turn):
        self.calls.append(turn)
        if self.degraded:
            from backend.db.models.enums import DegradedReason

            return ReasoningResult(
                reply="模型不可用",
                source=ModelSource.UNAVAILABLE,
                degraded=True,
                degraded_reason=DegradedReason.MODEL_UNAVAILABLE,
                retryable=True,
            )
        draft = self.drafts[self._index] if self._index < len(self.drafts) else None
        self._index += 1
        return ReasoningResult(
            reply="我梳理了一张问题地图。",
            source=ModelSource.DIRECT_LLM,
            reasoning_map=draft,
            questions=self.questions,
        )


async def _enter(client: httpx.AsyncClient, account, key: str = "enter-1") -> dict:
    response = await client.post(
        f"/api/workspaces/{account.workspace_id}/agent/turn",
        json={"trigger": "space_entered", "idempotencyKey": key},
        headers=account.headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_space_entered_creates_map_and_is_idempotent(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    account = await make_account()
    reasoner = MapReasoner(drafts=(_map_draft(primary=6),))
    use_reasoner(reasoner)

    body = await _enter(app_client, account)
    assert body["changed"] is True
    assert len(body["reasoning"]["nodes"]) == 6
    assert body["reasoning"]["focusHandle"] == "r1"
    assert body["reasoning"]["focusReason"] == "它决定后面几条路线是否成立"

    # 第二次进入(同一输入):不重复建节点、不再跑模型。
    again = await _enter(app_client, account, key="enter-2")
    assert again["replayed"] is True
    assert len(again["reasoning"]["nodes"]) == 6
    assert len(reasoner.calls) == 1, "同一输入重复进入不该再跑模型"

    await db.rollback()
    session = await db.scalar(select(GoalReasoningSession))
    assert session is not None and session.status is ReasoningSessionStatus.READY
    node_count = await db.scalar(select(func.count()).select_from(ReasoningNode))
    assert node_count == 6


async def test_space_entered_failure_writes_no_half_map_then_retries(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    account = await make_account()
    reasoner = MapReasoner(degraded=True)
    use_reasoner(reasoner)

    body = await _enter(app_client, account)
    assert body["degraded"] is True
    assert body["reasoning"]["status"] == "failed"
    assert body["reasoning"]["nodes"] == []

    await db.rollback()
    assert await db.scalar(select(func.count()).select_from(ReasoningNode)) == 0

    # 换一个能用的 reasoner,重试应成功建立地图。
    good = MapReasoner(drafts=(_map_draft(primary=5),))
    use_reasoner(good)
    retry = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/agent/turn",
        json={"trigger": "retry", "idempotencyKey": "retry-1"},
        headers=account.headers,
    )
    assert retry.status_code == 200, retry.text
    retried = retry.json()
    assert retried["reasoning"]["status"] == "ready"
    assert len(retried["reasoning"]["nodes"]) == 5


async def test_structurally_invalid_map_is_rejected_atomically(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    account = await make_account()
    # 只有 2 个一级维度 —— 低于 MIN_PRIMARY_NODES,整份地图拒绝。
    reasoner = MapReasoner(drafts=(_map_draft(primary=2),))
    use_reasoner(reasoner)

    body = await _enter(app_client, account)
    assert body["reasoning"]["status"] == "failed"
    assert body["reasoning"]["nodes"] == []
    assert "一级决策维度" in (body["reasoning"]["error"] or "")

    await db.rollback()
    assert await db.scalar(select(func.count()).select_from(ReasoningNode)) == 0


async def test_strategy_phase_drops_scheduling_questions(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    account = await make_account()
    scheduling = QuestionDraft(
        question="你每周能投入多少小时?",
        why_now="排期需要",
        response_mode="free_text",
    )
    strategic = QuestionDraft(
        question="你更愿意把主要精力放在保研还是就业?",
        why_now="它决定路线",
        response_mode="single_select",
        options=(QuestionOptionDraft(id="a", label="保研"),),
    )
    reasoner = MapReasoner(drafts=(_map_draft(primary=4),), questions=(scheduling, strategic))
    use_reasoner(reasoner)

    await _enter(app_client, account)

    await db.rollback()
    rows = list((await db.execute(select(AgentQuestion))).scalars())
    assert [row.question for row in rows] == ["你更愿意把主要精力放在保研还是就业?"]
    # 问题挂到了焦点地图节点上。
    assert rows[0].reasoning_node_id is not None


async def test_root_change_triggers_a_new_exploration(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    account = await make_account()
    reasoner = MapReasoner(drafts=(_map_draft(primary=4), _map_draft(primary=5)))
    use_reasoner(reasoner)

    first = await _enter(app_client, account, key="enter-a")
    assert first["changed"] is True
    assert len(first["reasoning"]["nodes"]) == 4

    # 改根目标内容 + 推进空间版本 -> input_version 变了。
    root = await db.scalar(select(PlanNode).where(PlanNode.depth == 0))
    workspace = await db.scalar(select(Workspace).where(Workspace.id == root.workspace_id))
    root.content_version += 1
    workspace.current_revision_version += 1
    await db.commit()

    second = await _enter(app_client, account, key="enter-b")
    assert second["changed"] is True, "根目标内容变了之后应该重新探索"
    assert len(second["reasoning"]["nodes"]) == 5
    assert len(reasoner.calls) == 2


def test_scripted_reasoner_accepts_reasoning_map_fixture() -> None:
    turns = parse_script(
        '{"turns":[{"reply":"x","reasoningMap":{"focus":"r1","nodes":['
        '{"handle":"r1","title":"用途","nodeType":"dimension","importance":5}]}}]}'
    )
    assert turns[0]["reasoningMap"]["focus"] == "r1"

    with pytest.raises(ScriptedConfigError):
        parse_script('{"turns":[{"reply":"x","reasoningmap":{}}]}')
