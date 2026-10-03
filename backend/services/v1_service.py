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

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

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
    NodeStatus,
    NodeType,
    QuestionPresentation,
    QuestionResponseMode,
    QuestionStatus,
    ReasoningSessionPhase,
    ReasoningSessionStatus,
    RevisionTrigger,
)
from backend.services import (
    audit_service,
    conversation_service,
    node_service,
    proposal_service,
    reasoning_service,
    turn_context,
    v01_service,
)
from backend.services.context import WorkspaceContext
from backend.services.timeutil import today_in

# =================================================================================
# V1 阶段一档位
# =================================================================================
V1_INITIAL_THINKING = "initial_thinking"
V1_GOAL_REFRAME = "goal_reframe"
V1_FACTOR_ANALYSIS = "factor_analysis"
V1_STRATEGY_DRAFT = "strategy_draft"
#: 用户已确认战略逻辑;P3 可以据此生成粗时间架构。
V1_STRATEGY_CONFIRMED = "strategy_confirmed_for_timeline"
#: P3:已生成 3–6 个阶段的粗时间架构草案,等用户确认。
V1_COARSE_TIMELINE_REVIEW = "coarse_timeline_review"
#: P4:粗时间架构已确认,进入周/日计划与执行。
V1_WEEKLY_EXECUTION = "weekly_execution"
#: P4:按执行偏差重规划未来。
V1_REPLANNING = "replanning"

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


async def _audit(db, ctx: WorkspaceContext, session, event_type: str, **kwargs):
    """写一条 V1 审计事件。**只 flush,随调用方的领域事务一起提交。**"""
    return await audit_service.record(db, ctx, session, event_type=event_type, **kwargs)


async def _guard_reject(db, ctx, session, *, reason: str, code: str = "GUARD_REJECTED") -> None:
    """守卫拒绝:记一条**失败**事件并提交,然后把拒绝交给调用方抛出。"""
    await _audit(
        db,
        ctx,
        session,
        "guard_rejected",
        summary=reason,
        validation_status="failed",
        error_code=code,
    )
    await db.commit()


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
    stage_before = session.v1_stage
    question_before = session.v1_question
    _existing = await _v1_questions(db, ctx)
    before_status = {
        q.v1_key: (q.v1_analysis or {}).get("status") for q in _existing if q.v1_key
    }
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
        await _audit(
            db,
            ctx,
            session,
            "model_unavailable",
            trigger="user_message",
            source=result.source.value if result.source else None,
            summary=session.v1_error,
            validation_status="failed",
            error_code=str(result.degraded_reason or "MODEL_UNAVAILABLE"),
        )
        await db.commit()
        return result
    if assessment is None or (not assessment.global_assessment and not assessment.node_updates):
        session.v1_status = V1_STATUS_FAILED
        session.v1_error = "模型这次的回答没能解析成战略判断。"
        await _audit(
            db,
            ctx,
            session,
            "model_output_invalid",
            trigger="user_message",
            source=result.source.value if result.source else None,
            summary=session.v1_error,
            validation_status="failed",
            error_code="MODEL_OUTPUT_INVALID",
        )
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
    changed, analyses, strategy_values = _apply_updates(questions, assessment)
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

    # ---- 审计:全局判断 / 关键问题 / 节点更新 / 战略草案 ----
    await _audit(
        db,
        ctx,
        session,
        "global_assessment_generated",
        trigger="user_message",
        stage_before=stage_before,
        stage_after=session.v1_stage,
        focus_key=session.v1_focus_key,
        focus_reason=session.v1_focus_reason,
        source=result.source.value if result.source else None,
        summary=assessment.global_assessment,
        payload={"globalAssessment": assessment.global_assessment},
    )
    if session.v1_question and session.v1_question != question_before:
        await _audit(
            db,
            ctx,
            session,
            "global_question_asked",
            trigger="user_message",
            focus_key=session.v1_focus_key,
            summary=session.v1_question,
            payload={"question": session.v1_question, "focus": session.v1_focus_key},
        )
    node_updates = [
        {
            "key": question.v1_key,
            "beforeStatus": before_status.get(question.v1_key),
            "afterStatus": analyses[question.v1_key]["status"],
            "summary": analyses[question.v1_key]["judgment"],
            "facts": analyses[question.v1_key]["knownFacts"],
            "assumptions": analyses[question.v1_key]["assumptions"],
            "evidence": analyses[question.v1_key]["evidence"],
        }
        for question in changed
    ]
    if node_updates:
        await _audit(
            db,
            ctx,
            session,
            "node_analysis_updated",
            trigger="user_message",
            focus_key=session.v1_focus_key,
            summary=f"更新了 {len(node_updates)} 个分析节点。",
            payload={"nodeUpdates": node_updates},
        )
    if strategy_values and session.v1_strategy:
        await _audit(
            db,
            ctx,
            session,
            "strategy_draft_generated",
            stage_before=stage_before,
            stage_after=session.v1_stage,
            focus_key=session.v1_focus_key,
            summary="形成战略路径草案。",
            payload={"strategy": session.v1_strategy},
        )
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
    trigger: str = "user_message",
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

    # ---- 审计:用户输入 / 紫色问题回答 ----
    if session.v1_stage == V1_INITIAL_THINKING:
        await _audit(
            db,
            ctx,
            session,
            "initial_thinking_submitted",
            trigger=trigger,
            stage_before=V1_INITIAL_THINKING,
            summary=content,
        )
    elif trigger == "question_answered" or context_node_id is not None:
        await _audit(
            db,
            ctx,
            session,
            "canvas_question_answered",
            trigger=trigger,
            focus_key=session.v1_focus_key,
            summary=content,
        )

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


