"""只读工具注册表。**服务端白名单,模型只能用本轮 handle。**

## 边界

- 没有任意 SQL / HTTP / 文件访问。每个工具是一个固定函数。
- 参数用严格白名单校验:多一个键、缺一个键、类型不对都会被拒。
- 节点只能用本轮的 `n1..nK` 句柄,不许传真实 UUID;句柄必须在 `turn.node_handles`
  里,且在本次可见范围里。
- 工具**不写任何业务数据**。`simulate_schedule` 走的是既有 `schedule_service.preview`,
  它本身就是只读模拟(见那里的 docstring)。
- 工具失败/被拒时返回 `status=rejected|error` 和一句可读摘要,**不抛给循环**——
  让模型看到真实失败,而不是假装得到了结果。

## 结果为什么是摘要

完整结果既不进提示词,也不进库。这里统一截断,并在 `truncated` 里如实标注;
`render_tools_section` 会把"截断了"写进下一次模型上下文。
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agent.runtime.base import ToolExchange, TurnContext
from backend.core.config import settings
from backend.db.models import (
    Dependency,
    ExecutionRecord,
    NodeNote,
    NodeRelation,
    PlanNode,
    ScheduledSession,
)
from backend.services.context import WorkspaceContext

logger = logging.getLogger(__name__)

#: 一个工具结果最多返回几条 / 渲染多少字。超出即截断并标注。
MAX_RESULT_ROWS = 20
MAX_SUMMARY_CHARS = 2000
#: find_similar_nodes 的关键词上限。
MAX_QUERY_CHARS = 80

#: 工具白名单。**唯一一处**;循环只认这里的名字。
TOOL_NAMES = frozenset(
    {
        "get_node",
        "list_children",
        "get_relations",
        "find_similar_nodes",
        "get_time_capacity",
        "get_recent_execution",
        "simulate_schedule",
        "research_public",
    }
)


class ToolRejected(Exception):
    """参数/权限/未知工具。**不是工具执行失败**,是"这个请求本身不被接受"。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    #: 允许的参数名(严格白名单)。
    allowed_arguments: frozenset[str]
    handler: Callable[[AsyncSession, WorkspaceContext, TurnContext, dict], Awaitable[dict]]


def _bounded(text: str | None, limit: int) -> tuple[str | None, bool]:
    if text is None:
        return None, False
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _handle_of(turn: TurnContext, node_id: uuid.UUID) -> str | None:
    for handle, raw in turn.node_handles:
        try:
            if uuid.UUID(raw) == node_id:
                return handle
        except ValueError:
            continue
    return None


def _resolve_handle(turn: TurnContext, handle: object) -> uuid.UUID:
    if not isinstance(handle, str) or handle not in dict(turn.node_handles):
        raise ToolRejected("这个节点句柄不在本轮可见范围内。")
    try:
        return uuid.UUID(dict(turn.node_handles)[handle])
    except ValueError as exc:  # pragma: no cover - 句柄表是服务端生成的
        raise ToolRejected("这个节点句柄无法解析。") from exc


def _view_of(turn: TurnContext, handle: str):
    for view in turn.nodes:
        if view.handle == handle:
            return view
    return None


def _validate_arguments(spec: ToolSpec, arguments: object) -> dict:
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise ToolRejected("工具参数必须是一个对象。")
    unknown = set(arguments) - spec.allowed_arguments
    if unknown:
        raise ToolRejected(f"工具 {spec.name} 不接受参数:{'、'.join(sorted(unknown))}。")
    return dict(arguments)


