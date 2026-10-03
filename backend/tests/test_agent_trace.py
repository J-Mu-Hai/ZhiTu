"""运行轨迹的持久状态、读取 API、脱敏与权限(阶段 9)。

这一组用例盯的是三件事:

1. `waiting_model` **在模型返回之前**就已经落库可读(轨迹不是前端假进度);
2. 终态错误码与脱敏摘要来自服务端真实判据,而不是模型自由文本;
3. 读取接口按 workspace 授权、不泄漏 prompt / 用户原文 / 密钥,开关关闭时不可见。
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
    ReasoningMapDraft,
    ReasoningMapNodeDraft,
    ReasoningResult,
    ToolRequest,
)
from backend.contracts.proposal import ActionError
from backend.core.config import settings
from backend.db.models import (
    GoalReasoningSession,
    Message,
    PlanNode,
    ReasoningNode,
    ReasoningState,
    ToolCallRecord,
    Workspace,
)
from backend.db.models.enums import (
    AgentTraceStep,
    DegradedReason,
    ModelSource,
    ReasoningNodeStatus,
    ReasoningNodeType,
    ReasoningSessionPhase,
    ReasoningSessionStatus,
    ReasoningSource,
    ReasoningTurnAction,
    ToolCallStatus,
)
from backend.db.session import SessionLocal
from backend.services import agent_loop_service, proposal_service


@dataclass
class InspectingReasoner:
    """在模型"思考期间"回读轨迹的假模型。

    它用**另一个会话**读库(不是请求那个会话):这正是诊断入口在真实场景里做的事 ——
    模型还在等响应时,浏览器轮询读到的必须是已经提交的 `waiting_model`。
    """

    observed: dict = field(default_factory=dict)
    reply: str = "看完了,这次不用改计划。"

    async def reason(self, turn):
        async with SessionLocal() as other:
            state = await other.scalar(
                select(ReasoningState).order_by(ReasoningState.created_at.desc()).limit(1)
            )
            if state is not None:
                self.observed["step"] = state.current_step
                self.observed["started_at"] = state.started_at
                self.observed["trigger"] = state.trigger
        return ReasoningResult(
            reply=self.reply,
            source=ModelSource.DIRECT_LLM,
            stop_reason="ready_to_propose",
        )


@dataclass
class ToolThenDoneReasoner:
    results: tuple[ReasoningResult, ...]
    calls: list = field(default_factory=list)
    _index: int = 0

    async def reason(self, turn):
        self.calls.append(turn)
        result = self.results[min(self._index, len(self.results) - 1)]
        self._index += 1
        return result


async def _send(client: httpx.AsyncClient, account, content: str) -> dict:
    response = await client.post(
        f"/api/workspaces/{account.workspace_id}/messages",
        json={"content": content, "clientMessageId": content},
        headers=account.headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _trace(client: httpx.AsyncClient, account, limit: int | None = None) -> dict:
    url = f"/api/workspaces/{account.workspace_id}/agent/trace"
    if limit is not None:
        url += f"?limit={limit}"
    response = await client.get(url, headers=account.headers)
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------------
# 1. 发请求前就写 waiting_model
# ---------------------------------------------------------------------------------
async def test_waiting_model_is_persisted_before_the_model_returns(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    account = await make_account()
    reasoner = use_reasoner(InspectingReasoner())
    await _send(app_client, account, "先帮我看看")

    assert reasoner.observed.get("step") is AgentTraceStep.WAITING_MODEL
    assert reasoner.observed.get("started_at") is not None

    body = await _trace(app_client, account)
    assert body["enabled"] is True
    assert len(body["turns"]) == 1
    turn = body["turns"][0]
    assert turn["currentStep"] == "completed"
    assert turn["status"] == "completed"
    assert turn["trigger"] == "user_message"
    assert turn["durationMs"] is not None
    assert turn["safeSummary"]
    # **真实的状态转移序列**:等待模型与完成都必须在里面。
    assert "waiting_model" in turn["steps"]
    assert "completed" in turn["steps"]


# ---------------------------------------------------------------------------------
# 2. 超时 / 模型错误:不同的可读终态
# ---------------------------------------------------------------------------------
async def test_model_timeout_has_its_own_readable_terminal(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    account = await make_account()
    use_reasoner(
        _degraded_reasoner(DegradedReason.MODEL_TIMEOUT)
    )
    await _send(app_client, account, "这句话要超时了")
    turn = (await _trace(app_client, account))["turns"][0]
    assert turn["status"] == "timed_out"
    assert turn["terminalCode"] == "MODEL_TIMEOUT"
    assert "超时" in turn["safeSummary"]
    assert turn["retryable"] is True


async def test_model_output_invalid_has_a_distinct_terminal(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    account = await make_account()
    use_reasoner(_degraded_reasoner(DegradedReason.MODEL_OUTPUT_INVALID))
    await _send(app_client, account, "这段输出不合法")
    turn = (await _trace(app_client, account))["turns"][0]
    assert turn["status"] == "failed"
    assert turn["terminalCode"] == "MODEL_OUTPUT_INVALID"
    assert "无法解析" in turn["safeSummary"]


# ---------------------------------------------------------------------------------
# 3. 工具摘要脱敏
# ---------------------------------------------------------------------------------
async def test_tool_rejection_is_summarized_without_raw_arguments(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    account = await make_account()
    use_reasoner(
        ToolThenDoneReasoner(
            results=(
                ReasoningResult(
                    reply="我试一下。",
                    source=ModelSource.DIRECT_LLM,
                    tool_requests=(
                        ToolRequest(
                            id="t1",
                            name="drop_table",
                            arguments={"table": "plan_nodes", "sql": "DROP TABLE plan_nodes"},
                            reason="危险操作",
                        ),
                    ),
                ),
                ReasoningResult(
                    reply="没有这个工具。",
                    source=ModelSource.DIRECT_LLM,
                    stop_reason="insufficient_evidence",
                ),
            )
        )
    )
    await _send(app_client, account, "查一个不存在的东西")

    await db.rollback()
    records = list((await db.execute(select(ToolCallRecord))).scalars())
    assert records and records[0].status is ToolCallStatus.REJECTED

    turn = (await _trace(app_client, account))["turns"][0]
    assert turn["tools"], "工具轨迹必须在接口上可见"
    tool = turn["tools"][0]
    assert tool["toolName"] == "drop_table"
    assert tool["status"] == "rejected"
    assert "被拒绝" in tool["summary"]
    # 原始参数里的表名 / SQL 不能出现在接口响应里。
    serialized = str(turn)
    assert "DROP TABLE" not in serialized
    assert "plan_nodes" not in serialized


# ---------------------------------------------------------------------------------
# 4. 脱敏:接口响应里没有用户原文 / prompt / 密钥
# ---------------------------------------------------------------------------------
async def test_trace_does_not_leak_user_text_or_secrets(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    account = await make_account()
    secret = "我的私密原文 SECRET_COOKIE_abc123"
    use_reasoner(InspectingReasoner(reply="收到。"))
    await _send(app_client, account, secret)
    raw = str(await _trace(app_client, account))
    assert secret not in raw
    assert "SECRET_COOKIE" not in raw
    assert "prompt" not in raw.lower()


# ---------------------------------------------------------------------------------
# 5. 权限:跨空间读取被拒;固定上限
# ---------------------------------------------------------------------------------
async def test_cross_workspace_trace_read_is_rejected(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    account_a = await make_account(email="a@example.com")
    account_b = await make_account(email="b@example.com", workspace_title="另一个空间")
    use_reasoner(InspectingReasoner())
    await _send(app_client, account_a, "写点东西")

    response = await app_client.get(
        f"/api/workspaces/{account_a.workspace_id}/agent/trace",
        headers=account_b.headers,
    )
    assert response.status_code == 404


async def test_trace_limit_is_capped(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    account = await make_account()
    use_reasoner(InspectingReasoner())
    await _send(app_client, account, "第一次")
    response = await app_client.get(
        f"/api/workspaces/{account.workspace_id}/agent/trace?limit=9999",
        headers=account.headers,
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------------
# 6. 开关:关闭时入口不可见(404),本地开发默认可用
# ---------------------------------------------------------------------------------
async def test_trace_api_is_hidden_when_disabled(
    app_client: httpx.AsyncClient, make_account, use_reasoner, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = await make_account()
    use_reasoner(InspectingReasoner())
    await _send(app_client, account, "本地写点东西")

    # 模拟生产环境且没有显式打开开关。
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "agent_trace_ui_enabled", False)
    response = await app_client.get(
        f"/api/workspaces/{account.workspace_id}/agent/trace",
        headers=account.headers,
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "TRACE_DISABLED"


async def test_trace_api_is_available_when_explicitly_enabled(
    app_client: httpx.AsyncClient, make_account, use_reasoner, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = await make_account()
    use_reasoner(InspectingReasoner())
    await _send(app_client, account, "生产里显式打开")

    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "agent_trace_ui_enabled", True)
    response = await app_client.get(
        f"/api/workspaces/{account.workspace_id}/agent/trace",
        headers=account.headers,
    )
    assert response.status_code == 200
    assert response.json()["enabled"] is True


# ---------------------------------------------------------------------------------
# 7. 重试:失败回合可重试,且不产生重复用户消息
# ---------------------------------------------------------------------------------
async def test_failed_turn_can_be_retried_without_duplicate_message(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    account = await make_account()
    use_reasoner(_degraded_reasoner(DegradedReason.MODEL_UNAVAILABLE))
    await _send(app_client, account, "失败一次")
    failed = (await _trace(app_client, account))["turns"][0]
    assert failed["retryable"] is True

    # 用同一个 clientMessageId 重发:这是既有幂等重试路径。
    use_reasoner(InspectingReasoner(reply="这次成功了。"))
    response = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/messages",
        json={"content": "失败一次", "clientMessageId": "失败一次"},
        headers=account.headers,
    )
    assert response.status_code == 200, response.text

    await db.rollback()
    count = len(list((await db.execute(select(Message))).scalars()))
    assert count == 2, "重试不应产生第二条用户消息"


def _degraded_reasoner(reason: DegradedReason) -> InspectingReasoner:
    """返回一个**降级**的假模型,同时保留可读的降级原因。"""

    @dataclass
    class _Degraded:
        observed: dict = field(default_factory=dict)

        async def reason(self, turn):
            return ReasoningResult(
                reply="模型这次没有成功。",
                source=ModelSource.UNAVAILABLE,
                degraded=True,
                degraded_reason=reason,
                retryable=True,
            )

    return _Degraded()  # type: ignore[return-value]


# ---------------------------------------------------------------------------------
# 8. 地图轮(reasoning_service)也必须有真实轨迹
#
# 阶段 9.1:**不能再拿“它不经过 agent_loop_service”当理由**。所有显式地图轮
# (space_entered / regenerate_roadmap / user_message 增量 / question_answered /
# strategy_confirmation / progress_update / execution_planning / refine)都写进同一张
# `reasoning_states`、同一个读取 API、同一个脱敏投影。
# ---------------------------------------------------------------------------------
def _roadmap_draft(stages: int = 4) -> ReasoningMapDraft:
    """一条合法的首轮路线图:恰好一条顶层 route + N 个 stage。"""
    nodes: list[ReasoningMapNodeDraft] = [
        ReasoningMapNodeDraft(
            handle="r1",
            title="推荐路线:约 8 周",
            node_type="route",
            summary="先打通最小闭环",
            timeframe="约 8 周",
            deliverable="一个可展示的项目",
            pass_criteria="能端到端做完并讲清结论",
        )
    ]
    for index in range(1, stages + 1):
        nodes.append(
            ReasoningMapNodeDraft(
                handle=f"r{index + 1}",
                title=f"阶段 {index}",
                node_type="stage",
                parent_handle="r1",
                timeframe=f"约 {index} 周",
                deliverable=f"成果 {index}",
                pass_criteria=f"通过标准 {index}",
            )
        )
    return ReasoningMapDraft(
        nodes=tuple(nodes),
        focus_handle="r2",
        focus_reason="第一阶段最该先定下来",
        phase="roadmap_draft",
        turn_action="ask_user",
    )


def _map_result(draft: ReasoningMapDraft | None, *, degraded: bool = False) -> ReasoningResult:
    if degraded:
        return ReasoningResult(
            reply="模型这次没有成功。",
            source=ModelSource.UNAVAILABLE,
            degraded=True,
            degraded_reason=DegradedReason.MODEL_UNAVAILABLE,
            retryable=True,
        )
    return ReasoningResult(
        reply="这是推荐路线与阶段。",
        source=ModelSource.DIRECT_LLM,
        reasoning_map=draft,
    )


@dataclass
class MapInspectingReasoner:
    """在模型“思考期间”用另一个会话回读轨迹的地图轮假模型。"""

    draft: ReasoningMapDraft | None = None
    degraded: bool = False
    observed: dict = field(default_factory=dict)

    async def reason(self, turn):
        async with SessionLocal() as other:
            state = await other.scalar(
                select(ReasoningState).order_by(ReasoningState.created_at.desc()).limit(1)
            )
            if state is not None:
                self.observed["step"] = state.current_step
                self.observed["trigger"] = state.trigger
                self.observed["started_at"] = state.started_at
        if getattr(turn, "purpose", "") == "strategic_intake":
            return ReasoningResult(
                reply="信息已经够了,我直接给你整体时间架构。",
                source=ModelSource.DIRECT_LLM,
                intake_decision=IntakeDecision(action="ready_for_architecture"),
            )
        return _map_result(self.draft, degraded=self.degraded)


@dataclass
class MapSequenceReasoner:
    """按顺序返回地图轮结果,用来验结构化纠错重试。"""

    results: tuple[ReasoningResult, ...]
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
        result = self.results[min(self._index, len(self.results) - 1)]
        self._index += 1
        return result


async def _map_turn(client: httpx.AsyncClient, account, trigger: str, key: str, **extra) -> dict:
    response = await client.post(
        f"/api/workspaces/{account.workspace_id}/agent/turn",
        json={"trigger": trigger, "idempotencyKey": key, **extra},
        headers=account.headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _make_legacy_map(db: AsyncSession, account) -> None:
    """造一张阶段 8 之前的旧地图(顶层一堆一级 dimension),用来触发重生成。"""
    root = await db.scalar(
        select(PlanNode).where(
            PlanNode.workspace_id == uuid.UUID(account.workspace_id), PlanNode.depth == 0
        )
    )
    assert root is not None
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


async def test_space_entered_trace_shows_waiting_model_during_the_call(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """地图轮在模型等待期间也必须读得到 `waiting_model`。"""
    account = await make_account()
    reasoner = use_reasoner(MapInspectingReasoner(draft=_roadmap_draft()))
    await _map_turn(app_client, account, "space_entered", "map-enter-1")

    assert reasoner.observed.get("step") is AgentTraceStep.WAITING_MODEL
    assert reasoner.observed.get("trigger") == "space_entered"
    assert reasoner.observed.get("started_at") is not None

    turn = (await _trace(app_client, account))["turns"][0]
    assert turn["trigger"] == "space_entered"
    assert turn["triggerLabel"] == "进入空间"
    assert turn["status"] == "completed"
    assert "waiting_model" in turn["steps"]
    assert "validating_output" in turn["steps"]
    assert "persisting" in turn["steps"]
    assert turn["safeSummary"]


async def test_regenerate_roadmap_traces_success(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    account = await make_account()
    await _make_legacy_map(db, account)
    use_reasoner(MapInspectingReasoner(draft=_roadmap_draft()))
    await _map_turn(app_client, account, "regenerate_roadmap", "regen-ok")

    turn = (await _trace(app_client, account))["turns"][0]
    assert turn["trigger"] == "regenerate_roadmap"
    assert turn["triggerLabel"] == "重新生成路线"
    assert turn["status"] == "completed"
    assert turn["terminalCode"] == "COMPLETED"


async def test_regenerate_roadmap_traces_failure(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    account = await make_account()
    await _make_legacy_map(db, account)
    use_reasoner(MapInspectingReasoner(degraded=True))
    await _map_turn(app_client, account, "regenerate_roadmap", "regen-fail")

    turn = (await _trace(app_client, account))["turns"][0]
    assert turn["trigger"] == "regenerate_roadmap"
    assert turn["status"] == "failed"
    assert turn["terminalCode"] == "MODEL_UNAVAILABLE"
    assert turn["safeSummary"]


async def test_roadmap_correction_retry_is_traced_then_succeeds(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """第一次输出不合法 -> 结构化纠错重试 -> 成功,全过程留痕。"""
    account = await make_account()
    use_reasoner(
        MapSequenceReasoner(
            results=(
                _map_result(None),  # 第一次只回文字,没有结构
                _map_result(_roadmap_draft()),  # 纠错后拿到合法路线图
            )
        )
    )
    await _map_turn(app_client, account, "space_entered", "enter-retry-ok")

    turn = (await _trace(app_client, account))["turns"][0]
    assert turn["status"] == "completed"
    assert turn["attempt"] == 2, "纠错重试必须体现为第二次尝试"
    assert "retrying" in turn["steps"]
    # 阶段 12:space_entered 先走一次 intake(直接 ready),再是架构调用 + 架构纠错,
    # 共三次模型边界。
    assert turn["steps"].count("waiting_model") == 3, "三次模型调用都要有等待边界"
    assert turn["steps"][-1] == "completed"


async def test_roadmap_correction_retry_exhausted_is_traced_as_failed(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """两次都不合法 -> 最终失败,而且**不泄露模型原文**。"""
    account = await make_account()
    use_reasoner(
        MapSequenceReasoner(
            results=(
                _map_result(None),
                ReasoningResult(
                    reply="第二次还是只有一句自然语言 SECRET_ROADMAP_TEXT",
                    source=ModelSource.DIRECT_LLM,
                    reasoning_map=None,
                ),
            )
        )
    )
    await _map_turn(app_client, account, "space_entered", "enter-retry-fail")

    body = await _trace(app_client, account)
    turn = body["turns"][0]
    assert turn["status"] == "failed"
    assert turn["terminalCode"] == "ROADMAP_INVALID"
    assert "retrying" in turn["steps"]
    assert "SECRET_ROADMAP_TEXT" not in str(body)


async def test_strategy_confirmation_failure_is_readable(
    app_client: httpx.AsyncClient, make_account, use_reasoner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """战略提案没过校验时,轨迹是可读的失败终态,而且只含脱敏码。"""
    account = await make_account()
    use_reasoner(MapInspectingReasoner(draft=_roadmap_draft()))
    await _map_turn(app_client, account, "space_entered", "strategy-enter")

    async def _reject(*args, **kwargs):
        return proposal_service.ProposalOutcome(
            proposal=None,
            errors=(ActionError(ordinal=1, code="ROUTE_CONFLICT", message="这条路线与现有战略冲突。"),),
        )

    monkeypatch.setattr(proposal_service, "build_from_actions", _reject)
    body = await _map_turn(app_client, account, "strategy_confirmation", "strategy-fail")
    assert body["proposalErrors"], "提案校验失败必须如实返回"

    turn = (await _trace(app_client, account))["turns"][0]
    assert turn["trigger"] == "strategy_confirmation"
    assert turn["triggerLabel"] == "确认战略"
    assert turn["status"] == "failed"
    assert turn["terminalCode"] == "PROPOSAL_VALIDATION_FAILED"
    assert "校验" in turn["safeSummary"]


async def test_strategy_confirmation_version_conflict_stays_readable(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    """确认战略**成功**产生提案后,用户在旧版本上确认 -> 版本冲突仍可读。"""
    account = await make_account()
    use_reasoner(MapInspectingReasoner(draft=_roadmap_draft()))
    await _map_turn(app_client, account, "space_entered", "strategy-v-enter")
    created = await _map_turn(app_client, account, "strategy_confirmation", "strategy-v-create")
    proposal_id = created["reasoning"]["strategyProposalId"]
    assert proposal_id, created.get("proposalErrors")

    # 计划版本前进 -> 这份提案基于的版本已经旧了。
    workspace = await db.scalar(
        select(Workspace).where(Workspace.id == uuid.UUID(account.workspace_id))
    )
    assert workspace is not None
    workspace.current_revision_version += 1
    await db.commit()

    confirm = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/proposals/{proposal_id}/confirm",
        json={"idempotencyKey": "strategy-stale-confirm"},
        headers=account.headers,
    )
    assert confirm.status_code == 409, confirm.text
    assert confirm.json()["error"]["code"] == "STALE_BASE_REVISION"

    # 战略确认那一轮本身是**成功**的(冲突发生在用户点确认,不是 Agent turn),
    # 轨迹必须如实,而不是把两者混成一句。
    turn = (await _trace(app_client, account))["turns"][0]
    assert turn["trigger"] == "strategy_confirmation"
    assert turn["status"] == "completed"


async def test_map_turn_trace_does_not_leak_user_message(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    account = await make_account()
    use_reasoner(MapInspectingReasoner(draft=_roadmap_draft()))
    await _map_turn(app_client, account, "space_entered", "leak-enter")

    secret = "地图轮私密原文 MAPSECRET-7788"
    await _map_turn(
        app_client, account, "user_message", "leak-msg", message=secret
    )
    body = await _trace(app_client, account)
    turn = body["turns"][0]
    assert turn["trigger"] == "user_message"
    raw = str(body)
    assert secret not in raw
    assert "MAPSECRET" not in raw
    # 摘要来自闭集,不是模型自由文本。
    from backend.services import agent_trace_service

    assert turn["safeSummary"] in agent_trace_service.TERMINAL_SUMMARIES.values()


async def test_cross_workspace_map_trace_is_rejected(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    account_a = await make_account(email="a@example.com")
    account_b = await make_account(email="b@example.com", workspace_title="另一个空间")
    use_reasoner(MapInspectingReasoner(draft=_roadmap_draft()))
    await _map_turn(app_client, account_a, "space_entered", "cross-enter")

    response = await app_client.get(
        f"/api/workspaces/{account_a.workspace_id}/agent/trace",
        headers=account_b.headers,
    )
    assert response.status_code == 404


#: 引用一下,避免 ruff 把未使用的 import 报掉(它在类型注解里用到)。
_ = agent_loop_service.MAX_MODEL_CALLS
