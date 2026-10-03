"""规划智能体 V0.1:程序控制的三阶段稳定闭包。

## 它是什么

一条**由服务端状态机驱动**的最小规划闭环:

```
DISCOVERY        阶段一:全局洞察 + 2–4 个核心问题 → 用户回答一次 → 4–6 个第一层节点
TIMELINE_DRAFT   阶段二:生成 3–6 个阶段的时间线草案(相对周)
TIMELINE_REVIEW  等用户确认(复用既有 proposal → confirm → version → transaction)
WEEKLY_EXECUTION 阶段三:本周计划 + 下周预览 → 记录执行反馈
REPLANNING       完成率 < 60% 或用户主动触发 → 未来时间线调整提案
```

## 三条不可破坏的边界

1. **LLM 不能自己改阶段。** 状态迁移只发生在 `transition()` 里,由服务端根据
   当前阶段与输入决定;LLM 只负责生成内容(本版为稳定性用确定性模板,
   模型介入仅作可选 fallback)。
2. **阶段一不写业务计划。** 第一层节点是**推理地图节点**(AI 维护),画布立即可见,
   但 `plan_nodes` 一行都不动;阶段二才通过 proposal 写业务阶段。
3. **确认链路不绕过。** 时间线、周计划、重规划都落成 `Proposal`,由用户点确认后
   才在既有事务里写入。

## 与老 workspace 的关系

`workflow_stage is None` = 非 V0.1,所有函数直接返回 `None`,调用方走原有分支。
本模块**只服务新建目标空间**,不删除、不重置任何历史。
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from backend.agent.runtime.base import (
    ReasoningMapDraft,
    ReasoningMapNodeDraft,
    ReasoningResult,
)
from backend.contracts.reasoning import AgentTurnResponse
from backend.db.models import Conversation, GoalReasoningSession, Message, PlanNode
from backend.db.models.enums import (
    ModelSource,
    NodeStatus,
    NodeType,
    PlanningWorkflowStage,
    ReasoningSessionPhase,
    ReasoningSessionStatus,
    RevisionTrigger,
)
from backend.services import (
    agent_trace_service,
    conversation_service,
    proposal_service,
    reasoning_service,
    turn_context,
)
from backend.services.context import WorkspaceContext
from backend.services.timeutil import today_in

# =================================================================================
# 阶段一:模板与确定性内容(稳定优先)
# =================================================================================
#: 每条模板产出 4–6 个第一层节点。(标题, 说明, 完成标准)
_TEMPLATE_NODES: dict[str, tuple[tuple[str, str, str], ...]] = {
    "learning": (
        ("明确目标与用途", "确认这项能力最终用来做什么、做到什么程度。", "能用一句话说清用途与成功标准。"),
        ("基础与环境", "补齐最小基础、把环境准备好,让后续练习能跑起来。", "能独立完成基础练习。"),
        ("核心练习", "围绕目标做一组可验证的练习。", "练习可复现、能自查对错。"),
        ("真实应用", "把能力用到一个真实或仿真的任务上。", "交付一个可用的真实成果。"),
        ("复盘与展示", "整理成果、暴露缺口、形成下一步。", "能讲清结论与下一步。"),
    ),
    "exam": (
        ("确认考试与目标", "明确要考什么、目标分数或档位。", "目标可衡量。"),
        ("诊断当前水平", "用一套真题定位短板。", "知道差距具体在哪几块。"),
        ("基础回补", "按短板补基础,不留空洞。", "基础题正确率达标。"),
        ("专项强化", "针对高频题型集中练。", "专项正确率明显提升。"),
        ("模考与冲刺", "整卷模考、控制节奏与时间。", "模考稳定落在目标区间。"),
    ),
    "project": (
        ("定义可交付成果", "明确最终交付什么、给谁用。", "成果可验证、有人用。"),
        ("拆出最小闭环", "先跑通一个最小可用版本。", "最小版本能跑。"),
        ("核心实现", "完成主要功能。", "核心功能通过验收。"),
        ("集成与验证", "联调、测试、修问题。", "通过既定验证标准。"),
        ("交付与复盘", "整理文档、复盘经验。", "能交付并讲清取舍。"),
    ),
}

_TEMPLATE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "exam": ("考", "考研", "考试", "六级", "四级", "雅思", "托福", "期末", "证书"),
    "project": ("项目", "副业", "产品", "上线", "创业", "作品", "网站", "app", "小程序"),
    "learning": ("学", "学习", "入门", "掌握", "python", "英语", "数学", "编程", "课程"),
}

_TEMPLATE_QUESTIONS: dict[str, tuple[str, ...]] = {
    "learning": (
        "你希望学完之后能做出什么、或解决什么具体问题?",
        "你希望大概在什么时间范围内看到结果(比如 30 天、一学期)?",
        "你现在在这件事上的基础大概到哪一步?",
    ),
    "exam": (
        "你要考的是哪一个考试?目标分数或档位是多少?",
        "距离考试大概还有多长时间?",
        "你现在最薄弱的是哪一部分?",
    ),
    "project": (
        "你要交付的成果是什么?主要给谁用?",
        "你希望什么时候完成?",
        "你现在已经具备哪些条件、缺什么?",
    ),
}

_TEMPLATE_DEFAULT_WEEKS = {"learning": 12, "exam": 8, "project": 10}


def detect_template(workspace_intent: str, goal_title: str, answer: str = "") -> str:
    """按关键词判定模板。**确定性**,不依赖模型。默认 learning。

    先看**目标标题**(最直接),再看空间意图与用户回答 —— 避免意图里顺口写的
    “项目”把“学习 Python”误判成项目模板。
    """
    for source in (goal_title, workspace_intent, answer):
        text = (source or "").lower()
        for template, keywords in _TEMPLATE_KEYWORDS.items():
            if any(keyword in text for keyword in keywords):
                return template
    return "learning"


def discovery_questions(template: str) -> tuple[str, ...]:
    return _TEMPLATE_QUESTIONS.get(template, _TEMPLATE_QUESTIONS["learning"])


def discovery_insight(goal_title: str, template: str) -> str:
    label = {"learning": "学习", "exam": "备考", "project": "项目"}.get(template, "目标")
    return (
        f"先把「{goal_title}」当成一个要交付结果的{label}来规划,而不是一串要看完的内容。"
        "路线应该围绕最终成果倒推:先定义要交出什么,再决定每个阶段练什么、怎么算过关。"
        "在生成整条时间线之前,我需要先确认三件事——它们会直接决定阶段的顺序和总时长。"
    )


def _has_enough(answer: str) -> bool:
    """用户一次回答是否已经够生成节点。**宽松**:有明显的实质内容就算够。"""
    return len((answer or "").strip()) >= 8


# =================================================================================
# 阶段一的节点骨架(写推理地图,不写业务计划)
# =================================================================================
def _skeleton_draft(template: str) -> ReasoningMapDraft:
    nodes = [
        ReasoningMapNodeDraft(
            handle="r1",
            title="规划骨架:从目标到可交付成果",
            node_type="route",
            summary="先定义成果,再按阶段推进。",
            status="exploring",
            importance=5,
            impact=5,
            confidence=3,
            rationale="它决定阶段顺序。",
            source="agent",
        )
    ]
    for index, (title, description, criteria) in enumerate(_TEMPLATE_NODES[template], start=1):
        nodes.append(
            ReasoningMapNodeDraft(
                handle=f"r{index + 1}",
                title=title,
                node_type="stage",
                parent_handle="r1",
                summary=description,
                status="unexplored",
                importance=4,
                uncertainty=2,
                urgency=1,
                impact=4,
                confidence=3,
                rationale=criteria,
                deliverable=criteria,
                source="agent",
            )
        )
    return ReasoningMapDraft(
        nodes=tuple(nodes),
        focus_handle="r2",
        focus_reason="第一阶段先定下来,后面的时间线才有依据。",
        phase="roadmap_draft",
        turn_action="ask_user",
    )


# =================================================================================
# 时间线 / 周计划的确定性派生
# =================================================================================
@dataclass(frozen=True, slots=True)
class TimelinePhase:
    title: str
    timeframe: str
    goal: str
    tasks: str
    deliverable: str
    criteria: str
    start_week: int
    end_week: int
    deadline: date | None
    start_date: date | None = None
    end_date: date | None = None


def _detect_total_weeks(text: str, default: int) -> int:
    """从文字里粗略读一个时间跨度(30 天 / 4 周 / 1 个月)。读不出就用默认。"""
    raw = text or ""
    match = re.search(r"(\d+)\s*天", raw)
    if match:
        return max(3, min(52, round(int(match.group(1)) / 7)))
    match = re.search(r"(\d+)\s*(?:个?\s*)(周|星期)", raw)
    if match:
        return max(3, min(52, int(match.group(1))))
    match = re.search(r"(\d+)\s*个?月", raw)
    if match:
        return max(3, min(52, int(match.group(1)) * 4))
    return default


def build_timeline(
    template: str,
    workspace_intent: str,
    answer: str,
    known_deadline: str | None,
    today: date,
    *,
    goal_title: str = "",
) -> tuple[TimelinePhase, ...]:
    """把模板 + 回答派生成 3–6 个阶段(相对周)。**无截止日期也能生成。**"""
    skeletons = _TEMPLATE_NODES[template]
    total_weeks = _detect_total_weeks(
        f"{goal_title} {workspace_intent} {answer}",
        _TEMPLATE_DEFAULT_WEEKS.get(template, 12),
    )
    deadline_date: date | None = None
    if known_deadline:
        try:
            deadline_date = date.fromisoformat(known_deadline)
            days = (deadline_date - today).days
            if days >= 7:
                total_weeks = max(3, min(52, round(days / 7)))
        except ValueError:
            deadline_date = None
    count = len(skeletons)
    span = max(1, total_weeks // count)
    phases: list[TimelinePhase] = []
    cursor = 1
    for index, (title, description, criteria) in enumerate(skeletons):
        end_week = total_weeks if index == count - 1 else min(total_weeks, cursor + span - 1)
        if end_week < cursor:
            end_week = cursor
        phase_deadline = None
        start_date = end_date = None
        if deadline_date is not None:
            phase_deadline = today + timedelta(weeks=end_week)
            start_date = today + timedelta(weeks=cursor - 1)
            end_date = phase_deadline
        phases.append(
            TimelinePhase(
                title=title,
                timeframe=f"第 {cursor}–{end_week} 周",
                goal=description,
                tasks=description,
                deliverable=criteria,
                criteria=criteria,
                start_week=cursor,
                end_week=end_week,
                deadline=phase_deadline,
                start_date=start_date,
                end_date=end_date,
            )
        )
        cursor = end_week + 1
    return tuple(phases)


def _timeline_payload(
    phases: tuple[TimelinePhase, ...], *, status: str = "draft"
) -> list[dict]:
    """把阶段派生结果变成前端的结构化时间线投影。**日期与周次二选一,都是真值。**"""
    payload: list[dict] = []
    for index, phase in enumerate(phases, start=1):
        payload.append(
            {
                "id": f"phase-{index}",
                "title": phase.title,
                "kind": "phase",
                "startWeek": phase.start_week,
                "endWeek": phase.end_week,
                "startDate": phase.start_date.isoformat() if phase.start_date else None,
                "endDate": phase.end_date.isoformat() if phase.end_date else None,
                "goal": phase.goal,
                "deliverable": phase.deliverable,
                "completionCriteria": phase.criteria,
                "status": status,
                "planNodeId": None,
            }
        )
    return payload


def build_weekly_tasks(phase_title: str, phase_description: str) -> tuple[str, ...]:
    """从阶段派生本周可执行的小任务(确定性,5 条)。"""
    base = phase_description.rstrip("。")
    return (
        f"{phase_title}:确认本周要交出的最小成果",
        f"{phase_title}:完成关键的一步 —— {base}",
        f"{phase_title}:做出一个可检查的产出",
        f"{phase_title}:记录卡点与下一步",
        f"{phase_title}:复盘并按需要更新下周计划",
    )


# =================================================================================
# 状态迁移(唯一入口)
# =================================================================================
#: 允许的迁移。**不在表里的迁移一律拒绝** —— 防的是"模型/前端把流程拽回去"。
_ALLOWED_TRANSITIONS: dict[PlanningWorkflowStage, frozenset[PlanningWorkflowStage]] = {
    PlanningWorkflowStage.DISCOVERY: frozenset(
        {PlanningWorkflowStage.TIMELINE_DRAFT}
    ),
    PlanningWorkflowStage.TIMELINE_DRAFT: frozenset(
        {PlanningWorkflowStage.TIMELINE_REVIEW}
    ),
    PlanningWorkflowStage.TIMELINE_REVIEW: frozenset(
        {PlanningWorkflowStage.WEEKLY_EXECUTION}
    ),
    PlanningWorkflowStage.WEEKLY_EXECUTION: frozenset(
        {PlanningWorkflowStage.REPLANNING, PlanningWorkflowStage.WEEKLY_EXECUTION}
    ),
    PlanningWorkflowStage.REPLANNING: frozenset(
        {PlanningWorkflowStage.WEEKLY_EXECUTION}
    ),
}


def can_transition(current: PlanningWorkflowStage | None, target: PlanningWorkflowStage) -> bool:
    if current is None:
        return False
    return target in _ALLOWED_TRANSITIONS.get(current, frozenset())


def transition(session: GoalReasoningSession, target: PlanningWorkflowStage) -> None:
    """唯一的阶段迁移点。非法迁移抛 `InvalidInput`。"""
    from backend.services.errors import InvalidInput

    if session.workflow_stage is None:
        session.workflow_stage = target
        return
    if session.workflow_stage == target:
        return
    if not can_transition(session.workflow_stage, target):
        raise InvalidInput(
            f"规划流程不能从 {session.workflow_stage.value} 直接进入 {target.value}。"
        )
    session.workflow_stage = target


def is_v01(session: GoalReasoningSession | None) -> bool:
    return session is not None and session.workflow_stage is not None


# =================================================================================
# 读写辅助
# =================================================================================
async def _append_assistant(db, ctx, *, reply: str, conversation: Conversation | None = None) -> Message:
    conversation = conversation or await conversation_service.get_or_create_primary_conversation(db, ctx)
    result = ReasoningResult(
        reply=reply,
        source=ModelSource.DIRECT_LLM,
        request_id=uuid.uuid4().hex,
        prompt_version="v01-template",
    )
    return await conversation_service.append_reply(
        db, ctx, conversation=conversation, result=result
    )


async def _response(db, ctx, session, *, message=None, changed: bool = False, trace=None, code: str | None = None):
    if trace is not None:
        agent_trace_service.mark_terminal(
            trace, degraded=False, degraded_reason=None, stopped_reason="ready_to_propose", code=code
        )
    await db.commit()
    return await reasoning_service._response(
        db, ctx, session, message=message, changed=changed
    )


async def _root_handle(db, ctx, root: PlanNode) -> tuple[str, tuple[tuple[str, str], ...]]:
    conversation = await conversation_service.find_primary_conversation(db, ctx)
    turn = await turn_context.build_turn_context(
        db,
        ctx,
        conversation_id=conversation.id if conversation else uuid.uuid4(),
        user_message="生成规划提案",
        context_node_id=root.id,
        scope_root_id=root.id,
    )
    handle = next(
        (h for h, node_id in turn.node_handles if node_id == str(root.id)), None
    )
    if handle is None:  # pragma: no cover - 根目标必然在记号表里
        from backend.services.errors import InvalidInput

        raise InvalidInput("找不到根目标的记号。")
    return handle, turn.node_handles


# =================================================================================
# 阶段一:DISCOVERY
# =================================================================================
async def start_discovery(
    db: AsyncSession,
    ctx: WorkspaceContext,
    root: PlanNode,
    session: GoalReasoningSession,
    *,
    trace,
) -> AgentTurnResponse:
    """首轮:全局洞察 + 2–4 个核心问题。**零业务节点、零问题实体。**"""
    template = detect_template(ctx.workspace.intent or "", root.title or "")
    questions = discovery_questions(template)
    insight = discovery_insight(root.title or "这个目标", template)
    numbered = "\n".join(f"{i}. {q}" for i, q in enumerate(questions, start=1))
    message = await _append_assistant(db, ctx, reply=f"{insight}\n\n{numbered}")
    session.discovery = {
        "template": template,
        "insight": insight,
        "questions": list(questions),
        "answer": None,
        "followups": 0,
        "messageId": str(message.id),
    }
    session.workflow_stage = PlanningWorkflowStage.DISCOVERY
    session.phase = ReasoningSessionPhase.INTAKE
    session.status = ReasoningSessionStatus.READY
    return await _response(db, ctx, session, message=message, changed=False, trace=trace)


async def answer_discovery(
    db: AsyncSession,
    ctx: WorkspaceContext,
    root: PlanNode,
    session: GoalReasoningSession,
    *,
    content: str,
    conversation: Conversation,
    user_message: Message,
):
    """用户回答一次:默认直接生成第一层节点;完全无法生成时才允许补问一次。"""
    if session.workflow_stage is not PlanningWorkflowStage.DISCOVERY:
        from backend.services.errors import InvalidInput

        raise InvalidInput("现在不在战略澄清阶段。")

    state = dict(session.discovery or {})
    template = str(state.get("template") or detect_template(ctx.workspace.intent or "", root.title or "", content))
    followups = int(state.get("followups") or 0)

    # 完全无法生成(回答几乎为空)且还没补问过 -> 补问一次,仍然留在 DISCOVERY。
    if not _has_enough(content) and followups < 1:
        state["template"] = template
        state["followups"] = followups + 1
        state["answer"] = content
        session.discovery = state
        message = await _append_assistant(
            db,
            ctx,
            reply=(
                "这条信息还不足以拆出阶段。补充一句就够了:"
                "你希望最终拿到什么成果、大概多长时间、现在基础如何?"
            ),
            conversation=conversation,
        )
        await db.commit()
        return conversation_service.turn_outcome_for_reply(
            user_message=user_message,
            assistant_message=message,
            brief=None,
        )

    state["template"] = template
    state["answer"] = content
    session.discovery = state

    # 写第一层节点骨架(推理地图,不写业务计划)。
    existing = await reasoning_service._load_nodes(db, session.id)
    draft = _skeleton_draft(template)
    reasoning_service._apply_draft(db, session, draft, {row.handle: row for row in existing})
    await reasoning_service._resolve_parents(db, session.id)
    imported = await reasoning_service._load_nodes(db, session.id)
    transition(session, PlanningWorkflowStage.TIMELINE_DRAFT)
    session.phase = ReasoningSessionPhase.ROADMAP_DRAFT
    session.status = ReasoningSessionStatus.READY

    count = len([row for row in imported if row.node_type.value == "stage"])
    message = await _append_assistant(
        db,
        ctx,
        reply=(
            f"好,我按「{root.title}」拆出了 {count} 个第一层规划节点(画布左侧)。"
            "接下来我会把它们排成一条带时间范围的完整时间线草案,再请你确认。"
        ),
        conversation=conversation,
    )
    await db.commit()
    return conversation_service.turn_outcome_for_reply(
        user_message=user_message,
        assistant_message=message,
        brief=None,
    )


async def answer_v01_in_conversation(
    db: AsyncSession,
    ctx: WorkspaceContext,
    session: GoalReasoningSession,
    *,
    content: str,
    client_message_id: str | None,
    context_node_id: uuid.UUID | None,
):
    """`conversation_service.submit_turn` 在 V0.1 DISCOVERY 阶段委托进来的入口。

    先落用户消息(与普通对话同一纪律),再走 `answer_discovery`。
    """
    root = await reasoning_service.root_plan_node(db, ctx)
    if root is None:
        from backend.services.errors import InvalidInput

        raise InvalidInput("这个空间还没有根目标。")
    conversation = await conversation_service.get_or_create_primary_conversation(db, ctx)
    user_message = await conversation_service.record_user_message(
        db,
        ctx,
        conversation=conversation,
        text=content,
        client_message_id=client_message_id,
        context_node_id=context_node_id,
    )
    return await answer_discovery(
        db,
        ctx,
        root,
        session,
        content=content,
        conversation=conversation,
        user_message=user_message,
    )


# =================================================================================
# 阶段二:TIMELINE_DRAFT / TIMELINE_REVIEW
# =================================================================================
async def generate_timeline(
    db: AsyncSession,
    ctx: WorkspaceContext,
    root: PlanNode,
    session: GoalReasoningSession,
    *,
    trace,
) -> AgentTurnResponse:
    """从阶段一节点生成 3–6 个 timeline phase,落成待确认提案。"""
    state = dict(session.discovery or {})
    template = str(state.get("template") or detect_template(ctx.workspace.intent or "", root.title or ""))
    answer = str(state.get("answer") or "")

    brief = await reasoning_service.brief_service.load_brief(db, ctx.id)
    known_deadline = brief.deadline.isoformat() if brief and brief.deadline else None
    today = today_in(ctx.timezone)
    phases = build_timeline(
        template, ctx.workspace.intent or "", answer, known_deadline, today,
        goal_title=root.title or "",
    )
    # 时间线的**唯一权威投影**:结构化周次/日期,前端不猜日期。
    session.v01_timeline = _timeline_payload(phases)

    root_handle, handles = await _root_handle(db, ctx, root)
    actions: list[dict] = []
    for index, phase in enumerate(phases, start=1):
        description = (
            f"时间范围:{phase.timeframe}\n"
            f"目标:{phase.goal}\n"
            f"核心任务:{phase.tasks}\n"
            f"成果:{phase.deliverable}"
        )
        actions.append(
            {
                "op": "create_node",
                "localId": f"n{9000 + index}",
                "parentRef": root_handle,
                "title": phase.title,
                "nodeType": "stage",
                "purpose": "planning",
                "description": description,
                "acceptanceCriteria": f"完成标准:{phase.criteria}",
                "deadline": phase.deadline.isoformat() if phase.deadline else None,
            }
        )

    conversation = await conversation_service.get_or_create_primary_conversation(db, ctx)
    outcome = await proposal_service.build_from_actions(
        db,
        ctx,
        conversation_id=conversation.id,
        actions=tuple(actions),
        handles=handles,
        reasoning="由规划骨架与目标信息派生的整体时间线草案。",
        assistant_message=None,
        trigger_type=RevisionTrigger.INITIAL_PLAN,
    )
    if outcome.proposal is None:
        session.status = ReasoningSessionStatus.FAILED
        session.last_error = (
            "; ".join(error.message for error in outcome.errors) or "时间线草案没有通过校验。"
        )
        return await _response(db, ctx, session, changed=False, trace=trace)

    session.timeline_proposal_id = outcome.proposal.id
    transition(session, PlanningWorkflowStage.TIMELINE_REVIEW)
    first = phases[0]
    message = await _append_assistant(
        db,
        ctx,
        reply=(
            f"时间线草案生成好了:{len(phases)} 个阶段,从{first.timeframe}开始,"
            "每个阶段都有目标、核心任务、成果和完成标准。"
            "（没有截止日期时我按相对周排,不编造日历日期。）确认后我才写进计划。"
        ),
        conversation=conversation,
    )
    return await _response(
        db, ctx, session, message=message, changed=True, trace=trace
    )


async def mark_timeline_confirmed(
    db: AsyncSession, ctx: WorkspaceContext, session: GoalReasoningSession
) -> None:
    """提案确认后由路由调用:进入 WEEKLY_EXECUTION,并把草案项标记为已计划。"""
    if session.workflow_stage is not PlanningWorkflowStage.TIMELINE_REVIEW:
        return
    transition(session, PlanningWorkflowStage.WEEKLY_EXECUTION)
    session.phase = ReasoningSessionPhase.STRATEGY_CONFIRMED
    # 回填真实 PlanNode id(按标题匹配),状态 draft → planned。
    root = await reasoning_service.root_plan_node(db, ctx)
    phases = await _phase_nodes(db, ctx, root) if root is not None else []
    by_title = {phase.title: phase.id for phase in phases}
    timeline = list(session.v01_timeline or [])
    for item in timeline:
        if not isinstance(item, dict):
            continue
        item["status"] = "planned"
        linked = by_title.get(str(item.get("title") or ""))
        item["planNodeId"] = str(linked) if linked else None
    session.v01_timeline = timeline
    flag_modified(session, "v01_timeline")
    await db.commit()


# =================================================================================
# 阶段三:WEEKLY_EXECUTION
# =================================================================================
async def _phase_nodes(db: AsyncSession, ctx: WorkspaceContext, root: PlanNode) -> list[PlanNode]:
    """V0.1 的阶段节点 = 根目标下、`node_type='stage'` 的直属子节点。"""
    result = await db.execute(
        select(PlanNode)
        .where(
            PlanNode.workspace_id == ctx.id,
            PlanNode.parent_id == root.id,
            PlanNode.node_type == NodeType.STAGE,
            PlanNode.deleted_at.is_(None),
        )
        .order_by(PlanNode.order_index.asc(), PlanNode.created_at.asc())
    )
    return list(result.scalars())


async def _week_nodes(
    db: AsyncSession, phases: list[PlanNode]
) -> list[PlanNode]:
    phase_ids = [phase.id for phase in phases]
    if not phase_ids:
        return []
    result = await db.execute(
        select(PlanNode).where(
            PlanNode.parent_id.in_(phase_ids),
            PlanNode.node_type == NodeType.STAGE,
            PlanNode.deleted_at.is_(None),
        )
    )
    return list(result.scalars())


async def generate_weekly_plan(
    db: AsyncSession,
    ctx: WorkspaceContext,
    root: PlanNode,
    session: GoalReasoningSession,
    *,
    trace,
) -> AgentTurnResponse:
    """从已确认的第一个 timeline phase 派生“本周计划 + 下周预览”。

    **不重复**:已经存在的周/任务节点用 `update_node` 就地更新,只有确实还没有的才
    `create_node`。所以重规划之后再次生成,周计划不会翻倍 —— 同一个锚点的计划永远
    只有一份。
    """
    phases = await _phase_nodes(db, ctx, root)
    if not phases:
        from backend.services.errors import InvalidInput

        raise InvalidInput("还没有已确认的时间线阶段。")

    conversation = await conversation_service.find_primary_conversation(db, ctx)
    turn = await turn_context.build_turn_context(
        db,
        ctx,
        conversation_id=conversation.id if conversation else uuid.uuid4(),
        user_message="生成周计划",
        context_node_id=root.id,
        scope_root_id=root.id,
    )
    handle_of = {node_id: handle for handle, node_id in turn.node_handles}
    _, handles = await _root_handle(db, ctx, root)

    # 现有周计划:按 (phase, 标题) 找到周节点,再取它的任务(按创建顺序)。
    existing_weeks = await _week_nodes(db, phases)
    existing_tasks: dict[uuid.UUID, list[PlanNode]] = {}
    if existing_weeks:
        rows = await db.execute(
            select(PlanNode)
            .where(
                PlanNode.parent_id.in_([week.id for week in existing_weeks]),
                PlanNode.deleted_at.is_(None),
            )
            .order_by(PlanNode.created_at.asc())
        )
        for row in rows.scalars():
            existing_tasks.setdefault(row.parent_id, []).append(row)

    actions: list[dict] = []
    this_week = phases[0]
    next_week = phases[1] if len(phases) > 1 else phases[0]
    counter = 0
    for label, phase in (("本周计划", this_week), ("下周预览", next_week)):
        phase_handle = handle_of.get(str(phase.id))
        if phase_handle is None:
            continue
        week_title = f"{label}:{phase.title}"
        week_desc = f"来源阶段:{phase.title}\n" + (phase.description or "")[:400]
        week = next(
            (
                candidate
                for candidate in existing_weeks
                if candidate.parent_id == phase.id and candidate.title == week_title
            ),
            None,
        )
        counter += 1
        if week is not None and str(week.id) in handle_of:
            week_ref = handle_of[str(week.id)]
            actions.append(
                {"op": "update_node", "target_ref": week_ref, "title": week_title, "description": week_desc}
            )
        else:
            week_ref = f"n{9100 + counter}"
            actions.append(
                {
                    "op": "create_node",
                    "localId": week_ref,
                    "parentRef": phase_handle,
                    "title": week_title,
                    "nodeType": "stage",
                    "purpose": "planning",
                    "description": week_desc,
                }
            )
        existing = existing_tasks.get(week.id, []) if week is not None else []
        for index, task in enumerate(build_weekly_tasks(phase.title, phase.description or phase.title)):
            task_desc = (
                f"所属:{label} · {phase.title}\n"
                f"来源阶段:{phase.title}\n"
                f"完成标准:{phase.acceptance_criteria or '见阶段说明'}"
            )
            if index < len(existing) and str(existing[index].id) in handle_of:
                actions.append(
                    {
                        "op": "update_node",
                        "target_ref": handle_of[str(existing[index].id)],
                        "title": task,
                        "description": task_desc,
                    }
                )
            else:
                counter += 1
                actions.append(
                    {
                        "op": "create_node",
                        "localId": f"n{9100 + counter}",
                        "parentRef": week_ref,
                        "title": task,
                        "nodeType": "task",
                        "purpose": "planning",
                        "description": task_desc,
                    }
                )

    if not actions:
        from backend.services.errors import InvalidInput

        raise InvalidInput("周计划没有可用的阶段。")

    outcome = await proposal_service.build_from_actions(
        db,
        ctx,
        conversation_id=conversation.id if conversation else None,
        actions=tuple(actions),
        handles=handles,
        reasoning="由已确认时间线的第一个阶段派生的本周计划与下周预览(就地更新,不重复)。",
        assistant_message=None,
        trigger_type=RevisionTrigger.INITIAL_PLAN,
    )
    if outcome.proposal is None:
        session.status = ReasoningSessionStatus.FAILED
        session.last_error = "周计划没有通过校验。"
        return await _response(db, ctx, session, changed=False, trace=trace)

    message = await _append_assistant(
        db,
        ctx,
        reply=(
            f"根据已确认的时间线,我排了「{this_week.title}」的本周计划,"
            f"并预览了下周的「{next_week.title}」。确认后写入计划。"
        ),
        conversation=await conversation_service.get_or_create_primary_conversation(db, ctx),
    )
    return await _response(db, ctx, session, message=message, changed=True, trace=trace)


async def weekly_completion(
    db: AsyncSession, ctx: WorkspaceContext, root: PlanNode
) -> tuple[int, int]:
    """**本周计划**的完成率(完成数, 总数)。下周预览不计入。"""
    phases = await _phase_nodes(db, ctx, root)
    weeks = await _week_nodes(db, phases)
    current = [week for week in weeks if week.title.startswith("本周计划")]
    week_ids = [week.id for week in current]
    if not week_ids:
        return (0, 0)
    tasks = await db.execute(
        select(PlanNode).where(
            PlanNode.parent_id.in_(week_ids),
            PlanNode.deleted_at.is_(None),
        )
    )
    rows = list(tasks.scalars())
    done = sum(1 for row in rows if row.status is NodeStatus.COMPLETED)
    return (done, len(rows))


async def record_feedback(
    db: AsyncSession,
    ctx: WorkspaceContext,
    root: PlanNode,
    session: GoalReasoningSession,
    *,
    node_id: uuid.UUID,
    outcome: str,
    trace=None,
) -> AgentTurnResponse:
    """记录一条任务反馈:完成 / 部分完成 / 未完成 / 延期。

    `outcome` 映射到现有 `NodeStatus`;部分完成与延期把说明追加到节点正文,
    不新建表、不改历史。
    """
    from backend.services import node_service

    node = await node_service.load_node(db, ctx, node_id)
    mapping = {
        "done": NodeStatus.COMPLETED,
        "partial": NodeStatus.DOING,
        "missed": NodeStatus.PENDING,
        "delayed": NodeStatus.PENDING,
    }
    if outcome not in mapping:
        from backend.services.errors import InvalidInput

        raise InvalidInput("不认识的反馈结果。")
    node.status = mapping[outcome]
    if outcome in {"partial", "delayed"}:
        note = "部分完成" if outcome == "partial" else "延期"
        node.description = (node.description or "") + f"\n【反馈】{note}"
    node.content_version += 1
    await db.commit()

    done, total = await weekly_completion(db, ctx, root)
    rate = (done / total) if total else 0.0
    if total and rate < 0.6:
        if session.workflow_stage is PlanningWorkflowStage.WEEKLY_EXECUTION:
            transition(session, PlanningWorkflowStage.REPLANNING)
        message = await _append_assistant(
            db,
            ctx,
            reply=f"本周完成率 {done}/{total}(低于 60%)。我先不催你,而是把剩余时间线往后再排一版,你看过再确认。",
        )
    else:
        message = await _append_assistant(
            db,
            ctx,
            reply=f"记下了。本周进度 {done}/{total}。",
        )
    return await _response(db, ctx, session, message=message, changed=True, trace=trace)


async def generate_replan(
    db: AsyncSession,
    ctx: WorkspaceContext,
    root: PlanNode,
    session: GoalReasoningSession,
    *,
    trace,
) -> AgentTurnResponse:
    """重规划:只为**未来**阶段生成调整提案,已完成的历史一律不动。"""
    phases = await _phase_nodes(db, ctx, root)
    if not phases:
        from backend.services.errors import InvalidInput

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
    phase_handles = {node_id: h for h, node_id in turn.node_handles}

    # 未来(未完成)阶段整体后移一档;已完成历史不动,并保留其已计划状态。
    completed_ids = {
        str(phase.id) for phase in phases if phase.status is NodeStatus.COMPLETED
    }
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
    # JSON 列的就地改动不会被 ORM 自动当成 dirty,显式打标。
    flag_modified(session, "v01_timeline")

    actions: list[dict] = []
    for phase in phases:
        handle = phase_handles.get(str(phase.id))
        if handle is None:
            continue
        # 已完成/已交付的历史阶段不动。
        if phase.status is NodeStatus.COMPLETED:
            continue
        actions.append(
            {
                "op": "update_node",
                "target_ref": handle,
                "description": (phase.description or "")
                + "\n【重规划】按当前完成情况,后续阶段整体后移一档,先保证前一个成果做扎实。",
            }
        )
    if not actions:
        message = await _append_assistant(
            db, ctx, reply="未来阶段都已经完成,暂时不需要重规划。", conversation=conversation
        )
        transition(session, PlanningWorkflowStage.WEEKLY_EXECUTION)
        return await _response(db, ctx, session, message=message, changed=False, trace=trace)

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
        session.status = ReasoningSessionStatus.FAILED
        session.last_error = "重规划提案没有通过校验。"
        return await _response(db, ctx, session, changed=False, trace=trace)

    message = await _append_assistant(
        db,
        ctx,
        reply="我按最近的完成情况提了一版**只调整未来阶段**的重规划,已完成的阶段原样保留。确认后生效。",
        conversation=conversation,
    )
    return await _response(db, ctx, session, message=message, changed=True, trace=trace)


async def mark_replan_confirmed(
    db: AsyncSession, ctx: WorkspaceContext, session: GoalReasoningSession
) -> None:
    # 时间线项确认后一律转正式样式;阶段只在确实处于 REPLANNING 时推进。
    timeline = list(session.v01_timeline or [])
    for item in timeline:
        if isinstance(item, dict):
            item["status"] = "planned"
    session.v01_timeline = timeline
    flag_modified(session, "v01_timeline")
    if session.workflow_stage is PlanningWorkflowStage.REPLANNING:
        transition(session, PlanningWorkflowStage.WEEKLY_EXECUTION)
    await db.commit()


async def on_proposal_confirmed(
    db: AsyncSession, ctx: WorkspaceContext, proposal_id: uuid.UUID
) -> None:
    """提案确认后由路由调用:按 V0.1 阶段决定是否推进。"""
    session = await reasoning_service.get_session(db, ctx)
    if not is_v01(session):
        return
    if (
        session.workflow_stage is PlanningWorkflowStage.TIMELINE_REVIEW
        and session.timeline_proposal_id == proposal_id
    ):
        await mark_timeline_confirmed(db, ctx, session)
    elif any(
        isinstance(item, dict) and item.get("status") == "draft"
        for item in (session.v01_timeline or [])
    ):
        # 还有未确认的时间线项 —— 刚确认的只可能是重规划(或时间线)提案。
        await mark_replan_confirmed(db, ctx, session)


async def _has_open_proposal(db: AsyncSession, ctx: WorkspaceContext) -> bool:
    """这个空间有没有还没处理完的提案。用来给 V0.1 的自动推进去重。"""
    from backend.db.models import Proposal
    from backend.db.models.enums import ProposalStatus

    result = await db.execute(
        select(Proposal.id)
        .where(
            Proposal.workspace_id == ctx.id,
            Proposal.status.in_(
                (ProposalStatus.VALIDATED, ProposalStatus.PENDING_CONFIRMATION)
            ),
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


async def advance(
    db: AsyncSession,
    ctx: WorkspaceContext,
    root: PlanNode,
    session: GoalReasoningSession,
    *,
    trace,
) -> AgentTurnResponse:
    """按当前 `workflow_stage` 推进**一步**。**唯一的自动推进入口。**"""
    stage = session.workflow_stage
    if stage is None or stage is PlanningWorkflowStage.DISCOVERY:
        state = dict(session.discovery or {})
        if state.get("questions"):
            # 已经问过、还在等回答:返回当前状态,不重复提问。
            return await _response(db, ctx, session, changed=False, trace=trace)
        return await start_discovery(db, ctx, root, session, trace=trace)
    if stage is PlanningWorkflowStage.TIMELINE_DRAFT:
        if await _has_open_proposal(db, ctx):
            return await _response(db, ctx, session, changed=False, trace=trace)
        return await generate_timeline(db, ctx, root, session, trace=trace)
    if stage is PlanningWorkflowStage.TIMELINE_REVIEW:
        return await _response(db, ctx, session, changed=False, trace=trace)
    if stage is PlanningWorkflowStage.WEEKLY_EXECUTION:
        if await _has_open_proposal(db, ctx):
            return await _response(db, ctx, session, changed=False, trace=trace)
        return await generate_weekly_plan(db, ctx, root, session, trace=trace)
    if stage is PlanningWorkflowStage.REPLANNING:
        if await _has_open_proposal(db, ctx):
            return await _response(db, ctx, session, changed=False, trace=trace)
        return await generate_replan(db, ctx, root, session, trace=trace)
    return await _response(db, ctx, session, changed=False, trace=trace)


__all__ = [
    "PlanningWorkflowStage",
    "advance",
    "answer_discovery",
    "answer_v01_in_conversation",
    "build_timeline",
    "build_weekly_tasks",
    "can_transition",
    "detect_template",
    "discovery_insight",
    "discovery_questions",
    "generate_replan",
    "generate_timeline",
    "generate_weekly_plan",
    "is_v01",
    "mark_replan_confirmed",
    "mark_timeline_confirmed",
    "on_proposal_confirmed",
    "record_feedback",
    "start_discovery",
    "transition",
    "weekly_completion",
]