async def _response(
    db,
    ctx: WorkspaceContext,
    session: GoalReasoningSession,
    *,
    message=None,
    changed: bool = False,
    trace=None,
) -> AgentTurnResponse:
    """先提交写入、再拼视图 —— **提交必须在拼视图之前**,否则视图看到的是旧状态。"""
    if trace is not None:
        from backend.services import agent_trace_service

        agent_trace_service.mark_terminal(
            trace, degraded=False, degraded_reason=None, stopped_reason="ready_to_propose", code=None
        )
    await db.commit()
    return await reasoning_service._response(db, ctx, session, message=message, changed=changed)


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
# P3:粗时间架构(战略确认后才生成)
# =================================================================================
def _timeline_payload(phases) -> list[dict]:
    """阶段草案 -> 前端时间轴的**唯一权威投影**(与 V0.1 同形)。"""
    payload: list[dict] = []
    for index, phase in enumerate(phases, start=1):
        payload.append(
            {
                "id": f"phase-{index}",
                "title": phase.title,
                "kind": "phase",
                "startWeek": phase.start_week,
                "endWeek": phase.end_week,
                "startDate": phase.start_date,
                "endDate": phase.end_date,
                "goal": phase.goal,
                "deliverable": phase.deliverable,
                "completionCriteria": phase.completion_criteria,
                "status": "draft",
                "planNodeId": None,
            }
        )
    return payload


async def _build_timeline_turn_context(
    db: AsyncSession, ctx: WorkspaceContext, session: GoalReasoningSession
) -> TurnContext:
    page = await conversation_service.list_messages(db, ctx, limit=HISTORY_TURNS)
    history = tuple((m.role.value, m.content) for m in page.messages)[-HISTORY_TURNS:]
    today = today_in(ctx.timezone)
    strategy = session.v1_strategy or {}
    lines = ["已确认的战略逻辑:"]
    for field, label in (
        ("mainLine", "主线"),
        ("parallelLine", "并行线"),
        ("deferOrAvoid", "暂缓/放弃"),
        ("riskControl", "风险控制"),
    ):
        if strategy.get(field):
            lines.append(f"- {label}:{strategy[field]}")
    if strategy.get("tradeoff"):
        lines.append(f"- 取舍:{strategy['tradeoff']}")
    return TurnContext(
        current_date=today.isoformat(),
        weekday=today.strftime("%A"),
        timezone=ctx.timezone,
        workspace_title=ctx.workspace.title or "",
        workspace_intent=ctx.workspace.intent or "",
        known=KnownConditions(),
        history=history,
        user_message="把已确认的战略逻辑投影成粗时间架构。",
        purpose="v1_timeline",
        reasoning_section="\n".join(lines),
    )


