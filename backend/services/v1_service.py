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
#: P2.2:目标定义经用户确认后,进入“问题结构”分析层(五个因素维度)。
V1_PROBLEM_STRUCTURE = "problem_structure"
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
MAX_V1_DIMENSIONS = 3
#: P2.3:problem_structure 中“继续形成战略路径”的显式下一步动作。
NEXT_CONTINUE_STRATEGY = "continue_strategy"
#: 阶段一全局关键问题的总预算。超过后服务端不再接受新问题。
MAX_V1_QUESTIONS = 3

#: 用户输入的语义分类(P2.1)。**只有前三类能写节点事实。**
INPUT_STRATEGIC_FACT = "strategic_fact"
INPUT_USER_PREFERENCE = "user_preference"
INPUT_USER_CORRECTION = "user_correction"
INPUT_CONVERSATION_FEEDBACK = "conversation_feedback"
INPUT_AMBIGUOUS = "ambiguous_or_irrelevant"

#: 元对话 / 情绪 / 对 AI 的反馈 —— **不是战略事实**。
_META_MARKERS = (
    "走神",
    "人工整",
    "机器人",
    "你在问",
    "问点",
    "换个问题",
    "换一个",
    "听不懂",
    "答非所问",
    "你是不是",
    "傻",
    "垃圾",
    "无聊",
    "滚",
    "烦死",
)
_LOW_INFO_MARKERS = (
    "不知道",
    "不清楚",
    "没想好",
    "不确定",
    "暂时没有",
    "说不上",
    "没有想法",
    "都行",
    "随便",
    "你来定",
    "无所谓",
    "没概念",
)
_CORRECTION_MARKERS = ("不是", "其实", "我说的不是", "更正", "你误解")
_PREFERENCE_MARKERS = ("我想", "我希望", "我更喜欢", "我倾向", "我比较", "我更愿意", "优先")


def classify_user_message(content: str) -> str:
    """把一条用户消息分类为 P2.1 的五类之一。**确定性规则**,不依赖模型。

    元对话 / 情绪 / 对 AI 的反馈(“你走神了”“人工整你”)归为 `conversation_feedback`,
    不得进入 `knownFacts`;“不知道”这类归为 `ambiguous_or_irrelevant`。
    """
    text = (content or "").strip()
    lowered = text.lower()
    if not text:
        return INPUT_AMBIGUOUS
    if len(text) <= 24 and any(marker in text for marker in _LOW_INFO_MARKERS):
        return INPUT_AMBIGUOUS
    if any(marker in lowered for marker in _META_MARKERS):
        return INPUT_CONVERSATION_FEEDBACK
    if any(marker in text for marker in _CORRECTION_MARKERS):
        return INPUT_USER_CORRECTION
    if any(marker in text for marker in _PREFERENCE_MARKERS):
        return INPUT_USER_PREFERENCE
    if len(text) < 4:
        return INPUT_AMBIGUOUS
    return INPUT_STRATEGIC_FACT


def _counts_as_low_info(classification: str) -> bool:
    return classification in (INPUT_AMBIGUOUS, INPUT_CONVERSATION_FEEDBACK)

#: 内部十维 + 战略四子的**展示标题**。画布只显示标题 + 一句判断。
_DIMENSION_TITLES: dict[str, str] = {
    **{key: title for _group, key, title, _q in _ANALYSIS},
    **{key: title for key, title, _q in _STRATEGY_KEYS},
}
#: **内部十维**的键(不含战略子项)—— 可见性/隐藏数只针对这十个。
ANALYSIS_DIMENSION_KEYS = frozenset(key for _group, key, _title, _q in _ANALYSIS)
#: goal_reframe 阶段默认可见的三个**核心分析维度**。
_CORE_GOAL_KEYS: tuple[str, ...] = ("true_intent", "key_conflict", "goal_definition")
#: 进入 problem_structure 后默认可见的五个因素维度。
_PROBLEM_KEYS: tuple[str, ...] = (
    "hard_constraints",
    "controllable_factors",
    "key_levers",
    "major_risks",
    "external_conditions",
)


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


def dimension_title(key: str | None) -> str | None:
    """分析维度键 -> 展示标题。"""
    return _DIMENSION_TITLES.get(key or "")


