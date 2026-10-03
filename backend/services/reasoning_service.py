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
    ReasoningLinkView,
    ReasoningNodeView,
)
from backend.db.base import utcnow
from backend.db.models import (
    AgentQuestion,
    GoalReasoningSession,
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
    session = GoalReasoningSession(
        workspace_id=ctx.id,
        root_plan_node_id=root.id,
        # 阶段 11:新会话从**战略澄清 intake**开始 —— 先问关键问题,再给时间架构。
        phase=ReasoningSessionPhase.INTAKE,
        turn_action=ReasoningTurnAction.ANALYZE,
        status=ReasoningSessionStatus.IDLE,
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
    return await db.scalar(
        select(AgentQuestion)
        .where(
            AgentQuestion.workspace_id == ctx.id,
            AgentQuestion.status.in_(tuple(ACTIVE_QUESTION_STATUSES)),
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
        purpose="goal_reasoning",
        reasoning_section=render_map_section(nodes, links),
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

    if session.status == ReasoningSessionStatus.RUNNING and not _running_is_stale(session, now):
        return await _response(db, ctx, session, replayed=True)
    if (
        not force
        and session.status == ReasoningSessionStatus.READY
        and session.input_version == await input_version(db, ctx, root)
    ):
        return await _response(db, ctx, session, replayed=True)
    if (
        not force
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
        current_iv=await input_version(db, ctx, root),
        idempotency_key=payload.idempotency_key,
    )


def _has_architecture_draft(result: ReasoningResult) -> bool:
    """这一轮的结果里有没有**带阶段的战略架构**。"""
    draft = result.reasoning_map
    return draft is not None and any(node.node_type == "stage" for node in draft.nodes)


async def _apply_intake_questions(
    db: AsyncSession,
    ctx: WorkspaceContext,
    root: PlanNode,
    session: GoalReasoningSession,
    result: ReasoningResult,
    *,
    trace: ReasoningState,
) -> AgentTurnResponse | None:
    """intake 回合:把模型提的关键问题落成 `conversation_intake` 问题。

    **只在还能问(未到 5 个上限)且模型确实给了一个可用问题时返回。** 否则返回 None,
    由调用方走结构化纠错、要求给出时间架构 —— 不能无限追问。

    这些问题是**唯一**能用橙色标记的提问:只在对话区显示,不生成画布 Question Node。
    """
    if session.intake_questions_asked >= MAX_INTAKE_QUESTIONS:
        return None
    # **不要先切片**:第一道题可能被战略/重复守卫拦掉,后面那道才是可用的。
    # 由 `create_from_drafts(max_questions=1)` 在**过滤之后**再限 1 个。
    drafts = result.questions
    if not drafts:
        return None
    conversation = await conversation_service.get_or_create_primary_conversation(db, ctx)
    message = await conversation_service.append_reply(
        db, ctx, conversation=conversation, result=result
    )
    created = await question_service.create_from_drafts(
        db,
        ctx,
        drafts,
        source_message_id=message.id,
        source_node_id=root.id,
        reasoning_node_id=None,
        strategy_phase=True,
        require_judgment=True,
        max_questions=MAX_STRATEGIC_QUESTIONS,
        presentation=QuestionPresentation.CONVERSATION_INTAKE.value,
    )
    if not created:
        return None
    session.intake_questions_asked += 1
    session.phase = ReasoningSessionPhase.INTAKE
    session.status = ReasoningSessionStatus.READY
    session.last_evaluated_at = utcnow()
    agent_trace_service.mark_terminal(
        trace, degraded=False, degraded_reason=None, stopped_reason="ready_to_propose"
    )
    await db.commit()
    return await _response(
        db, ctx, session, message=message, question=created[0], result=result, changed=False
    )


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

    turn = await _build_reasoning_context(
        db, ctx, root, session, trigger_message=trigger_message
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
    # 阶段 11:战略澄清 intake。**先只问关键问题,不生路线图。** 这一轮只要模型给了
    # 一个可用的关键问题,就落成 conversation_intake 并停在 intake;到 5 个上限或模型
    # 说够了,再走下面的结构化纠错,要求给出时间架构。
    if (
        error is not None
        and not result.degraded
        and session.phase.is_intake
        and not _has_architecture_draft(result)
    ):
        applied = await _apply_intake_questions(
            db, ctx, root, session, result, trace=trace
        )
        if applied is not None:
            return applied
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
            trigger_message=f"{trigger_message}\n\n{ROADMAP_CORRECTION_MESSAGE}",
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