async def _root_handle(db: AsyncSession, ctx: WorkspaceContext, root: PlanNode):
    conversation = await conversation_service.find_primary_conversation(db, ctx)
    turn = await turn_context.build_turn_context(
        db,
        ctx,
        conversation_id=conversation.id if conversation else uuid.uuid4(),
        user_message="生成时间架构提案",
        context_node_id=root.id,
        scope_root_id=root.id,
    )
    handle = next((h for h, node_id in turn.node_handles if node_id == str(root.id)), None)
    if handle is None:
        from backend.services.errors import InvalidInput

        raise InvalidInput("找不到根目标的记号。")
    return handle, turn.node_handles


async def generate_coarse_timeline(
    db: AsyncSession,
    ctx: WorkspaceContext,
    session: GoalReasoningSession,
    reasoner,
    *,
    trace=None,
) -> ReasoningResult:
    """把已确认战略投影为 3–6 个阶段,落成**待确认提案**。不写正式计划。"""
    from backend.services.errors import InvalidInput

    if session.v1_stage != V1_STRATEGY_CONFIRMED:
        await _guard_reject(db, ctx, session, reason="当前不在“可生成粗时间架构”的状态。")
        raise InvalidInput("当前不在“可生成粗时间架构”的状态。")
    root = await reasoning_service.root_plan_node(db, ctx)
    if root is None:
        raise InvalidInput("这个空间还没有根目标。")
    turn = await _build_timeline_turn_context(db, ctx, session)
    session.v1_status = V1_STATUS_RUNNING
    session.v1_error = None
    await db.commit()

    result = await reasoner.reason(turn)
    draft = result.v1_timeline
    if result.degraded:
        session.v1_status = V1_STATUS_FAILED
        session.v1_error = result.reply or "模型暂时不可用。"
        await _audit(
            db,
            ctx,
            session,
            "model_unavailable",
            trigger="strategy_confirmation",
            source=result.source.value if result.source else None,
            summary=session.v1_error,
            validation_status="failed",
            error_code=str(result.degraded_reason or "MODEL_UNAVAILABLE"),
        )
        await db.commit()
        return result
    if draft is None or not draft.phases:
        session.v1_status = V1_STATUS_FAILED
        session.v1_error = "模型这次没有给出合法的时间架构。"
        await _audit(
            db,
            ctx,
            session,
            "model_output_invalid",
            trigger="strategy_confirmation",
            source=result.source.value if result.source else None,
            summary=session.v1_error,
            validation_status="failed",
            error_code="MODEL_OUTPUT_INVALID",
        )
        await db.commit()
        return ReasoningResult(
            reply="模型这次没有给出合法的时间架构,可以再试一次。",
            source=result.source,
            degraded=True,
            degraded_reason=DegradedReason.MODEL_OUTPUT_INVALID,
            retryable=True,
            request_id=result.request_id,
            prompt_version=result.prompt_version,
        )

    session.v01_timeline = _timeline_payload(draft.phases)
    root_handle, handles = await _root_handle(db, ctx, root)
    actions: list[dict] = []
    for index, phase in enumerate(draft.phases, start=1):
        description = (
            f"相对范围:第 {phase.start_week}–{phase.end_week} 周\n"
            f"目标:{phase.goal}\n"
            f"成果:{phase.deliverable}"
        )
        actions.append(
            {
                "op": "create_node",
                "localId": f"n{9200 + index}",
                "parentRef": root_handle,
                "title": phase.title,
                "nodeType": "stage",
                "purpose": "planning",
                "description": description,
                "acceptanceCriteria": phase.completion_criteria or None,
            }
        )
    conversation = await conversation_service.get_or_create_primary_conversation(db, ctx)
    outcome = await proposal_service.build_from_actions(
        db,
        ctx,
        conversation_id=conversation.id,
        actions=tuple(actions),
        handles=handles,
        reasoning="由已确认战略投影出的粗时间架构草案。",
        assistant_message=None,
        trigger_type=RevisionTrigger.INITIAL_PLAN,
    )
    if outcome.proposal is None:
        session.v1_status = V1_STATUS_FAILED
        session.v1_error = ";".join(error.message for error in outcome.errors) or "时间架构没有通过校验。"
        await _audit(
            db,
            ctx,
            session,
            "proposal_validation_failed",
            trigger="strategy_confirmation",
            summary=session.v1_error,
            validation_status="failed",
            error_code="PROPOSAL_VALIDATION_FAILED",
        )
        await db.commit()
        return ReasoningResult(
            reply=session.v1_error,
            source=result.source,
            degraded=True,
            degraded_reason=DegradedReason.MODEL_OUTPUT_INVALID,
            retryable=True,
            request_id=result.request_id,
            prompt_version=result.prompt_version,
        )

    session.timeline_proposal_id = outcome.proposal.id
    session.v1_stage = V1_COARSE_TIMELINE_REVIEW
    session.v1_status = V1_STATUS_IDLE
    session.v1_error = None
    await _audit(
        db,
        ctx,
        session,
        "coarse_timeline_draft_generated",
        stage_before=V1_STRATEGY_CONFIRMED,
        stage_after=V1_COARSE_TIMELINE_REVIEW,
        summary="生成 3–6 个阶段的粗时间架构草案。",
        payload={
            "phases": [
                {
                    "title": phase.title,
                    "startWeek": phase.start_week,
                    "endWeek": phase.end_week,
                }
                for phase in draft.phases
            ]
        },
    )
    await _audit(
        db,
        ctx,
        session,
        "timeline_proposal_created",
        summary="粗时间架构提案已生成,待用户确认。",
        payload={
            "proposal": {
                "id": str(outcome.proposal.id),
                "kind": "timeline",
                "status": "pending_confirmation",
            }
        },
    )
    await db.commit()
    if trace is not None:
        from backend.services import agent_trace_service

        agent_trace_service.mark_terminal(
            trace, degraded=False, degraded_reason=None, stopped_reason="ready_to_propose", code=None
        )
    return result