def visible_dimension_keys(session: GoalReasoningSession) -> set[str]:
    """当前**画布默认可见**的分析维度键。内部复杂,外部简单。

    - goal_reframe / initial_thinking:只显示三个核心分析维度;
    - problem_structure 及之后:显示五个因素维度(+ 焦点);
    - 战略子项一旦形成就显示;
    - 焦点始终可见。
    """
    stage = session.v1_stage
    if stage in (None, V1_INITIAL_THINKING, V1_GOAL_REFRAME):
        keys: set[str] = set(_CORE_GOAL_KEYS)
    else:
        keys = set(_PROBLEM_KEYS)
    if session.v1_focus_key:
        keys.add(session.v1_focus_key)
    return keys


def dimension_projection(session: GoalReasoningSession) -> list[dict]:
    """十维 + 四战略的分析维度投影(可见性、阶段、焦点、是否真需回答)。"""
    visible = visible_dimension_keys(session)
    projection: list[dict] = []
    for key in (k for _group, k, _title, _q in _ANALYSIS):
        title = _DIMENSION_TITLES[key]
        projection.append(
            {
                "key": key,
                "title": title,
                "visible": key in visible,
                "isFocus": key == session.v1_focus_key,
                #: 分析维度**从不**是“待回答问题”;真正的问题只来自对话区那一个。
                "requiresResponse": False,
            }
        )
    return projection


def actual_pending_question_count(session: GoalReasoningSession) -> int:
    """真正需要用户回答的问题数:只数会话上那个全局关键问题(0 或 1)。"""
    return 1 if (session.v1_question or "").strip() else 0


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
    questions: list[AgentQuestion],
    draft,
    *,
    classification: str,
    is_local_discussion: bool,
    discussion_key: str | None,
    raw_message: str,
) -> tuple[list[AgentQuestion], dict[str, dict], dict[str, str]]:
    """把形状合法的判断落到**已存在的分析节点**上。

    P2.1:
    - `knownFacts` 只接受战略事实/偏好/纠正;元对话/含混内容一律不进事实;
    - `discussionCount` 只统计**该节点的真正局部讨论/回答**,全局对话不虚增;
    - **只有确有新判断/新事实/新假设时才写**;内容未变则不动,也不产生审计事件。
    """
    by_key = {question.v1_key: question for question in questions if question.v1_key}
    changed: list[AgentQuestion] = []
    analyses: dict[str, dict] = {}
    strategy_values: dict[str, str] = {}

    # node_updates 优先;keyDimensions 只在未被 nodeUpdates 覆盖时补充判断。
    normalized: list[tuple] = [
        (
            update.node_key,
            update.judgment,
            update.importance_reason,
            update.known_facts,
            update.assumptions,
            update.evidence,
            update.uncertainty,
            update.status,
            update.impacted_node_keys,
        )
        for update in draft.node_updates[:MAX_V1_UPDATES]
    ]
    covered = {item[0] for item in normalized}
    for dimension in draft.key_dimensions[:MAX_V1_DIMENSIONS]:
        if dimension.key in covered or not dimension.judgment:
            continue
        normalized.append(
            (dimension.key, dimension.judgment, dimension.why_it_matters, (), (), (), "medium", "discussing", ())
        )

    for key, judgment, importance, facts, assumptions, evidence, uncertainty, status, impacts_raw in normalized:
        if key not in ALLOWED_V1_KEYS:
            continue
        if key in _STRATEGY_KEY_SET:
            if judgment:
                strategy_values[key] = judgment
            continue
        question = by_key.get(key)
        if question is None:
            continue
        existing = dict(question.v1_analysis or {})
        new_facts = _sanitized_facts(facts, classification=classification, raw_message=raw_message)
        analysis = {
            "judgment": judgment or existing.get("judgment", ""),
            "knownFacts": new_facts or list(existing.get("knownFacts", [])),
            "assumptions": list(assumptions) or list(existing.get("assumptions", [])),
            "evidence": list(evidence) or list(existing.get("evidence", [])),
            "importanceReason": importance or existing.get("importanceReason", ""),
            "uncertainty": uncertainty,
            "status": status,
            "impactedNodeKeys": [item for item in impacts_raw if item in by_key],
            "discussionCount": int(existing.get("discussionCount", 0))
            + (1 if is_local_discussion and key == discussion_key else 0),
        }
        if _analysis_unchanged(existing, analysis):
            continue
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


