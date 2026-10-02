"""阶段 8:路线优先的战略规划(roadmap-first)。

这一组钉的是**服务端强制的形状与阶段语义**,不是提示词措辞:

1. 首轮路线图(**恰好一条** route + 3–5 个挂在它下面的 stage)被接受,
   stage 的 `timeframe` / `deliverable` / `pass_criteria` 真的读得回来;
2. 形状不合规的路线图**整份拒绝**,不写半成品(可安全重试);
3. 路线阶段是战略档:排期/投入类问题由服务端拦掉,不信模型自觉;
4. 路线图**不写任何 PlanNode 执行条目**。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agent.runtime.base import (
    QuestionDraft,
    QuestionOptionDraft,
    ReasoningMapDraft,
    ReasoningMapNodeDraft,
    ReasoningResult,
)
from backend.db.models import AgentQuestion, PlanNode, ReasoningNode
from backend.db.models.enums import ModelSource, ReasoningSessionStatus


def _roadmap_draft(
    *,
    stages: int = 4,
    with_route: bool = True,
    question: QuestionDraft | None = None,
) -> ReasoningMapDraft:
    nodes: list[ReasoningMapNodeDraft] = []
    if with_route:
        nodes.append(
            ReasoningMapNodeDraft(
                handle="r1",
                title="推荐路线:约 12 周从基础到可展示项目",
                node_type="route",
                summary="先最小闭环,再补统计与可视化",
                timeframe="约 12 周",
                deliverable="一个可展示的数据分析项目",
                pass_criteria="能端到端做完并讲清结论",
            )
        )
    for index in range(1, stages + 1):
        nodes.append(
            ReasoningMapNodeDraft(
                handle=f"s{index}",
                title=f"阶段 {index}",
                node_type="stage",
                parent_handle="r1" if with_route else None,
                timeframe=f"约 {index + 1} 周",
                deliverable=f"成果 {index}",
                pass_criteria=f"通过标准 {index}",
            )
        )
    return ReasoningMapDraft(
        nodes=tuple(nodes),
        focus_handle="r1" if with_route else "s1",
        focus_reason="它决定阶段顺序",
        phase="roadmap_draft",
        turn_action="ask_user",
    )


@dataclass
class MapReasoner:
    drafts: tuple[ReasoningMapDraft | None, ...] = ()
    questions: tuple[QuestionDraft, ...] = ()
    calls: list = field(default_factory=list)
    _index: int = 0

    async def reason(self, turn):
        self.calls.append(turn)
        draft = self.drafts[self._index] if self._index < len(self.drafts) else None
        self._index += 1
        return ReasoningResult(
            reply="这是推荐路线与阶段。",
            source=ModelSource.DIRECT_LLM,
            reasoning_map=draft,
            questions=self.questions,
        )


async def _enter(
    client: httpx.AsyncClient, account, key: str = "roadmap-1"
) -> dict:
    response = await client.post(
        f"/api/workspaces/{account.workspace_id}/agent/turn",
        json={"trigger": "space_entered", "idempotencyKey": key},
        headers=account.headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _nodes(db: AsyncSession, account) -> list[ReasoningNode]:
    from backend.db.models import GoalReasoningSession

    session = await db.scalar(
        select(GoalReasoningSession).where(
            GoalReasoningSession.workspace_id == uuid.UUID(account.workspace_id)
        )
    )
    assert session is not None
    rows = await db.execute(
        select(ReasoningNode).where(ReasoningNode.session_id == session.id)
    )
    return list(rows.scalars())


async def test_roadmap_draft_is_accepted_with_stage_fields(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    account = await make_account()
    use_reasoner(MapReasoner(drafts=(_roadmap_draft(stages=4),)))

    body = await _enter(app_client, account)
    assert body["changed"] is True
    view = body["reasoning"]
    assert view["phase"] == "roadmap_draft"
    assert len(view["nodes"]) == 5

    route = next(node for node in view["nodes"] if node["nodeType"] == "route")
    assert route["timeframe"] == "约 12 周"
    assert route["deliverable"] == "一个可展示的数据分析项目"

    stage = next(node for node in view["nodes"] if node["nodeType"] == "stage")
    assert stage["parentHandle"] == "r1"
    assert stage["timeframe"] and stage["deliverable"] and stage["passCriteria"]

    # 路线图不是执行计划:没有 phase/week/day 的 PlanNode。
    plan_rows = await db.execute(
        select(PlanNode).where(PlanNode.workspace_id == uuid.UUID(account.workspace_id))
    )
    assert all(
        (node.planning_level or "") not in {"phase", "week", "day"}
        for node in plan_rows.scalars()
    )


async def test_roadmap_with_too_few_stages_is_rejected_whole(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    account = await make_account()
    use_reasoner(MapReasoner(drafts=(_roadmap_draft(stages=2),)))

    body = await _enter(app_client, account)
    assert body["changed"] is False
    assert body["reasoning"]["status"] == ReasoningSessionStatus.FAILED.value
    assert body["reasoning"]["nodes"] == []
    assert await _nodes(db, account) == []


async def test_roadmap_without_top_level_route_is_rejected_whole(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    account = await make_account()
    use_reasoner(MapReasoner(drafts=(_roadmap_draft(stages=4, with_route=False),)))

    body = await _enter(app_client, account)
    assert body["changed"] is False
    assert body["reasoning"]["status"] == ReasoningSessionStatus.FAILED.value
    assert await _nodes(db, account) == []


async def test_roadmap_phase_blocks_execution_questions(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    account = await make_account()
    execution_question = QuestionDraft(
        question="你每周能投入几小时?",
        why_now="排期需要",
        response_mode="single_select",
        options=(QuestionOptionDraft(id="a", label="A"), QuestionOptionDraft(id="b", label="B")),
        allow_custom_input=True,
    )
    use_reasoner(
        MapReasoner(drafts=(_roadmap_draft(stages=4),), questions=(execution_question,))
    )

    await _enter(app_client, account)
    rows = await db.execute(
        select(AgentQuestion).where(AgentQuestion.workspace_id == uuid.UUID(account.workspace_id))
    )
    assert list(rows.scalars()) == []


async def test_python_data_analysis_first_turn_is_roadmap_not_dimensions(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    """用户说“Python 数据分析 + 每周 150 分钟”时,首轮必须是**路线 + 阶段**,
    不能是旧的散乱一级维度图,也不能再问每周投入。"""
    account = await make_account()
    await app_client.patch(
        f"/api/workspaces/{account.workspace_id}",
        json={"intent": "我想学习 Python 做数据分析，每周 150 分钟。"},
        headers=account.headers,
    )
    scheduling = QuestionDraft(
        question="你每周能投入多少小时?",
        why_now="排期需要",
        response_mode="free_text",
    )
    strategic = QuestionDraft(
        question="先求能跑通的最小闭环,还是先补统计基础?",
        why_now="它决定阶段顺序",
        response_mode="single_select",
        options=(QuestionOptionDraft(id="minimal", label="最小闭环"),),
    )
    use_reasoner(
        MapReasoner(drafts=(_roadmap_draft(stages=4),), questions=(scheduling, strategic))
    )

    body = await _enter(app_client, account)
    view = body["reasoning"]
    assert body["changed"] is True
    assert view["phase"] == "roadmap_draft"

    routes = [node for node in view["nodes"] if node["nodeType"] == "route"]
    stages = [node for node in view["nodes"] if node["nodeType"] == "stage"]
    assert len(routes) == 1, "首轮必须恰好一条路线"
    assert 3 <= len(stages) <= 5, "首轮必须是 3–5 个阶段"
    # **不是**旧的散乱一级维度图:除路线外没有顶层节点。
    assert not [
        node
        for node in view["nodes"]
        if node["parentHandle"] is None and node["nodeType"] != "route"
    ]
    assert all(node["parentHandle"] == routes[0]["handle"] for node in stages)
    assert all(node["timeframe"] and node["deliverable"] and node["passCriteria"] for node in stages)

    # 执行层问题(每周几小时)被拦掉;最多一个活动问题,而且不是投入类。
    rows = list(
        (
            await db.execute(
                select(AgentQuestion).where(
                    AgentQuestion.workspace_id == uuid.UUID(account.workspace_id)
                )
            )
        ).scalars()
    )
    assert len(rows) <= 1
    assert all("每周" not in row.question for row in rows)

    # 确认战略前没有任何周/日执行条目。
    plan_rows = await db.execute(
        select(PlanNode).where(PlanNode.workspace_id == uuid.UUID(account.workspace_id))
    )
    assert all((node.planning_level or "") not in {"week", "day"} for node in plan_rows.scalars())


async def test_first_turn_legacy_dimension_map_is_rejected_then_retries(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    """新首轮给旧形状 -> 整份拒绝、不写半成品;换成路线图后重试成功。"""
    account = await make_account()
    legacy = ReasoningMapDraft(
        nodes=tuple(
            ReasoningMapNodeDraft(handle=f"r{i}", title=f"决策维度 {i}")
            for i in range(1, 7)
        ),
        focus_handle="r1",
        phase="strategic_exploration",
    )
    use_reasoner(MapReasoner(drafts=(legacy,)))

    body = await _enter(app_client, account)
    assert body["changed"] is False
    assert body["reasoning"]["status"] == "failed"
    assert body["reasoning"]["nodes"] == []

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
    assert retried["reasoning"]["nodes"][0]["nodeType"] == "route"