async def _proposal_kind(db: AsyncSession, proposal_id) -> str | None:
    """从提案条目标题判断它是周计划还是日计划(用于审计事件)。"""
    from backend.db.models import ProposalItem

    items = list(
        await db.scalars(
            select(ProposalItem).where(ProposalItem.proposal_id == proposal_id)
        )
    )
    titles = " ".join(str((item.payload or {}).get("title") or "") for item in items)
    if "本周计划" in titles or "下周预览" in titles:
        return "weekly"
    if "日计划" in titles:
        return "daily"
    return None


async def on_proposal_confirmed(
    db: AsyncSession, ctx: WorkspaceContext, proposal_id
) -> None:
    """提案确认后由路由调用:V1 的时间架构 / 重规划在这里推进状态。"""
    session = await reasoning_service.get_session(db, ctx)
    if session is None or not is_v1(session):
        return
    timeline = list(session.v01_timeline or [])
    if session.v1_stage == V1_COARSE_TIMELINE_REVIEW and session.timeline_proposal_id == proposal_id:
        root = await reasoning_service.root_plan_node(db, ctx)
        if root is not None:
            stages = await db.execute(
                select(PlanNode).where(
                    PlanNode.workspace_id == ctx.id,
                    PlanNode.parent_id == root.id,
                    PlanNode.node_type == NodeType.STAGE,
                    PlanNode.deleted_at.is_(None),
                )
            )
            by_title = {node.title: node.id for node in stages.scalars()}
            for item in timeline:
                if isinstance(item, dict):
                    item["status"] = "planned"
                    linked = by_title.get(str(item.get("title") or ""))
                    item["planNodeId"] = str(linked) if linked else None
        session.v01_timeline = timeline
        session.v1_stage = V1_WEEKLY_EXECUTION
        await _audit(
            db,
            ctx,
            session,
            "timeline_confirmed",
            stage_before=V1_COARSE_TIMELINE_REVIEW,
            stage_after=V1_WEEKLY_EXECUTION,
            summary="时间线已确认,写入正式阶段。",
            payload={"proposal": {"id": str(proposal_id), "kind": "timeline", "status": "applied"}},
        )
        await db.commit()
    elif session.v1_stage == V1_REPLANNING:
        for item in timeline:
            if isinstance(item, dict):
                item["status"] = "planned"
        session.v01_timeline = timeline
        session.v1_stage = V1_WEEKLY_EXECUTION
        await _audit(
            db,
            ctx,
            session,
            "replan_confirmed",
            stage_before=V1_REPLANNING,
            stage_after=V1_WEEKLY_EXECUTION,
            summary="未来重规划已确认,已完成历史不变。",
            payload={"proposal": {"id": str(proposal_id), "kind": "replan", "status": "applied"}},
        )
        await db.commit()
    else:
        kind = await _proposal_kind(db, proposal_id)
        if kind in ("weekly", "daily"):
            await _audit(
                db,
                ctx,
                session,
                "weekly_plan_confirmed" if kind == "weekly" else "daily_plan_confirmed",
                summary="本周计划已确认。" if kind == "weekly" else "日计划已确认。",
                payload={"proposal": {"id": str(proposal_id), "kind": kind, "status": "applied"}},
            )
            await db.commit()


