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
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agent.runtime.base import (
    IntakeDecision,
    QuestionDraft,
    QuestionOptionDraft,
    ReasoningMapDraft,
    ReasoningMapNodeDraft,
    ReasoningResult,
)
from backend.db.models import AgentQuestion, GoalReasoningSession, PlanNode, ReasoningNode
from backend.db.models.enums import (
    ModelSource,
    ReasoningNodeStatus,
    ReasoningNodeType,
    ReasoningSessionPhase,
    ReasoningSessionStatus,
    ReasoningSource,
    ReasoningTurnAction,
)


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
        if getattr(turn, "purpose", "") == "strategic_intake":
            # 阶段 12:测试默认让 intake 直接“信息够了”,不消耗脚本/草稿序号。
            return ReasoningResult(
                reply="信息已经够了,我直接给你整体时间架构。",
                source=ModelSource.DIRECT_LLM,
                intake_decision=IntakeDecision(action="ready_for_architecture"),
            )
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
    await seed_known_conditions(account)
    use_reasoner(MapReasoner(drafts=(_roadmap_draft(stages=4),)))

    body = await _enter(app_client, account)
    assert body["changed"] is True
    view = body["reasoning"]
    assert view["phase"] == "temporal_architecture_draft"
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
    await seed_known_conditions(account)
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
    await seed_known_conditions(account)
    use_reasoner(MapReasoner(drafts=(_roadmap_draft(stages=4, with_route=False),)))

    body = await _enter(app_client, account)
    assert body["changed"] is False
    assert body["reasoning"]["status"] == ReasoningSessionStatus.FAILED.value
    assert await _nodes(db, account) == []


async def test_roadmap_phase_blocks_execution_questions(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    account = await make_account()
    await seed_known_conditions(account)
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
    await seed_known_conditions(account)
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
        options=(QuestionOptionDraft(id="minimal", label="最小闭环", recommended=True),),
        analysis_summary="已知每周 150 分钟;先补语法会拉长见效时间。",
        recommendation="先跑通一个最小分析闭环。",
        decision_impact="不同选择会改变阶段 2 的项目素材与总时长。",
    )
    use_reasoner(
        MapReasoner(
            drafts=(_roadmap_draft(stages=4), _roadmap_draft(stages=4)),
            questions=(scheduling, strategic),
        )
    )

    body = await _enter(app_client, account)
    # 阶段 12:intake 模式默认“信息够了”,首轮仍先给路线与阶段;
    # 执行层问题(每周几小时)由服务端战略守卫拦掉。
    assert body["changed"] is True
    view = body["reasoning"]
    assert view["phase"] == "temporal_architecture_draft"

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
    await seed_known_conditions(account)
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


async def _questions(db: AsyncSession, account) -> list[AgentQuestion]:
    rows = await db.execute(
        select(AgentQuestion).where(AgentQuestion.workspace_id == uuid.UUID(account.workspace_id))
    )
    return list(rows.scalars())


