"""规划智能体 V1 的**决策审计记录与导出**(P5)。

## 记录什么

用户可理解、可审计、可复现的决策过程:用户输入、所处阶段、AI 可见判断、焦点与理由、
本轮一个关键问题、更新了哪些节点、用户事实 / AI 假设 / 公开依据、校验/守卫/失败、
提案生成与确认、状态前后变化。

## **绝不**记录

API Key / Bearer Token / 密码 / 连接串 / 原始系统提示词 / 隐藏思维链 / 未清洗的工具
参数 / 私密研究查询 / 堆栈或内部路径。所有入库内容(含用户输入)都过 `_redact`,
命中敏感模式写 `[已脱敏]`。

## 事务与顺序

`record` **只 flush,不 commit** —— 它与对应的领域写入处于同一事务;领域操作回滚时,
审计事件一起回滚,不会伪造“已成功应用”。失败事件在调用方的失败分支里随状态一起提交。

`sequence` 按空间单调递增;`autoflush=False`,所以 `record` 里显式 flush,保证同一事务
内连续多条事件序号不重复。
"""

from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.base import utcnow
from backend.db.models import (
    AgentAuditEvent,
    AgentQuestion,
    GoalReasoningSession,
    PlanNode,
    Proposal,
)
from backend.db.models.enums import ProposalStatus
from backend.services.context import WorkspaceContext

#: 允许的审计事件类型。**闭集** —— 有事件发生就必须能导出;未知类型不写。
AUDIT_EVENT_TYPES = frozenset(
    {
        "space_entered",
        "initial_thinking_submitted",
        "global_assessment_generated",
        "global_question_asked",
        "canvas_question_opened",
        "canvas_question_answered",
        "node_analysis_updated",
        "strategy_draft_generated",
        "strategy_confirmed",
        "coarse_timeline_draft_generated",
        "timeline_proposal_created",
        "timeline_confirmed",
        "weekly_plan_proposal_created",
        "weekly_plan_confirmed",
        "daily_plan_proposal_created",
        "daily_plan_confirmed",
        "execution_feedback_recorded",
        "weekly_review_started",
        "replan_proposal_created",
        "replan_confirmed",
        "model_unavailable",
        "model_output_invalid",
        "guard_rejected",
        "proposal_validation_failed",
        # P2.1:战略判断优先、有限追问。
        "user_message_received",
        "strategic_thesis_generated",
        "candidate_directions_offered",
        "candidate_direction_selected",
        "provisional_synthesis_created",
        "conversation_feedback_received",
        "goal_definition_confirmed",
        # P2.3:消除 problem_structure 空转,建立战略路径自动推进。
        "problem_structure_entered",
        "problem_structure_synthesized",
        "strategy_review_ready",
        "direction_reselection_started",
        "strategy_continue_triggered",
        # V1 工作流编排器:每次自动推进 / 阻塞 / 超时都留痕。
        "v1_workflow_advanced",
        "v1_workflow_blocked",
        "v1_step_timed_out",
        # R1:V1 要求真实 OpenJiuwen;来源不满足时准确失败,不静默降级。
        "v1_openjiuwen_required",
    }
)

_REDACTED = "[已脱敏]"

#: 敏感模式。命中即整段替换为 `[已脱敏]`。顺序有意义:先处理带前缀的密钥/令牌,
#: 再处理连接串与赋值,最后处理邮箱/电话。
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-[A-Za-z0-9_\-]{6,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]+"),
    re.compile(r"(?i)(?:postgres(?:ql)?|mysql|redis|mongodb|sqlite)://[^\s\"'<>]+"),
    re.compile(r"(?i)(?:api[_-]?key|access[_-]?key|auth[_-]?token|token|password|secret)\s*[:=]\s*[^\s,;\"']+"),
    re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"),
    re.compile(r"(?<!\d)(?:\+?86)?1[3-9]\d{9}(?!\d)"),
    # 长随机串(32+ 位无空格)按疑似令牌处理。会误伤偶发的长标识,宁可多脱敏。
    re.compile(r"\b[A-Za-z0-9_\-]{32,}\b"),
)


def redact_text(value: str | None) -> str | None:
    """把命中敏感模式的内容替换为 `[已脱敏]`。空值原样返回。"""
    if not value:
        return value
    text = value
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(_REDACTED, text)
    return text