# =================================================================================
# P4:周/日计划、执行反馈与自动回顾重规划
# =================================================================================
async def generate_weekly_plan(
    db: AsyncSession, ctx: WorkspaceContext, session: GoalReasoningSession, *, trace=None
) -> AgentTurnResponse:
    """从已确认时间线派生**本周计划 + 下周预览**(复用 V0.1 的版本化提案)。"""
    from backend.services.errors import InvalidInput

    if session.v1_stage != V1_WEEKLY_EXECUTION:
        await _guard_reject(db, ctx, session, reason="当前不在周计划阶段。")
        raise InvalidInput("当前不在周计划阶段。")
    root = await reasoning_service.root_plan_node(db, ctx)
    if root is None:
        raise InvalidInput("这个空间还没有根目标。")
    response = await v01_service.generate_weekly_plan(db, ctx, root, session, trace=trace)
    if await v01_service._has_open_proposal(db, ctx):
        await _audit(
            db,
            ctx,
            session,
            "weekly_plan_proposal_created",
            summary="生成本周计划与下周预览(待确认)。",
            payload={"proposal": {"kind": "weekly"}},
        )
        await db.commit()
    return response


async def generate_daily_plan(
    db: AsyncSession, ctx: WorkspaceContext, session: GoalReasoningSession, *, trace=None
) -> AgentTurnResponse:
    """把本周计划拆成**少量工作日工作块**(不是均摊七天),落成待确认提案。"""
    from backend.services.errors import InvalidInput

    if session.v1_stage != V1_WEEKLY_EXECUTION:
        await _guard_reject(db, ctx, session, reason="当前不在周计划阶段。")
        raise InvalidInput("当前不在周计划阶段。")
    root = await reasoning_service.root_plan_node(db, ctx)
    if root is None:
        raise InvalidInput("这个空间还没有根目标。")
    phases = await v01_service._phase_nodes(db, ctx, root)
    weeks = await v01_service._week_nodes(db, phases)
    current = [
        week
        for week in weeks
        if week.title.startswith("本周计划")
        and week.status in (NodeStatus.PENDING, NodeStatus.DOING)
    ]
    if not current:
        raise InvalidInput("还没有可排的本周计划。")
    week = current[0]
    tasks = await db.execute(
        select(PlanNode)
        .where(PlanNode.parent_id == week.id, PlanNode.deleted_at.is_(None))
        .order_by(PlanNode.order_index.asc())
    )
    task_rows = list(tasks.scalars())
    if not task_rows:
        raise InvalidInput("本周计划里还没有任务。")

    conversation = await conversation_service.get_or_create_primary_conversation(db, ctx)
    turn = await turn_context.build_turn_context(
        db,
        ctx,
        conversation_id=conversation.id,
        user_message="生成日计划",
        context_node_id=root.id,
        scope_root_id=root.id,
    )
    week_handle = next((h for h, node_id in turn.node_handles if node_id == str(week.id)), None)
    if week_handle is None:
        raise InvalidInput("找不到本周计划的记号。")

    # 少量工作日:最多 3 天,不均匀摊到七天。
    days = ("第 1 个工作日", "第 2 个工作日", "第 3 个工作日")
    actions: list[dict] = []
    counter = 0
    for day_index, day in enumerate(days):
        day_ref = f"n{9300 + day_index}"
        actions.append(
            {
                "op": "create_node",
                "localId": day_ref,
                "parentRef": week_handle,
                "title": f"日计划 · {day}",
                "nodeType": "stage",
                "purpose": "planning",
                "description": "少量可执行的工作块。",
            }
        )
        blocks = [task_rows[i] for i in range(day_index, len(task_rows), len(days))][:2]
        for block in blocks:
            counter += 1
            actions.append(
                {
                    "op": "create_node",
                    "localId": f"n{9300 + 100 + counter}",
                    "parentRef": day_ref,
                    "title": f"{block.title} · 工作块",
                    "nodeType": "task",
                    "purpose": "planning",
                    "description": f"来源:本周计划「{week.title}」。",
                }
            )
    outcome = await proposal_service.build_from_actions(
        db,
        ctx,
        conversation_id=conversation.id,
        actions=tuple(actions),
        handles=turn.node_handles,
        reasoning="由本周计划拆出的少量工作日工作块。",
        assistant_message=None,
        trigger_type=RevisionTrigger.INITIAL_PLAN,
    )
    if outcome.proposal is None:
        session.v1_status = V1_STATUS_FAILED
        session.v1_error = "日计划没有通过校验。"
        await _audit(
            db,
            ctx,
            session,
            "proposal_validation_failed",
            summary="日计划没有通过校验。",
            validation_status="failed",
            error_code="PROPOSAL_VALIDATION_FAILED",
        )
        return await _response(db, ctx, session, changed=False)
    await _audit(
        db,
        ctx,
        session,
        "daily_plan_proposal_created",
        summary="把本周计划拆成少量工作日工作块(待确认)。",
        payload={"proposal": {"id": str(outcome.proposal.id), "kind": "daily", "status": "pending_confirmation"}},
    )
    message = await _append_assistant(
        db, ctx, reply="我把本周计划拆成了几天的工作块,确认后写入。(不是均摊七天。)"
    )
    return await _response(db, ctx, session, message=message, changed=True)


