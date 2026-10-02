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
from datetime import datetime, timedelta
from hashlib import blake2b

from sqlalchemy import func, select
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
    ReasoningNode,
    ReasoningNodeLink,
)
from backend.db.models.enums import (
    ACTIVE_QUESTION_STATUSES,
    ReasoningLinkType,
    ReasoningNodeStatus,
    ReasoningNodeType,
    ReasoningSessionPhase,
    ReasoningSessionStatus,
    ReasoningSource,
    ReasoningTurnAction,
)
from backend.services import conversation_service, question_service, turn_context
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
        phase=ReasoningSessionPhase.STRATEGIC_EXPLORATION,
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
    """把"会影响地图的输入"压成一个稳定摘要。**幂等比较只用它,不用时间。**"""
    brief_version = await db.scalar(
        select(PlanningBrief.version).where(PlanningBrief.workspace_id == ctx.id)
    )
    live_nodes = await db.scalar(
        select(func.count())
        .select_from(PlanNode)
        .where(PlanNode.workspace_id == ctx.id, PlanNode.deleted_at.is_(None))
    )
    parts = "|".join(
        [
            str(root.id),
            str(root.content_version),
            (root.title or "").strip(),
            str(brief_version or 0),
            str(int(live_nodes or 0)),
            str(ctx.workspace.current_revision_version),
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
            phase="strategic_exploration",
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
) -> None:
    """写入**之前**把整份草稿验一遍。任何一处不合法就整份拒绝。"""
    fresh = not existing
    primary = [node for node in draft.nodes if not node.parent_handle]
    if fresh and not (MIN_PRIMARY_NODES <= len(primary) <= MAX_PRIMARY_NODES):
        raise MapValidationError(
            f"首次探索需要 {MIN_PRIMARY_NODES}–{MAX_PRIMARY_NODES} 个一级决策维度,收到 {len(primary)} 个。"
        )
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
) -> AgentTurnResponse:
    if question is None:
        question = await _primary_pending_question(db, ctx)
    return AgentTurnResponse(
        reasoning=await build_view(db, ctx, session),
        message=_message_view(message) if message is not None else None,
        question=question_service.to_view(question) if question is not None else None,
        replayed=replayed,
        changed=changed,
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

    # 标记 running 并**立即提交** —— 并发的第二次进入看得到,不会重复跑模型。
    session.status = ReasoningSessionStatus.RUNNING
    session.last_error = None
    await db.commit()

    current_iv = await input_version(db, ctx, root)
    turn = await _build_reasoning_context(
        db, ctx, root, session, trigger_message=SPACE_ENTERED_PROMPT
    )
    result = await reasoner.reason(turn)

    if result.degraded:
        session.status = ReasoningSessionStatus.FAILED
        session.last_error = result.reply or "模型这次没能完成探索。"
        await db.commit()
        return await _response(db, ctx, session, result=result)

    draft = result.reasoning_map
    if draft is None:
        session.status = ReasoningSessionStatus.FAILED
        session.last_error = "模型没有给出可用的推理地图结构,这次没有写入任何节点。"
        await db.commit()
        return await _response(db, ctx, session, result=result)

    existing_rows = await _load_nodes(db, session.id)
    existing = {row.handle: row for row in existing_rows}
    handle_of_id = {row.id: row.handle for row in existing_rows}
    try:
        _validate_draft(draft, existing=existing, handle_of_id=handle_of_id)
    except MapValidationError as exc:
        session.status = ReasoningSessionStatus.FAILED
        session.last_error = str(exc)
        await db.commit()
        return await _response(db, ctx, session, result=result)

    # ---- 全部校验通过,才动地图 ----
    _apply_draft(db, session, draft, existing)
    await _resolve_parents(db, session.id)
    await _apply_links(db, session.id, draft)
    focus = await _pick_focus(db, session.id, draft)
    if focus is not None:
        session.focus_reasoning_node_id = focus.id
        session.focus_reason = draft.focus_reason or "它当前最影响下一步该怎么做。"
    if draft.phase:
        session.phase = ReasoningSessionPhase(draft.phase)
    if draft.turn_action:
        session.turn_action = ReasoningTurnAction(draft.turn_action)

    # 助手解释:同一事务落库。
    conversation = await conversation_service.get_or_create_primary_conversation(db, ctx)
    message = await conversation_service.append_reply(
        db, ctx, conversation=conversation, result=result
    )

    # 问题:挂到当前焦点节点上;战略阶段由服务端拦住排期类问题。
    strategy_phase = session.phase in (
        ReasoningSessionPhase.STRATEGIC_EXPLORATION,
        ReasoningSessionPhase.STRATEGIC_CONVERGENCE,
    )
    created_questions = await question_service.create_from_drafts(
        db,
        ctx,
        result.questions[:MAX_QUESTIONS_PER_TURN],
        source_message_id=message.id,
        source_node_id=root.id,
        reasoning_node_id=focus.id if focus is not None else None,
        strategy_phase=strategy_phase,
    )

    session.status = ReasoningSessionStatus.READY
    session.input_version = current_iv
    session.last_idempotency_key = payload.idempotency_key
    session.map_version += 1
    session.explored_at = now
    session.last_evaluated_at = now
    await db.commit()

    primary_question = created_questions[0] if created_questions else None
    return await _response(
        db,
        ctx,
        session,
        message=message,
        question=primary_question,
        result=result,
        changed=True,
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
    # 回答后重评 / 战略收敛由步骤 4 接入;在那之前如实返回当前地图,不假装做了事。
    session = await get_session(db, ctx)
    if session is None:
        raise InvalidInput("这个空间还没有开始目标推理,请先进入空间触发一次探索。")
    return await _response(db, ctx, session, replayed=True)


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
    "render_map_section",
    "root_plan_node",
    "run_space_entered",
    "run_turn",
    "update_node",
]