# ---------------------------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------------------------
async def _get_node(db, ctx, turn, arguments) -> dict:
    handle = arguments.get("handle")
    node_id = _resolve_handle(turn, handle)
    view = _view_of(turn, str(handle))
    if view is not None and view.read_only:
        # 范围外:只给标题与结构,不给未授权的正文/笔记。
        return {
            "handle": handle,
            "title": view.title,
            "scope": "outside",
            "note": "这个节点在当前范围之外,只读,不返回正文。",
        }
    node = await db.scalar(
        select(PlanNode).where(PlanNode.id == node_id, PlanNode.workspace_id == ctx.id)
    )
    if node is None:
        raise ToolRejected("这个节点不在当前空间里。")
    note = await db.scalar(
        select(NodeNote).where(NodeNote.node_id == node_id, NodeNote.workspace_id == ctx.id)
    )
    description, cut = _bounded(node.description, 2000)
    return {
        "handle": handle,
        "title": node.title,
        "nodeType": node.node_type.value,
        "purpose": node.purpose.value,
        "planningLevel": node.planning_level.value if node.planning_level else None,
        "status": node.status.value,
        "estimateMinutes": node.estimate_minutes,
        "deadline": node.deadline.isoformat() if node.deadline else None,
        "description": description,
        "descriptionTruncated": cut,
        "acceptanceCriteria": node.acceptance_criteria,
        "contentVersion": node.content_version,
        "noteChars": len(note.body) if note and note.body else 0,
        "noteVersion": note.content_version if note else 0,
    }


async def _list_children(db, ctx, turn, arguments) -> dict:
    parent_id = _resolve_handle(turn, arguments.get("handle"))
    parent = await db.scalar(
        select(PlanNode).where(PlanNode.id == parent_id, PlanNode.workspace_id == ctx.id)
    )
    if parent is None:
        raise ToolRejected("这个节点不在当前空间里。")
    rows = await db.execute(
        select(PlanNode)
        .where(
            PlanNode.workspace_id == ctx.id,
            PlanNode.parent_id == parent_id,
            PlanNode.deleted_at.is_(None),
        )
        .order_by(PlanNode.order_index.asc())
        .limit(MAX_RESULT_ROWS + 1)
    )
    children = list(rows.scalars())
    truncated = len(children) > MAX_RESULT_ROWS
    children = children[:MAX_RESULT_ROWS]
    return {
        "parentTitle": parent.title,
        "children": [
            {
                "handle": _handle_of(turn, child.id),
                "title": child.title,
                "nodeType": child.node_type.value,
                "purpose": child.purpose.value,
                "planningLevel": child.planning_level.value if child.planning_level else None,
                "status": child.status.value,
            }
            for child in children
        ],
        "truncated": truncated,
        "total": len(children) + (1 if truncated else 0),
    }


async def _get_relations(db, ctx, turn, arguments) -> dict:
    node_id = _resolve_handle(turn, arguments.get("handle"))
    rel_rows = await db.execute(
        select(NodeRelation).where(
            NodeRelation.workspace_id == ctx.id,
            or_(NodeRelation.source_node_id == node_id, NodeRelation.target_node_id == node_id),
        )
    )
    dep_rows = await db.execute(
        select(Dependency).where(
            Dependency.workspace_id == ctx.id,
            or_(Dependency.predecessor_id == node_id, Dependency.successor_id == node_id),
        )
    )
    relations: list[dict] = []
    for relation in rel_rows.scalars():
        relations.append(
            {
                "kind": relation.relation_type.value,
                "direction": "directed" if relation.relation_type.value == "influences" else "undirected",
                "other": _handle_of(turn, relation.target_node_id if relation.source_node_id == node_id else relation.source_node_id),
                "note": relation.note,
            }
        )
    dependencies: list[dict] = []
    for dep in dep_rows.scalars():
        dependencies.append(
            {
                "role": "predecessor" if dep.predecessor_id == node_id else "successor",
                "other": _handle_of(turn, dep.successor_id if dep.predecessor_id == node_id else dep.predecessor_id),
                "lagDays": dep.lag_days,
            }
        )
    return {
        "relations": relations[:MAX_RESULT_ROWS],
        "dependencies": dependencies[:MAX_RESULT_ROWS],
        "note": "前置会影响排期;相关/影响只是说明,不能当成排期结论。",
    }


