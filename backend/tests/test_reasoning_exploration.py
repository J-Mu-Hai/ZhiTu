"""阶段 7 步骤 2 / 阶段 8:首次进入的路线优先战略梳理。

这一组验证**幂等、失败安全与结构约束**:
- 首次进入生成**路线图**(1 条 route + 3–5 个 stage)与一个高价值问题;连续进入不重复生成;
- **新首轮只接受路线图**;旧的散乱一级维度形状整份拒绝、不写半成品、可安全重试;
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


def _roadmap_draft(
    *,
    stages: int = 4,
    route_title: str = "推荐路线:约 10 周",
    question: QuestionDraft | None = None,
) -> ReasoningMapDraft:
    """首轮路线图:一条顶层 route + `stages` 个挂在它下面的 stage。"""
    nodes: list[ReasoningMapNodeDraft] = [
        ReasoningMapNodeDraft(
            handle="r1",
            title=route_title,
            node_type="route",
            summary="先打通最小闭环,再补统计与可视化",
            timeframe="约 10 周",
            deliverable="一个可展示的项目",
            pass_criteria="能端到端做完并讲清结论",
            importance=5,
            uncertainty=2,
            urgency=1,
            impact=5,
            confidence=3,
            rationale="它决定阶段顺序",
        )
    ]
    for index in range(1, stages + 1):
        nodes.append(
            ReasoningMapNodeDraft(
                handle=f"r{index + 1}",
                title=f"阶段 {index}",
                node_type="stage",
                parent_handle="r1",
                summary=f"阶段 {index} 的摘要",
                timeframe=f"约 {index + 1} 周",
                deliverable=f"成果 {index}",
                pass_criteria=f"通过标准 {index}",
                importance=4,
                uncertainty=3,
                urgency=1,
                impact=4,
                confidence=2,
                rationale=f"它决定第 {index + 1} 步",
            )
        )
    return ReasoningMapDraft(
        nodes=tuple(nodes),
        focus_handle="r2",
        focus_reason="第一个阶段最该先定下来",
        phase="roadmap_draft",
        turn_action="ask_user",
    )


def _legacy_dimension_draft(primary: int = 4) -> ReasoningMapDraft:
    """阶段 7 的旧形状(散乱一级维度)。**新首轮必须拒绝它。**"""
    return ReasoningMapDraft(
        nodes=tuple(
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
            )
            for index in range(1, primary + 1)
        ),
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
            reply="我先给一条推荐路线与阶段。",
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


async def test_space_entered_creates_roadmap_and_is_idempotent(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    account = await make_account()
    reasoner = MapReasoner(drafts=(_roadmap_draft(stages=4),))
    use_reasoner(reasoner)

    body = await _enter(app_client, account)
    assert body["changed"] is True
    assert body["reasoning"]["phase"] == "roadmap_draft"
    # 1 条路线 + 4 个阶段。
    assert len(body["reasoning"]["nodes"]) == 5
    assert body["reasoning"]["focusHandle"] == "r2"

    # 第二次进入(同一输入):不重复建节点、不再跑模型。
    again = await _enter(app_client, account, key="enter-2")
    assert again["replayed"] is True
    assert len(again["reasoning"]["nodes"]) == 5
    assert len(reasoner.calls) == 1, "同一输入重复进入不该再跑模型"

    await db.rollback()
    session = await db.scalar(select(GoalReasoningSession))
    assert session is not None and session.status is ReasoningSessionStatus.READY
    node_count = await db.scalar(select(func.count()).select_from(ReasoningNode))
    assert node_count == 5


async def test_first_turn_legacy_dimension_map_is_rejected(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    """**路线优先的核心回归**:新首轮给旧的散乱一级维度图,整份拒绝、不写半成品。"""
    account = await make_account()
    use_reasoner(MapReasoner(drafts=(_legacy_dimension_draft(primary=6), _legacy_dimension_draft(primary=6))))

    body = await _enter(app_client, account)
    assert body["changed"] is False
    assert body["reasoning"]["status"] == "failed"
    assert body["reasoning"]["nodes"] == []
    error = body["reasoning"]["error"] or ""
    assert "战略路线" in error or "阶段" in error, error

    await db.rollback()
    assert await db.scalar(select(func.count()).select_from(ReasoningNode)) == 0

    # 拒绝之后换成路线图,重试应当成功 —— 拒绝是安全的,不是死局。
    good = MapReasoner(drafts=(_roadmap_draft(stages=3),))
    use_reasoner(good)
    retry = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/agent/turn",
        json={"trigger": "retry", "idempotencyKey": "retry-after-legacy"},
        headers=account.headers,
    )
    assert retry.status_code == 200, retry.text
    retried = retry.json()
    assert retried["reasoning"]["status"] == "ready"
    assert len(retried["reasoning"]["nodes"]) == 4


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

    # 换一个能用的 reasoner,重试应成功建立路线图。
    good = MapReasoner(drafts=(_roadmap_draft(stages=5),))
    use_reasoner(good)
    retry = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/agent/turn",
        json={"trigger": "retry", "idempotencyKey": "retry-1"},
        headers=account.headers,
    )
    assert retry.status_code == 200, retry.text
    retried = retry.json()
    assert retried["reasoning"]["status"] == "ready"
    assert len(retried["reasoning"]["nodes"]) == 6


async def test_structurally_invalid_roadmap_is_rejected_atomically(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    account = await make_account()
    # 只有 2 个阶段 —— 低于 3,整份路线图拒绝。给两份(纠错重试也拿到同样的错),
    # 断言最终错误说的是形状问题。
    reasoner = MapReasoner(drafts=(_roadmap_draft(stages=2), _roadmap_draft(stages=2)))
    use_reasoner(reasoner)

    body = await _enter(app_client, account)
    assert body["reasoning"]["status"] == "failed"
    assert body["reasoning"]["nodes"] == []
    assert "阶段" in (body["reasoning"]["error"] or "")

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
        options=(QuestionOptionDraft(id="a", label="保研", recommended=True),),
        analysis_summary="两边的准备周期与成果物不同,现在不清楚你的优先级。",
        recommendation="先把精力放在你更看重的那一边。",
        decision_impact="不同选择会改变阶段顺序与阶段 2 的成果物。",
    )
    reasoner = MapReasoner(drafts=(_roadmap_draft(stages=4),), questions=(scheduling, strategic))
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
    reasoner = MapReasoner(drafts=(_roadmap_draft(stages=3), _roadmap_draft(stages=5)))
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
    assert len(second["reasoning"]["nodes"]) == 6
    assert len(reasoner.calls) == 2


async def test_user_edit_is_preserved_across_agent_turns(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    account = await make_account()
    reasoner = MapReasoner(drafts=(_roadmap_draft(stages=4), _roadmap_draft(stages=4)))
    use_reasoner(reasoner)

    first = await _enter(app_client, account)
    node_id = first["reasoning"]["nodes"][0]["id"]

    # 用户改标题 + 写原文。
    edited = await app_client.patch(
        f"/api/workspaces/{account.workspace_id}/reasoning/nodes/{node_id}",
        json={"title": "我自己的路线", "userDescription": "这是我自己写的原文"},
        headers=account.headers,
    )
    assert edited.status_code == 200, edited.text
    view = edited.json()
    assert view["nodes"][0]["title"] == "我自己的路线"
    assert view["nodes"][0]["userDescription"] == "这是我自己写的原文"

    # 再跑一轮(force retry):模型又想写回原标题,但用户字段必须原样保留。
    retry = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/agent/turn",
        json={"trigger": "retry", "idempotencyKey": "retry-user-edit"},
        headers=account.headers,
    )
    assert retry.status_code == 200, retry.text
    node = next(n for n in retry.json()["reasoning"]["nodes"] if n["id"] == node_id)
    assert node["title"] == "我自己的路线", "Agent 覆盖了用户改过的标题"
    assert node["userDescription"] == "这是我自己写的原文", "Agent 覆盖了用户原文"


def _route_draft(route_title: str) -> ReasoningMapDraft:
    return _roadmap_draft(stages=4, route_title=route_title)


async def _plan(client: httpx.AsyncClient, account) -> dict:
    response = await client.get(
        f"/api/workspaces/{account.workspace_id}/plan", headers=account.headers
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_answering_a_question_advances_the_map(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    account = await make_account()
    reasoner = MapReasoner(
        drafts=(_roadmap_draft(stages=3),),
        questions=(
            QuestionDraft(
                question="你主要用它做什么?",
                why_now="它决定路线",
                response_mode="free_text",
                allow_custom_input=True,
                analysis_summary="用途不同会改变第一条路线的形状。",
                recommendation="先按通用最小闭环走,再按用途收窄。",
                decision_impact="不同用途会改变阶段 2 的项目素材。",
            ),
        ),
    )
    use_reasoner(reasoner)
    body = await _enter(app_client, account)
    question = body["question"]
    assert question is not None and question["reasoningNodeId"]
    handle = next(
        node["handle"] for node in body["reasoning"]["nodes"] if node["id"] == question["reasoningNodeId"]
    )

    answered = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/questions/{question['id']}/answer",
        json={"selectedOptionIds": [], "clientAnswerId": "answer-key-1", "customInput": "主要用来做数据分析"},
        headers=account.headers,
    )
    assert answered.status_code == 200, answered.text

    turn = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/agent/turn",
        json={
            "trigger": "question_answered",
            "reasoningHandle": handle,
            "message": "主要用来做数据分析",
            "idempotencyKey": "qa-1",
        },
        headers=account.headers,
    )
    assert turn.status_code == 200, turn.text
    view = turn.json()["reasoning"]
    node = next(item for item in view["nodes"] if item["handle"] == handle)
    assert node["status"] == "resolved", "回答之后地图节点状态必须真实推进"


async def test_strategy_confirmation_goes_through_pending_proposal(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    account = await make_account()
    reasoner = MapReasoner(drafts=(_route_draft("战略:先英语"),))
    use_reasoner(reasoner)
    await _enter(app_client, account)

    body = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/agent/turn",
        json={"trigger": "strategy_confirmation", "reasoningHandle": "r1", "idempotencyKey": "strategy-1"},
        headers=account.headers,
    )
    assert body.status_code == 200, body.text
    view = body.json()["reasoning"]
    assert view["phase"] == "roadmap_review"
    assert view["strategyProposalId"], body.json().get("proposalErrors")

    # **未确认前没有业务战略节点。**
    plan = await _plan(app_client, account)
    assert len(plan["nodes"]) == 1, "未确认前不该写入任何业务计划节点"
    assert not any(node["title"] == "战略:先英语" for node in plan["nodes"])

    confirm = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/proposals/{view['strategyProposalId']}/confirm",
        json={"idempotencyKey": "confirm-strategy"},
        headers=account.headers,
    )
    assert confirm.status_code == 200, confirm.text

    # 确认之后再跑一次:回写关联,进入 strategy_confirmed。
    after = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/agent/turn",
        json={"trigger": "strategy_confirmation", "idempotencyKey": "strategy-2"},
        headers=account.headers,
    )
    assert after.status_code == 200, after.text
    final = after.json()["reasoning"]
    assert final["phase"] == "strategy_confirmed"
    route = next(node for node in final["nodes"] if node["handle"] == "r1")
    assert route["linkedPlanNodeId"] is not None, "确认之后路线必须关联到真实战略节点"

    plan_after = await _plan(app_client, account)
    assert any(node["title"] == "战略:先英语" for node in plan_after["nodes"])

    # 执行桥接:已确认战略之后才能细化阶段。
    refine = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/agent/strategy/refine",
        headers=account.headers,
    )
    assert refine.status_code == 200, refine.text
    body = refine.json()
    assert body["userMessage"]["content"].strip()
    assert body["assistantMessage"]["content"].strip()


async def test_refine_requires_a_confirmed_strategy(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    account = await make_account()
    use_reasoner(MapReasoner(drafts=()))
    response = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/agent/strategy/refine",
        headers=account.headers,
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "INVALID_INPUT"


def test_scripted_reasoner_accepts_reasoning_map_fixture() -> None:
    turns = parse_script(
        '{"turns":[{"reply":"x","reasoningMap":{"focus":"r1","nodes":['
        '{"handle":"r1","title":"路线","nodeType":"route","importance":5}]}}]}'
    )
    assert turns[0]["reasoningMap"]["focus"] == "r1"

    with pytest.raises(ScriptedConfigError):
        parse_script('{"turns":[{"reply":"x","reasoningmap":{}}]}')
