"""规划智能体重构 V1 — P2:AI 战略判断、因素筛选与战略路径。

## 结构:分组是画布节点,子项是**画布问题节点**

```
根目标 (画布节点)
├─ 目标重构        ← 画布节点(PlanNode,固定框架,可进入)
│   └─ 进入后有若干**紫色画布问题节点**围绕它
├─ 问题结构        ← 画布节点
│   └─ 进入后有若干紫色画布问题节点
└─ 战略路径        ← 画布节点(先空,信息足够后长出问题)
```

- 三个分组是 `plan_nodes`(`purpose=information`),**正常在主画布看不到子项**;
  进入某个分组(它的子空间)才看到归属它的 `agent_questions`。
- 这些紫色问题节点是 `AgentQuestion`(`presentation=canvas_question`),不是计划节点:
  不参与排期 / 依赖 / 统计。
- 需要在**对话框**回答的问题走橙色(会话 intake),不出现在画布上。

## 程序控制 + 模型判断

模型只负责判断:**整体判断、已存在问题的结论/事实/假设、一个焦点、一个问题、战略取舍**。
它只能更新已存在的问题键,不能新建任意节点;每轮最多 3 个更新;战略只在四个受限键上加
节点。服务端决定接受什么、写什么。

## 失败不落半成品

模型不可用 / 输出不合格分别记为准确状态,一个问句内容都不改。`v1_stage is None` = 非 V1,
本模块所有入口直接返回/不介入。
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
from backend.db.models import AgentQuestion, Conversation, GoalReasoningSession, Message, PlanNode
from backend.db.models.enums import (
    DegradedReason,
    MessageRole,
    ModelSource,
    NodeOrigin,
    NodePurpose,
    NodeType,
    QuestionPresentation,
    QuestionResponseMode,
    QuestionStatus,
    ReasoningSessionPhase,
    ReasoningSessionStatus,
)
from backend.services import conversation_service, node_service, reasoning_service
from backend.services.context import WorkspaceContext
from backend.services.timeutil import today_in

# =================================================================================
# V1 阶段一档位
# =================================================================================
V1_INITIAL_THINKING = "initial_thinking"
V1_GOAL_REFRAME = "goal_reframe"
V1_FACTOR_ANALYSIS = "factor_analysis"
V1_STRATEGY_DRAFT = "strategy_draft"
#: 用户已确认战略逻辑;P3 可以据此生成粗时间架构。**P2 本身不实现 P3。**
V1_STRATEGY_CONFIRMED = "strategy_confirmed_for_timeline"

V1_STATUS_IDLE = "idle"
V1_STATUS_RUNNING = "running"
V1_STATUS_FAILED = "failed"

#: 三个一级分组:(键, 标题, 说明)。
_GROUPS: tuple[tuple[str, str, str], ...] = (
    ("goal_reframe", "目标重构", "把一句愿望变成可判断的定义。"),
    ("problem_structure", "问题结构", "看清结果由什么决定、什么真正卡住你。"),
    ("strategy_path", "战略路径", "待形成战略路径。"),
)

#: 十个固定画布问题节点:(分组键, 键, 问题, 为什么问)。
_ANALYSIS: tuple[tuple[str, str, str, str], ...] = (
    ("goal_reframe", "current_state", "你现在在哪?", "已有基础、可投入时间与可用资源决定起点,也决定每个阶段多长。"),
    ("goal_reframe", "true_intent", "你希望最后能拿出什么具体结果,证明它真正解决了你的问题?", "真实意图不同,成果定义与阶段顺序会完全不同。"),
    ("goal_reframe", "value_assessment", "这件事值得做吗?如果三年内没有直接回报,你还会做吗?", "先判断值不值得投入,再谈怎么投入,避免把时间花在伪目标上。"),
    ("goal_reframe", "key_conflict", "真正卡你的是什么?只解决一个障碍,哪一个解决了整件事就会推进?", "只处理最关键的一两个矛盾,比同时补十个短板更有效。"),
    ("goal_reframe", "goal_definition", "最后到底要做到什么?做到什么程度、拿出什么,你就认为这件事成了?", "没有可观察的成果定义,后面的阶段与时间线都无从判断。"),
    ("problem_structure", "hard_constraints", "有哪些是你不能改、只能接受的限制?", "硬约束决定哪些路线根本不可行,必须先于偏好确认。"),
    ("problem_structure", "controllable_factors", "在这件事上,哪些是你能直接行动改变的?", "只讨论能改变的东西,才能把注意力放在真正有产出的动作上。"),
    ("problem_structure", "key_levers", "哪个变量一旦改善,最终结果的提升最大?", "抓住关键杠杆,比均匀用力更快看到结果。"),
    ("problem_structure", "major_risks", "最可能让这件事失败的是什么?你能提前看到什么信号?", "提前识别风险与信号,才能设置检查点与备用路径。"),
    ("problem_structure", "external_conditions", "有哪些外部因素不在你控制内,却会明显影响结果?", "外部条件不在你控制内,却常常决定路线的可行性。"),
)

#: 战略路径的四个受限问题:(键, 标题, 问题)。
_STRATEGY_KEYS: tuple[tuple[str, str, str], ...] = (
    ("main_line", "主线", "主线:最优先投入什么?"),
    ("parallel_line", "并行线", "并行线:哪些可以同时做,但不该挤占主线?"),
    ("defer_or_avoid", "暂缓或放弃", "暂缓/放弃:当前不值得做什么?"),
    ("risk_control", "风险控制", "风险控制:在哪里设置检查点或备用路径?"),
)

MAX_V1_UPDATES = 3

#: 模型可以指涉的全部键(10 个固定问题 + 4 个战略问题)。**分组键不可被模型更新。**
ALLOWED_V1_KEYS = frozenset(
    {key for _group, key, _q, _why in _ANALYSIS} | {key for key, _t, _q in _STRATEGY_KEYS}
)
_STRATEGY_KEY_SET = frozenset(key for key, _t, _q in _STRATEGY_KEYS)
_STRATEGY_GROUP_KEY = "strategy_path"
_GROUP_KEY_SET = frozenset(key for key, _t, _d in _GROUPS)

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
# 读
# =================================================================================
async def _v1_groups(db: AsyncSession, ctx: WorkspaceContext) -> list[PlanNode]:
    result = await db.execute(
        select(PlanNode)
        .where(
            PlanNode.workspace_id == ctx.id,
            PlanNode.v1_key.in_(tuple(_GROUP_KEY_SET)),
            PlanNode.deleted_at.is_(None),
        )
        .order_by(PlanNode.order_index.asc(), PlanNode.created_at.asc())
    )
    return list(result.scalars())


async def _v1_questions(db: AsyncSession, ctx: WorkspaceContext) -> list[AgentQuestion]:
    result = await db.execute(
        select(AgentQuestion)
        .where(AgentQuestion.workspace_id == ctx.id, AgentQuestion.v1_key.is_not(None))
        .order_by(AgentQuestion.created_at.asc())
    )
    return list(result.scalars())


# =================================================================================
# 建:分组(PlanNode)+ 固定问题节点(AgentQuestion)
# =================================================================================
async def _create_question(
    db: AsyncSession,
    ctx: WorkspaceContext,
    *,
    group: PlanNode,
    key: str,
    question: str,
    why_now: str,
) -> AgentQuestion:
    row = AgentQuestion(
        workspace_id=ctx.id,
        source_node_id=group.id,
        source_message_id=None,
        presentation=QuestionPresentation.CANVAS_QUESTION,
        question=question,
        why_now=why_now,
        response_mode=QuestionResponseMode.FREE_TEXT,
        options=[],
        allow_custom_input=True,
        status=QuestionStatus.PENDING,
        events=[],
        v1_key=key,
    )
    db.add(row)
    await db.flush()
    return row


async def _create_containers(
    db: AsyncSession, ctx: WorkspaceContext, root: PlanNode
) -> None:
    """建立三组画布节点与十个固定紫色问题节点。**幂等**。"""
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

    for group_key, key, question, why_now in _ANALYSIS:
        await _create_question(
            db, ctx, group=groups[group_key], key=key, question=question, why_now=why_now
        )


async def _ensure_strategy_questions(
    db: AsyncSession, ctx: WorkspaceContext, values: dict[str, str]
) -> None:
    """战略成形时,在“战略路径”分组下建立受限问题节点(每个键最多一次)。"""
    groups = {group.v1_key: group for group in await _v1_groups(db, ctx)}
    group = groups.get(_STRATEGY_GROUP_KEY)
    if group is None:
        return
    existing = {q.v1_key for q in await _v1_questions(db, ctx)}
    for key, title, question in _STRATEGY_KEYS:
        if key not in values or key in existing:
            continue
        row = await _create_question(
            db, ctx, group=group, key=key, question=question, why_now=title
        )
        row.analysis_summary = values[key]
        row.v1_analysis = {"judgment": values[key], "status": "discussing"}


# =================================================================================
# 回合上下文
# =================================================================================
def _render_canvas(groups: list[PlanNode], questions: list[AgentQuestion]) -> str:
    by_group: dict[str | None, list[AgentQuestion]] = {}
    for question in questions:
        by_group.setdefault(str(question.source_node_id) if question.source_node_id else None, []).append(question)
    lines: list[str] = []
    title_of = {group.v1_key: group.title for group in groups}
    for key, title, _summary in _GROUPS:
        if key not in title_of:
            continue
        group = next(group for group in groups if group.v1_key == key)
        lines.append(f"[{key}] {title}")
        for question in by_group.get(str(group.id), []):
            lines.append(f"  - [{question.v1_key}] {question.question}")
            analysis = question.v1_analysis or {}
            if analysis.get("judgment"):
                lines.append(f"      判断:{analysis['judgment']}")
            if analysis.get("knownFacts"):
                lines.append("      已知事实:" + ";".join(str(x) for x in analysis["knownFacts"]))
            if analysis.get("assumptions"):
                lines.append("      AI 假设(未验证):" + ";".join(str(x) for x in analysis["assumptions"]))
            if question.answer:
                answer = question.answer.get("customInput") if isinstance(question.answer, dict) else None
                if answer:
                    lines.append(f"      用户已回答:{answer}")
            lines.append(
                f"      状态:{analysis.get('status', 'unexplored')} / "
                f"不确定性:{analysis.get('uncertainty', 'medium')} / 提问状态:{question.status.value}"
            )
    strategy_keys = " / ".join(key for key, _t, _q in _STRATEGY_KEYS)
    if _STRATEGY_GROUP_KEY in title_of:
        lines.append(f"战略路径可用子项键:{strategy_keys}(信息足够时才用)")
    return "\n".join(lines) if lines else "(还没有固定问题)"


async def _build_turn_context(
    db: AsyncSession,
    ctx: WorkspaceContext,
    session: GoalReasoningSession,
    root: PlanNode,
    *,
    user_message: str,
    exclude_message_id=None,
) -> TurnContext:
    questions = await _v1_questions(db, ctx)
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
        nodes=tuple(
            PlanNodeView(
                handle=question.v1_key or "",
                title=question.question[:80],
                node_type="question",
                status=question.status.value,
                depth=1,
                purpose="information",
            )
            for question in questions
            if question.v1_key
        ),
        history=history,
        user_message=user_message,
        purpose="v1_strategy",
        reasoning_section=_render_canvas(await _v1_groups(db, ctx), questions),
        node_handles=tuple(
            (question.v1_key, str(question.id)) for question in questions if question.v1_key
        ),
    )


# =================================================================================
# 应用模型判断
# =================================================================================
def _apply_updates(
    questions: list[AgentQuestion], draft
) -> tuple[list[AgentQuestion], dict[str, dict], dict[str, str]]:
    by_key = {question.v1_key: question for question in questions if question.v1_key}
    changed: list[AgentQuestion] = []
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
        question = by_key.get(key)
        if question is None:
            continue
        existing = dict(question.v1_analysis or {})
        impacts = [item for item in update.impacted_node_keys if item in by_key]
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
        question.v1_analysis = analysis
        # 卡片/问题节点上的一句话:分析摘要与依据。
        if analysis["judgment"]:
            question.analysis_summary = analysis["judgment"]
        if analysis["importanceReason"]:
            question.decision_impact = analysis["importanceReason"]
        if analysis["assumptions"]:
            question.confidence_note = "AI 假设:" + ";".join(str(x) for x in analysis["assumptions"])
        analyses[key] = analysis
        changed.append(question)
    return changed, analyses, strategy_values


def _strategy_ready(analyses: dict[str, dict], draft) -> bool:
    """服务端的战略成形条件。**模型说了不算,这里再判一次。**"""

    def has(key: str) -> bool:
        return bool((analyses.get(key) or {}).get("judgment"))

    return bool(
        has("goal_definition")
        and has("key_conflict")
        and (has("key_levers") or has("hard_constraints"))
        and has("major_risks")
    )


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

    if result.degraded:
        session.v1_status = V1_STATUS_FAILED
        session.v1_error = result.reply or "模型暂时不可用。"
        await db.commit()
        return result
    if assessment is None or (not assessment.global_assessment and not assessment.node_updates):
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

    questions = await _v1_questions(db, ctx)
    _changed, _analyses, strategy_values = _apply_updates(questions, assessment)
    persisted = {q.v1_key: (q.v1_analysis or {}) for q in questions if q.v1_key}

    if strategy_values and _strategy_ready(persisted, assessment):
        await _ensure_strategy_questions(db, ctx, strategy_values)
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
    #: 需要在对话框回答的橙色问题(至多一个)。
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
    """V1 空间里的用户消息(含紫色问题节点的回答):交给模型做战略判断。"""
    conversation = await conversation_service.get_or_create_primary_conversation(db, ctx)
    user_message = await conversation_service.record_user_message(
        db,
        ctx,
        conversation=conversation,
        text=content,
        client_message_id=client_message_id,
        context_node_id=context_node_id,
    )
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
# 自动推进与战略确认
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
