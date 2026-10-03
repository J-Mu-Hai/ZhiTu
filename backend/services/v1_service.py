"""规划智能体重构 V1 — P2:AI 战略判断、因素筛选与战略路径。

## 它是什么

程序控制流程与安全边界,模型负责**判断**:

```
INITIAL_THINKING   初始界面只有根目标 + 右侧大号“初步思考”输入区
        │  用户提交目标
        ▼
GOAL_REFRAME       模型给出整体判断 + 一个关键问题;
                   固定容器(3 组 + 10 项)作为真实 PlanNode 建立
        │  用户回答 / 讨论
        ▼
FACTOR_ANALYSIS    模型只更新焦点与受影响容器(每轮最多 3 个)
        │  信息足够
        ▼
STRATEGY_DRAFT     模型给出战略路径取舍;服务端建立最多 4 个受限战略子项
        │  用户确认
        ▼
STRATEGY_CONFIRMED_FOR_TIMELINE   P3 的交接状态(本身不生成时间架构)
```

## 三条不可破坏的边界

1. **模型只能建议,服务端决定接受什么。** 输出契约 `V1AssessmentDraft` 里没有
   “创建任意节点 / 任务 / 日期 / 周计划”的字段;服务端再按允许键、数量上限与
   前置条件校验一次(见 `_apply_assessment`)。
2. **固定容器是真实 PlanNode**(`purpose=information`),可以直接进入、详情、讨论;
   但它们不排期、不计完成度。P2 更新的是这些容器的**内容**,不生成时间线/任务/提案。
3. **失败不落半成品。** 模型不可用或输出不合格时,只记状态与可读原因,一个节点都
   不写;界面拿到的是“可重试”,不是一句编出来的结论。

`v1_stage is None` = 非 V1,本模块所有入口直接返回/不介入。老空间不迁移、不重写。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agent.runtime.base import (
    HISTORY_TURNS,
    KnownConditions,
    PlanNodeView,
    ReasoningResult,
    TurnContext,
)
from backend.contracts.reasoning import AgentTurnResponse
from backend.db.models import Conversation, GoalReasoningSession, Message, PlanNode
from backend.db.models.enums import (
    DegradedReason,
    MessageRole,
    ModelSource,
    NodeOrigin,
    NodePurpose,
    NodeType,
    ReasoningSessionPhase,
    ReasoningSessionStatus,
)
from backend.services import conversation_service, node_service, reasoning_service
from backend.services.context import WorkspaceContext
from backend.services.timeutil import today_in

# =================================================================================
# V1 阶段一档位
# =================================================================================
#: 初始界面:只有根目标与干净画布,右侧是大号“初步思考”输入区。
V1_INITIAL_THINKING = "initial_thinking"
#: 已建立三组画布,正在澄清目标定义。
V1_GOAL_REFRAME = "goal_reframe"
#: 目标定义足以讨论后,进入影响因素分析。
V1_FACTOR_ANALYSIS = "factor_analysis"
#: 目标与因素稳定后,形成战略路径草案。
V1_STRATEGY_DRAFT = "strategy_draft"
#: 用户已确认战略逻辑;P3 可以据此生成粗时间架构。**P2 本身不实现 P3。**
V1_STRATEGY_CONFIRMED = "strategy_confirmed_for_timeline"

#: V1 模型回合状态。给 UI 准确状态与重试入口。
V1_STATUS_IDLE = "idle"
V1_STATUS_RUNNING = "running"
V1_STATUS_FAILED = "failed"

#: 三个一级分组:(键, 标题, 说明)。
_GROUPS: tuple[tuple[str, str, str], ...] = (
    ("goal_reframe", "目标重构", "把一句愿望变成可判断的定义。"),
    ("problem_structure", "问题结构", "看清结果由什么决定、什么真正卡住你。"),
    ("strategy_path", "战略路径", "待形成战略路径。"),
)

#: 十个固定分析容器:(分组键, 键, 标题)。
_ANALYSIS: tuple[tuple[str, str, str], ...] = (
    ("goal_reframe", "current_state", "你现在在哪"),
    ("goal_reframe", "true_intent", "你真正想要什么"),
    ("goal_reframe", "value_assessment", "这件事值得做吗"),
    ("goal_reframe", "key_conflict", "真正卡你的是什么"),
    ("goal_reframe", "goal_definition", "最后到底要做到什么"),
    ("problem_structure", "hard_constraints", "硬约束"),
    ("problem_structure", "controllable_factors", "可控变量"),
    ("problem_structure", "key_levers", "关键杠杆"),
    ("problem_structure", "major_risks", "主要风险"),
    ("problem_structure", "external_conditions", "外部条件"),
)

#: 战略路径允许的四个受限子项:(键, 标题)。
_STRATEGY_KEYS: tuple[tuple[str, str], ...] = (
    ("main_line", "主线"),
    ("parallel_line", "并行线"),
    ("defer_or_avoid", "暂缓或放弃"),
    ("risk_control", "风险控制"),
)

#: 一轮最多接受几个容器更新。解析层已夹过一次;服务层再夹一次 ——
#: “模型可以建议,但服务端决定接受多少”不能只靠上游那一处。
MAX_V1_UPDATES = 3

#: 模型可以指涉的全部键(固定容器 + 战略受限子项)。
ALLOWED_V1_KEYS = frozenset(
    {key for key, _title, _desc in _GROUPS}
    | {key for _group, key, _title in _ANALYSIS}
    | {key for key, _title in _STRATEGY_KEYS}
)
_STRATEGY_KEY_SET = frozenset(key for key, _title in _STRATEGY_KEYS)
_STRATEGY_GROUP_KEY = "strategy_path"

#: 战略子项键 -> 线格式(camelCase)字段名。前端 `V1StrategyView` 直接读后者。
_STRATEGY_FIELD = {
    "main_line": "mainLine",
    "parallel_line": "parallelLine",
    "defer_or_avoid": "deferOrAvoid",
    "risk_control": "riskControl",
}


def is_v1(session: GoalReasoningSession | None) -> bool:
    """这个会话是不是重构 V1。`v1_stage is None` = 非 V1。"""
    return session is not None and session.v1_stage is not None


# =================================================================================
# 固定容器:真实 PlanNode(purpose=information,不排期、不计完成度)
# =================================================================================
async def _v1_nodes(db: AsyncSession, ctx: WorkspaceContext) -> list[PlanNode]:
    result = await db.execute(
        select(PlanNode)
        .where(
            PlanNode.workspace_id == ctx.id,
            PlanNode.v1_key.is_not(None),
            PlanNode.deleted_at.is_(None),
        )
        .order_by(PlanNode.depth.asc(), PlanNode.order_index.asc(), PlanNode.created_at.asc())
    )
    return list(result.scalars())


async def _create_containers(
    db: AsyncSession, ctx: WorkspaceContext, root: PlanNode
) -> None:
    """建立三组与十个固定分析容器。**幂等** —— 根下已有子节点就不重复建。"""
    existing = await db.scalar(
        select(PlanNode.id)
        .where(
            PlanNode.workspace_id == ctx.id,
            PlanNode.parent_id == root.id,
            PlanNode.deleted_at.is_(None),
        )
        .limit(1)
    )
    if existing is not None:
        return

    groups: dict[str, PlanNode] = {}
    for key, title, summary in _GROUPS:
        result = await node_service.create_node(
            db,
            ctx,
            parent_id=root.id,
            title=title,
            node_type=NodeType.CAPABILITY.value,
            purpose=NodePurpose.INFORMATION.value,
            description=summary,
            origin=NodeOrigin.AI,
            v1_key=key,
        )
        groups[key] = result.node

    for group_key, key, title in _ANALYSIS:
        parent = groups[group_key]
        await node_service.create_node(
            db,
            ctx,
            parent_id=parent.id,
            title=title,
            node_type=NodeType.CAPABILITY.value,
            purpose=NodePurpose.INFORMATION.value,
            origin=NodeOrigin.AI,
            v1_key=key,
        )


async def _ensure_strategy_nodes(
    db: AsyncSession, ctx: WorkspaceContext, session: GoalReasoningSession, values: dict[str, str]
) -> None:
    """按模型给出的战略子项建立对应的受限节点(每个键最多一次)。"""
    nodes = await _v1_nodes(db, ctx)
    existing = {node.v1_key: node for node in nodes}
    group = existing.get(_STRATEGY_GROUP_KEY)
    if group is None:
        return
    for key, title in _STRATEGY_KEYS:
        if key not in values or key in existing:
            continue
        await node_service.create_node(
            db,
            ctx,
            parent_id=group.id,
            title=title,
            node_type=NodeType.CAPABILITY.value,
            purpose=NodePurpose.INFORMATION.value,
            description=values[key],
            origin=NodeOrigin.AI,
            v1_key=key,
        )


# =================================================================================
# 回合上下文与提示词输入
# =================================================================================
def _render_canvas(nodes: list[PlanNode]) -> str:
    """把 V1 画布渲染给模型:**只有固定键,没有真实 UUID、没有用户原文之外的东西**。"""
    by_key = {node.v1_key: node for node in nodes if node.v1_key}
    lines: list[str] = []
    for key, title, _summary in _GROUPS:
        group = by_key.get(key)
        if group is not None:
            lines.append(f"[{key}] {title}")
        for group_key, child_key, child_title in _ANALYSIS:
            if group_key != key:
                continue
            child = by_key.get(child_key)
            if child is None:
                continue
            analysis = child.v1_analysis or {}
            lines.append(f"  - [{child_key}] {child_title}")
            if analysis.get("judgment"):
                lines.append(f"      判断:{analysis['judgment']}")
            if analysis.get("knownFacts"):
                lines.append("      已知事实:" + ";".join(str(item) for item in analysis["knownFacts"]))
            if analysis.get("assumptions"):
                lines.append("      AI 假设(未验证):" + ";".join(str(item) for item in analysis["assumptions"]))
            lines.append(
                f"      状态:{analysis.get('status', 'unexplored')} / "
                f"不确定性:{analysis.get('uncertainty', 'medium')}"
            )
    strategy_keys = " / ".join(key for key, _title in _STRATEGY_KEYS)
    if by_key.get(_STRATEGY_GROUP_KEY) is not None:
        lines.append(f"战略路径可用子项键:{strategy_keys}(信息足够时才用)")
    return "\n".join(lines) if lines else "(还没有固定容器)"


async def _build_turn_context(
    db: AsyncSession,
    ctx: WorkspaceContext,
    session: GoalReasoningSession,
    root: PlanNode,
    *,
    user_message: str,
    exclude_message_id=None,
) -> TurnContext:
    nodes = await _v1_nodes(db, ctx)
    page = await conversation_service.list_messages(db, ctx, limit=HISTORY_TURNS + 1)
    history = tuple(
        (message.role.value, message.content)
        for message in page.messages
        if message.id != exclude_message_id
    )[-HISTORY_TURNS:]
    today = today_in(ctx.timezone)
    return TurnContext(
        current_date=today.isoformat(),
        weekday=today.strftime("%A"),
        timezone=ctx.timezone,
        workspace_title=ctx.workspace.title or "",
        workspace_intent=ctx.workspace.intent or "",
        known=KnownConditions(goal=root.title or None),
        #: 模型只看到固定键(作为 handle)与标题;真实 UUID 不进提示词。
        nodes=tuple(
            PlanNodeView(
                handle=node.v1_key or "",
                title=node.title,
                node_type=node.node_type.value,
                status=node.status.value,
                depth=node.depth,
                parent_handle=(
                    next(
                        (
                            other.v1_key
                            for other in nodes
                            if other.id == node.parent_id and other.v1_key
                        ),
                        None,
                    )
                ),
                purpose=node.purpose.value,
            )
            for node in nodes
        ),
        history=history,
        user_message=user_message,
        purpose="v1_strategy",
        reasoning_section=_render_canvas(nodes),
        node_handles=tuple(
            (node.v1_key, str(node.id)) for node in nodes if node.v1_key
        ),
    )


# =================================================================================
# 应用模型判断(服务端强制边界)
# =================================================================================
def _apply_updates(
    nodes: list[PlanNode], draft
) -> tuple[list[PlanNode], dict[str, dict], dict[str, str]]:
    """把形状合法的更新落到**已存在的固定容器**上。返回 (改动的节点, 新分析, 战略值)。

    - 未知键直接丢弃(不新建任意节点);
    - `impacted_node_keys` 里不存在的键也丢弃;
    - 战略子项不在这里落库,由 `_ensure_strategy_nodes` 处理。
    """
    by_key = {node.v1_key: node for node in nodes if node.v1_key}
    changed: list[PlanNode] = []
    analyses: dict[str, dict] = {}
    strategy_values: dict[str, str] = {}
    for update in draft.node_updates[:MAX_V1_UPDATES]:
        key = update.node_key
        if key not in ALLOWED_V1_KEYS:
            continue
        if key in _STRATEGY_KEY_SET:
            if update.judgment:
                strategy_values[key] = update.judgment
            continue
        node = by_key.get(key)
        if node is None:
            continue
        existing = dict(node.v1_analysis or {})
        impacts = [item for item in update.impacted_node_keys if item in by_key]
        #: 键全部用 camelCase —— 线格式与 `V1NodeAnalysis` 一致,前端直接读。
        analysis = {
            "judgment": update.judgment or existing.get("judgment", ""),
            "knownFacts": list(update.known_facts) or list(existing.get("knownFacts", [])),
            "assumptions": list(update.assumptions) or list(existing.get("assumptions", [])),
            "evidence": list(update.evidence) or list(existing.get("evidence", [])),
            "importanceReason": update.importance_reason or existing.get("importanceReason", ""),
            "uncertainty": update.uncertainty,
            "status": update.status,
            "impactedNodeKeys": impacts,
            "discussionCount": int(existing.get("discussionCount", 0)) + 1,
        }
        node.v1_analysis = analysis
        # 卡片上的一句话摘要就是判断;长解释留在结构化字段里。
        if analysis["judgment"]:
            node.description = analysis["judgment"]
        node.content_version += 1
        analyses[key] = analysis
        changed.append(node)
    return changed, analyses, strategy_values


def _strategy_ready(analyses: dict[str, dict], draft) -> bool:
    """服务端的战略成形条件。**模型说了不算,这里再判一次。**"""

    def has(key: str) -> bool:
        return bool((analyses.get(key) or {}).get("judgment"))

    goal = has("goal_definition")
    conflict = has("key_conflict")
    lever = has("key_levers") or has("hard_constraints")
    risk = has("major_risks")
    return bool(goal and conflict and lever and risk)


# =================================================================================
# 回合执行
# =================================================================================
async def _run_assessment(
    db: AsyncSession,
    ctx: WorkspaceContext,
    session: GoalReasoningSession,
    *,
    user_message: str,
    reasoner,
    exclude_message_id=None,
) -> ReasoningResult:
    root = await reasoning_service.root_plan_node(db, ctx)
    if root is None:
        from backend.services.errors import InvalidInput

        raise InvalidInput("这个空间还没有根目标。")
    turn = await _build_turn_context(
        db, ctx, session, root, user_message=user_message, exclude_message_id=exclude_message_id
    )
    session.v1_status = V1_STATUS_RUNNING
    session.v1_error = None
    await db.commit()

    result = await reasoner.reason(turn)
    assessment = result.v1_assessment

    # 模型不可用 / 输出解析失败:一个字都不写,如实记录状态。
    if result.degraded:
        session.v1_status = V1_STATUS_FAILED
        #: 给用户看的那句话(已区分认证失败 / 限流 / 不可用),不是枚举名。
        session.v1_error = result.reply or "模型暂时不可用。"
        await db.commit()
        return result
    if assessment is None or (not assessment.global_assessment and not assessment.node_updates):
        # 模型没按形状给判断 —— 是“输出不合格”,不是“模型不可用”。
        session.v1_status = V1_STATUS_FAILED
        session.v1_error = "模型这次的回答没能解析成战略判断。"
        await db.commit()
        return ReasoningResult(
            reply="模型这次的回答没能解析成战略判断,可以再试一次。",
            source=result.source,
            degraded=True,
            degraded_reason=DegradedReason.MODEL_OUTPUT_INVALID,
            retryable=True,
            request_id=result.request_id,
            prompt_version=result.prompt_version,
            model_name=result.model_name,
            latency_ms=result.latency_ms,
        )

    nodes = await _v1_nodes(db, ctx)
    _changed, _analyses, strategy_values = _apply_updates(nodes, assessment)
    # 战略成形条件看的是**库里当前**的判断(包含这一轮刚写进去的),不是只看本轮。
    persisted = {node.v1_key: (node.v1_analysis or {}) for node in nodes if node.v1_key}

    # 战略路径:服务端条件满足且模型给了子项时,建立受限节点并记录草案。
    if strategy_values and _strategy_ready(persisted, assessment):
        await _ensure_strategy_nodes(db, ctx, session, strategy_values)
        merged = dict(session.v1_strategy or {})
        for key, value in strategy_values.items():
            merged[_STRATEGY_FIELD.get(key, key)] = value
        merged["tradeoff"] = assessment.strategy_tradeoff or merged.get("tradeoff", "")
        merged["confirmed"] = False
        session.v1_strategy = merged
        if session.v1_stage in (V1_GOAL_REFRAME, V1_FACTOR_ANALYSIS):
            session.v1_stage = V1_STRATEGY_DRAFT

    if assessment.global_assessment:
        session.v1_judgment = assessment.global_assessment
    session.v1_question = assessment.question or None
    session.v1_focus_key = assessment.focus_key
    session.v1_focus_reason = assessment.focus_reason or None
    if session.v1_stage == V1_INITIAL_THINKING:
        session.v1_stage = V1_GOAL_REFRAME
    session.phase = ReasoningSessionPhase.ROADMAP_DRAFT
    session.status = ReasoningSessionStatus.READY
    session.v1_status = V1_STATUS_IDLE
    session.v1_error = None
    await db.commit()
    return result


async def answer_v1_in_conversation(
    db: AsyncSession,
    ctx: WorkspaceContext,
    reasoner,
    session: GoalReasoningSession,
    *,
    content: str,
    client_message_id: str | None,
    context_node_id,
):
    """V1 空间里的用户消息:交给模型做战略判断,服务端再决定写入什么。"""
    conversation = await conversation_service.get_or_create_primary_conversation(db, ctx)
    user_message = await conversation_service.record_user_message(
        db,
        ctx,
        conversation=conversation,
        text=content,
        client_message_id=client_message_id,
        context_node_id=context_node_id,
    )
    # 幂等:这一条已经有一轮助手回复就不再重复跑模型、重复写。
    existing = await conversation_service.find_reply_after(
        db, conversation.id, user_message.seq
    )
    if existing is not None and existing.role is MessageRole.ASSISTANT:
        return conversation_service.turn_outcome_for_reply(
            user_message=user_message, assistant_message=existing, brief=None
        )

    root = await reasoning_service.root_plan_node(db, ctx)
    if root is not None:
        await _create_containers(db, ctx, root)

    result = await _run_assessment(
        db,
        ctx,
        session,
        user_message=content,
        reasoner=reasoner,
        exclude_message_id=user_message.id,
    )
    message = await _append_assistant(
        db, ctx, reply=result.reply, conversation=conversation, result=result
    )
    await db.commit()
    return conversation_service.turn_outcome_for_reply(
        user_message=user_message, assistant_message=message, brief=None
    )


async def _append_assistant(
    db,
    ctx: WorkspaceContext,
    *,
    reply: str,
    conversation: Conversation | None = None,
    result: ReasoningResult | None = None,
) -> Message:
    conversation = conversation or await conversation_service.get_or_create_primary_conversation(
        db, ctx
    )
    reason = result or ReasoningResult(
        reply=reply,
        source=ModelSource.DIRECT_LLM,
        request_id="v1",
        prompt_version="v1-strategy",
    )
    return await conversation_service.append_reply(
        db, ctx, conversation=conversation, result=reason
    )


# =================================================================================
# 自动推进(space_entered / retry)与战略确认
# =================================================================================
async def advance(
    db: AsyncSession,
    ctx: WorkspaceContext,
    root,
    session: GoalReasoningSession,
    *,
    trace,
) -> AgentTurnResponse:
    """进入空间:只建立初始状态,不调用模型、不生成计划。"""
    if session.v1_stage is None:
        session.v1_stage = V1_INITIAL_THINKING
        session.phase = ReasoningSessionPhase.INTAKE
    session.status = ReasoningSessionStatus.READY
    await db.commit()
    if trace is not None:
        from backend.services import agent_trace_service

        agent_trace_service.mark_terminal(
            trace,
            degraded=False,
            degraded_reason=None,
            stopped_reason="ready_to_propose",
            code=None,
        )
    return await reasoning_service._response(db, ctx, session, changed=False)


async def confirm_strategy(
    db: AsyncSession, ctx: WorkspaceContext, session: GoalReasoningSession
) -> AgentTurnResponse:
    """用户确认战略逻辑:**只进入 P3 的准备状态**,不生成任何阶段或时间线。"""
    from backend.services.errors import InvalidInput

    strategy = dict(session.v1_strategy or {})
    if not strategy:
        raise InvalidInput("现在还没有可确认的战略路径。")
    strategy["confirmed"] = True
    session.v1_strategy = strategy
    session.v1_stage = V1_STRATEGY_CONFIRMED
    await db.commit()
    return await reasoning_service._response(db, ctx, session, changed=True)


__all__ = [
    "ALLOWED_V1_KEYS",
    "V1_FACTOR_ANALYSIS",
    "V1_GOAL_REFRAME",
    "V1_INITIAL_THINKING",
    "V1_STATUS_FAILED",
    "V1_STATUS_IDLE",
    "V1_STATUS_RUNNING",
    "V1_STRATEGY_CONFIRMED",
    "V1_STRATEGY_DRAFT",
    "advance",
    "answer_v1_in_conversation",
    "confirm_strategy",
    "is_v1",
]
