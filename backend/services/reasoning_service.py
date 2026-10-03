"""目标推理地图的读取、幂等探索与增量更新。

## 职责边界

这个模块**只**读写 `goal_reasoning_sessions` / `reasoning_nodes` /
`reasoning_node_links`。它不写 `plan_nodes`、不写排期、不写依赖和关系。用户确认某条
战略路线之后要落到业务计划时,它**通过既有 proposal 链路**提议,再由用户确认 ——
这里最多创建一份待确认的 `Proposal`,绝不直接写计划。

## 幂等

`space_entered` 的判据是 `input_version`:根目标正文版本 + 规划简报版本 + 活节点数 +
空间计划版本,压成一个稳定摘要。同一输入重进直接返回当前地图,绝不重复建节点。
模型失败/输出不合法时只更新会话的 `status=failed` 与 `last_error`,**地图一行都不动**,
下一次可安全重试。

## 优先级不落库

`priority` 是四个维度的可解释启发式(见 `compute_priority`)。它是**排序依据**,不是
客观概率;用户看到的是 `focus_reason` 那句人话。落库会让"改了评分忘了改排序"成为
可能,所以它每次读取时现算。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import replace
from datetime import date, datetime, timedelta
from hashlib import blake2b

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agent.runtime.base import ReasoningMapDraft, ReasoningResult, TurnContext
from backend.contracts.reasoning import (
    AgentTurnRequest,
    AgentTurnResponse,
    GoalReasoningView,
    PendingIntakeView,
    ReasoningLinkView,
    ReasoningNodeView,
)
from backend.core.config import settings
from backend.db.base import utcnow
from backend.db.models import (
    AgentQuestion,
    GoalReasoningSession,
    Message,
    PlanningBrief,
    PlanNode,
    Proposal,
    ReasoningNode,
    ReasoningNodeLink,
    ReasoningState,
)
from backend.db.models.enums import (
    ACTIVE_QUESTION_STATUSES,
    AgentTraceStep,
    DegradedReason,
    ModelSource,
    PlanningWorkflowStage,
    ProposalStatus,
    QuestionPresentation,
    ReasoningLinkType,
    ReasoningNodeStatus,
    ReasoningNodeType,
    ReasoningSessionPhase,
    ReasoningSessionStatus,
    ReasoningSource,
    ReasoningTurnAction,
    RevisionTrigger,
)
from backend.services import (
    agent_trace_service,
    brief_service,
    conversation_service,
    proposal_service,
    question_service,
    turn_context,
)
from backend.services.context import WorkspaceContext
from backend.services.errors import InvalidInput, NodeNotFound

logger = logging.getLogger(__name__)

#: 一轮探索最多生成多少个一级决策维度。规范:4–8 个。
MIN_PRIMARY_NODES = 4
MAX_PRIMARY_NODES = 8
#: 整张地图的节点上限(只是护栏,防止模型复读)。一级 + 展开的子树都算在内。
MAX_MAP_NODES = 40
#: 一次最多呈现几个高价值问题。规范:复杂目标最多 3 个相互独立的问题。
MAX_QUESTIONS_PER_TURN = 3
#: 阶段 10:战略阶段**每轮最多 1 个活动问题** —— 先给路线判断,再问一个真正会
#: 改变方向的问题。
MAX_STRATEGIC_QUESTIONS = 1
#: 阶段 11:战略澄清 intake 最多问几个关键问题。到顶必须继续生成时间架构,不能无限追问。
MAX_INTAKE_QUESTIONS = 5

#: 阶段 12:intake 模式的回合用途。它决定用哪份系统提示词与哪条解析路径。
PURPOSE_INTAKE = "strategic_intake"
#: 时间架构模式的回合用途(与阶段 7–11 的 goal_reasoning 一致)。
PURPOSE_ARCHITECTURE = "goal_reasoning"

#: intake 决策 `decisionScope` 的闭集。**服务端硬闸,不只是解析层。**
INTAKE_SCOPES = frozenset({"route", "duration", "sequence", "deliverable", "constraint"})

#: intake 完成、进入时间架构时喂给模型的那句触发。前面的问答都在对话历史里。
ARCHITECTURE_TRIGGER = (
    "战略澄清已经结束。请基于上面的目标与对话,一次性给出整体时间架构:"
    "恰好一条 route + 3–5 个挂在它下面的 stage,每个 stage 带 timeframe / deliverable / "
    "passCriteria 与结构化时间范围。**不要再提问。**"
)
#: 评分维度范围。
SCORE_MIN = 0
SCORE_MAX = 5


def clamp_score(value: object) -> int:
    """把模型给的评分夹到 0–5 的整数。非法值一律按 0(不是"不给"就没分)。"""
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return max(SCORE_MIN, min(SCORE_MAX, number))


def compute_priority(
    *, importance: int, uncertainty: int, urgency: int, impact: int
) -> float:
    """可解释启发式排序。**不是概率,不对外叫置信度。**

    `priority = 0.30×importance + 0.25×uncertainty + 0.25×impact + 0.20×urgency`
    """
    return round(
        0.30 * importance + 0.25 * uncertainty + 0.25 * impact + 0.20 * urgency,
        3,
    )


async def get_session(db: AsyncSession, ctx: WorkspaceContext) -> GoalReasoningSession | None:
    """只查,不建。**读接口必须走这一条** —— 打开空间不该凭空造一个会话。"""
    return await db.scalar(
        select(GoalReasoningSession).where(GoalReasoningSession.workspace_id == ctx.id)
    )


async def get_or_create_session(
    db: AsyncSession, ctx: WorkspaceContext
) -> GoalReasoningSession | None:
    """取会话,没有就建一个。**只有显式 Agent turn 可以调它。**

    根目标不存在时返回 `None`(调用方负责报错)—— 没有根目标就没有可推理的对象。
    """
    existing = await get_session(db, ctx)
    if existing is not None:
        return existing
    root = await root_plan_node(db, ctx)
    if root is None:
        return None
    # 规划智能体重构 V1:开启时优先于 V0.1。新会话停在 `initial_thinking` ——
    # 画布只有根目标,右侧是大号“初步思考”输入区;用户提交目标后才建分组。
    # **`workflow_stage` 保持 NULL**,不走 V0.1 状态机。
    v1_stage = None
    if settings.planning_v1:
        from backend.services.v1_service import V1_INITIAL_THINKING  # 延迟 import

        v1_stage = V1_INITIAL_THINKING
    session = GoalReasoningSession(
        workspace_id=ctx.id,
        root_plan_node_id=root.id,
        # 阶段 11:新会话从**战略澄清 intake**开始 —— 先问关键问题,再给时间架构。
        phase=ReasoningSessionPhase.INTAKE,
        turn_action=ReasoningTurnAction.ANALYZE,
        status=ReasoningSessionStatus.IDLE,
        # 规划智能体 V0.1:新建目标空间走程序控制的三阶段工作流(需显式开启)。
        # 关闭时这一列为 NULL,行为与以前完全一样。
        workflow_stage=(
            None
            if v1_stage is not None
            else (PlanningWorkflowStage.DISCOVERY if settings.v01_planning else None)
        ),
        v1_stage=v1_stage,
    )
    db.add(session)
    await db.flush()
    return session


async def root_plan_node(db: AsyncSession, ctx: WorkspaceContext) -> PlanNode | None:
    """这个空间的根目标(`depth == 0`)。一个空间只有一个。"""
    return await db.scalar(
        select(PlanNode)
        .where(
            PlanNode.workspace_id == ctx.id,
            PlanNode.depth == 0,
            PlanNode.deleted_at.is_(None),
        )
        .order_by(PlanNode.created_at.asc())
        .limit(1)
    )


async def input_version(
    db: AsyncSession, ctx: WorkspaceContext, root: PlanNode
) -> str:
    """把"会影响地图的输入"压成一个稳定摘要。**幂等比较只用它,不用时间。**

    ## 只认根目标与简报,**不认整个空间的计划版本**

    最初的版本把 `current_revision_version` 和活节点数也算进来了。后果是**每一次计划
    写入**(用户确认提案、AI 建一个节点)都会让 `input_version` 变化,于下一次进入时
    重新跑一整轮模型 —— 既贵,又与用户的对话轮次抢同一个会话的写入,直接造成
    `messages.seq` 唯一约束冲突。

    “根目标内容/版本显著变化才重新探索”里的“根目标”就是字面意思:把根目标的
    正文版本、标题与已确认简报算进来就够了。改一个子节点、确认一份提案,不该让整张
    地图重来。
    """
    brief_version = await db.scalar(
        select(PlanningBrief.version).where(PlanningBrief.workspace_id == ctx.id)
    )
    parts = "|".join(
        [
            str(root.id),
            str(root.content_version),
            (root.title or "").strip(),
            str(brief_version or 0),
        ]
    )
    return blake2b(parts.encode(), digest_size=16).hexdigest()


async def _load_nodes(db: AsyncSession, session_id: uuid.UUID) -> list[ReasoningNode]:
    rows = await db.execute(
        select(ReasoningNode)
        .where(ReasoningNode.session_id == session_id)
        .order_by(ReasoningNode.created_at.asc(), ReasoningNode.handle.asc())
    )
    return list(rows.scalars())


async def _load_links(
    db: AsyncSession, session_id: uuid.UUID
) -> list[ReasoningNodeLink]:
    rows = await db.execute(
        select(ReasoningNodeLink)
        .where(ReasoningNodeLink.session_id == session_id)
        .order_by(ReasoningNodeLink.created_at.asc())
    )
    return list(rows.scalars())


def _node_view(node: ReasoningNode, parent_handle: str | None) -> ReasoningNodeView:
    return ReasoningNodeView(
        id=node.id,
        handle=node.handle,
        parent_handle=parent_handle,
        linked_plan_node_id=node.linked_plan_node_id,
        title=node.title,
        summary=node.summary,
        user_description=node.user_description,
        node_type=node.node_type.value,
        status=node.status.value,
        next_action=node.next_action.value,
        importance=node.importance,
        uncertainty=node.uncertainty,
        urgency=node.urgency,
        impact=node.impact,
        confidence=node.confidence,
        priority=compute_priority(
            importance=node.importance,
            uncertainty=node.uncertainty,
            urgency=node.urgency,
            impact=node.impact,
        ),
        rationale=node.rationale,
        assumptions=[str(item) for item in (node.assumptions or [])],
        evidence=[str(item) for item in (node.evidence or [])],
        timeframe=node.timeframe,
        deliverable=node.deliverable,
        pass_criteria=node.pass_criteria,
        timeframe_kind=node.timeframe_kind,
        start_week=node.start_week,
        end_week=node.end_week,
        start_date=node.start_date,
        end_date=node.end_date,
        source=node.source.value,
        version=node.version,
        updated_at=node.updated_at,
        #: 规划智能体重构 V1:非 V1 节点三列均为 None,老前端行为不变。
        v1_kind=node.v1_kind,
        v1_key=node.v1_key,
        v1_question=node.v1_question,
    )


async def build_view(
    db: AsyncSession,
    ctx: WorkspaceContext,
    session: GoalReasoningSession | None,
) -> GoalReasoningView:
    """把会话、节点、连线拼成给前端的视图。没有会话时返回一个空的地图。"""
    if session is None:
        root = await root_plan_node(db, ctx)
        return GoalReasoningView(
            workspace_id=ctx.id,
            root_plan_node_id=root.id if root else None,
            phase="orientation",
            turn_action="analyze",
            status=ReasoningSessionStatus.IDLE.value,
            intake_question_limit=MAX_INTAKE_QUESTIONS,
        )

    nodes = await _load_nodes(db, session.id)
    links = await _load_links(db, session.id)
    handle_of = {node.id: node.handle for node in nodes}
    focus = next((node for node in nodes if node.id == session.focus_reasoning_node_id), None)
    return GoalReasoningView(
        workspace_id=ctx.id,
        session_id=session.id,
        root_plan_node_id=session.root_plan_node_id,
        phase=session.phase.value,
        turn_action=session.turn_action.value,
        status=session.status.value,
        map_version=session.map_version,
        focus_handle=focus.handle if focus else None,
        focus_reasoning_node_id=session.focus_reasoning_node_id,
        focus_reason=session.focus_reason,
        #: 阶段 11:intake 进度,给对话区那行极轻量文字用。
        intake_questions_asked=session.intake_questions_asked,
        intake_question_limit=MAX_INTAKE_QUESTIONS,
        #: 阶段 12:正在等回答的 intake 关键问题(只来自会话状态,不是问题实体)。
        pending_intake=(
            PendingIntakeView(
                message_id=session.pending_intake_message_id,
                question=str(session.pending_intake_decision.get("question") or ""),
                decision_scope=str(session.pending_intake_decision.get("decisionScope") or ""),
                why_this_matters=str(session.pending_intake_decision.get("whyThisMatters") or ""),
                quick_replies=[
                    str(item)
                    for item in (session.pending_intake_decision.get("quickReplies") or [])
                    if str(item).strip()
                ],
            )
            if isinstance(session.pending_intake_decision, dict)
            else None
        ),
        #: 规划智能体 V0.1:程序控制的工作流阶段与阶段一问题。
        workflow_stage=(
            session.workflow_stage.value if session.workflow_stage is not None else None
        ),
        discovery_questions=[
            str(item) for item in ((session.discovery or {}).get("questions") or [])
        ],
        #: V0.1 时间线投影(唯一权威来源;非 V0.1 为空)。
        #:
        #: `status` 是**读的时候算的**:还在 TIMELINE_REVIEW / REPLANNING 就是草案,
        #: 其余(已确认)就是正式计划 —— 落一列状态就得在每次确认时记得改写,
        #: 漏一次就会出现“已确认却仍标着待确认”。
        v01_timeline=[
            {
                **item,
                "status": (
                    "draft"
                    if session.workflow_stage
                    in (
                        PlanningWorkflowStage.TIMELINE_REVIEW,
                        PlanningWorkflowStage.REPLANNING,
                    )
                    # 规划智能体重构 V1(P3/P4):同样在“审阅中”算草案。
                    or session.v1_stage in ("coarse_timeline_review", "replanning")
                    else "planned"
                ),
            }
            for item in (session.v01_timeline or [])
            if isinstance(item, dict)
        ],
        v01_timeline_proposal_id=session.timeline_proposal_id,
        #: 规划智能体重构 V1:非 V1 会话所有 v1_* 列均为 None,老前端行为不变。
        v1_stage=session.v1_stage,
        v1_judgment=session.v1_judgment,
        v1_question=session.v1_question,
        v1_focus_key=session.v1_focus_key,
        v1_focus_reason=session.v1_focus_reason,
        v1_strategy=session.v1_strategy,
        v1_status=session.v1_status,
        v1_error=session.v1_error,
        #: P2.1:战略判断优先 + 候选方向与用户选择。
        v1_strategic_thesis=session.v1_strategic_thesis,
        v1_candidate_directions=session.v1_candidate_directions,
        v1_selected_direction=session.v1_selected_direction,
        #: P5:只有 V1 空间且开关打开时才允许导出审计记录。
        v1_audit_export_enabled=bool(settings.agent_audit_export and session.v1_stage is not None),
        dates_calibrated=session.dates_calibrated,
        input_version=session.input_version,
        strategy_proposal_id=session.strategy_proposal_id,
        explored_at=session.explored_at,
        last_evaluated_at=session.last_evaluated_at,
        nodes=[
            _node_view(node, handle_of.get(node.parent_id) if node.parent_id else None)
            for node in nodes
        ],
        links=[
            ReasoningLinkView(
                id=link.id,
                source_handle=handle_of.get(link.source_reasoning_node_id, ""),
                target_handle=handle_of.get(link.target_reasoning_node_id, ""),
                link_type=link.link_type.value,
                note=link.note,
            )
            for link in links
        ],
        error=session.last_error,
    )


async def load_map(db: AsyncSession, ctx: WorkspaceContext) -> GoalReasoningView:
    """当前目标推理地图。**只读,不创建会话。**"""
    session = await get_session(db, ctx)
    return await build_view(db, ctx, session)


# ---------------------------------------------------------------------------------
# 写入:幂等的自动探索与地图操作
# ---------------------------------------------------------------------------------
#: `running` 多久算卡死,允许下一次接管。比一次模型调用的最长超时宽裕得多。
STALE_RUNNING_SECONDS = 120
#: 系统自动探索时写进提示词的那句话。**不是用户消息**,不会落进对话历史。
SPACE_ENTERED_PROMPT = "(系统)用户刚进入这个空间,请开始一次有界的目标探索与问题地图梳理。"
#: “细化第一阶段”那个入口替用户说的那句话。**执行规划阶段才问排期条件。**
REFINE_PHASE_MESSAGE = (
    "基于已确认的战略,请细化第一阶段:给出阶段与里程碑。"
    "需要时再问我在执行规划阶段才该问的条件(截止时间、每周可用时间、当前水平);"
    "具体任务安排仍然提成待确认提案。"
)

#: 路线图不合法时,给模型的**结构化纠错提示**。
#:
#: 真实模型经常“只回一句话”或“只提一个问题”—— 提示词管不住它们。所以在同一轮
#: 预算内**再试一次**,而这一次把“必须给出什么形状”写死。再失败就诚实报错,不降级成
#: “先问一个碎问题”。
ROADMAP_CORRECTION_MESSAGE = (
    "你上一轮没有给出合法的战略路线图。现在只输出 JSON,并且必须包含 reasoningMap:"
    "恰好一条顶层 route(nodeType=\"route\", parent=null),以及挂在它下面的 3–5 个 "
    "stage(nodeType=\"stage\", parent=route 的 handle);每个 stage 都必须有 "
    "timeframe(粗粒度时间带,例如“约 2 周”)、deliverable(成果物)、passCriteria(通过标准)。"
    "不要只写自然语言回复,不要只提问题,不要输出一堆一级 dimension。"
)

#: “重新生成战略路线”按钮替用户说的那句话。旧地图(只有一级维度)走它。
REGENERATE_ROADMAP_MESSAGE = (
    "用户要求基于当前目标与已有信息重新生成一条**战略路线图**。"
    "请给出:恰好一条顶层 route + 3–5 个挂在它下面的 stage,每个 stage 带 "
    "timeframe / deliverable / passCriteria。旧的一级维度不用再展开。"
)

#: 阶段 11:intake 纠错。模型在 intake 期间**必须先问一个高杠杆关键问题**,不能直接给路线。
INTAKE_CORRECTION_MESSAGE = (
    "现在是战略澄清 intake。请只输出 `reply`(1–3 句当前判断)与 `intakeDecision`。"
    "`intakeDecision.action` 只能是 `ask` 或 `ready_for_architecture`;若还要问,"
    "`question` 只能是**一个**能改变整体路线/总时长/阶段顺序/阶段成果/重大约束的关键问题,"
    "并带上 `decisionScope`(route/duration/sequence/deliverable/constraint)与 `whyThisMatters`。"
    "**不要输出 reasoningMap / route / stage / questions / options / actions**,不要问工具、"
    "课程、教材、IDE、练习、每天几点、具体任务或代码细节这类执行细节。"
)


class MapValidationError(Exception):
    """模型给的地图操作不合法。**不写半成品**,把原因留给会话的 `last_error`。"""


def _message_view(message) -> object:
    """助手消息 -> 接口视图。与 routes/workspaces.py 的 `_message_view` 同形。"""
    from backend.contracts.conversation import MessageView

    return MessageView(
        id=message.id,
        role=message.role.value,
        content=message.content,
        seq=message.seq,
        created_at=message.created_at,
        context_node_id=message.context_node_id,
        proposal_id=message.proposal_id,
        model_source=message.model_source.value if message.model_source else None,
        degraded=message.degraded,
        degraded_reason=message.degraded_reason.value if message.degraded_reason else None,
        research=message.research,
    )


# --- 渲染 ------------------------------------------------------------------
def render_map_section(nodes: list[ReasoningNode], links: list[ReasoningNodeLink]) -> str:
    """把当前地图渲染给模型。**只有短记号,没有真实 UUID。**"""
    if not nodes:
        return "(这是第一次探索,地图还是空的。)"
    handle_of = {node.id: node.handle for node in nodes}
    lines: list[str] = []
    for node in nodes:
        parent = handle_of.get(node.parent_id) if node.parent_id else None
        parent_note = f"  parent={parent}" if parent else ""
        lines.append(
            f"- {node.handle}: {node.title} [{node.node_type.value}/{node.status.value}]"
            f" 重要度{node.importance} 不确定{node.uncertainty} 影响{node.impact} 紧迫{node.urgency}"
            f"{parent_note}"
        )
        if node.summary:
            lines.append(f"    摘要:{node.summary}")
        if node.user_description:
            # 用户原话必须回给模型 —— 否则它下一轮会拿摘要当作用户说过的话。
            lines.append(f"    用户原话:{node.user_description}")
    for link in links:
        lines.append(
            f"  边:{handle_of.get(link.source_reasoning_node_id, '?')}"
            f" -{link.link_type.value}-> {handle_of.get(link.target_reasoning_node_id, '?')}"
        )
    return "\n".join(lines)


# --- 校验 ------------------------------------------------------------------
def _validate_draft(
    draft: ReasoningMapDraft,
    *,
    existing: dict[str, ReasoningNode],
    handle_of_id: dict[uuid.UUID, str],
    force_roadmap: bool = False,
) -> None:
    """写入**之前**把整份草稿验一遍。任何一处不合法就整份拒绝。

    `force_roadmap=True` 时,即使已经有存量节点(旧地图),这一份草稿也**必须**是
    合法路线图 —— 用于“重新生成战略路线”。
    """
    fresh = not existing or force_roadmap
    if fresh:
        # 阶段 8:**路线优先**。新建会话 / 首次目标梳理 / 未确认战略的首轮**只能**是
        # “恰好一条顶层战略路线 + 3–5 个挂在它下面的阶段”。
        #
        # 旧的“4–8 个一级维度”形状**不再作为新一轮模型输出的成功条件**;
        # 它只允许被**读取与展示**—— 存量会话里的历史地图(`existing` 非空时
        # 根本不进这个分支)仍然读得出来、画得出来。
        #
        # 新首轮若给出散乱的一级维度,整份拒绝、**不写半成品**,下一次可安全重试。
        top_routes = [
            node for node in draft.nodes
            if node.node_type == "route" and not node.parent_handle
        ]
        stages = [node for node in draft.nodes if node.node_type == "stage"]
        top_level = [node for node in draft.nodes if not node.parent_handle]
        if len(top_routes) != 1 or len(top_level) != 1:
            raise MapValidationError(
                f"首轮必须给出**恰好一条**顶层战略路线(顶层节点只有它一条),"
                f"收到 {len(top_routes)} 条路线 / {len(top_level)} 个顶层节点;"
                "旧的散乱一级维度图不再被接受。"
            )
        if not 3 <= len(stages) <= 5:
            raise MapValidationError(
                f"首轮路线图需要 3–5 个阶段,收到 {len(stages)} 个。"
            )
        route_handle = top_routes[0].handle
        if any(node.parent_handle != route_handle for node in stages):
            raise MapValidationError("每个阶段都必须挂在推荐路线上(parent = 路线 handle)。")
    if len(existing) + len(draft.nodes) > MAX_MAP_NODES:
        raise MapValidationError(f"推理地图节点数超过上限 {MAX_MAP_NODES}。")

    known = set(existing) | {node.handle for node in draft.nodes}
    parent_of: dict[str, str | None] = {}
    for node in existing.values():
        parent_of[node.handle] = handle_of_id.get(node.parent_id) if node.parent_id else None
    for node in draft.nodes:
        if node.handle in existing:
            parent_of[node.handle] = (
                node.parent_handle if node.parent_handle is not None else parent_of.get(node.handle)
            )
        else:
            parent_of[node.handle] = node.parent_handle
    for node in draft.nodes:
        if node.parent_handle and node.parent_handle not in known:
            raise MapValidationError(
                f"节点 {node.handle} 的父节点 {node.parent_handle} 不在当前地图里。"
            )
        if node.parent_handle == node.handle:
            raise MapValidationError(f"节点 {node.handle} 不能把自己当父节点。")

    # 父链成环检测(地图很小,代价可忽略)。
    for handle in parent_of:
        seen: set[str] = set()
        cursor = parent_of.get(handle)
        while cursor:
            if cursor == handle or cursor in seen:
                raise MapValidationError(f"问题地图的父子关系成环:{handle} -> {cursor}")
            seen.add(cursor)
            cursor = parent_of.get(cursor)

    for link in draft.links:
        if link.source_handle not in known or link.target_handle not in known:
            raise MapValidationError(
                f"连线 {link.source_handle}->{link.target_handle} 的一端不在当前地图里。"
            )
    if draft.focus_handle and draft.focus_handle not in known:
        raise MapValidationError(f"焦点 {draft.focus_handle} 不在当前地图里。")


def _has_roadmap(rows: list[ReasoningNode]) -> bool:
    """这张地图里是否已经有一条**可当主路线**的 route(至少 3 个直属 stage)。

    用来区分“路线优先的新地图”与“阶段 8 之前的旧式一级维度图”。旧图仍然
    读得出来、画得出来,但它不是新体验的主路线。
    """
    for route in rows:
        if route.node_type is not ReasoningNodeType.ROUTE:
            continue
        stages = [
            row
            for row in rows
            if row.parent_id == route.id and row.node_type is ReasoningNodeType.STAGE
        ]
        if len(stages) >= 3:
            return True
    return False


def _roadmap_error(
    result: ReasoningResult,
    *,
    existing: dict[str, ReasoningNode],
    handle_of_id: dict[uuid.UUID, str],
    force_roadmap: bool,
) -> str | None:
    """这一轮的结果是不是一个可落库的路线图。不是就返回**给用户看的原因**。

    真实模型经常会“只回一句话”或“只提一个问题”—— 提示词管不住它们,所以这里
    是服务端的硬闸:不合格就返回原因,由调用方决定要不要结构化纠错重试。
    """
    if result.degraded:
        return result.reply or "模型这次没能完成探索。"
    draft = result.reasoning_map
    if draft is None:
        return "模型没有给出结构化的战略路线图(只回了文字或问题),这次没有写入任何节点。"
    try:
        _validate_draft(
            draft, existing=existing, handle_of_id=handle_of_id, force_roadmap=force_roadmap
        )
    except MapValidationError as exc:
        return str(exc)
    return None


# --- 写入 ------------------------------------------------------------------
def _apply_draft(
    db: AsyncSession,
    session: GoalReasoningSession,
    draft: ReasoningMapDraft,
    existing: dict[str, ReasoningNode],
) -> None:
    """把已校验的草稿写进库。调用方在同一个事务里提交。"""
    for item in draft.nodes:
        node = existing.get(item.handle)
        if node is None:
            node = ReasoningNode(
                session_id=session.id,
                handle=item.handle,
                title=item.title,
                node_type=ReasoningNodeType(item.node_type),
                status=ReasoningNodeStatus(item.status or ReasoningNodeStatus.UNEXPLORED.value),
                next_action=ReasoningTurnAction.ANALYZE,
                source=ReasoningSource(item.source),
                version=1,
            )
            db.add(node)
            existing[item.handle] = node
        else:
            node.version += 1
        # 用户改过的标题**永不覆盖**;其余 Agent 维护的字段可以更新。
        if not node.title_locked:
            node.title = item.title
        node.summary = item.summary
        node.node_type = ReasoningNodeType(item.node_type)
        if item.status:
            node.status = ReasoningNodeStatus(item.status)
        node.importance = item.importance
        node.uncertainty = item.uncertainty
        node.urgency = item.urgency
        node.impact = item.impact
        node.confidence = item.confidence
        node.rationale = item.rationale
        node.assumptions = list(item.assumptions)
        node.evidence = list(item.evidence)
        # 阶段 8:路线 / 阶段的三个展示字段。非路线节点为空。
        node.timeframe = item.timeframe
        node.deliverable = item.deliverable
        node.pass_criteria = item.pass_criteria
        # 阶段 11:结构化时间架构。**没有日期就不写**,不伪造。
        node.timeframe_kind = item.timeframe_kind
        node.start_week = item.start_week
        node.end_week = item.end_week
        node.start_date = date.fromisoformat(item.start_date) if item.start_date else None
        node.end_date = date.fromisoformat(item.end_date) if item.end_date else None
        # 父节点引用在 flush 之后才能解析 —— 先把 handle 暂存着。
        node._pending_parent_handle = item.parent_handle  # type: ignore[attr-defined]


async def _resolve_parents(db: AsyncSession, session_id: uuid.UUID) -> None:
    """把暂存的父 handle 解析成真实 id。**需要先 flush 拿到主键。**"""
    await db.flush()
    rows = await _load_nodes(db, session_id)
    by_handle = {row.handle: row for row in rows}
    for row in rows:
        pending = getattr(row, "_pending_parent_handle", None)
        if pending is None:
            continue
        parent = by_handle.get(pending)
        row.parent_id = parent.id if parent else None
        delattr(row, "_pending_parent_handle")


async def _apply_links(
    db: AsyncSession, session_id: uuid.UUID, draft: ReasoningMapDraft
) -> None:
    if not draft.links:
        return
    rows = await _load_nodes(db, session_id)
    by_handle = {row.handle: row for row in rows}
    existing = {
        (link.source_reasoning_node_id, link.target_reasoning_node_id, link.link_type)
        for link in await _load_links(db, session_id)
    }
    for item in draft.links:
        source = by_handle.get(item.source_handle)
        target = by_handle.get(item.target_handle)
        if source is None or target is None:
            continue
        link_type = ReasoningLinkType(item.link_type)
        key = (source.id, target.id, link_type)
        if key in existing:
            continue
        existing.add(key)
        db.add(
            ReasoningNodeLink(
                session_id=session_id,
                source_reasoning_node_id=source.id,
                target_reasoning_node_id=target.id,
                link_type=link_type,
                note=item.note,
            )
        )


async def _pick_focus(
    db: AsyncSession, session_id: uuid.UUID, draft: ReasoningMapDraft
) -> ReasoningNode | None:
    rows = await _load_nodes(db, session_id)
    if not rows:
        return None
    if draft.focus_handle:
        chosen = next((node for node in rows if node.handle == draft.focus_handle), None)
        if chosen is not None:
            return chosen
    candidates = [
        n for n in rows if n.status in (ReasoningNodeStatus.UNEXPLORED, ReasoningNodeStatus.EXPLORING)
    ]
    pool = candidates or rows
    return max(
        pool,
        key=lambda node: compute_priority(
            importance=node.importance,
            uncertainty=node.uncertainty,
            urgency=node.urgency,
            impact=node.impact,
        ),
    )


def _running_is_stale(session: GoalReasoningSession, now: datetime) -> bool:
    stamp = session.updated_at
    if stamp is None:
        return True
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=now.tzinfo)
    return now - stamp > timedelta(seconds=STALE_RUNNING_SECONDS)


async def _primary_pending_question(
    db: AsyncSession, ctx: WorkspaceContext
) -> AgentQuestion | None:
    """当前唯一一个**画布**待回答问题。

    阶段 12 起,战略 intake 的关键问题**不再落成 `agent_questions`**;老数据里可能
    留下 `conversation_intake` 行。它们必须被排除,否则一个早已被取代的旧问题会
    重新变成响应里的 `question`、让前端弹回“定位到画布”。
    """
    return await db.scalar(
        select(AgentQuestion)
        .where(
            AgentQuestion.workspace_id == ctx.id,
            AgentQuestion.status.in_(tuple(ACTIVE_QUESTION_STATUSES)),
            AgentQuestion.presentation == QuestionPresentation.CANVAS_QUESTION,
        )
        .order_by(AgentQuestion.created_at.desc())
        .limit(1)
    )


async def _response(
    db: AsyncSession,
    ctx: WorkspaceContext,
    session: GoalReasoningSession,
    *,
    message=None,
    question: AgentQuestion | None = None,
    result: ReasoningResult | None = None,
    replayed: bool = False,
    changed: bool = False,
    proposal_errors=(),
) -> AgentTurnResponse:
    if question is None:
        question = await _primary_pending_question(db, ctx)
    return AgentTurnResponse(
        reasoning=await build_view(db, ctx, session),
        message=_message_view(message) if message is not None else None,
        question=question_service.to_view(question) if question is not None else None,
        replayed=replayed,
        changed=changed,
        proposal_errors=list(proposal_errors),
        degraded=bool(result.degraded) if result else False,
        degraded_reason=(
            result.degraded_reason.value
            if result and result.degraded_reason is not None
            else None
        ),
        retryable=bool(result.retryable) if result else False,
    )


async def _build_reasoning_context(
    db: AsyncSession,
    ctx: WorkspaceContext,
    root: PlanNode,
    session: GoalReasoningSession,
    *,
    trigger_message: str,
    purpose: str = PURPOSE_ARCHITECTURE,
) -> TurnContext:
    conversation = await conversation_service.find_primary_conversation(db, ctx)
    conversation_id = conversation.id if conversation is not None else uuid.uuid4()
    turn = await turn_context.build_turn_context(
        db,
        ctx,
        conversation_id=conversation_id,
        user_message=trigger_message,
        context_node_id=root.id,
        scope_root_id=root.id,
    )
    nodes = await _load_nodes(db, session.id)
    links = await _load_links(db, session.id)
    return replace(
        turn,
        purpose=purpose,
        reasoning_section=render_map_section(nodes, links),
    )


def should_start_strategic_intake(
    session: GoalReasoningSession, existing_nodes: list[ReasoningNode]
) -> bool:
    """这个会话现在是否**应该由 AI 主动发起第一轮战略 intake**。

    它是“要不要主动开口”的**唯一判据**。前端与后端都围绕这个语义工作,但后端是
    最终裁决者:前端只是提前决定要不要发 `space_entered`,真正说了算的是这里。

    只有以下条件**全部**成立才返回 True:
    - phase 是 intake(含旧的 orientation / strategic_exploration);
    - 还没有待回答的 intake 问题(`pending_intake_message_id is None`);
    - 还没有任何 reasoning node(尤其没有 route/stage);
    - status 不是 running(交给并发锁);
    - status 不是 failed(交给用户显式重试,不偷偷无限请求)。

    “还没有一条本轮主动 intake assistant message”由
    `pending_intake_message_id` 这一个标记表达:它就是“上一轮主动问出的那条消息”。
    非空即说明已经开过口,重进必须继续等回答,不能重复提问。
    """
    return bool(
        session.phase.is_intake
        and session.pending_intake_message_id is None
        and not existing_nodes
        and session.status is not ReasoningSessionStatus.RUNNING
        and session.status is not ReasoningSessionStatus.FAILED
    )


async def run_space_entered(
    db: AsyncSession,
    ctx: WorkspaceContext,
    reasoner,
    *,
    payload: AgentTurnRequest,
    force: bool = False,
) -> AgentTurnResponse:
    """首次/重新进入空间时的**幂等**目标探索。

    - 未探索、输入版本变化、上次失败、或 `force`(显式重试)才真的跑模型;
    - 正在运行(未过期)或已完成且输入未变 → 直接返回当前地图,绝不重复建节点;
    - 模型失败/输出不合法 → 只写会话的 failed 状态,地图一行不动。
    """
    root = await root_plan_node(db, ctx)
    if root is None:
        raise InvalidInput("这个空间还没有根目标,先建一个再开始目标推理。")
    session = await get_or_create_session(db, ctx)
    assert session is not None  # root 存在时必然建得出来
    now = utcnow()

    # 规划智能体重构 V1(P1):阶段一画布与节点讨论由 `v1_service.advance` 推进。
    # `v1_stage is None` = 非 V1,继续走下面的 V0.1 / intake 逻辑。
    if session.v1_stage is not None:
        from backend.services import v1_service  # 延迟 import,避免循环

        trace = agent_trace_service.start_map_trace(
            ctx, trigger=payload.trigger, context_node_id=root.id
        )
        db.add(trace)
        await db.commit()
        return await v1_service.advance(db, ctx, root, session, trace=trace)

    # 规划智能体 V0.1:新建目标空间由 `v01_service.advance` 按状态机推进。
    # 老会话 `workflow_stage is None`,继续走下面的 intake / 架构逻辑。
    if session.workflow_stage is not None:
        from backend.services import v01_service  # 延迟 import,避免循环

        trace = agent_trace_service.start_map_trace(
            ctx, trigger=payload.trigger, context_node_id=root.id
        )
        db.add(trace)
        await db.commit()
        return await v01_service.advance(
            db, ctx, root, session, trace=trace
        )

    if session.status == ReasoningSessionStatus.RUNNING and not _running_is_stale(session, now):
        return await _response(db, ctx, session, replayed=True)

    # 阶段 12:**已经有 pending intake 问题的会话必须继续等回答。**
    #
    # intake 提问那一轮**不写 `input_version`**(它是“输入变化才重新探索”的摘要,
    # 与“正在等用户回答”不是一回事),所以下面的 `READY + input_version 未变` 这条
    # 通用重放判据抓不住它。没有这一段的话,重进一个正在等回答的空间会**再跑一次
    # strategic_intake**,把用户还没回答的那道题直接推过去 —— 这是实测过的。
    if (
        not force
        and session.phase.is_intake
        and session.pending_intake_message_id is not None
    ):
        return await _response(db, ctx, session, replayed=True)

    # ============================================================================
    # 阶段 12 修复:一个**空的、还没发出第一题的 intake 会话**不是“已完成”。
    #
    # 唯一判据是 `should_start_strategic_intake`。阶段 11/12 升级前留下的会话、
    # 或上一轮中断在第一题之前的会话,都会落在这种形状里,必须真正发起第一轮。
    #
    # **查真实 session 的 reasoning nodes**,不信前端传值:
    #   - 有 route/stage/任何节点 -> 旧战略会话,绝不能重新变回 intake;
    #   - 有 pending 问题         -> 正在等回答,已在上面 replayed;
    #   - RUNNING / FAILED        -> 交给并发锁或既有重试语义,不在这里抢跑。
    # ============================================================================
    existing_nodes = await _load_nodes(db, session.id)
    needs_initial_intake = not force and should_start_strategic_intake(
        session, existing_nodes
    )

    current_iv = await input_version(db, ctx, root)
    if (
        not force
        and not needs_initial_intake
        and session.status == ReasoningSessionStatus.READY
        and session.input_version == current_iv
    ):
        return await _response(db, ctx, session, replayed=True)
    if (
        not force
        and not needs_initial_intake
        and payload.idempotency_key
        and session.last_idempotency_key == payload.idempotency_key
    ):
        return await _response(db, ctx, session, replayed=True)

    # 标记 running 并**立即提交** —— 并发的第二次进入看得到,不会重复跑模型;
    # 轨迹行也随着这次提交可被诊断入口读到。
    trace = agent_trace_service.start_map_trace(
        ctx, trigger=payload.trigger, context_node_id=root.id
    )
    db.add(trace)
    session.status = ReasoningSessionStatus.RUNNING
    session.last_error = None
    await db.commit()

    return await _explore_and_apply(
        db, ctx, root, session, reasoner,
        trigger_message=SPACE_ENTERED_PROMPT,
        trace=trace,
        current_iv=current_iv,
        idempotency_key=payload.idempotency_key,
    )


def _known_dimensions(known) -> frozenset[str]:
    """已知的战略维度集合。intake 守卫用它跳过“已经说过”的信息。

    只看**用户亲口说过**的条件(简报里 `user_stated` 的那些),`None` 一律不算已知。
    """
    dims: set[str] = set()
    if known.weekly_available_minutes is not None:
        dims.add("weekly_available_minutes")
    if known.deadline:
        dims.add("deadline")
    if known.current_level:
        dims.add("current_level")
    return frozenset(dims)


def _first_turn_may_skip_intake(known) -> bool:
    """首个 intake 回合是否已经掌握足够的战略信息,可以直接进入时间架构。

    只有当用户**初始目标**里已经明确给出:可验证成果、时间窗口、当前基础、
    稳定可投入的时间、以及关键约束/取舍时,才允许首轮跳过提问。普通简短目标
    (“学习 Python”“准备考研”“做一个副业项目”)缺其中任何一项,都必须先问一个
    决定整体路线的核心问题。
    """
    return bool(
        known is not None
        and known.goal
        and known.deadline
        and known.current_level
        and known.weekly_available_minutes is not None
        and (known.success_criteria or known.constraints)
    )


def _intake_decision_error(
    decision, *, known_dimensions, first_turn: bool = False, known=None
) -> str | None:
    """服务端对一条 intake 决策的**硬闸**。返回可读原因或 `None`。

    规则(阶段 12 §4.1):
    - 必须有决策,动作必须在闭集里;
    - `ask` 必须带一个问题、且说明 `decisionScope`(会改变整体战略的哪一件大事);
    - **首个 intake 回合默认不能直接 `ready_for_architecture`** —— 除非初始目标
      已经明确给出成果/时间/基础/稳定投入/取舍,否则必须先问一个关键问题;
    - 问题不得重复用户已经说过的信息、不得问执行细节。
    """
    if decision is None:
        return "intake 回合没有给出 intakeDecision"
    if decision.action == "ready_for_architecture":
        if first_turn and not _first_turn_may_skip_intake(known):
            return "首个 intake 回合必须先给出整体判断并问一个决定战略的关键问题"
        return None
    if decision.action != "ask":
        return "intakeDecision.action 不合法"
    question = (decision.question or "").strip()
    if not question:
        return "intakeDecision.action=ask 却没有给问题"
    if decision.decision_scope not in INTAKE_SCOPES:
        return "问题没有说明会改变整体战略的哪一件大事(decisionScope)"
    return question_service.intake_question_conflict(
        question, known_dimensions=known_dimensions
    )


def _compose_intake_reply(reply: str, question: str) -> str:
    """把“当前判断 + 一个关键问题”合成一条助手消息。

    对话式 intake 里问题必须**留在消息正文**里(而不是只挂在一条活动问题上):
    问题回答后活动问题会被归档,但那句话仍然是这段对话的一部分。
    """
    clean = (reply or "").strip()
    asked = (question or "").strip()
    if not asked or asked in clean:
        return clean
    if not clean:
        return asked
    return f"{clean}\n\n{asked}"


async def _persist_failure_notice(
    db,
    ctx,
    *,
    reply: str,
    source: ModelSource = ModelSource.DIRECT_LLM,
    degraded_reason: DegradedReason = DegradedReason.MODEL_OUTPUT_INVALID,
):
    """把一条**可重试的失败提示**落成助手消息。

    它是一条真正的助手消息(不是编造的模型回复):`degraded=True`,
    界面上会如实显示为“这次没有完成”,而不是普通回答。

    `source` / `degraded_reason` 由调用方给出 —— 这是正确归因的关键:
    - **模型真实不可达 / 超时 / 无 Key** -> `UNAVAILABLE` + 真实原因
      (界面才会显示“模型不可用”);
    - **模型可用、但输出被战略守卫拒绝** -> `DIRECT_LLM` + `MODEL_OUTPUT_INVALID`
      (绝不能伪装成模型不可用)。
    """
    conversation = await conversation_service.get_or_create_primary_conversation(db, ctx)
    synthetic = ReasoningResult(
        reply=reply,
        source=source,
        degraded=True,
        degraded_reason=degraded_reason,
        retryable=True,
    )
    return await conversation_service.append_reply(
        db, ctx, conversation=conversation, result=synthetic
    )


async def _run_intake(
    db: AsyncSession,
    ctx: WorkspaceContext,
    root: PlanNode,
    session: GoalReasoningSession,
    reasoner,
    *,
    trace: ReasoningState,
    trigger_message: str,
) -> AgentTurnResponse | str:
    """战略 intake 模式的一轮。**它绝不写 reasoning node / agent_question / plan node。**

    返回 `AgentTurnResponse` = 已经问出一个问题(或失败终态);返回 `str` = intake 结束,
    调用方应转到时间架构模式(返回的是架构触发文本)。

    不合格输出(缺决策、动作不合法、问执行细节、重复已知信息)的处理:
    - **未接近上限** -> 本轮预算内用结构化纠正**再试一次**;两次都不合格就拒绝该
      输出、写终态 `failed`,不写半张地图、不写半个问题卡、不假装成功;
    - **已经问过 4 个(接近上限)** -> 不再纠正、不展示不合格问题、不判失败,
      直接收束到时间架构;
    - **已经到 5 问上限** -> 连模型都不再问,直接收束。
    """
    # 到顶:直接进入时间架构。**不再接受或校验新的 intake 问题。**
    if session.intake_questions_asked >= MAX_INTAKE_QUESTIONS:
        session.pending_intake_message_id = None
        session.pending_intake_decision = None
        session.status = ReasoningSessionStatus.RUNNING
        await db.commit()
        return ARCHITECTURE_TRIGGER

    result: ReasoningResult | None = None
    known_dimensions: frozenset[str] = frozenset()
    prompt = trigger_message
    for attempt in range(2):
        turn = await _build_reasoning_context(
            db, ctx, root, session, trigger_message=prompt, purpose=PURPOSE_INTAKE
        )
        known_dimensions = _known_dimensions(turn.known)
        agent_trace_service.mark_step(trace, AgentTraceStep.WAITING_MODEL)
        await db.commit()
        result = await reasoner.reason(turn)
        agent_trace_service.mark_step(trace, AgentTraceStep.VALIDATING_OUTPUT)
        if result.degraded:
            # **真实的**模型不可达 / 超时 / 无 Key —— 只有这里才允许 `unavailable`。
            session.status = ReasoningSessionStatus.FAILED
            session.last_error = "模型这次没有给出可用的战略判断。"
            agent_trace_service.mark_terminal(
                trace,
                degraded=True,
                degraded_reason=result.degraded_reason,
                stopped_reason="failed",
            )
            message = await _persist_failure_notice(
                db,
                ctx,
                reply="模型这次没有响应,可以重试。",
                source=ModelSource.UNAVAILABLE,
                degraded_reason=result.degraded_reason or DegradedReason.MODEL_UNAVAILABLE,
            )
            await db.commit()
            return await _response(db, ctx, session, message=message, result=result)
        error = _intake_decision_error(
            result.intake_decision,
            known_dimensions=known_dimensions,
            first_turn=session.intake_questions_asked == 0,
            known=turn.known,
        )
        if error is None:
            break
        # 接近上限(已经问过 4 个):别再纠正、不把不合格问题展示给用户、也不判失败。
        # 此前几轮已经收集到足够信息,直接收束到时间架构。
        if session.intake_questions_asked >= MAX_INTAKE_QUESTIONS - 1:
            agent_trace_service.mark_retry(trace)
            session.pending_intake_message_id = None
            session.pending_intake_decision = None
            session.status = ReasoningSessionStatus.RUNNING
            await db.commit()
            return ARCHITECTURE_TRIGGER
        if attempt == 0:
            agent_trace_service.mark_retry(trace)
            prompt = f"{trigger_message}\n\n{INTAKE_CORRECTION_MESSAGE}"
            continue
        # 两次都不合格(未接近上限):拒绝该输出,不写半张地图、不写半个问题卡。
        # **归因是“模型输出不合格”,不是“模型不可用”。**
        session.status = ReasoningSessionStatus.FAILED
        session.last_error = error
        agent_trace_service.mark_terminal(
            trace,
            degraded=False,
            degraded_reason=None,
            stopped_reason="failed",
            code="INTAKE_INVALID",
        )
        message = await _persist_failure_notice(
            db,
            ctx,
            reply="这次的问题偏离了战略澄清范围,尚未生成时间架构。可以重试。",
            source=ModelSource.DIRECT_LLM,
            degraded_reason=DegradedReason.MODEL_OUTPUT_INVALID,
        )
        await db.commit()
        return await _response(db, ctx, session, message=message, result=result)

    assert result is not None and result.intake_decision is not None
    # 用户答案里的可验证事实(例如“每周 8 小时”)在这一轮落进简报,
    # 下一问才不会重复问同一件事。
    await brief_service.apply_claims(db, ctx.id, result.brief_claims)
    decision = result.intake_decision
    cap_reached = session.intake_questions_asked >= MAX_INTAKE_QUESTIONS
    if decision.action == "ask" and not cap_reached:
        conversation = await conversation_service.get_or_create_primary_conversation(db, ctx)
        message = await conversation_service.append_reply(
            db, ctx, conversation=conversation, result=result
        )
        message.content = _compose_intake_reply(message.content, decision.question)
        # 待回答问题**只存在会话状态里**,不创建 agent_question / 画布问题节点。
        session.pending_intake_message_id = message.id
        session.pending_intake_decision = {
            "question": decision.question,
            "decisionScope": decision.decision_scope,
            "whyThisMatters": decision.why_this_matters,
            "quickReplies": list(decision.quick_replies),
        }
        session.intake_questions_asked += 1
        session.phase = ReasoningSessionPhase.INTAKE
        session.status = ReasoningSessionStatus.READY
        session.last_evaluated_at = utcnow()
        agent_trace_service.mark_terminal(
            trace, degraded=False, degraded_reason=None, stopped_reason="ready_to_propose"
        )
        await db.commit()
        return await _response(
            db, ctx, session, message=message, result=result, changed=False
        )
    # ready_for_architecture 或到 5 问上限:清掉待回答状态,交给时间架构模式。
    session.pending_intake_message_id = None
    session.pending_intake_decision = None
    session.status = ReasoningSessionStatus.RUNNING
    await db.commit()
    return ARCHITECTURE_TRIGGER




async def _explore_and_apply(
    db: AsyncSession,
    ctx: WorkspaceContext,
    root: PlanNode,
    session: GoalReasoningSession,
    reasoner,
    *,
    trigger_message: str,
    trace: ReasoningState,
    current_iv: str | None = None,
    idempotency_key: str | None = None,
) -> AgentTurnResponse:
    """跑一轮路线探索并落库。**真实模型的服务端硬闸在这里。**

    1. 地图里还没有合法路线(新建会话,或阶段 8 前留下的旧式一级维度图)→ 这一轮
       的输出**必须**是路线图;
    2. 模型只回文字 / 只提问题 / 给旧形状 → 用结构化纠错提示**再试一次**;
    3. 再失败就写 `failed` + 可读原因,**不写问题节点、不降级成“先问一个碎问题”**。

    只有校验通过才真的动地图,调用方在同一个事务里提交。

    `trace` 是调用方建好并已 `add` 的轨迹行(见 `agent_trace_service.start_map_trace`)。
    本函数在每个**真实执行边界**推进它:发模型请求前 `waiting_model`、返回后
    `validating_output`、结构不合法时 `retrying`、校验通过后 `persisting`、终态收口。
    """
    existing_rows = await _load_nodes(db, session.id)
    existing = {row.handle: row for row in existing_rows}
    handle_of_id = {row.id: row.handle for row in existing_rows}
    force_roadmap = not _has_roadmap(existing_rows)

    # ============================================================================
    # 阶段 12:战略澄清 intake 是**另一种回合契约**,不是架构回合的例外分支。
    # 它在 intake 模式下只问一个问题、绝不写任何节点/问题实体;只有它结束后
    # 才进入下面的时间架构模式。
    # ============================================================================
    architecture_trigger = trigger_message
    # **只有空地图才走 intake。** 旧会话(顶层已有 dimension 节点)不能被当成一次
    # 全新 intake —— 那是“旧地图污染新体验”的入口。旧地图直接进时间架构重生成。
    if session.phase.is_intake and not existing_rows:
        intake = await _run_intake(
            db, ctx, root, session, reasoner, trace=trace, trigger_message=trigger_message
        )
        if isinstance(intake, AgentTurnResponse):
            return intake
        architecture_trigger = intake

    turn = await _build_reasoning_context(
        db, ctx, root, session, trigger_message=architecture_trigger,
        purpose=PURPOSE_ARCHITECTURE,
    )
    # **发模型请求前就写 waiting_model 并提交。** 模型卡住时诊断入口能读到
    # “正在等模型”和最后一次心跳,而不是一片空白。
    agent_trace_service.mark_step(trace, AgentTraceStep.WAITING_MODEL)
    await db.commit()
    result = await reasoner.reason(turn)
    agent_trace_service.mark_step(trace, AgentTraceStep.VALIDATING_OUTPUT)

    error = _roadmap_error(
        result, existing=existing, handle_of_id=handle_of_id, force_roadmap=force_roadmap
    )
    # 模型**可用**但输出不合格 -> 结构化纠错,再试一次(有限预算内一次)。
    if error is not None and not result.degraded:
        # 第一次模型输出不合法这一事实**必须留在轨迹里**;`retrying` 只在这里出现。
        agent_trace_service.mark_retry(trace)
        agent_trace_service.mark_step(trace, AgentTraceStep.WAITING_MODEL)
        await db.commit()
        retry_turn = await _build_reasoning_context(
            db,
            ctx,
            root,
            session,
            trigger_message=f"{architecture_trigger}\n\n{ROADMAP_CORRECTION_MESSAGE}",
            purpose=PURPOSE_ARCHITECTURE,
        )
        retry = await reasoner.reason(retry_turn)
        result = retry
        agent_trace_service.mark_step(trace, AgentTraceStep.VALIDATING_OUTPUT)
        error = _roadmap_error(
            retry, existing=existing, handle_of_id=handle_of_id, force_roadmap=force_roadmap
        )
    if error is not None:
        session.status = ReasoningSessionStatus.FAILED
        session.last_error = error
        if result.degraded:
            agent_trace_service.mark_terminal(
                trace,
                degraded=True,
                degraded_reason=result.degraded_reason,
                stopped_reason="failed",
            )
        else:
            # 模型可用,但结构不合法 —— 用**自己的**闭集码,不是模型的自由文本。
            agent_trace_service.mark_terminal(
                trace,
                degraded=False,
                degraded_reason=None,
                stopped_reason="failed",
                code="ROADMAP_INVALID",
            )
        await db.commit()
        return await _response(db, ctx, session, result=result)

    draft = result.reasoning_map
    assert draft is not None  # `_roadmap_error` 已经确认过

    # ---- 全部校验通过,才动地图 ----
    agent_trace_service.mark_step(trace, AgentTraceStep.PERSISTING)
    _apply_draft(db, session, draft, existing)
    await _resolve_parents(db, session.id)
    await _apply_links(db, session.id, draft)
    focus = await _pick_focus(db, session.id, draft)
    if focus is not None:
        session.focus_reasoning_node_id = focus.id
        session.focus_reason = draft.focus_reason or "它当前最影响下一步该怎么做。"
    stage_items = [node for node in draft.nodes if node.node_type == "stage"]
    if stage_items:
        # 阶段 11:产出了带阶段的战略时间架构。日期没校准也**继续** —— 用相对周,
        # 标“日期待校准”,不阻塞、不伪造日历日期。
        session.phase = ReasoningSessionPhase.TEMPORAL_ARCHITECTURE_DRAFT
        session.dates_calibrated = all(
            node.timeframe_kind == "dated" and node.start_date and node.end_date
            for node in stage_items
        )
    elif draft.phase:
        session.phase = ReasoningSessionPhase(draft.phase)
    elif session.phase in (
        ReasoningSessionPhase.ORIENTATION,
        ReasoningSessionPhase.INTAKE,
    ):
        session.phase = ReasoningSessionPhase.TEMPORAL_ARCHITECTURE_DRAFT
    if draft.turn_action:
        session.turn_action = ReasoningTurnAction(draft.turn_action)

    # 助手解释:同一事务落库。
    conversation = await conversation_service.get_or_create_primary_conversation(db, ctx)
    message = await conversation_service.append_reply(
        db, ctx, conversation=conversation, result=result
    )

    # 问题:挂到当前焦点节点上;战略阶段由服务端拦住排期类问题。
    created_questions = await question_service.create_from_drafts(
        db,
        ctx,
        result.questions[:MAX_QUESTIONS_PER_TURN],
        source_message_id=message.id,
        source_node_id=root.id,
        reasoning_node_id=focus.id if focus is not None else None,
        strategy_phase=session.phase.is_strategic,
        require_judgment=session.phase.is_strategic,
        max_questions=MAX_STRATEGIC_QUESTIONS if session.phase.is_strategic else MAX_QUESTIONS_PER_TURN,
    )

    session.status = ReasoningSessionStatus.READY
    if current_iv is not None:
        session.input_version = current_iv
    if idempotency_key:
        session.last_idempotency_key = idempotency_key
    session.map_version += 1
    session.explored_at = utcnow()
    session.last_evaluated_at = session.explored_at
    agent_trace_service.mark_terminal(
        trace,
        degraded=False,
        degraded_reason=None,
        stopped_reason="ready_to_propose",
    )
    await db.commit()

    return await _response(
        db,
        ctx,
        session,
        message=message,
        question=created_questions[0] if created_questions else None,
        result=result,
        changed=True,
    )


async def regenerate_roadmap(
    db: AsyncSession,
    ctx: WorkspaceContext,
    reasoner,
    *,
    payload: AgentTurnRequest,
) -> AgentTurnResponse:
    """“重新生成战略路线”按钮。

    针对**阶段 8 之前的旧地图**(顶层是一堆一级 dimension)。旧节点**不删** —— 它们
    会落到前端的“历史思考”折叠层;新路线覆盖主画布。不写任何 `PlanNode`。
    """
    root = await root_plan_node(db, ctx)
    if root is None:
        raise InvalidInput("这个空间还没有根目标,先建一个再开始目标推理。")
    session = await get_or_create_session(db, ctx)
    assert session is not None
    trace = agent_trace_service.start_map_trace(
        ctx, trigger=payload.trigger, context_node_id=root.id
    )
    db.add(trace)
    session.status = ReasoningSessionStatus.RUNNING
    session.last_error = None
    await db.commit()
    return await _explore_and_apply(
        db, ctx, root, session, reasoner,
        trigger_message=REGENERATE_ROADMAP_MESSAGE,
        trace=trace,
        current_iv=await input_version(db, ctx, root),
        idempotency_key=payload.idempotency_key,
    )


async def _advance_after_answer(
    db: AsyncSession,
    ctx: WorkspaceContext,
    session: GoalReasoningSession,
    payload: AgentTurnRequest,
) -> str:
    """回答之后**确定性**地推进一步:标记节点已澄清、选新焦点。

    返回一句给模型看的触发说明(含用户答案原文)。这一步不依赖模型 —— 即使模型
    这一轮失败,地图上的状态与焦点也已经真实推进。
    """
    node: ReasoningNode | None = None
    if payload.reasoning_handle:
        node = await db.scalar(
            select(ReasoningNode).where(
                ReasoningNode.session_id == session.id,
                ReasoningNode.handle == payload.reasoning_handle,
            )
        )
    if node is None:
        question = await db.scalar(
            select(AgentQuestion)
            .where(
                AgentQuestion.workspace_id == ctx.id,
                AgentQuestion.reasoning_node_id.is_not(None),
            )
            .order_by(AgentQuestion.created_at.desc())
            .limit(1)
        )
        if question is not None and question.reasoning_node_id is not None:
            node = await db.scalar(
                select(ReasoningNode).where(ReasoningNode.id == question.reasoning_node_id)
            )
    answer_text = (payload.message or "").strip()[:500]
    if node is not None:
        node.status = ReasoningNodeStatus.RESOLVED
        node.version += 1
        if answer_text:
            # 用户答案作为**依据**记录下来,但不假装成用户的原文。
            evidence = list(node.evidence or [])
            evidence.append(f"用户回答:{answer_text}")
            node.evidence = evidence[-12:]
    focus = await _pick_focus(db, session.id, ReasoningMapDraft())
    if focus is not None:
        session.focus_reasoning_node_id = focus.id
        session.focus_reason = "回答已记录,它成为下一个最值得处理的维度。"
    suffix = f"用户回答:{answer_text}" if answer_text else "(没有附带文本答案)"
    return f"用户回答了上一轮的问题。{suffix}。请据此更新相关节点的状态与摘要,并选择新焦点。"


async def _run_incremental(
    db: AsyncSession,
    ctx: WorkspaceContext,
    reasoner,
    *,
    payload: AgentTurnRequest,
    trigger_message: str,
) -> AgentTurnResponse:
    """回答/选中/讨论后的增量重评。**地图只增量更新,不重头建。**"""
    root = await root_plan_node(db, ctx)
    session = await get_session(db, ctx)
    if root is None or session is None:
        raise InvalidInput("这个空间还没有开始目标推理,请先进入空间触发一次探索。")
    now = utcnow()
    if session.status == ReasoningSessionStatus.RUNNING and not _running_is_stale(session, now):
        return await _response(db, ctx, session, replayed=True)

    if payload.trigger == "question_answered":
        trigger_message = await _advance_after_answer(db, ctx, session, payload)

    # 轨迹:这一轮的真实执行从这里开始。触发来源就是显式的 `payload.trigger`。
    trace = agent_trace_service.start_map_trace(
        ctx, trigger=payload.trigger, context_node_id=root.id
    )
    db.add(trace)

    # **旧地图优先补路线。** 阶段 8 前留下的地图顶层是一堆一级 dimension;用户
    # 下一条消息(包括回答问题)不应该继续把它们展开,而应该先把宏观路线补上。
    existing_rows = await _load_nodes(db, session.id)
    if session.phase.is_intake:
        # 阶段 11:回答之后**继续 intake** —— 可能问下一个关键问题,或直接给出时间架构。
        session.status = ReasoningSessionStatus.RUNNING
        session.last_error = None
        await db.commit()
        return await _explore_and_apply(
            db, ctx, root, session, reasoner,
            trigger_message=trigger_message,
            trace=trace,
        )
    if session.phase.is_strategic and not _has_roadmap(existing_rows):
        session.status = ReasoningSessionStatus.RUNNING
        session.last_error = None
        await db.commit()
        return await _explore_and_apply(
            db, ctx, root, session, reasoner,
            trigger_message=f"{trigger_message}\n\n{ROADMAP_CORRECTION_MESSAGE}",
            trace=trace,
        )

    session.status = ReasoningSessionStatus.RUNNING
    await db.commit()

    turn = await _build_reasoning_context(
        db, ctx, root, session, trigger_message=trigger_message
    )
    # 发模型请求前写 waiting_model 并提交:等待期间诊断入口可读。
    agent_trace_service.mark_step(trace, AgentTraceStep.WAITING_MODEL)
    await db.commit()
    result = await reasoner.reason(turn)
    agent_trace_service.mark_step(trace, AgentTraceStep.VALIDATING_OUTPUT)

    changed = False
    if not result.degraded and result.reasoning_map is not None:
        existing_rows = await _load_nodes(db, session.id)
        existing = {row.handle: row for row in existing_rows}
        handle_of_id = {row.id: row.handle for row in existing_rows}
        try:
            _validate_draft(result.reasoning_map, existing=existing, handle_of_id=handle_of_id)
        except MapValidationError as exc:
            session.status = ReasoningSessionStatus.READY
            session.last_error = str(exc)
            agent_trace_service.mark_terminal(
                trace,
                degraded=False,
                degraded_reason=None,
                stopped_reason="failed",
                code="ROADMAP_INVALID",
            )
            await db.commit()
            return await _response(db, ctx, session, result=result)
        agent_trace_service.mark_step(trace, AgentTraceStep.PERSISTING)
        _apply_draft(db, session, result.reasoning_map, existing)
        await _resolve_parents(db, session.id)
        await _apply_links(db, session.id, result.reasoning_map)
        focus = await _pick_focus(db, session.id, result.reasoning_map)
        if focus is not None:
            session.focus_reasoning_node_id = focus.id
            if result.reasoning_map.focus_reason:
                session.focus_reason = result.reasoning_map.focus_reason
        if result.reasoning_map.phase:
            session.phase = ReasoningSessionPhase(result.reasoning_map.phase)
        if result.reasoning_map.turn_action:
            session.turn_action = ReasoningTurnAction(result.reasoning_map.turn_action)
        session.map_version += 1
        changed = True

    # 助手说明:增量轮次也说话,但每一轮只讲变化与焦点。
    conversation = await conversation_service.get_or_create_primary_conversation(db, ctx)
    message = await conversation_service.append_reply(
        db, ctx, conversation=conversation, result=result
    )
    focus_node = None
    if session.focus_reasoning_node_id is not None:
        focus_node = await db.scalar(
            select(ReasoningNode).where(ReasoningNode.id == session.focus_reasoning_node_id)
        )
    strategy_phase = session.phase.is_strategic
    created_questions = await question_service.create_from_drafts(
        db,
        ctx,
        result.questions[:MAX_QUESTIONS_PER_TURN],
        source_message_id=message.id,
        source_node_id=root.id,
        reasoning_node_id=focus_node.id if focus_node is not None else None,
        strategy_phase=strategy_phase,
        require_judgment=strategy_phase,
        max_questions=MAX_STRATEGIC_QUESTIONS if strategy_phase else MAX_QUESTIONS_PER_TURN,
    )
    session.status = ReasoningSessionStatus.READY
    session.last_evaluated_at = now
    if result.degraded:
        agent_trace_service.mark_terminal(
            trace,
            degraded=True,
            degraded_reason=result.degraded_reason,
            stopped_reason="failed",
        )
    else:
        agent_trace_service.mark_terminal(
            trace,
            degraded=False,
            degraded_reason=None,
            stopped_reason="ready_to_propose",
        )
    await db.commit()
    return await _response(
        db,
        ctx,
        session,
        message=message,
        question=created_questions[0] if created_questions else None,
        result=result,
        changed=changed,
    )


async def _link_strategy_nodes(
    db: AsyncSession, ctx: WorkspaceContext, session: GoalReasoningSession
) -> None:
    """策略提案被确认后,把路线节点关联到真实的战略计划节点。"""
    strategy_nodes = list(
        (
            await db.execute(
                select(PlanNode).where(
                    PlanNode.workspace_id == ctx.id,
                    PlanNode.planning_level == "strategy",
                    PlanNode.deleted_at.is_(None),
                )
            )
        ).scalars()
    )
    if not strategy_nodes:
        return
    for route in await _load_nodes(db, session.id):
        if route.node_type is not ReasoningNodeType.ROUTE or route.linked_plan_node_id:
            continue
        match = next((node for node in strategy_nodes if node.title == route.title), None)
        if match is not None:
            route.linked_plan_node_id = match.id


async def _confirm_strategy(
    db: AsyncSession, ctx: WorkspaceContext, *, payload: AgentTurnRequest
) -> AgentTurnResponse:
    """战略确认:产出待确认提案;提案被确认后再回写关联并进入执行规划。"""
    root = await root_plan_node(db, ctx)
    session = await get_session(db, ctx)
    if root is None or session is None:
        raise InvalidInput("这个空间还没有开始目标推理。")

    # 战略确认本身**不调模型**:它把已选中的路线收敛成一份待确认提案。所以它的轨迹
    # 从 queued -> resolving_context 直接进入校验/写入,不会出现 waiting_model ——
    # 那不是遗漏,是这一轮真的没有模型等待。
    trace = agent_trace_service.start_map_trace(
        ctx, trigger=payload.trigger, context_node_id=root.id
    )
    db.add(trace)

    # 已经有一份战略提案且用户确认过了 -> 回写关联,进入执行规划阶段。
    if session.strategy_proposal_id is not None:
        proposal = await db.scalar(
            select(Proposal).where(Proposal.id == session.strategy_proposal_id)
        )
        if proposal is not None and proposal.status is ProposalStatus.APPLIED:
            await _link_strategy_nodes(db, ctx, session)
            session.phase = ReasoningSessionPhase.STRATEGY_CONFIRMED
            session.turn_action = ReasoningTurnAction.CONFIRM
            session.last_evaluated_at = utcnow()
            agent_trace_service.mark_step(trace, AgentTraceStep.PERSISTING)
            agent_trace_service.mark_terminal(
                trace, degraded=False, degraded_reason=None, stopped_reason="ready_to_propose"
            )
            await db.commit()
            return await _response(db, ctx, session, changed=True)

    # 选一条路线(用户点的,或优先级最高的一条 route 节点)。
    route: ReasoningNode | None = None
    if payload.reasoning_handle:
        route = await db.scalar(
            select(ReasoningNode).where(
                ReasoningNode.session_id == session.id,
                ReasoningNode.handle == payload.reasoning_handle,
            )
        )
    if route is None or route.node_type is not ReasoningNodeType.ROUTE:
        routes = [
            node for node in await _load_nodes(db, session.id)
            if node.node_type is ReasoningNodeType.ROUTE
        ]
        route = routes[0] if routes else None
    if route is None:
        raise InvalidInput("问题地图里还没有可确认的战略路线。")

    conversation = await conversation_service.get_or_create_primary_conversation(db, ctx)
    turn = await turn_context.build_turn_context(
        db,
        ctx,
        conversation_id=conversation.id,
        user_message="确认战略",
        scope_root_id=root.id,
    )
    root_handle = next(
        (handle for handle, node_id in turn.node_handles if node_id == str(root.id)), None
    )
    if root_handle is None:  # pragma: no cover - 根目标一定在记号表里
        raise InvalidInput("找不到根目标的记号。")
    action = {
        "op": "create_node",
        "localId": "n9",
        "parentRef": root_handle,
        "title": route.title,
        "nodeType": "goal",
        "planningLevel": "strategy",
        "description": route.summary or route.rationale or "",
    }
    agent_trace_service.mark_step(trace, AgentTraceStep.VALIDATING_OUTPUT)
    outcome = await proposal_service.build_from_actions(
        db,
        ctx,
        conversation_id=conversation.id,
        actions=(action,),
        handles=turn.node_handles,
        reasoning="由目标推理地图收敛得到的战略草案。",
        assistant_message=None,
        trigger_type=RevisionTrigger.MANUAL_REPLAN,
        writable_handles=turn.writable_handles,
    )
    if outcome.proposal is not None:
        session.strategy_proposal_id = outcome.proposal.id
    session.phase = ReasoningSessionPhase.ROADMAP_REVIEW
    session.turn_action = ReasoningTurnAction.CONFIRM
    session.last_evaluated_at = utcnow()
    if outcome.proposal is None and outcome.errors:
        # 提案没通过校验 —— 本轮没有战略可确认。用**自己的**闭集码,不写模型原文。
        agent_trace_service.mark_terminal(
            trace,
            degraded=False,
            degraded_reason=None,
            stopped_reason="failed",
            code="PROPOSAL_VALIDATION_FAILED",
        )
    else:
        agent_trace_service.mark_terminal(
            trace, degraded=False, degraded_reason=None, stopped_reason="ready_to_propose"
        )
    await db.commit()
    return await _response(db, ctx, session, changed=True, proposal_errors=outcome.errors)


async def is_awaiting_intake_answer(db: AsyncSession, ctx: WorkspaceContext) -> bool:
    """这个空间是不是正在等用户回答一个战略 intake 问题。

    判据只有会话状态一处:`phase.is_intake` 且 `pending_intake_message_id` 非空。
    **不看 `agent_questions`** —— intake 早就不落问题实体了。
    """
    session = await get_session(db, ctx)
    return bool(
        session is not None
        and session.phase.is_intake
        and session.pending_intake_message_id is not None
    )


async def answer_intake_in_conversation(
    db: AsyncSession,
    ctx: WorkspaceContext,
    reasoner,
    *,
    content: str,
    client_message_id: str | None,
    context_node_id: uuid.UUID | None,
):
    """把一条用户消息当作 **战略 intake 的回答**。

    它由 `conversation_service.submit_turn` 在“正在等 intake 回答”时委托进来。
    用户消息先落库(与普通对话同一条纪律),然后跑一轮 intake 模式:更新简报、
    只问下一个关键问题(或直接转入时间架构)。**不创建任何问题实体。**

    返回 `conversation_service.TurnOutcome`,好让发消息那条路由的响应形状不变。
    """
    root = await root_plan_node(db, ctx)
    session = await get_session(db, ctx)
    if root is None or session is None:
        raise InvalidInput("这个空间还没有开始目标推理。")

    conversation = await conversation_service.get_or_create_primary_conversation(db, ctx)
    user_message = await conversation_service.record_user_message(
        db,
        ctx,
        conversation=conversation,
        text=content,
        client_message_id=client_message_id,
        context_node_id=context_node_id,
    )
    # 幂等:同一条用户消息已经处理过(已经有后续助手回复)就原样返回,绝不重复问一题。
    existing_reply = await conversation_service.find_reply_after(
        db, conversation.id, user_message.seq
    )
    if existing_reply is not None:
        brief = await brief_service.load_brief(db, ctx.id)
        return conversation_service.turn_outcome_for_reply(
            user_message=user_message,
            assistant_message=existing_reply,
            brief=brief,
        )
    trace = agent_trace_service.start_map_trace(
        ctx, trigger="user_message", context_node_id=root.id
    )
    db.add(trace)
    session.status = ReasoningSessionStatus.RUNNING
    session.last_error = None
    # **已经拿到用户回答、开始处理**:旧 pending 在这一刻(与 RUNNING 同一个事务)
    # 清掉。此后无论成功问下一题、生成架构、被守卫拒绝还是失败,都不会留下
    # “failed 但仍挂着旧 pending” 的矛盾状态。
    session.pending_intake_message_id = None
    session.pending_intake_decision = None
    await db.commit()

    response = await _explore_and_apply(
        db,
        ctx,
        root,
        session,
        reasoner,
        trigger_message=content,
        trace=trace,
    )
    assistant = None
    if response.message is not None:
        assistant = await db.get(Message, response.message.id)
    if assistant is None:
        # 时间架构最终失败时也必须给用户一条可重试的提示,而不是一个空回合。
        # **归因如实**:真实不可达才叫模型不可用;其余是输出不合格。
        if response.degraded:
            assistant = await _persist_failure_notice(
                db,
                ctx,
                reply="这次没有生成可用的时间架构,可以重试。",
                source=ModelSource.UNAVAILABLE,
                degraded_reason=DegradedReason.MODEL_UNAVAILABLE,
            )
        else:
            assistant = await _persist_failure_notice(
                db,
                ctx,
                reply="这次没有生成可用的时间架构,可以重试。",
                source=ModelSource.DIRECT_LLM,
                degraded_reason=DegradedReason.MODEL_OUTPUT_INVALID,
            )
        await db.commit()
    brief = await brief_service.load_brief(db, ctx.id)
    return conversation_service.turn_outcome_for_reply(
        user_message=user_message,
        assistant_message=assistant,
        brief=brief,
    )


async def run_turn(
    db: AsyncSession,
    ctx: WorkspaceContext,
    reasoner,
    *,
    payload: AgentTurnRequest,
) -> AgentTurnResponse:
    """显式 Agent turn 的总入口。"""
    if payload.trigger in ("space_entered", "retry"):
        return await run_space_entered(
            db, ctx, reasoner, payload=payload, force=payload.trigger == "retry"
        )
    # 规划智能体重构 V1:阶段一不使用 reasoning-map 的 Agent turn(节点讨论走普通
    # 对话面板)。返回当前状态,不让旧的问题地图被重新生成。老空间 / V0.1 不受影响。
    session = await get_session(db, ctx)
    if session is not None and session.v1_stage is not None:
        return await _response(db, ctx, session, changed=False)
    if payload.trigger == "regenerate_roadmap":
        return await regenerate_roadmap(db, ctx, reasoner, payload=payload)
    if payload.trigger == "strategy_confirmation":
        return await _confirm_strategy(db, ctx, payload=payload)

    trigger_message = {
        "node_selected": f"用户点了地图节点 {payload.reasoning_handle or '?'}。请重新评估它,必要时更新状态并选新的焦点。",
        "user_message": payload.message or "用户发来一句话,请据此更新问题地图。",
        "progress_update": "用户报告了执行进展,请按影响范围局部重评地图。",
        "question_answered": "用户回答了上一轮的问题。请据此更新相关节点的状态与摘要,并选择新焦点。",
    }.get(payload.trigger, payload.message or "请更新问题地图。")
    return await _run_incremental(
        db, ctx, reasoner, payload=payload, trigger_message=trigger_message
    )


async def update_node(
    db: AsyncSession,
    ctx: WorkspaceContext,
    node_id: uuid.UUID,
    *,
    title: str | None = None,
    user_description: str | None = None,
    status: str | None = None,
) -> GoalReasoningView:
    """用户编辑一个地图节点的标题 / 原文。**只碰这两列**,不碰计划。

    标题被用户改过就 `title_locked=True` —— 此后 Agent 的任何一轮都不再覆盖它
    (见 `_apply_draft`)。这是“用户编辑不被 Agent 静默覆盖”的落点。
    """
    session = await get_session(db, ctx)
    if session is None:
        raise InvalidInput("这个空间还没有开始目标推理。")
    node = await db.scalar(
        select(ReasoningNode).where(
            ReasoningNode.id == node_id, ReasoningNode.session_id == session.id
        )
    )
    if node is None:
        raise NodeNotFound("没有找到这个推理节点。")
    if title is not None:
        node.title = title.strip()[:200]
        node.title_locked = True
    if user_description is not None:
        node.user_description = user_description.strip()[:4000]
    if status is not None:
        try:
            node.status = ReasoningNodeStatus(status.strip())
        except ValueError as exc:
            raise InvalidInput("不认识的地图节点状态。") from exc
    node.version += 1
    await db.commit()
    return await build_view(db, ctx, session)


async def refine_strategy(
    db: AsyncSession,
    ctx: WorkspaceContext,
    reasoner,
    *,
    node_id: uuid.UUID | None = None,
):
    """执行桥接:把**已确认**的战略细化成阶段/里程碑/周计划提案。

    复用完全相同的对话工作流(`submit_turn`):它会把那句系统消息落成一条用户消息
    与一条助手消息,并在对话里留下痕迹 —— 按钮与对话不能有两个真相。

    **未确认战略时拒绝。** 这是产品规则,不是技术细节:没有已确认的方向就往
    执行层拆,拆出来的东西没有依据。
    """
    strategy = await db.scalar(
        select(PlanNode)
        .where(
            PlanNode.workspace_id == ctx.id,
            PlanNode.planning_level == "strategy",
            PlanNode.deleted_at.is_(None),
        )
        .order_by(PlanNode.created_at.desc())
        .limit(1)
    )
    if strategy is None:
        raise InvalidInput("还没有已确认的战略。先确认一个战略,再往下细化阶段。")
    if node_id is not None and node_id != strategy.id:
        raise InvalidInput("这个空间里没有这个已确认的战略。")
    # 用户**明确**点了“细化某个阶段”才允许进入执行细化。这样“问每天几点”就有了
    # 一个确定的门槛:在此之前会话一直在战略档,服务端会拦住执行类问题。
    session = await get_session(db, ctx)
    if session is not None:
        session.phase = ReasoningSessionPhase.EXECUTION_REFINEMENT
        session.turn_action = ReasoningTurnAction.EXPAND
        await db.flush()
    return await conversation_service.submit_turn(
        db,
        ctx,
        reasoner,
        content=REFINE_PHASE_MESSAGE,
        context_node_id=strategy.id,
        trigger="refine",
    )


__all__ = [
    "MAX_MAP_NODES",
    "MAX_PRIMARY_NODES",
    "MAX_QUESTIONS_PER_TURN",
    "MIN_PRIMARY_NODES",
    "MapValidationError",
    "build_view",
    "clamp_score",
    "compute_priority",
    "get_or_create_session",
    "get_session",
    "input_version",
    "load_map",
    "refine_strategy",
    "regenerate_roadmap",
    "render_map_section",
    "root_plan_node",
    "run_space_entered",
    "run_turn",
    "update_node",
]