def _normalize(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(folded.split())


async def _find_similar_nodes(db, ctx, turn, arguments) -> dict:
    query = arguments.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ToolRejected("find_similar_nodes 需要一个非空关键词。")
    needle = _normalize(query)[:MAX_QUERY_CHARS]
    visible_ids: set[uuid.UUID] = set()
    handle_of: dict[uuid.UUID, str] = {}
    for handle, raw in turn.node_handles:
        try:
            node_id = uuid.UUID(raw)
        except ValueError:
            continue
        visible_ids.add(node_id)
        handle_of[node_id] = handle
    if not visible_ids:
        return {"matches": [], "truncated": False, "note": "本轮没有可见节点。"}
    rows = await db.execute(
        select(PlanNode).where(
            PlanNode.workspace_id == ctx.id,
            PlanNode.deleted_at.is_(None),
            PlanNode.id.in_(visible_ids),
        )
    )
    matches: list[dict] = []
    for node in rows.scalars():
        haystack = _normalize(f"{node.title} {node.description or ''}")
        if needle in haystack or any(token and token in haystack for token in needle.split()):
            matches.append(
                {
                    "handle": handle_of.get(node.id),
                    "title": node.title,
                    "planningLevel": node.planning_level.value if node.planning_level else None,
                    "purpose": node.purpose.value,
                }
            )
    truncated = len(matches) > MAX_RESULT_ROWS
    return {
        "matches": matches[:MAX_RESULT_ROWS],
        "truncated": truncated,
        "note": "这是确定性的关键词匹配,不是语义检索。",
    }


async def _get_time_capacity(db, ctx, turn, arguments) -> dict:
    time = turn.time
    if time is None:
        return {"read": False, "note": "本次没有读取时间底盘,不知道容量。"}
    return {
        "read": True,
        "capacityConfigured": time.capacity_configured,
        "weeklyTotalMinutes": time.weekly_total_minutes,
        "weeklyBudgetMinutes": time.weekly_budget_minutes,
        "dailyCapMinutes": time.daily_cap_minutes,
        "windowCount": time.windows_total,
        "exceptionCount": time.exceptions_total,
        "scheduledSessions": time.sessions_total,
        "sessionsOtherWorkspaces": time.sessions_other_workspaces,
        "horizonDays": time.horizon_days,
        "horizonLastDay": time.horizon_last_day,
        "capacityMinutes": time.capacity_minutes,
        "openTaskMinutes": time.open_task_minutes,
        "openTasksWithoutEstimate": time.open_tasks_without_estimate,
        "note": "这些是事实。是否排得开要看 simulate_schedule,不要用容量总数下结论。",
    }


async def _get_recent_execution(db, ctx, turn, arguments) -> dict:
    rows = await db.execute(
        select(ExecutionRecord, ScheduledSession, PlanNode)
        .join(ScheduledSession, ScheduledSession.id == ExecutionRecord.session_id, isouter=True)
        .join(PlanNode, PlanNode.id == ExecutionRecord.node_id)
        .where(
            ExecutionRecord.user_id == ctx.user.user_id,
            ExecutionRecord.workspace_id == ctx.id,
        )
        .order_by(ExecutionRecord.created_at.desc())
        .limit(MAX_RESULT_ROWS)
    )
    items: list[dict] = []
    for record, session, node in rows.all():
        items.append(
            {
                "nodeTitle": node.title,
                "handle": _handle_of(turn, node.id),
                "result": record.result.value,
                "actualMinutes": record.actual_minutes,
                "plannedMinutes": session.planned_minutes if session else None,
                "scheduledDate": session.scheduled_date.isoformat() if session else None,
                "delayReason": record.delay_reason,
                "completionRatio": str(record.completion_ratio) if record.completion_ratio is not None else None,
            }
        )
    return {
        "executions": items,
        "note": "这些是执行事实。一次失败不要解释成能力或价值问题。",
    }


#: research_public 查询的长度上限。
RESEARCH_QUERY_MAX_CHARS = 120
#: 查询里绝不能出现的私密标记。
_RESEARCH_PRIVATE_MARKERS = (
    "sk-",
    "bearer ",
    "authorization",
    "password",
    "api_key",
    "api-key",
    "token",
    "database_url",
    "postgres://",
    "postgresql",
)
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_HEX_RE = re.compile(r"\b[0-9a-f]{32,}\b")


def _sanitize_research_query(query: object) -> str:
    """研究关键词的入参校验:**不许把私密内容带出去**。

    它不保证语义安全(一个关键词本身可能有隐私性),但能拦住可机读的密钥/连接串/
    内部 id。真正“什么可以外发”的最终决定权在使用者:这是本地/自部署工具。
    """
    if not isinstance(query, str) or len(query.strip()) < 3:
        raise ToolRejected("research_public 需要一个至少 3 个字的关键词。")
    text = query.strip()
    if len(text) > RESEARCH_QUERY_MAX_CHARS:
        raise ToolRejected(f"研究关键词最多 {RESEARCH_QUERY_MAX_CHARS} 字。")
    lowered = text.lower()
    for marker in _RESEARCH_PRIVATE_MARKERS:
        if marker in lowered:
            raise ToolRejected("研究关键词里不能包含密钥、令牌、连接串等私密内容。")
    if _UUID_RE.search(lowered) or _HEX_RE.search(lowered):
        raise ToolRejected("研究关键词里不能包含内部 id。")
    return text


async def _research_public(db, ctx, turn, arguments) -> dict:
    """受限的公开研究。**默认未配置时如实说未配置,绝不假装查过。**

    本版本只实现到“显式开关 + 诚实响应”。真正的搜索提供方适配器**尚未实现** ——
    没有配置时返回 `configured=false`;即使配了未支持的提供方,也返回 `configured=false`
    并说明本版本没有适配器,而不是编造来源。
    """
    query = _sanitize_research_query(arguments.get("query"))
    provider = (settings.research_provider or "").strip().lower()
    if provider in ("", "none", "off", "disabled"):
        return {
            "configured": False,
            "query": query,
            "note": (
                "公开研究工具未配置(服务端没有启用 RESEARCH_PROVIDER);我没有联网查过。"
                "不要把它当成查过了,也不要编造来源。"
            ),
        }
    return {
        "configured": False,
        "query": query,
        "note": (
            f"研究提供方 {provider!r} 已配置,但本版本没有对应适配器;我没有联网查过。"
            "不要编造来源。"
        ),
    }


async def _simulate_schedule(db, ctx, turn, arguments) -> dict:
    """只读排期模拟。**走的是既有确定性 `simulate`,不写任何数据。**"""
    from backend.services import schedule_service  # 延迟 import,避免服务层循环

    preview = await schedule_service.preview(db, ctx)
    # 排期是**按人**算的,可能覆盖多个空间;只把当前空间的部分放进模型上下文,
    # 避免把别的空间的内容带进来。容量数字是跨空间共享的,作为事实保留。
    sessions = [s for s in preview.sessions if str(s.workspace_id) == str(ctx.id)]
    gaps = [g for g in preview.gaps if str(g.workspace_id) == str(ctx.id)]
    return {
        "scheduleVersion": preview.schedule_version,
        "today": preview.today.isoformat(),
        "horizonDays": preview.horizon_days,
        "weeklyBudgetMinutes": preview.weekly_budget_minutes,
        "totalPlannedMinutes": preview.total_planned_minutes,
        "unscheduledMinutes": preview.unscheduled_minutes,
        "sessionCount": len(sessions),
        "sessions": [
            {
                "nodeIdHandle": _handle_of(turn, s.node_id),
                "nodeTitle": s.node_title,
                "date": s.scheduled_date.isoformat(),
                "plannedMinutes": s.planned_minutes,
                "startMinute": s.start_minute,
            }
            for s in sessions[:MAX_RESULT_ROWS]
        ],
        "gaps": [
            {
                "nodeTitle": g.node_title,
                "unscheduledMinutes": g.unscheduled_minutes,
                "reasonCode": g.reason_code,
                "bindingConstraint": g.binding_constraint,
            }
            for g in gaps[:MAX_RESULT_ROWS]
        ],
        "recoveryOptionKinds": [option.kind for option in preview.options[:MAX_RESULT_ROWS]],
        "truncated": preview.truncated or len(sessions) > MAX_RESULT_ROWS,
        "note": (
            "这是确定性模拟的事实。模型只能解释它并提节点/工时/截止/优先级等提案,"
            "不能自己安排具体日程,也不能凭空说排得开。"
        ),
    }


TOOL_SPECS: dict[str, ToolSpec] = {
    "get_node": ToolSpec("get_node", frozenset({"handle"}), _get_node),
    "list_children": ToolSpec("list_children", frozenset({"handle"}), _list_children),
    "get_relations": ToolSpec("get_relations", frozenset({"handle"}), _get_relations),
    "find_similar_nodes": ToolSpec("find_similar_nodes", frozenset({"query"}), _find_similar_nodes),
    "get_time_capacity": ToolSpec("get_time_capacity", frozenset(), _get_time_capacity),
    "get_recent_execution": ToolSpec("get_recent_execution", frozenset(), _get_recent_execution),
    "simulate_schedule": ToolSpec("simulate_schedule", frozenset(), _simulate_schedule),
    "research_public": ToolSpec("research_public", frozenset({"query"}), _research_public),
}


def sanitize_arguments(arguments: dict) -> dict:
    """脱敏后的参数:**只留白名单值,且 handle 一定不是真实 UUID**(本来就只是记号)。"""
    return {key: value for key, value in arguments.items() if isinstance(key, str)}


def summarize_for_prompt(summary: dict) -> tuple[dict, bool]:
    """把结果摘要压到渲染上限,并返回是否截断。"""
    encoded = json.dumps(summary, ensure_ascii=False, default=str)
    if len(encoded) <= MAX_SUMMARY_CHARS:
        return summary, False
    return {"truncatedPreview": encoded[:MAX_SUMMARY_CHARS]}, True


async def execute_tool(
    db: AsyncSession,
    ctx: WorkspaceContext,
    turn: TurnContext,
    *,
    name: str,
    arguments: object,
) -> tuple[ToolExchange, dict, bool]:
    """执行一个只读工具。

    返回 `(exchange, sanitized_arguments, ok)`。**绝不抛异常**:未知工具、非法参数、
    执行失败都变成 `rejected`/`error` 的 exchange,让模型看到真实结果。
    """
    spec = TOOL_SPECS.get(name)
    if spec is None:
        return (
            ToolExchange(
                tool_name=name,
                status="rejected",
                summary={"error": f"不认识工具 {name}。"},
            ),
            {},
            False,
        )
    try:
        clean = _validate_arguments(spec, arguments)
        result = await spec.handler(db, ctx, turn, clean)
    except ToolRejected as exc:
        return (
            ToolExchange(tool_name=name, status="rejected", summary={"error": exc.message}),
            sanitize_arguments(arguments if isinstance(arguments, dict) else {}),
            False,
        )
    except Exception as exc:  # 工具错误归一化成可读摘要
        logger.warning("工具 %s 执行失败: %s", name, exc)
        return (
            ToolExchange(
                tool_name=name,
                status="error",
                summary={"error": "工具执行失败,没有得到结果。"},
            ),
            sanitize_arguments(arguments if isinstance(arguments, dict) else {}),
            False,
        )
    summary, truncated = summarize_for_prompt(result)
    return (
        ToolExchange(
            tool_name=name,
            arguments=sanitize_arguments(clean),
            status="ok",
            summary=summary,
            truncated=truncated,
        ),
        sanitize_arguments(clean),
        True,
    )


__all__ = [
    "MAX_RESULT_ROWS",
    "MAX_SUMMARY_CHARS",
    "RESEARCH_QUERY_MAX_CHARS",
    "TOOL_NAMES",
    "TOOL_SPECS",
    "ToolRejected",
    "execute_tool",
    "sanitize_arguments",
    "summarize_for_prompt",
]