async def record_feedback(
    db: AsyncSession,
    ctx: WorkspaceContext,
    session: GoalReasoningSession,
    *,
    node_id,
    outcome: str,
    trace=None,
) -> AgentTurnResponse:
    """记录一条任务反馈;完成率 < 60% 时把 V1 推进到重规划阶段。"""
    from backend.services.errors import InvalidInput

    node = await node_service.load_node(db, ctx, node_id)
    mapping = {
        "done": NodeStatus.COMPLETED,
        "partial": NodeStatus.DOING,
        "missed": NodeStatus.PENDING,
        "delayed": NodeStatus.PENDING,
    }
    if outcome not in mapping:
        raise InvalidInput("不认识的反馈结果。")
    node.status = mapping[outcome]
    if outcome in {"partial", "delayed"}:
        note = "部分完成" if outcome == "partial" else "延期"
        node.description = (node.description or "") + f"\n【反馈】{note}"
    node.content_version += 1
    await db.commit()

    root = await reasoning_service.root_plan_node(db, ctx)
    done, total = await v01_service.weekly_completion(db, ctx, root) if root else (0, 0)
    rate = (done / total) if total else 0.0
    if total and rate < 0.6:
        session.v1_stage = V1_REPLANNING
        reply = f"本周完成率 {done}/{total}(低于 60%)。我先不催你,而是把剩余时间线往后再排一版,你看过再确认。"
    else:
        reply = f"记下了。本周进度 {done}/{total}。"
    await _audit(
        db,
        ctx,
        session,
        "execution_feedback_recorded",
        summary=f"任务反馈:{outcome};本周完成 {done}/{total}。",
        payload={"nodeId": str(node_id), "outcome": outcome, "done": done, "total": total},
    )
    message = await _append_assistant(db, ctx, reply=reply)
    return await _response(db, ctx, session, message=message, changed=True)


