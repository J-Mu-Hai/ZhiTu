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
from backend.services import research_service
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


#: 工具结果里**只给服务端看**的保留键。`execute_tool` 会在把摘要交给模型之前
#: 摘掉它。它存在的原因只有一条:公开来源证据不能被摘要截断(`MAX_SUMMARY_CHARS`),
#: 也不能进提示词。
RESEARCH_EVIDENCE_KEY = "_researchEvidence"


@dataclass(frozen=True, slots=True)
class ToolExecution:
    """一次工具执行的完整返回。

    `exchange` 是要回填给模型、并写入 `ToolCallRecord` 的摘要;`research` 是
    **服务端专用**的公开研究引用记录(只有 `research_public` 会带)。
    """

    exchange: ToolExchange
    sanitized_arguments: dict
    ok: bool
    research: dict | None = None


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


#: research_public 查询的长度上限的兼容默认值;实际取 `settings.research_max_query_chars`。
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
#: 私有/内部标识(带这些词的查询不应出网)。
_RESEARCH_PRIVATE_URL_MARKERS = (
    "cookie",
    "sessionid",
    "session_id",
    "access_token",
    "private",
    "internal use",
    "内网",
    "内部资料",
    "我的邮箱",
    "我的手机号",
)
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_HEX_RE = re.compile(r"\b[0-9a-f]{32,}\b")
#: 邮箱、手机号、证件/卡号 —— 属于“只有用户知道”的个人信息,不能送去公网搜索。
_EMAIL_RE = re.compile(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}", re.IGNORECASE)
_CN_MOBILE_RE = re.compile(r"(?<!\d)(?:\+?86[\s-]?)?1[3-9]\d{9}(?!\d)")
_INTL_PHONE_RE = re.compile(r"\+\d[\d\s().\-]{7,}\d")
_ID_CARD_RE = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
_BANK_CARD_RE = re.compile(r"(?<!\d)\d{13,19}(?!\d)")
#: 私有/内部地址与域名。
_PRIVATE_HOST_TOKENS = (
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
    "::1",
    ".local",
    ".internal",
    ".corp",
    ".lan",
    ".home",
)
_PRIVATE_IP_RE = re.compile(
    r"(?<!\d)(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})(?!\d)"
)


def _looks_private_host(text: str) -> bool:
    if any(token in text for token in _PRIVATE_HOST_TOKENS):
        return True
    return _PRIVATE_IP_RE.search(text) is not None


def _sanitize_research_query(query: object) -> str:
    """研究关键词的入参校验:**不许把私密内容带出去**。

    它不保证语义安全(一个关键词本身可能有隐私性),但能拦住可机读的密钥/连接串/
    内部 id。真正“什么可以外发”的最终决定权在使用者:这是本地/自部署工具。
    """
    if not isinstance(query, str) or len(query.strip()) < 3:
        raise ToolRejected("research_public 需要一个至少 3 个字的关键词。")
    text = query.strip()
    limit = settings.research_max_query_chars or RESEARCH_QUERY_MAX_CHARS
    if len(text) > limit:
        raise ToolRejected(f"研究关键词最多 {limit} 字。")
    # 整段对话/多行文本不是“关键词”。
    if "\n" in text or "\r" in text:
        raise ToolRejected("研究关键词只能是短语,不能是整段对话。")
    lowered = text.lower()
    for marker in _RESEARCH_PRIVATE_MARKERS:
        if marker in lowered:
            raise ToolRejected("研究关键词里不能包含密钥、令牌、连接串等私密内容。")
    for marker in _RESEARCH_PRIVATE_URL_MARKERS:
        if marker in lowered:
            raise ToolRejected("研究关键词里不能包含私有或内部标识。")
    if _EMAIL_RE.search(text):
        raise ToolRejected("研究关键词里不能包含邮箱等个人标识。")
    if _CN_MOBILE_RE.search(text) or _INTL_PHONE_RE.search(text):
        raise ToolRejected("研究关键词里不能包含电话号码等个人标识。")
    if _ID_CARD_RE.search(text) or _BANK_CARD_RE.search(text):
        raise ToolRejected("研究关键词里不能包含证件号或卡号等个人标识。")
    if _UUID_RE.search(lowered) or _HEX_RE.search(lowered):
        raise ToolRejected("研究关键词里不能包含内部 id。")
    if _looks_private_host(lowered):
        raise ToolRejected("研究关键词里不能包含私有地址或内部域名。")
    return text