def _analysis_unchanged(existing: dict, analysis: dict) -> bool:
    """判断是否与库里那一份实质相同 —— 相同就不写、不发事件。"""
    keys = (
        "judgment",
        "knownFacts",
        "assumptions",
        "evidence",
        "importanceReason",
        "uncertainty",
        "status",
        "impactedNodeKeys",
        "discussionCount",
    )
    for key in keys:
        before = existing.get(key)
        after = analysis.get(key)
        if isinstance(before, list) or isinstance(after, list):
            if list(before or []) != list(after or []):
                return False
        elif before != after:
            return False
    return True


def _sanitized_facts(facts, *, classification: str, raw_message: str) -> list[str]:
    """只有战略事实/偏好/纠正能写事实;元对话与含混内容一律丢弃。"""
    if classification not in (
        INPUT_STRATEGIC_FACT,
        INPUT_USER_PREFERENCE,
        INPUT_USER_CORRECTION,
    ):
        return []
    clean: list[str] = []
    for item in facts or ():
        text = str(item).strip()
        if not text:
            continue
        if any(marker in text for marker in _META_MARKERS):
            continue
        clean.append(text)
    return clean


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
    classification: str,
    is_local_discussion: bool = False,
    force_no_question: bool = False,
    trigger: str = "user_message",
    exclude_message_id=None,
) -> ReasoningResult:
    root = await reasoning_service.root_plan_node(db, ctx)
    if root is None:
        from backend.services.errors import InvalidInput

        raise InvalidInput("这个空间还没有根目标。")
    stage_before = session.v1_stage
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
    if assessment is None or not (
        assessment.global_assessment
        or assessment.strategic_thesis
        or assessment.node_updates
        or assessment.key_dimensions
    ):
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
    changed, analyses, strategy_values = _apply_updates(
        questions,
        assessment,
        classification=classification,
        is_local_discussion=is_local_discussion,
        discussion_key=assessment.focus_key,
        raw_message=user_message,
    )
    persisted = {q.v1_key: (q.v1_analysis or {}) for q in questions if q.v1_key}

    from_problem_structure = session.v1_stage == V1_PROBLEM_STRUCTURE
    synthesized = False
    if strategy_values and _strategy_ready(persisted, assessment):
        await _ensure_strategy_questions(db, ctx, strategy_values)
        merged = dict(session.v1_strategy or {})
        for key, value in strategy_values.items():
            merged[_STRATEGY_FIELD.get(key, key)] = value
        merged["tradeoff"] = assessment.strategy_tradeoff or merged.get("tradeoff", "")
        merged["confirmed"] = False
        session.v1_strategy = merged
        if session.v1_stage in (V1_GOAL_REFRAME, V1_FACTOR_ANALYSIS, V1_PROBLEM_STRUCTURE):
            session.v1_stage = V1_STRATEGY_DRAFT
        synthesized = True

    thesis = assessment.strategic_thesis or assessment.global_assessment
    if thesis:
        session.v1_strategic_thesis = thesis
        session.v1_judgment = thesis

    # ---- P2.1 服务端追问守卫:默认不问;同焦点最多 1 次;总预算 3;低信息强制给候选 ----
    budget_used = int(session.v1_question_budget_used or 0)
    last_focus = session.v1_last_focus_key
    low_streak = int(session.v1_low_info_streak or 0)
    #: 兼容旧字段:模型/测试可能只给 `question`。
    proposed_question = (assessment.critical_question or assessment.question or "").strip()
    same_focus_repeat = bool(
        proposed_question and assessment.focus_key and assessment.focus_key == last_focus
    )
    budget_exhausted = budget_used >= MAX_V1_QUESTIONS
    force_options = low_streak >= 2
    question_accepted = bool(proposed_question) and not (
        same_focus_repeat or budget_exhausted or force_options or force_no_question
    )

    if force_options:
        # 用户连续两轮答不上来:不再问,强制给候选方向或退回暂定综合。
        response_mode = "offer_options" if assessment.candidate_directions else "provisional_synthesis"
    else:
        response_mode = assessment.response_mode

    if assessment.candidate_directions:
        session.v1_candidate_directions = [
            {"key": d.key, "title": d.title, "reason": d.reason, "path": d.path}
            for d in assessment.candidate_directions
        ]

    if assessment.focus_key:
        session.v1_focus_key = assessment.focus_key
        session.v1_focus_reason = assessment.focus_reason or None
    if question_accepted:
        session.v1_question = proposed_question
        session.v1_last_focus_key = assessment.focus_key
        session.v1_question_budget_used = budget_used + 1
    else:
        session.v1_question = None
    if session.v1_stage == V1_INITIAL_THINKING:
        session.v1_stage = V1_GOAL_REFRAME
    # P2.3:非终态阶段不允许“idle + 无问题 + 无 CTA + 无战略”。
    if session.v1_stage == V1_PROBLEM_STRUCTURE:
        session.v1_next_action = None if synthesized else NEXT_CONTINUE_STRATEGY
    else:
        session.v1_next_action = None
    session.phase = ReasoningSessionPhase.ROADMAP_DRAFT
    session.status = ReasoningSessionStatus.READY
    session.v1_status = V1_STATUS_IDLE
    session.v1_error = None

    # ---- 审计:战略判断 / 关键问题 / 候选方向 / 节点更新 / 战略草案 ----
    await _audit(
        db,
        ctx,
        session,
        "strategic_thesis_generated",
        trigger=trigger,
        stage_before=stage_before,
        stage_after=session.v1_stage,
        focus_key=session.v1_focus_key,
        focus_reason=session.v1_focus_reason,
        source=result.source.value if result.source else None,
        summary=thesis,
        payload={
            "strategicThesis": thesis,
            "keyDimensions": [
                {
                    "key": dimension.key,
                    "judgment": dimension.judgment,
                    "whyItMatters": dimension.why_it_matters,
                }
                for dimension in assessment.key_dimensions
            ],
            "responseMode": response_mode,
        },
    )
    await _audit(
        db,
        ctx,
        session,
        "global_assessment_generated",
        trigger=trigger,
        stage_before=stage_before,
        stage_after=session.v1_stage,
        focus_key=session.v1_focus_key,
        focus_reason=session.v1_focus_reason,
        source=result.source.value if result.source else None,
        summary=thesis,
        payload={"globalAssessment": assessment.global_assessment or thesis},
    )
    if question_accepted:
        await _audit(
            db,
            ctx,
            session,
            "global_question_asked",
            trigger=trigger,
            stage_before=stage_before,
            stage_after=session.v1_stage,
            focus_key=assessment.focus_key,
            focus_reason=assessment.focus_reason or None,
            summary=proposed_question,
            payload={
                "question": proposed_question,
                "focus": assessment.focus_key,
                "reason": assessment.focus_reason,
                "responseMode": response_mode,
            },
        )
    if assessment.candidate_directions:
        await _audit(
            db,
            ctx,
            session,
            "candidate_directions_offered",
            stage_before=stage_before,
            stage_after=session.v1_stage,
            focus_key=session.v1_focus_key,
            summary="给出候选方向,由用户选择、修正或否定。",
            payload={
                "candidateDirections": [
                    {"key": d.key, "title": d.title, "reason": d.reason, "path": d.path}
                    for d in assessment.candidate_directions
                ]
            },
        )
    if response_mode == "provisional_synthesis":
        await _audit(
            db,
            ctx,
            session,
            "provisional_synthesis_created",
            summary=thesis,
            payload={"strategicThesis": thesis},
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
            trigger="question_answered" if is_local_discussion else "user_message",
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
        if from_problem_structure:
            await _audit(
                db,
                ctx,
                session,
                "problem_structure_synthesized",
                stage_before=V1_PROBLEM_STRUCTURE,
                stage_after=session.v1_stage,
                summary="problem_structure 中主动完成第一版战略路径。",
                payload={"strategy": session.v1_strategy},
            )
        await _audit(
            db,
            ctx,
            session,
            "strategy_review_ready",
            stage_after=session.v1_stage,
            summary="战略草案已就绪,等用户确认或调整。",
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

    # ---- P2.1 输入分类:元对话 / 情绪 / 对 AI 的反馈不是战略事实 ----
    classification = classify_user_message(content)
    is_local_discussion = trigger == "question_answered" or context_node_id is not None
    await _audit(
        db,
        ctx,
        session,
        "user_message_received",
        trigger=trigger,
        stage_before=session.v1_stage,
        focus_key=session.v1_focus_key,
        summary=content,
        payload={"classification": classification, "isLocalDiscussion": is_local_discussion},
    )
    if classification == INPUT_CONVERSATION_FEEDBACK:
        await _audit(
            db,
            ctx,
            session,
            "conversation_feedback_received",
            trigger=trigger,
            summary=content,
            payload={"classification": classification},
        )
    # 连续低信息 / 元对话回答计数:>=2 时服务端强制给候选方向或暂定综合。
    if _counts_as_low_info(classification):
        session.v1_low_info_streak = int(session.v1_low_info_streak or 0) + 1
    else:
        session.v1_low_info_streak = 0

    # ---- 审计:首轮提交 / 紫色问题回答 ----
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
    elif is_local_discussion:
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
        classification=classification,
        is_local_discussion=is_local_discussion,
        trigger=trigger,
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


async def select_candidate_direction(
    db: AsyncSession,
    ctx: WorkspaceContext,
    session: GoalReasoningSession,
    key: str,
    reasoner=None,
) -> AgentTurnResponse:
    """用户选择一个候选方向:幂等记录 + **立即发起一次 V1 推理回合**更新判断。

    - 同一方向重复选择**幂等**:不再审计、不再调模型;
    - 选中后强制**不再提问**,并更新目标定义;
    - 不进入时间线、不生成任务。
    """
    from backend.services.errors import InvalidInput

    # P2.3:候选方向只属于 goal_reframe。目标定义确认后必须走“重新选择起点”。
    if session.v1_stage != V1_GOAL_REFRAME:
        await _guard_reject(db, ctx, session, reason="目标定义已确认,请先“重新选择起点”。")
        raise InvalidInput("目标定义已确认;如要改方向,请先选择“重新选择起点”。")
    directions = [d for d in (session.v1_candidate_directions or []) if isinstance(d, dict)]
    valid = {str(d.get("key")) for d in directions}
    if key not in valid:
        await _guard_reject(db, ctx, session, reason="没有这个候选方向。")
        raise InvalidInput("没有这个候选方向。")
    # 幂等:同一方向重复点击不再写审计、不再跑模型。
    if session.v1_selected_direction == key:
        return await reasoning_service._response(db, ctx, session, changed=False)

    direction = next(d for d in directions if str(d.get("key")) == key)
    session.v1_selected_direction = key
    await _audit(
        db,
        ctx,
        session,
        "candidate_direction_selected",
        focus_key=session.v1_focus_key,
        focus_reason=session.v1_focus_reason,
        summary=f"用户选择了候选方向:{key}。",
        payload={
            "focusKey": session.v1_focus_key,
            "focusReason": session.v1_focus_reason,
            "selectedDirection": key,
        },
    )
    await db.commit()
    if reasoner is None:
        return await reasoning_service._response(db, ctx, session, changed=True)

    message = (
        f"用户选择了候选方向「{direction.get('title') or key}」。"
        f"理由:{direction.get('reason') or ''}。路径:{direction.get('path') or ''}。"
        "请据此更新战略判断,并更新目标定义;不要再问新问题。"
    )
    result = await _run_assessment(
        db,
        ctx,
        session,
        user_message=message,
        reasoner=reasoner,
        classification=INPUT_USER_PREFERENCE,
        force_no_question=True,
        trigger="direction_selected",
    )
    conversation = await conversation_service.get_or_create_primary_conversation(db, ctx)
    await _append_assistant(db, ctx, reply=result.reply, conversation=conversation, result=result)
    await db.commit()
    return await reasoning_service._response(db, ctx, session, changed=True)


async def confirm_goal_definition(
    db: AsyncSession,
    ctx: WorkspaceContext,
    session: GoalReasoningSession,
    reasoner=None,
) -> AgentTurnResponse:
    """用户确认目标定义:进入 problem_structure,并**自动发起一次战略合成回合**。

    P2.3:确认后绝不是“idle 无下一步”。自动合成要么形成战略草案(等确认),
    要么留下显式 CTA `continue_strategy`。
    """
    from backend.services.errors import InvalidInput

    if session.v1_stage != V1_GOAL_REFRAME:
        await _guard_reject(db, ctx, session, reason="当前不在目标重构阶段。")
        raise InvalidInput("当前不在目标重构阶段。")
    session.v1_stage = V1_PROBLEM_STRUCTURE
    #: 先给一个明确 CTA;合成成功后会被清掉。
    session.v1_next_action = NEXT_CONTINUE_STRATEGY
    await _audit(
        db,
        ctx,
        session,
        "goal_definition_confirmed",
        stage_before=V1_GOAL_REFRAME,
        stage_after=V1_PROBLEM_STRUCTURE,
        summary="用户确认了目标定义,进入问题结构。",
    )
    await _audit(
        db,
        ctx,
        session,
        "problem_structure_entered",
        stage_before=V1_GOAL_REFRAME,
        stage_after=V1_PROBLEM_STRUCTURE,
        summary="进入问题结构,自动发起战略路径合成。",
    )
    await db.commit()
    if reasoner is None:
        return await reasoning_service._response(db, ctx, session, changed=True)

    result = await _run_assessment(
        db,
        ctx,
        session,
        user_message=(
            "目标定义已确认。请基于已有目标定义、已采用的起点与已知事实,"
            "主动识别可控变量、主要风险与关键杠杆,形成第一版战略路径;不要再问新问题。"
        ),
        reasoner=reasoner,
        classification=INPUT_USER_PREFERENCE,
        force_no_question=True,
        trigger="problem_structure_entered",
    )
    await _append_assistant(
        db,
        ctx,
        reply=result.reply,
        conversation=await conversation_service.get_or_create_primary_conversation(db, ctx),
        result=result,
    )
    await db.commit()
    return await reasoning_service._response(db, ctx, session, changed=True)


async def continue_strategy(
    db: AsyncSession,
    ctx: WorkspaceContext,
    session: GoalReasoningSession,
    reasoner=None,
) -> AgentTurnResponse:
    """`responseMode=none` 的兜底 CTA:受控地再跑一次战略合成回合。"""
    from backend.services.errors import InvalidInput

    if session.v1_stage not in (V1_PROBLEM_STRUCTURE, V1_FACTOR_ANALYSIS):
        await _guard_reject(db, ctx, session, reason="当前不在问题结构阶段。")
        raise InvalidInput("当前不在问题结构阶段。")
    await _audit(
        db,
        ctx,
        session,
        "strategy_continue_triggered",
        stage_before=session.v1_stage,
        summary="用户触发继续形成战略路径。",
    )
    await db.commit()
    if reasoner is None:
        return await reasoning_service._response(db, ctx, session, changed=True)
    result = await _run_assessment(
        db,
        ctx,
        session,
        user_message="请基于已有分析继续形成第一版战略路径;不要再问新问题。",
        reasoner=reasoner,
        classification=INPUT_USER_PREFERENCE,
        force_no_question=True,
        trigger="strategy_continue",
    )
    await _append_assistant(
        db,
        ctx,
        reply=result.reply,
        conversation=await conversation_service.get_or_create_primary_conversation(db, ctx),
        result=result,
    )
    await db.commit()
    return await reasoning_service._response(db, ctx, session, changed=True)


async def reopen_direction_selection(
    db: AsyncSession, ctx: WorkspaceContext, session: GoalReasoningSession
) -> AgentTurnResponse:
    """用户明确要求“重新选择起点”:回到 goal_reframe,重新开放候选方向。"""
    from backend.services.errors import InvalidInput

    if not session.v1_candidate_directions:
        await _guard_reject(db, ctx, session, reason="还没有可重新选择的候选方向。")
        raise InvalidInput("还没有可重新选择的候选方向。")
    stage_before = session.v1_stage
    session.v1_stage = V1_GOAL_REFRAME
    session.v1_selected_direction = None
    session.v1_next_action = None
    await _audit(
        db,
        ctx,
        session,
        "direction_reselection_started",
        stage_before=stage_before,
        stage_after=V1_GOAL_REFRAME,
        summary="用户选择重新选择起点,回到目标重构。",
    )
    await db.commit()
    return await reasoning_service._response(db, ctx, session, changed=True)


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
    "ANALYSIS_DIMENSION_KEYS",
    "NEXT_CONTINUE_STRATEGY",
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
    "actual_pending_question_count",
    "advance",
    "answer_v1_in_conversation",
    "classify_user_message",
    "confirm_goal_definition",
    "confirm_strategy",
    "continue_strategy",
    "dimension_projection",
    "dimension_title",
    "generate_coarse_timeline",
    "generate_daily_plan",
    "generate_replan",
    "generate_weekly_plan",
    "is_v1",
    "on_proposal_confirmed",
    "record_feedback",
    "reopen_direction_selection",
    "select_candidate_direction",
    "visible_dimension_keys",
    "weekend_review",
]