async def generate_replan(
    db: AsyncSession, ctx: WorkspaceContext, session: GoalReasoningSession, *, trace=None
) -> AgentTurnResponse:
    """重规划:只为**未来**阶段生成调整提案,已完成的历史一律不动。"""
    from backend.services.errors import InvalidInput

    root = await reasoning_service.root_plan_node(db, ctx)
    if root is None:
        raise InvalidInput("这个空间还没有根目标。")
    phases = await v01_service._phase_nodes(db, ctx, root)
    if not phases:
        raise InvalidInput("还没有已确认的时间线阶段。")
    conversation = await conversation_service.get_or_create_primary_conversation(db, ctx)
    turn = await turn_context.build_turn_context(
        db,
        ctx,
        conversation_id=conversation.id,
        user_message="重规划未来时间线",
        context_node_id=root.id,
        scope_root_id=root.id,
    )
    stage_before_replan = session.v1_stage
    phase_handles = {node_id: h for h, node_id in turn.node_handles}
    completed_ids = {str(phase.id) for phase in phases if phase.status is NodeStatus.COMPLETED}
    timeline = list(session.v01_timeline or [])
    for item in timeline:
        if not isinstance(item, dict) or item.get("planNodeId") in completed_ids:
            continue
        if isinstance(item.get("startWeek"), int):
            item["startWeek"] += 1
        if isinstance(item.get("endWeek"), int):
            item["endWeek"] += 1
        item["status"] = "draft"
    session.v01_timeline = timeline
    flag_modified(session, "v01_timeline")

    actions: list[dict] = []
    for phase in phases:
        if phase.status is NodeStatus.COMPLETED:
            continue
        handle = phase_handles.get(str(phase.id))
        if handle is None:
            continue
        actions.append(
            {
                "op": "update_node",
                "target_ref": handle,
                "description": (phase.description or "")
                + "\n【重规划】按当前完成情况,后续阶段整体后移一档。",
            }
        )
    if not actions:
        message = await _append_assistant(
            db, ctx, reply="未来阶段都已经完成,暂时不需要重规划。", conversation=conversation
        )
        session.v1_stage = V1_WEEKLY_EXECUTION
        return await _response(db, ctx, session, message=message, changed=False)
    outcome = await proposal_service.build_from_actions(
        db,
        ctx,
        conversation_id=conversation.id,
        actions=tuple(actions),
        handles=turn.node_handles,
        reasoning="根据最近执行情况,对未完成阶段提出的未来调整。",
        assistant_message=None,
        trigger_type=RevisionTrigger.EXECUTION_DEVIATION,
    )
    if outcome.proposal is None:
        session.v1_status = V1_STATUS_FAILED
        session.v1_error = "重规划提案没有通过校验。"
        await _audit(
            db,
            ctx,
            session,
            "proposal_validation_failed",
            trigger="replan",
            summary=session.v1_error,
            validation_status="failed",
            error_code="PROPOSAL_VALIDATION_FAILED",
        )
        return await _response(db, ctx, session, changed=False)
    session.v1_stage = V1_REPLANNING
    await _audit(
        db,
        ctx,
        session,
        "replan_proposal_created",
        stage_before=stage_before_replan,
        stage_after=V1_REPLANNING,
        summary="生成只调整未来阶段的重规划草案,待确认。",
        payload={"proposal": {"id": str(outcome.proposal.id), "kind": "replan", "status": "pending_confirmation"}},
    )
    message = await _append_assistant(
        db,
        ctx,
        reply="我按最近的完成情况提了一版**只调整未来阶段**的重规划,已完成的阶段原样保留。确认后生效。",
        conversation=conversation,
    )
    return await _response(db, ctx, session, message=message, changed=True)