async def _research_public(db, ctx, turn, arguments) -> dict:
    """受限的公开研究。**未配置/失败/超额时如实说“没有完成”,绝不假装查过。**

    真实检索只在 `RESEARCH_ENABLED=true` + `RESEARCH_PROVIDER=tavily` + 有 Key 时发生;
    默认不发任何请求。返回的 `sources` 是公开来源(标题/URL/访问时间/摘录),属于
    `tool` 来源,不是用户事实,也不能直接写计划。
    """
    query = _sanitize_research_query(arguments.get("query"))
    outcome = await research_service.search(db, query)
    #: **服务端验证过的引用。** 它不经过摘要截断,也不进提示词。
    evidence = research_service.research_record(outcome)
    if not outcome.sources:
        note = research_service.REASON_NOTE.get(
            outcome.reason,
            "公开研究没有完成;我没有拿到来源,不要编造。",
        )
        return {
            "configured": False,
            "query": query,
            "reason": outcome.reason,
            "note": note,
            RESEARCH_EVIDENCE_KEY: evidence,
        }
    return {
        **research_service.sources_to_summary(outcome),
        "note": (
            "这些是公开来源。引用外部事实时要标明来源;它只是依据,不能直接写进计划 —— "
            "正式变更仍要提案并由用户确认。"
        ),
        RESEARCH_EVIDENCE_KEY: evidence,
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
) -> ToolExecution:
    """执行一个只读工具。

    返回 `ToolExecution`。**绝不抛异常**:未知工具、非法参数、执行失败都变成
    `rejected`/`error` 的 exchange,让模型看到真实结果。
    """
    spec = TOOL_SPECS.get(name)
    if spec is None:
        return ToolExecution(
            exchange=ToolExchange(
                tool_name=name,
                status="rejected",
                summary={"error": f"不认识工具 {name}。"},
            ),
            sanitized_arguments={},
            ok=False,
        )
    try:
        clean = _validate_arguments(spec, arguments)
        result = await spec.handler(db, ctx, turn, clean)
    except ToolRejected as exc:
        return ToolExecution(
            exchange=ToolExchange(tool_name=name, status="rejected", summary={"error": exc.message}),
            sanitized_arguments=sanitize_arguments(arguments if isinstance(arguments, dict) else {}),
            ok=False,
        )
    except Exception as exc:  # 工具错误归一化成可读摘要
        logger.warning("工具 %s 执行失败: %s", name, exc)
        return ToolExecution(
            exchange=ToolExchange(
                tool_name=name,
                status="error",
                summary={"error": "工具执行失败,没有得到结果。"},
            ),
            sanitized_arguments=sanitize_arguments(arguments if isinstance(arguments, dict) else {}),
            ok=False,
        )
    # 先摘掉**只给服务端**的证据,再截断:公开来源既不能被摘要截断,也不进提示词。
    raw = dict(result)
    evidence = raw.pop(RESEARCH_EVIDENCE_KEY, None)
    summary, truncated = summarize_for_prompt(raw)
    return ToolExecution(
        exchange=ToolExchange(
            tool_name=name,
            arguments=sanitize_arguments(clean),
            status="ok",
            summary=summary,
            truncated=truncated,
        ),
        sanitized_arguments=sanitize_arguments(clean),
        ok=True,
        research=evidence if isinstance(evidence, dict) else None,
    )


__all__ = [
    "MAX_RESULT_ROWS",
    "MAX_SUMMARY_CHARS",
    "RESEARCH_EVIDENCE_KEY",
    "RESEARCH_QUERY_MAX_CHARS",
    "TOOL_NAMES",
    "TOOL_SPECS",
    "ToolExecution",
    "ToolRejected",
    "execute_tool",
    "sanitize_arguments",
    "summarize_for_prompt",
]
