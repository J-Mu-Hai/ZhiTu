"""首页「本周计划 / 本日计划」的跨空间聚合读取。

## 唯一真相仍是 `plan_nodes` + `scheduled_sessions`

这里不建第二套数据:周计划是 `stage` 节点(标题以「本周计划」开头),周任务是它下面
`purpose=planning` 的 `task` / `milestone`,今天的安排是 `scheduled_sessions`。
首页、任务面板、时间线读的是同一批行 —— 所以"首页看到的"和"工作台里看到的"不可能
不一致。

## 只收正式、活跃、未归档

- proposal 里的东西**根本没有 PlanNode 行**,所以查不到;
- 被重规划替换的历史周计划状态是 `archived`,在这里被排除;
- 软删除(`deleted_at` 非空)的节点与场次也不进入。

## "当前自然周"怎么判

用户手工建的周计划把周起始日写进标题(`... · 2026-10-05`),按它比对本周一;AI 建的
周计划没有日期后缀,它本身就是"本周"的语义,直接纳入。两边都不猜、不伪造成当前周。
"""

from __future__ import annotations

import re
import uuid
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.contracts.agenda import (
    AgendaPhaseTargetView,
    AgendaSessionView,
    AgendaTodayItemView,
    AgendaWeekPlanView,
    AgendaWeekSessionView,
    AgendaWeekTaskView,
    AgendaWorkspaceTargetView,
    TodayPlansResponse,
)
from backend.core.security import CurrentUser
from backend.db.models import PlanNode, ScheduledSession, Workspace
from backend.db.models.enums import (
    NodePurpose,
    NodeStatus,
    NodeType,
    ScheduledSessionStatus,
    WorkspaceStatus,
)
from backend.services import execution_service
from backend.services.timeutil import today_in

#: 标题末尾的 ISO 周起始日:用户手工建的周计划用它标识"哪一周"。
_WEEK_SUFFIX = re.compile(r"·\s*(\d{4})-(\d{2})-(\d{2})\s*$")

#: 不作为"当前安排"的场次墓碑。与 `plan_service` / `execution_service` 同一口径。
_TOMBSTONE = (ScheduledSessionStatus.CANCELED, ScheduledSessionStatus.MOVED)

#: 周计划的标题前缀。AI 与用户手工创建用的是同一个前缀,所以一种判据覆盖两条路。
_WEEK_PREFIX = "本周计划"


def week_bounds(day: date) -> tuple[date, date]:
    """`day` 所在自然周的周一与周日。Python 的 `weekday()` 里周一是 0。"""
    start = day - timedelta(days=day.weekday())
    return start, start + timedelta(days=6)


def _week_start_of_title(title: str) -> date | None:
    """从周计划标题里读出周起始日;没有(或格式不对)返回 `None`。"""
    match = _WEEK_SUFFIX.search(title)
    if match is None:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _active(node: PlanNode) -> bool:
    return node.status is not NodeStatus.ARCHIVED


def _top_stage_of(node: PlanNode, by_id: dict[uuid.UUID, PlanNode]) -> PlanNode | None:
    """沿父链取**最靠上的** stage 祖先。

    对「根 → 阶段 → 周计划 → 任务」这条链,最近的 stage 是周计划,而用户说的"所属
    阶段"是再上面那个正式阶段。取最靠上的一个才和任务面板的「本阶段」一致。
    """
    top: PlanNode | None = None
    seen = {node.id}
    current = node.parent_id
    while current is not None and current in by_id and current not in seen:
        seen.add(current)
        candidate = by_id[current]
        if candidate.node_type is NodeType.STAGE:
            top = candidate
        current = candidate.parent_id
    return top