def _redact(value):
    """递归脱敏 dict / list / str。其它类型原样返回(只允许 JSON 安全类型)。"""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {str(key): _redact(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value


async def _next_sequence(db: AsyncSession, workspace_id) -> int:
    current = await db.scalar(
        select(func.max(AgentAuditEvent.sequence)).where(
            AgentAuditEvent.workspace_id == workspace_id
        )
    )
    return int(current or 0) + 1


async def record(
    db: AsyncSession,
    ctx: WorkspaceContext,
    session: GoalReasoningSession | None,
    *,
    event_type: str,
    trigger: str | None = None,
    stage_before: str | None = None,
    stage_after: str | None = None,
    focus_key: str | None = None,
    focus_reason: str | None = None,
    summary: str | None = None,
    payload: dict | None = None,
    validation_status: str = "ok",
    error_code: str | None = None,
    source: str | None = None,
) -> AgentAuditEvent | None:
    """写一条审计事件。**只 flush 不 commit**,与领域写入同事务。"""
    if event_type not in AUDIT_EVENT_TYPES:
        return None
    event = AgentAuditEvent(
        workspace_id=ctx.id,
        goal_reasoning_session_id=session.id if session is not None else None,
        sequence=await _next_sequence(db, ctx.id),
        event_type=event_type,
        occurred_at=utcnow(),
        stage_before=stage_before,
        stage_after=stage_after,
        trigger=trigger,
        source=source,
        focus_key=focus_key,
        focus_reason=redact_text(focus_reason),
        summary=redact_text(summary),
        payload_json=_redact(payload) if payload is not None else None,
        validation_status=validation_status,
        error_code=error_code,
    )
    db.add(event)
    await db.flush()
    return event


# =================================================================================
# 导出
# =================================================================================
def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


async def _snapshot(db: AsyncSession, ctx: WorkspaceContext, session) -> dict:
    """当前快照:阶段、焦点、分组/问题状态、战略、时间线、周计划、未处理提案、错误。"""
    groups = list(
        await db.scalars(
            select(PlanNode)
            .where(PlanNode.workspace_id == ctx.id, PlanNode.v1_key.is_not(None))
            .order_by(PlanNode.order_index.asc())
        )
    )
    questions = list(
        await db.scalars(
            select(AgentQuestion)
            .where(AgentQuestion.workspace_id == ctx.id, AgentQuestion.v1_key.is_not(None))
            .order_by(AgentQuestion.created_at.asc())
        )
    )
    proposals = list(
        await db.scalars(
            select(Proposal)
            .where(
                Proposal.workspace_id == ctx.id,
                Proposal.status.in_(
                    (ProposalStatus.VALIDATED, ProposalStatus.PENDING_CONFIRMATION)
                ),
            )
            .order_by(Proposal.created_at.asc())
        )
    )
    weeks = [
        node
        for node in await db.scalars(
            select(PlanNode).where(
                PlanNode.workspace_id == ctx.id, PlanNode.deleted_at.is_(None)
            )
        )
        if str(node.title).startswith(("本周计划", "下周预览"))
    ]
    #: 可见分析维度 / 隐藏维度数 / 真实待回答问题数。**内部十维 ≠ 十个待回答问题。**
    visible_keys: list[str] = []
    hidden_count = 0
    pending_questions = 0
    if session is not None:
        # 延迟 import,避免 services 之间的循环。
        from backend.services import v1_service

        visible_keys = sorted(v1_service.visible_dimension_keys(session))
        hidden_count = sum(
            1 for item in v1_service.dimension_projection(session) if not item["visible"]
        )
        pending_questions = v1_service.actual_pending_question_count(session)
    return {
        "v1Stage": session.v1_stage if session is not None else None,
        "v1Status": session.v1_status if session is not None else None,
        "v1Error": session.v1_error if session is not None else None,
        "strategicThesis": session.v1_strategic_thesis if session is not None else None,
        "focusKey": session.v1_focus_key if session is not None else None,
        "selectedDirection": session.v1_selected_direction if session is not None else None,
        "candidateDirections": session.v1_candidate_directions if session is not None else None,
        "visibleAnalysisDimensionKeys": visible_keys,
        "hiddenAnalysisDimensionCount": hidden_count,
        "actualPendingQuestionCount": pending_questions,
        "focus": (
            {"key": session.v1_focus_key, "reason": session.v1_focus_reason}
            if session is not None and session.v1_focus_key
            else None
        ),
        "strategy": session.v1_strategy if session is not None else None,
        "timeline": [
            _redact(item) for item in (session.v01_timeline or []) if isinstance(item, dict)
        ]
        if session is not None
        else [],
        "groups": [
            {"key": group.v1_key, "title": group.title, "status": group.status.value}
            for group in groups
        ],
        "questions": [
            {
                "key": question.v1_key,
                "question": redact_text(question.question),
                "status": question.status.value,
                "presentation": question.presentation.value,
                "summary": redact_text(question.analysis_summary),
                "v1Analysis": _redact(question.v1_analysis),
            }
            for question in questions
        ],
        "weeklyPlans": [
            {"id": str(week.id), "title": redact_text(week.title), "status": week.status.value}
            for week in weeks
        ],
        "openProposals": [
            {
                "id": str(proposal.id),
                "status": proposal.status.value,
                "triggerType": proposal.trigger_type.value,
                "reasoning": redact_text(proposal.reasoning),
                "changeSummary": _redact(proposal.change_summary),
            }
            for proposal in proposals
        ],
    }


async def _events(db: AsyncSession, ctx: WorkspaceContext) -> list[AgentAuditEvent]:
    return list(
        await db.scalars(
            select(AgentAuditEvent)
            .where(AgentAuditEvent.workspace_id == ctx.id)
            .order_by(AgentAuditEvent.sequence.asc())
        )
    )


def _event_dict(event: AgentAuditEvent) -> dict:
    return {
        "sequence": event.sequence,
        "eventType": event.event_type,
        "occurredAt": _iso(event.occurred_at),
        "stageBefore": event.stage_before,
        "stageAfter": event.stage_after,
        "trigger": event.trigger,
        "source": event.source,
        "focus": (
            {"key": event.focus_key, "reason": event.focus_reason}
            if event.focus_key
            else None
        ),
        "summary": event.summary,
        "payload": event.payload_json or {},
        "validationStatus": event.validation_status,
        "errorCode": event.error_code,
    }


async def export_json(db: AsyncSession, ctx: WorkspaceContext, session) -> dict:
    events = await _events(db, ctx)
    return {
        "exportVersion": 1,
        "generatedAt": _iso(utcnow()),
        "workspace": {"id": str(ctx.id), "title": ctx.workspace.title or ""},
        "session": {
            "id": str(session.id) if session is not None else None,
            "v1Stage": session.v1_stage if session is not None else None,
            "status": session.v1_status if session is not None else None,
        },
        "events": [_event_dict(event) for event in events],
        "currentSnapshot": await _snapshot(db, ctx, session),
        "note": None if events else "本空间尚未产生可导出的规划记录。",
    }


def _md_list(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items) if items else "- （无）"


async def export_markdown(db: AsyncSession, ctx: WorkspaceContext, session) -> str:
    events = await _events(db, ctx)
    snapshot = await _snapshot(db, ctx, session)
    lines: list[str] = []
    lines.append("# 规划决策记录")
    lines.append("")
    lines.append(f"- 空间：{ctx.workspace.title or '(未命名)'}")
    lines.append(f"- 导出时间：{_iso(utcnow())}")
    lines.append(f"- 当前阶段：{snapshot['v1Stage'] or '（未开始）'}")
    lines.append(f"- 当前状态：{snapshot['v1Status'] or '（无）'}")
    lines.append("")
    lines.append("## 当前结论")
    if snapshot.get("strategicThesis"):
        lines.append(f"- 当前战略判断：{snapshot['strategicThesis']}")
    if snapshot.get("selectedDirection"):
        lines.append(f"- 用户选择的候选方向：{snapshot['selectedDirection']}")
    lines.append(
        f"- 用户真正需要回答的问题：{snapshot.get('actualPendingQuestionCount', 0)} 个"
    )
    lines.append(
        f"- 内部分析维度：可见 {len(snapshot.get('visibleAnalysisDimensionKeys') or [])} 个，"
        f"隐藏 {snapshot.get('hiddenAnalysisDimensionCount', 0)} 个"
        "（其余只是 AI 的内部思考框架，不需要你逐条回答）"
    )
    if snapshot.get("visibleAnalysisDimensionKeys"):
        lines.append(
            "- 当前展示的分析维度："
            + "、".join(snapshot["visibleAnalysisDimensionKeys"])
        )
    focus = snapshot.get("focus")
    lines.append(f"- 当前焦点：{focus['key'] if focus else '（无）'}")
    if focus and focus.get("reason"):
        lines.append(f"- 焦点理由：{focus['reason']}")
    strategy = snapshot.get("strategy") or {}
    for field, label in (
        ("mainLine", "主线"),
        ("parallelLine", "并行线"),
        ("deferOrAvoid", "暂缓/放弃"),
        ("riskControl", "风险控制"),
        ("tradeoff", "取舍"),
    ):
        if strategy.get(field):
            lines.append(f"- 战略{label}：{strategy[field]}")
    lines.append("")

    lines.append("## 决策时间线")
    lines.append("")
    if not events:
        lines.append("本空间尚未产生可导出的规划记录。")
    for index, event in enumerate(events, start=1):
        lines.append(f"### #{index:02d} {event.event_type}")
        lines.append(f"- 时间：{_iso(event.occurred_at)}")
        if event.stage_before or event.stage_after:
            lines.append(f"- 阶段：{event.stage_before or '—'} → {event.stage_after or '—'}")
        if event.trigger:
            lines.append(f"- 触发：{event.trigger}")
        if event.summary:
            lines.append(f"- 摘要：{event.summary}")
        if event.focus_key:
            lines.append(f"- 焦点：{event.focus_key}（{event.focus_reason or '—'}）")
        if event.validation_status == "failed":
            lines.append(f"- **失败**：{event.error_code or 'unknown'}")
        payload = event.payload_json or {}
        facts = payload.get("facts") or []
        assumptions = payload.get("assumptions") or []
        evidence = payload.get("evidence") or []
        pending = payload.get("pendingConfirmation") or []
        # **四类是固定栏目** —— 即使为空也要写出来,读者才能明确区分事实/假设/依据/待确认。
        lines.append(f"- 用户事实：{'；'.join(str(x) for x in facts) if facts else '（无）'}")
        lines.append(f"- AI 假设：{'；'.join(str(x) for x in assumptions) if assumptions else '（无）'}")
        lines.append(f"- 公开依据：{'；'.join(str(x) for x in evidence) if evidence else '（无）'}")
        lines.append(f"- 待确认事项：{'；'.join(str(x) for x in pending) if pending else '（无）'}")
        updates = payload.get("nodeUpdates") or []
        for update in updates:
            if not isinstance(update, dict):
                continue
            lines.append(
                f"- 节点更新：{update.get('key')} "
                f"({update.get('beforeStatus') or '—'} → {update.get('afterStatus') or '—'})"
            )
            if update.get("summary"):
                lines.append(f"  - 判断：{update['summary']}")
            if update.get("facts"):
                lines.append(f"  - 用户事实：{'；'.join(str(x) for x in update['facts'])}")
            if update.get("assumptions"):
                lines.append(f"  - AI 假设：{'；'.join(str(x) for x in update['assumptions'])}")
        if payload.get("proposal"):
            proposal = payload["proposal"]
            lines.append(
                f"- 提案：{proposal.get('kind')} · {proposal.get('status')} · {proposal.get('id')}"
            )
        if payload.get("question"):
            lines.append(f"- 关键问题：{payload['question']}")
        lines.append("")

    lines.append("## 当前画布摘要")
    lines.append("")
    lines.append("### 分组")
    lines.append(_md_list([f"{g['key']} · {g['title']}（{g['status']}）" for g in snapshot["groups"]]))
    lines.append("")
    lines.append("### 紫色画布问题节点")
    purple = [q for q in snapshot["questions"] if q["presentation"] == "canvas_question"]
    lines.append(
        _md_list(
            [
                f"{q['key']} · {q['question']}（{q['status']}）"
                + (f" — {q['summary']}" if q["summary"] else "")
                for q in purple
            ]
        )
    )
    lines.append("")

    lines.append("## 时间线 / 周计划 / 重规划状态")
    lines.append("")
    if snapshot["timeline"]:
        for item in snapshot["timeline"]:
            lines.append(
                f"- {item.get('title')}（{item.get('status')}，第 {item.get('startWeek')}–{item.get('endWeek')} 周）"
            )
    else:
        lines.append("- 还没有时间架构。")
    lines.append("")
    lines.append("### 周计划")
    lines.append(
        _md_list([f"{w['title']}（{w['status']}）" for w in snapshot["weeklyPlans"]])
    )
    lines.append("")
    lines.append("### 未处理提案")
    lines.append(
        _md_list(
            [
                f"{p['triggerType']} · {p['status']} · {p['id']}"
                + (f" — {p['reasoning']}" if p["reasoning"] else "")
                for p in snapshot["openProposals"]
            ]
        )
    )
    lines.append("")

    lines.append("## 失败与守卫记录")
    lines.append("")
    failures = [event for event in events if event.validation_status == "failed"]
    lines.append(
        _md_list(
            [
                f"#{event.sequence} {event.event_type} · {event.error_code or 'unknown'} — {event.summary or ''}"
                for event in failures
            ]
        )
    )
    lines.append("")
    return "\n".join(lines)


__all__ = [
    "AUDIT_EVENT_TYPES",
    "export_json",
    "export_markdown",
    "record",
    "redact_text",
]
