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
from hashlib import blake2b

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.contracts.reasoning import (
    GoalReasoningView,
    ReasoningLinkView,
    ReasoningNodeView,
)
from backend.db.models import (
    GoalReasoningSession,
    PlanningBrief,
    PlanNode,
    ReasoningNode,
    ReasoningNodeLink,
)
from backend.db.models.enums import ReasoningSessionStatus
from backend.services.context import WorkspaceContext

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


__all__ = [
    "MAX_MAP_NODES",
    "MAX_PRIMARY_NODES",
    "MAX_QUESTIONS_PER_TURN",
    "MIN_PRIMARY_NODES",
    "build_view",
    "clamp_score",
    "compute_priority",
    "get_session",
    "input_version",
    "load_map",
    "root_plan_node",
]