async def load_today_plans(
    db: AsyncSession,
    user: CurrentUser,
    timezone: str,
    week_start: date | None = None,
) -> TodayPlansResponse:
    """聚合这个账号全部活动空间的本周计划与今天安排。

    `week_start` 可选,给顶部「本周时间线」翻到别的周时用;不给就是当前自然周。
    翻周只影响「这一周」两段(周计划与逐场次),今天相关的两段始终是今天。
    """
    today = today_in(timezone)
    current_week_start, _ = week_bounds(today)
    target_week_start, target_week_end = week_bounds(week_start or today)

    workspaces = list(
        await db.scalars(
            select(Workspace).where(
                Workspace.owner_id == user.user_id,
                Workspace.status == WorkspaceStatus.ACTIVE,
            )
        )
    )
    empty = TodayPlansResponse(
        today=today,
        timezone=timezone,
        week_start=target_week_start,
        week_end=target_week_end,
    )
    if not workspaces:
        return empty
    workspace_by_id = {workspace.id: workspace for workspace in workspaces}
    workspace_ids = list(workspace_by_id)

    nodes = list(
        await db.scalars(
            select(PlanNode).where(
                PlanNode.workspace_id.in_(workspace_ids),
                PlanNode.deleted_at.is_(None),
            )
        )
    )
    node_by_id = {node.id: node for node in nodes}
    children: dict[uuid.UUID | None, list[PlanNode]] = {}
    for node in nodes:
        children.setdefault(node.parent_id, []).append(node)

    sessions = list(
        await db.scalars(
            select(ScheduledSession).where(
                ScheduledSession.workspace_id.in_(workspace_ids),
                ScheduledSession.status.not_in(_TOMBSTONE),
            )
        )
    )
    sessions_by_node: dict[uuid.UUID, list[ScheduledSession]] = {}
    for session in sessions:
        sessions_by_node.setdefault(session.node_id, []).append(session)

    # ---- 当前周的活跃周计划 ----
    active_week_nodes = [
        node
        for node in nodes
        if node.node_type is NodeType.STAGE
        and _active(node)
        and node.title.startswith(_WEEK_PREFIX)
    ]
    current_week_plan_by_phase: dict[uuid.UUID, PlanNode] = {}
    week_plans: list[AgendaWeekPlanView] = []
    for plan_node in active_week_nodes:
        workspace = workspace_by_id.get(plan_node.workspace_id)
        if workspace is None:
            continue
        encoded = _week_start_of_title(plan_node.title)
        # 手工周计划按标题里的周次过滤;AI 的没有日期,只在看当前周时视为"本周"。
        if encoded is not None:
            if encoded != target_week_start:
                continue
        elif target_week_start != current_week_start:
            continue
        phase = node_by_id.get(plan_node.parent_id) if plan_node.parent_id else None
        if phase is not None and phase.node_type is NodeType.STAGE:
            current_week_plan_by_phase.setdefault(phase.id, plan_node)
        tasks: list[AgendaWeekTaskView] = []
        ordered = sorted(
            children.get(plan_node.id, []),
            key=lambda child: (child.order_index, child.created_at),
        )
        for child in ordered:
            if child.node_type not in (NodeType.TASK, NodeType.MILESTONE):
                continue
            if not _active(child) or child.purpose is not NodePurpose.PLANNING:
                continue
            task_sessions = sorted(
                sessions_by_node.get(child.id, []),
                key=lambda session: (session.scheduled_date, session.seq),
            )
            tasks.append(
                AgendaWeekTaskView(
                    node_id=child.id,
                    title=child.title,
                    node_type=child.node_type.value,
                    status=child.status.value,
                    deadline=child.deadline,
                    estimate_minutes=child.estimate_minutes,
                    priority=child.priority.value,
                    sessions=[
                        AgendaSessionView(
                            id=session.id,
                            scheduled_date=session.scheduled_date,
                            planned_minutes=session.planned_minutes,
                            start_minute=session.start_minute,
                            end_minute=session.end_minute,
                            seq=session.seq,
                            status=session.status.value,
                        )
                        for session in task_sessions
                    ],
                )
            )
        week_plans.append(
            AgendaWeekPlanView(
                workspace_id=workspace.id,
                workspace_title=workspace.title,
                plan_node_id=plan_node.id,
                plan_title=plan_node.title,
                stage_id=phase.id if phase is not None else None,
                stage_title=phase.title if phase is not None else None,
                tasks=tasks,
            )
        )
    week_plans.sort(key=lambda view: (view.workspace_title, view.plan_title))

    # ---- 今天:已排场次 + 今天截止但还没排的任务 ----
    today_response = await execution_service.load_today(db, user, timezone)
    today_items: list[AgendaTodayItemView] = []
    scheduled_node_ids: set[uuid.UUID] = set()
    for workspace_view in today_response.workspaces:
        for item in workspace_view.items:
            scheduled_node_ids.add(item.node_id)
            node = node_by_id.get(item.node_id)
            stage = _top_stage_of(node, node_by_id) if node is not None else None
            today_items.append(
                AgendaTodayItemView(
                    kind="block",
                    session_id=item.session_id,
                    node_id=item.node_id,
                    workspace_id=item.workspace_id,
                    workspace_title=item.workspace_title,
                    title=item.node_title,
                    stage_id=stage.id if stage is not None else None,
                    stage_title=stage.title if stage is not None else None,
                    scheduled_date=today,
                    planned_minutes=item.planned_minutes,
                    start_minute=item.start_minute,
                    end_minute=item.end_minute,
                    session_status=item.status,
                    node_status=node.status.value if node is not None else NodeStatus.PENDING.value,
                    result=item.result,
                    recorded=item.recorded,
                )
            )
    for node in nodes:
        if node.deadline != today:
            continue
        if node.node_type not in (NodeType.TASK, NodeType.MILESTONE):
            continue
        if not _active(node) or node.purpose is not NodePurpose.PLANNING:
            continue
        # **不重复显示**:同一节点今天已经有场次时,它已经在上面作为工作块出现。
        if node.id in scheduled_node_ids:
            continue
        stage = _top_stage_of(node, node_by_id)
        workspace = workspace_by_id.get(node.workspace_id)
        if workspace is None:
            continue
        today_items.append(
            AgendaTodayItemView(
                kind="task",
                session_id=None,
                node_id=node.id,
                workspace_id=node.workspace_id,
                workspace_title=workspace.title,
                title=node.title,
                stage_id=stage.id if stage is not None else None,
                stage_title=stage.title if stage is not None else None,
                scheduled_date=today,
                planned_minutes=node.estimate_minutes,
                start_minute=None,
                end_minute=None,
                session_status=None,
                node_status=node.status.value,
                result=None,
                recorded=False,
            )
        )
    # 有具体时间的在前、按时间;没有时间的排到后面。
    today_items.sort(
        key=lambda item: (
            0 if item.start_minute is not None else 1,
            item.start_minute if item.start_minute is not None else 24 * 60,
            item.title,
        )
    )

    # ---- 这一周的逐场安排(顶部「本周时间线」) ----
    week_sessions: list[AgendaWeekSessionView] = []
    for session in sessions:
        if session.scheduled_date < target_week_start or session.scheduled_date > target_week_end:
            continue
        node = node_by_id.get(session.node_id)
        workspace = workspace_by_id.get(session.workspace_id)
        if node is None or workspace is None:
            continue
        week_sessions.append(
            AgendaWeekSessionView(
                id=session.id,
                node_id=session.node_id,
                workspace_id=workspace.id,
                workspace_title=workspace.title,
                node_title=node.title,
                scheduled_date=session.scheduled_date,
                planned_minutes=session.planned_minutes,
                start_minute=session.start_minute,
                end_minute=session.end_minute,
                status=session.status.value,
            )
        )
    week_sessions.sort(
        key=lambda view: (
            view.scheduled_date,
            view.start_minute if view.start_minute is not None else 24 * 60,
            view.node_title,
            str(view.id),
        )
    )

    # ---- 添加表单的可选目标 ----
    targets: list[AgendaWorkspaceTargetView] = []
    for workspace in sorted(workspaces, key=lambda row: row.title):
        root = next(
            (
                node
                for node in children.get(None, [])
                if node.workspace_id == workspace.id
            ),
            None,
        )
        phases: list[AgendaPhaseTargetView] = []
        if root is not None:
            for child in sorted(
                children.get(root.id, []),
                key=lambda node: (node.order_index, node.created_at),
            ):
                if child.node_type is not NodeType.STAGE or not _active(child):
                    continue
                plan = current_week_plan_by_phase.get(child.id)
                phases.append(
                    AgendaPhaseTargetView(
                        stage_id=child.id,
                        stage_title=child.title,
                        week_plan_id=plan.id if plan is not None else None,
                        week_plan_title=plan.title if plan is not None else None,
                    )
                )
        targets.append(
            AgendaWorkspaceTargetView(
                workspace_id=workspace.id,
                workspace_title=workspace.title,
                phases=phases,
            )
        )

    return TodayPlansResponse(
        today=today,
        timezone=timezone,
        week_start=target_week_start,
        week_end=target_week_end,
        week_plans=week_plans,
        today_items=today_items,
        week_sessions=week_sessions,
        targets=targets,
    )