async def test_plain_text_first_turn_is_rejected_without_question(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    """真实模型只回一段文字(带一个问题)时:两轮都不合格 -> 失败,**不写问题节点**。"""
    account = await make_account()
    await seed_known_conditions(account)
    use_reasoner(
        MapReasoner(
            drafts=(None, None),
            questions=(
                QuestionDraft(question="你学它是想解决什么具体问题?", why_now="想了解用途", response_mode="free_text"),
            ),
        )
    )
    body = await _enter(app_client, account)
    assert body["changed"] is False
    assert body["reasoning"]["status"] == "failed"
    assert body["reasoning"]["nodes"] == []
    assert await _questions(db, account) == [], "不合格的首轮不许落问题卡"


async def test_question_only_first_turn_is_rejected(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    account = await make_account()
    await seed_known_conditions(account)
    use_reasoner(
        MapReasoner(
            drafts=(None, None),
            questions=(
                QuestionDraft(question="为什么学?", why_now="它决定路线", response_mode="free_text"),
            ),
        )
    )
    body = await _enter(app_client, account)
    assert body["reasoning"]["status"] == "failed"
    assert body["reasoning"]["nodes"] == []
    assert await _questions(db, account) == []


async def test_old_dimension_map_first_turn_is_rejected(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    account = await make_account()
    await seed_known_conditions(account)
    legacy = ReasoningMapDraft(
        nodes=tuple(
            ReasoningMapNodeDraft(handle=f"r{i}", title=f"维度 {i}") for i in range(1, 6)
        ),
        focus_handle="r1",
        phase="strategic_exploration",
    )
    use_reasoner(MapReasoner(drafts=(legacy, legacy)))
    body = await _enter(app_client, account)
    assert body["reasoning"]["status"] == "failed"
    assert body["reasoning"]["nodes"] == []
    assert await _questions(db, account) == []


async def test_first_turn_retries_once_with_correction_then_succeeds(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    """第一轮只回文字 -> 服务端用结构化纠错提示重试一次 -> 第二轮路线图成功。"""
    account = await make_account()
    await seed_known_conditions(account)
    reasoner = MapReasoner(drafts=(None, _roadmap_draft(stages=4)))
    use_reasoner(reasoner)

    body = await _enter(app_client, account)
    assert body["changed"] is True
    assert body["reasoning"]["phase"] == "temporal_architecture_draft"
    assert len([n for n in body["reasoning"]["nodes"] if n["nodeType"] == "stage"]) == 4
    # 纠错提示确实进了模型看到的输入。
    # 阶段 12:首轮先是一次 intake(直接 ready),然后是架构调用 + 架构纠错重试。
    assert len(reasoner.calls) >= 3
    assert "reasoningMap" in reasoner.calls[-1].user_message or "路线图" in reasoner.calls[-1].user_message


async def test_regenerate_roadmap_keeps_legacy_nodes_in_history(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    """旧地图重生成:新 route+stage 写进去,旧的 dimension **不删**。"""
    account = await make_account()
    await seed_known_conditions(account)
    root = await db.scalar(
        select(PlanNode).where(PlanNode.workspace_id == uuid.UUID(account.workspace_id), PlanNode.depth == 0)
    )
    assert root is not None
    # 直接造一张阶段 8 之前的旧地图(顶层一堆一级 dimension)。
    session = GoalReasoningSession(
        workspace_id=uuid.UUID(account.workspace_id),
        root_plan_node_id=root.id,
        phase=ReasoningSessionPhase.STRATEGIC_EXPLORATION,
        turn_action=ReasoningTurnAction.ANALYZE,
        status=ReasoningSessionStatus.READY,
    )
    db.add(session)
    await db.flush()
    for index in range(1, 5):
        db.add(
            ReasoningNode(
                session_id=session.id,
                handle=f"old{index}",
                title=f"旧维度 {index}",
                node_type=ReasoningNodeType.DIMENSION,
                status=ReasoningNodeStatus.EXPLORING,
                next_action=ReasoningTurnAction.ANALYZE,
                source=ReasoningSource.AGENT,
            )
        )
    await db.commit()

    use_reasoner(MapReasoner(drafts=(_roadmap_draft(stages=4),)))
    response = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/agent/turn",
        json={"trigger": "regenerate_roadmap", "idempotencyKey": "regen-1"},
        headers=account.headers,
    )
    assert response.status_code == 200, response.text
    view = response.json()["reasoning"]
    handles = {node["handle"] for node in view["nodes"]}
    # 新路线在。
    assert any(node["nodeType"] == "route" for node in view["nodes"])
    assert len([n for n in view["nodes"] if n["nodeType"] == "stage"]) == 4
    # 旧维度仍在(归入历史思考层)。
    assert {"old1", "old2", "old3", "old4"} <= handles


# ---------------------------------------------------------------------------
# 真实模型 adapter:chat/completions 的原始载荷 -> 解析 -> 首轮是否成立
# ---------------------------------------------------------------------------
from backend.agent.runtime.response import (  # noqa: E402
    PayloadInvalid,
    payload_from_chat_completion,
    payload_to_result,
)
from backend.db.models.enums import DegradedReason  # noqa: E402


def _chat(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}], "usage": {}}


def test_adapter_plain_text_is_not_parseable() -> None:
    """模型只回自然语言 -> adapter 明确判定“输出无效”,不猜、不硬解。"""
    with pytest.raises((PayloadInvalid, ValueError)):
        payload_from_chat_completion(_chat("你好,我想先了解你的目标。"))


def test_adapter_question_only_has_no_reasoning_map() -> None:
    raw = _chat(
        '{"reply":"先问你一个问题。","questions":['
        '{"question":"为什么学?","whyNow":"它决定路线","responseMode":"free_text","options":[]}]}'
    )
    body, _usage = payload_from_chat_completion(raw)
    result = payload_to_result(body, source=ModelSource.DIRECT_LLM, request_id="r", prompt_version="t")
    assert result.reasoning_map is None
    assert len(result.questions) == 1


@dataclass
class PayloadReasoner:
    """把**原始 chat/completions JSON**经真实 adapter 解析后返回,模拟真实模型。"""

    contents: tuple[str, ...] = ()
    calls: list = field(default_factory=list)
    _index: int = 0

    async def reason(self, turn):
        self.calls.append(turn)
        if getattr(turn, "purpose", "") == "strategic_intake":
            return ReasoningResult(
                reply="信息已经够了,我直接给你整体时间架构。",
                source=ModelSource.DIRECT_LLM,
                intake_decision=IntakeDecision(action="ready_for_architecture"),
            )
        if self._index >= len(self.contents):
            return ReasoningResult(
                reply="模型没有给出结构化结果。",
                source=ModelSource.UNAVAILABLE,
                degraded=True,
                degraded_reason=DegradedReason.MODEL_OUTPUT_INVALID,
                retryable=True,
            )
        raw = _chat(self.contents[self._index])
        self._index += 1
        try:
            body, _usage = payload_from_chat_completion(raw)
            return payload_to_result(
                body,
                source=ModelSource.DIRECT_LLM,
                request_id="r",
                prompt_version="t",
            )
        except (PayloadInvalid, ValueError):
            return ReasoningResult(
                reply="模型这次的回答没能解析出来。",
                source=ModelSource.UNAVAILABLE,
                degraded=True,
                degraded_reason=DegradedReason.MODEL_OUTPUT_INVALID,
                retryable=True,
            )


ROADMAP_JSON = (
    '{"reply":"先给你一条约 12 周的路线。","reasoningMap":{'
    '"phase":"roadmap_draft","turnAction":"ask_user","focus":"r2","focusReason":"先定阶段顺序",'
    '"nodes":['
    '{"handle":"r1","title":"推荐路线:约 12 周","nodeType":"route","parent":null,'
    '"timeframe":"约 12 周","deliverable":"一个可展示项目","passCriteria":"能端到端做完"},'
    '{"handle":"r2","title":"阶段 1:基础","nodeType":"stage","parent":"r1",'
    '"timeframe":"约 3 周","deliverable":"一组小练习","passCriteria":"能写函数"},'
    '{"handle":"r3","title":"阶段 2:数据","nodeType":"stage","parent":"r1",'
    '"timeframe":"约 3 周","deliverable":"一份分析","passCriteria":"能清洗数据"},'
    '{"handle":"r4","title":"阶段 3:作品","nodeType":"stage","parent":"r1",'
    '"timeframe":"约 3 周","deliverable":"一个项目","passCriteria":"能讲清结论"}],'
    '"links":[{"source":"r1","target":"r2","type":"influences","note":"路线决定顺序"}]}}'
)
OLD_DIMENSION_JSON = (
    '{"reply":"先梳理这些维度。","reasoningMap":{"phase":"strategic_exploration",'
    '"focus":"r1","nodes":['
    '{"handle":"r1","title":"目标用途","nodeType":"dimension"},'
    '{"handle":"r2","title":"能力基础","nodeType":"dimension"},'
    '{"handle":"r3","title":"最小能力","nodeType":"dimension"},'
    '{"handle":"r4","title":"验证方式","nodeType":"dimension"}],"links":[]}}'
)


async def test_adapter_old_dimension_map_never_becomes_successful_first_turn(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    account = await make_account()
    await seed_known_conditions(account)
    use_reasoner(PayloadReasoner(contents=(OLD_DIMENSION_JSON, OLD_DIMENSION_JSON)))
    body = await _enter(app_client, account)
    assert body["changed"] is False
    assert body["reasoning"]["status"] == "failed"
    assert body["reasoning"]["nodes"] == []
    assert await _questions(db, account) == []


async def test_adapter_plain_text_never_becomes_successful_first_turn(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    account = await make_account()
    await seed_known_conditions(account)
    use_reasoner(PayloadReasoner(contents=("你好,我想先了解你的目标。", "先问一个问题:你为什么学?")))
    body = await _enter(app_client, account)
    assert body["changed"] is False
    assert body["reasoning"]["status"] == "failed"
    assert await _questions(db, account) == []


async def test_adapter_valid_roadmap_json_succeeds(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    account = await make_account()
    await seed_known_conditions(account)
    use_reasoner(PayloadReasoner(contents=(ROADMAP_JSON,)))
    body = await _enter(app_client, account)
    assert body["changed"] is True
    assert body["reasoning"]["phase"] == "temporal_architecture_draft"
    assert len([n for n in body["reasoning"]["nodes"] if n["nodeType"] == "stage"]) == 3
    assert all(
        n["timeframe"] and n["deliverable"] and n["passCriteria"]
        for n in body["reasoning"]["nodes"]
        if n["nodeType"] == "stage"
    )


# ---------------------------------------------------------------------------
# 真实模型 transport:httpx.MockTransport 走一遍 DirectLLMReasoner 的
# HTTP -> adapter -> 解析 路径
# ---------------------------------------------------------------------------
import backend.agent.runtime.direct_llm as direct_llm  # noqa: E402
from backend.agent.runtime.direct_llm import DirectLLMReasoner  # noqa: E402
from backend.core.config import Settings  # noqa: E402
from backend.tests.conftest import (  # noqa: E402
    seed_known_conditions,
)


def _mock_llm(monkeypatch: pytest.MonkeyPatch, content: str) -> DirectLLMReasoner:
    """让 DirectLLMReasoner 的 httpx 客户端走 MockTransport,返回固定的模型文本。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"choices": [{"message": {"content": content}}], "usage": {}}
        )

    original = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    monkeypatch.setattr(direct_llm.httpx, "AsyncClient", factory)
    settings = Settings(
        _env_file=None,
        llm_api_key="test-key",
        llm_model="deepseek-chat",
        llm_base_url="https://api.deepseek.com",
    )
    return DirectLLMReasoner(settings)


async def _call(reasoner: DirectLLMReasoner):
    raw = await reasoner._post({"model": "deepseek-chat", "messages": []})
    return reasoner._parse(
        raw, request_id="r", latency_ms=0, payload={"model": "deepseek-chat"}, prompt_version="t"
    )


async def test_mock_transport_plain_text_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    reasoner = _mock_llm(monkeypatch, "你好,我想先了解你的目标。")
    result = await _call(reasoner)
    assert result.degraded is True
    assert result.degraded_reason is DegradedReason.MODEL_OUTPUT_INVALID
    assert result.reasoning_map is None


async def test_mock_transport_question_only_parses_without_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reasoner = _mock_llm(
        monkeypatch,
        '{"reply":"先问你一个问题。","questions":['
        '{"question":"为什么学?","whyNow":"它决定路线","responseMode":"free_text","options":[]}]}',
    )
    result = await _call(reasoner)
    assert result.degraded is False
    assert result.reasoning_map is None
    assert len(result.questions) == 1


async def test_mock_transport_old_dimension_map_parses_as_dimensions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reasoner = _mock_llm(monkeypatch, OLD_DIMENSION_JSON)
    result = await _call(reasoner)
    assert result.degraded is False
    assert result.reasoning_map is not None
    assert all(node.node_type == "dimension" for node in result.reasoning_map.nodes)
    assert not [node for node in result.reasoning_map.nodes if node.node_type == "route"]


async def test_mock_transport_valid_roadmap_parses_route_and_stages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reasoner = _mock_llm(monkeypatch, ROADMAP_JSON)
    result = await _call(reasoner)
    assert result.degraded is False
    assert result.reasoning_map is not None
    routes = [n for n in result.reasoning_map.nodes if n.node_type == "route"]
    stages = [n for n in result.reasoning_map.nodes if n.node_type == "stage"]
    assert len(routes) == 1
    assert 3 <= len(stages) <= 5
    assert all(stage.timeframe and stage.deliverable and stage.pass_criteria for stage in stages)