async def weekend_review(
    db: AsyncSession, ctx: WorkspaceContext, session: GoalReasoningSession, *, trace=None
) -> AgentTurnResponse:
    """自动/主动发起的周末回顾入口:汇总完成度,并准备一份未来重规划草案。"""
    from backend.services.errors import InvalidInput

    if session.v1_stage not in (V1_WEEKLY_EXECUTION, V1_REPLANNING):
        await _guard_reject(db, ctx, session, reason="当前不在周执行阶段。")
        raise InvalidInput("当前不在周执行阶段。")
    root = await reasoning_service.root_plan_node(db, ctx)
    done, total = await v01_service.weekly_completion(db, ctx, root) if root else (0, 0)
    await _audit(
        db,
        ctx,
        session,
        "weekly_review_started",
        summary=f"发起周末回顾:本周完成 {done}/{total}。",
        payload={"done": done, "total": total},
    )
    await _append_assistant(
        db,
        ctx,
        reply=f"这一周完成 {done}/{total}。我按完成情况准备一份**只调整未来**的重规划,你看过再确认。",
    )
    await db.commit()
    return await generate_replan(db, ctx, session, trace=trace)


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
    stage_before = session.v1_stage
    if session.v1_stage is None:
        session.v1_stage = V1_INITIAL_THINKING
        session.phase = ReasoningSessionPhase.INTAKE
    session.status = ReasoningSessionStatus.READY
    if stage_before is None:
        await _audit(
            db,
            ctx,
            session,
            "space_entered",
            trigger="space_entered",
            stage_after=session.v1_stage,
            summary="进入空间,建立初步思考状态。",
        )
    await db.commit()
    # P4:周末**自动发起回顾入口** —— 有活跃周计划、且没有未处理提案时,
    # 汇总完成度并准备一份只调整未来的重规划草案(不静默改写已确认计划)。
    if (
        session.v1_stage == V1_WEEKLY_EXECUTION
        and today_in(ctx.timezone).weekday() >= 5
        and not await v01_service._has_open_proposal(db, ctx)
    ):
        return await weekend_review(db, ctx, session, trace=trace)
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
    db: AsyncSession,
    ctx: WorkspaceContext,
    session: GoalReasoningSession,
    reasoner=None,
) -> AgentTurnResponse:
    """用户确认战略逻辑:进入 P3 准备状态;有 reasoner 时立即生成粗时间架构草案。

    **不生成任何正式计划** —— 粗时间架构先落成待确认提案,用户确认后才写入阶段。
    """
    from backend.services.errors import InvalidInput

    strategy = dict(session.v1_strategy or {})
    if not strategy:
        await _guard_reject(db, ctx, session, reason="现在还没有可确认的战略路径。")
        raise InvalidInput("现在还没有可确认的战略路径。")
    strategy["confirmed"] = True
    session.v1_strategy = strategy
    session.v1_stage = V1_STRATEGY_CONFIRMED
    await _audit(
        db,
        ctx,
        session,
        "strategy_confirmed",
        stage_before=V1_STRATEGY_DRAFT,
        stage_after=V1_STRATEGY_CONFIRMED,
        summary="用户确认了战略逻辑。",
        payload={"strategy": strategy},
    )
    await db.commit()
    if reasoner is not None:
        await generate_coarse_timeline(db, ctx, session, reasoner)
    return await reasoning_service._response(db, ctx, session, changed=True)


__all__ = [
    "ALLOWED_V1_KEYS",
    "V1_COARSE_TIMELINE_REVIEW",
    "V1_FACTOR_ANALYSIS",
    "V1_GOAL_REFRAME",
    "V1_INITIAL_THINKING",
    "V1_REPLANNING",
    "V1_STATUS_FAILED",
    "V1_STATUS_IDLE",
    "V1_STATUS_RUNNING",
    "V1_STRATEGY_CONFIRMED",
    "V1_STRATEGY_DRAFT",
    "V1_WEEKLY_EXECUTION",
    "advance",
    "answer_v1_in_conversation",
    "confirm_strategy",
    "generate_coarse_timeline",
    "generate_daily_plan",
    "generate_replan",
    "generate_weekly_plan",
    "is_v1",
    "on_proposal_confirmed",
    "record_feedback",
    "weekend_review",
]
