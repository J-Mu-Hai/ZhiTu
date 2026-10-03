"""Agent 运行轨迹的读取与脱敏(阶段 9)。

## 这个模块解决的是什么

模型请求停住、超时、结构校验失败、提案版本冲突时,用户与维护者此前只能从对话区
的大段警告反推"卡在哪一步"。这里把 `agent_loop_service` 在**真实执行边界**写下的
`ReasoningState` 轨迹,收敛成一份**只读、脱敏、按 workspace 授权**的诊断视图。

## 三条硬边界

1. **只展示可审阅的状态。** 步骤、耗时、工具名与结果计数、可读终态原因。绝不返回
   模型原始 prompt、隐藏思维链、用户原文、密钥、Cookie 或连接串。
2. **不另造一份状态。** 读的是 `ReasoningState.current_step` / `started_at` /
   `last_progress_at` / `terminal_code` / `safe_summary` —— 这些字段由
   `agent_loop_service` 与 `reasoning_service` 在发模型请求前、工具执行前后、终态处
   写入。前端只读,不猜。
3. **不返回 ORM 原始 JSON。** 工具详情只保留 `tool_name` / `status` / 耗时,以及一个
   由白名单字段拼出的中文摘要;`result_summary` 的原始内容一律不出接口。

## 历史行怎么办

加轨迹字段之前的 `ReasoningState` 没有 `started_at` / `current_step` 细节。它们返回
`status="unavailable"` 与 `step="unavailable"`,**不伪造**实时细节 —— 这正是文档里
"字段不足时返回 unavailable"那条。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.contracts.trace import (
    AgentTraceToolView,
    AgentTraceTurnView,
    AgentTraceView,
)
from backend.core.config import settings
from backend.db.base import utcnow
from backend.db.models import ReasoningState, ToolCallRecord
from backend.db.models.enums import (
    AgentTraceStep,
    DegradedReason,
    ReasoningAction,
    ReasoningStatus,
    ToolCallStatus,
)
from backend.services.context import WorkspaceContext

#: 一次最多返回多少个 turn。**固定上限**,客户端不能放大。
DEFAULT_TRACE_LIMIT = 20
MAX_TRACE_LIMIT = 50

#: 等待模型多久算"仍在等待"并给出一句提示。**只看服务端时间**。
WAITING_MODEL_NOTICE_SECONDS = 25

#: 触发来源 -> 给人看的短标签。与 `AgentTurnTrigger` 以及对话路径的内部来源对齐。
TRIGGER_LABELS: dict[str, str] = {
    "space_entered": "进入空间",
    "user_message": "用户消息",
    "node_selected": "选中节点",
    "question_answered": "回答问题",
    "strategy_confirmation": "确认战略",
    "regenerate_roadmap": "重新生成路线",
    "progress_update": "报告进展",
    "execution_planning": "执行细化",
    "retry": "重试",
    "reanalyze": "重新分析节点",
    "refine": "细化战略",
    "unavailable": "未知来源",
}

#: 步骤 -> 给人看的短标签。
STEP_LABELS: dict[str, str] = {
    AgentTraceStep.QUEUED.value: "排队",
    AgentTraceStep.RESOLVING_CONTEXT.value: "准备上下文",
    AgentTraceStep.WAITING_MODEL.value: "等待模型",
    AgentTraceStep.RUNNING_TOOL.value: "执行工具",
    AgentTraceStep.RETRYING.value: "纠错重试",
    AgentTraceStep.VALIDATING_OUTPUT.value: "校验输出",
    AgentTraceStep.PERSISTING.value: "写入",
    AgentTraceStep.COMPLETED.value: "已完成",
    AgentTraceStep.FAILED.value: "失败",
    AgentTraceStep.TIMED_OUT.value: "超时",
    AgentTraceStep.CANCELLED.value: "已取消",
    "unavailable": "无轨迹",
}

#: 终态错误码 -> **脱敏的**可读原因。键是服务端自己写的闭集,值不含任何模型/用户原文。
TERMINAL_SUMMARIES: dict[str, str] = {
    "MODEL_TIMEOUT": "模型响应超时，本轮没有得出可应用结论，可以重试。",
    "MODEL_OUTPUT_INVALID": "模型返回的内容无法解析，本轮没有写入任何更改。",
    "MODEL_AUTH_FAILED": "模型鉴权失败，请检查服务端配置后再试。",
    "MODEL_RATE_LIMITED": "模型限流，稍后重试即可。",
    "MODEL_UNAVAILABLE": "模型暂时不可用，本轮上下文已保留，可以重试。",
    "NO_API_KEY": "服务端没有配置模型密钥，本轮没有得出结论。",
    "CIRCUIT_OPEN": "模型调用被熔断保护拦下，稍后重试。",
    "OPENJIUWEN_NOT_INSTALLED": "编排层未安装，已回退到直连模型。",
    "BUDGET_EXHAUSTED": "本轮达到模型/工具调用上限，未应用任何未确认更改。",
    "TOOL_ERROR": "只读工具执行失败，本轮记录已保留。",
    "ROADMAP_INVALID": "模型给出的路线结构不合法，本轮没有写入任何节点，可以重试。",
    "PROPOSAL_VALIDATION_FAILED": "这一轮生成的战略提案没有通过校验，计划没有改动。",
    "INTERNAL": "服务端在处理这一轮时出错，本轮没有写入任何更改。",
    "COMPLETED": "本轮已完成，等待你确认或无需更改。",
}

#: **允许出现在 `safe_summary` 里的字符来源只有这张表。** 别处不要往轨迹里写自由文本。
_SAFE_SUMMARY_CODES = frozenset(TERMINAL_SUMMARIES)


def trace_ui_enabled() -> bool:
    """诊断入口是否可用。

    本地开发默认可用;其余环境必须显式打开 `AGENT_TRACE_UI_ENABLED`。关闭时入口与
    读取 API 都不产生任何内容,普通用户看不到。
    """
    if settings.is_development:
        return True
    return bool(settings.agent_trace_ui_enabled)


def safe_terminal_summary(code: str | None, *, fallback: str | None = None) -> str | None:
    """把终态码翻成一句**脱敏**的可读原因。未知码不原样返回。"""
    if code is None:
        return fallback
    return TERMINAL_SUMMARIES.get(code, "本轮没有完成，可以查看状态序列后再决定是否重试。")


def _terminal_from_result(
    *, degraded: bool, degraded_reason: DegradedReason | None, stopped_reason: str
) -> tuple[AgentTraceStep, str]:
    """从真实终态判据推出 (step, terminal_code)。**闭集,不猜。**"""
    if degraded:
        code = degraded_reason.value if degraded_reason is not None else "MODEL_UNAVAILABLE"
        step = AgentTraceStep.TIMED_OUT if code == "MODEL_TIMEOUT" else AgentTraceStep.FAILED
        return step, code
    if stopped_reason == "budget_exhausted":
        return AgentTraceStep.FAILED, "BUDGET_EXHAUSTED"
    return AgentTraceStep.COMPLETED, "COMPLETED"


def _step_for_code(code: str) -> AgentTraceStep:
    """显式终态码 -> 步骤。只允许闭集里的码走到这里。"""
    if code == "MODEL_TIMEOUT":
        return AgentTraceStep.TIMED_OUT
    if code == "COMPLETED":
        return AgentTraceStep.COMPLETED
    return AgentTraceStep.FAILED


def _tool_duration_ms(record: ToolCallRecord) -> int | None:
    if record.finished_at is None or record.created_at is None:
        return None
    return max(0, int((record.finished_at - record.created_at).total_seconds() * 1000))


def _tool_summary(record: ToolCallRecord) -> str:
    """工具记录 -> 一句**脱敏**的中文摘要。**绝不回显 result_summary 的值。**"""
    if record.status is ToolCallStatus.OK:
        label = "成功"
    elif record.status is ToolCallStatus.REJECTED:
        label = "被拒绝"
    else:
        label = "失败"
    extra = ""
    summary = record.result_summary if isinstance(record.result_summary, dict) else {}
    if record.tool_name == "research_public":
        citations = summary.get("citations")
        count = len(citations) if isinstance(citations, list) else 0
        if count:
            extra = f"，{count} 条来源"
        elif summary.get("status") == "blocked":
            label = "被隐私策略拦下"
        elif summary.get("status") in {"failed", "timeout"}:
            label = "未取得来源"
    return f"{record.tool_name}：{label}{extra}"


def _tool_view(record: ToolCallRecord) -> AgentTraceToolView:
    return AgentTraceToolView(
        tool_name=record.tool_name,
        status=record.status.value,
        duration_ms=_tool_duration_ms(record),
        summary=_tool_summary(record),
    )


def _duration_ms(started: datetime | None, finished: datetime | None) -> int | None:
    if started is None or finished is None:
        return None
    return max(0, int((finished - started).total_seconds() * 1000))


def _status_of(state: ReasoningState) -> str:
    if state.started_at is None:
        return "unavailable"
    step = state.current_step
    if step is AgentTraceStep.COMPLETED:
        return "completed"
    if step is AgentTraceStep.TIMED_OUT:
        return "timed_out"
    if step is AgentTraceStep.CANCELLED:
        return "cancelled"
    if step is AgentTraceStep.FAILED:
        return "failed"
    return "running"


def _is_retryable(state: ReasoningState) -> bool:
    """失败且没有已确认写入的 turn 才允许重试。

    已确认的提案写入走的是另一条链路(proposal confirm),不会出现在这里;这里只对
    "模型/工具失败、本轮没有落任何业务更改"的 turn 给重试入口。
    """
    if state.current_step not in (AgentTraceStep.FAILED, AgentTraceStep.TIMED_OUT):
        return False
    if state.terminal_code in {"BUDGET_EXHAUSTED"}:
        # 预算耗尽是"这一轮的设计",重试同样会耗尽;不给注定失败的按钮。
        return False
    return state.terminal_code in TERMINAL_SUMMARIES and state.terminal_code != "COMPLETED"


def build_turn_view(state: ReasoningState, *, now: datetime) -> AgentTraceTurnView:
    """一行 `ReasoningState` -> 一条脱敏 trace。只读字段,不做额外查询。"""
    finished = state.updated_at if state.current_step.is_terminal else None
    status = _status_of(state)
    started = state.started_at
    waiting_seconds = None
    waiting_too_long = False
    if state.current_step is AgentTraceStep.WAITING_MODEL and state.last_progress_at is not None:
        waiting_seconds = max(0, int((now - state.last_progress_at).total_seconds()))
        waiting_too_long = waiting_seconds >= WAITING_MODEL_NOTICE_SECONDS
    return AgentTraceTurnView(
        id=state.id,
        short_id=str(state.id)[:8],
        trigger=state.trigger or "unavailable",
        trigger_label=TRIGGER_LABELS.get(state.trigger or "", "未知来源"),
        current_step=state.current_step.value if started is not None else "unavailable",
        step_label=(
            STEP_LABELS.get(state.current_step.value, "无轨迹")
            if started is not None
            else STEP_LABELS["unavailable"]
        ),
        status=status,
        terminal=state.current_step.is_terminal,
        started_at=started,
        finished_at=finished,
        duration_ms=_duration_ms(started, finished),
        last_progress_at=state.last_progress_at,
        waiting_seconds=waiting_seconds,
        waiting_too_long=waiting_too_long,
        attempt=state.attempt,
        terminal_code=state.terminal_code,
        safe_summary=state.safe_summary,
        steps=[
            str(item.get("step"))
            for item in (state.trace_steps or [])
            if isinstance(item, dict) and item.get("step")
        ],
        tools=[_tool_view(record) for record in state.tool_calls],
        retryable=_is_retryable(state),
    )


async def load_turns(
    db: AsyncSession, ctx: WorkspaceContext, *, limit: int = DEFAULT_TRACE_LIMIT
) -> list[AgentTraceTurnView]:
    """按 workspace 授权,倒序取最近的 turn。**固定上限,不返回 ORM 原始 JSON。**"""
    bounded = max(1, min(MAX_TRACE_LIMIT, limit))
    rows = await db.execute(
        select(ReasoningState)
        # **必须预加载工具记录。** 延迟加载在 async 会话里会抛 MissingGreenlet ——
        # 那不是"偶发慢",是一条注定失败的路径(见错误堆栈)。
        .options(selectinload(ReasoningState.tool_calls))
        .where(ReasoningState.workspace_id == ctx.id)
        .order_by(ReasoningState.created_at.desc())
        .limit(bounded)
    )
    now = utcnow()
    return [build_turn_view(state, now=now) for state in rows.scalars()]


async def load_trace(
    db: AsyncSession, ctx: WorkspaceContext, *, limit: int = DEFAULT_TRACE_LIMIT
) -> AgentTraceView:
    """整个诊断视图。`enabled` 由配置决定;关闭时路由会直接拒绝,这里仍如实返回。"""
    turns = await load_turns(db, ctx, limit=limit)
    return AgentTraceView(
        workspace_id=ctx.id,
        workspace_short_id=str(ctx.id)[:8],
        enabled=trace_ui_enabled(),
        generated_at=utcnow(),
        limit=max(1, min(MAX_TRACE_LIMIT, limit)),
        turns=turns,
    )


# ---------------------------------------------------------------------------------
# 写入辅助(由 `agent_loop_service` / `reasoning_service` 在真实边界调用)
# ---------------------------------------------------------------------------------
def mark_step(
    state: ReasoningState,
    step: AgentTraceStep,
    *,
    now: datetime | None = None,
) -> None:
    """把 state 推进到某一步并打一次心跳。**调用方负责提交。**

    同一步骤连续出现只记一次(重试心跳不刷屏);跨到新步骤时向 `trace_steps` 追加
    一条**只含步骤名与时刻**的记录。
    """
    stamp = now or utcnow()
    previous = state.current_step
    state.current_step = step
    state.last_progress_at = stamp
    if state.started_at is None:
        state.started_at = stamp
    if previous != step:
        state.trace_steps = [
            *(state.trace_steps or []),
            {"step": step.value, "at": stamp.isoformat()},
        ]


def mark_retry(state: ReasoningState, *, now: datetime | None = None) -> None:
    """记录一次**结构化纠错重试**并进入它。

    只在地图轮真的因为路线结构不合法而第二次调用模型时调用。它不改业务数据,
    只把“第一次输出不合法、现在重试”这件事写进真实轨迹。
    """
    state.attempt = max(1, state.attempt) + 1
    mark_step(state, AgentTraceStep.RETRYING, now=now)


def mark_terminal(
    state: ReasoningState,
    *,
    degraded: bool,
    degraded_reason: DegradedReason | None,
    stopped_reason: str,
    code: str | None = None,
    now: datetime | None = None,
) -> None:
    """写入终态 step / code / 脱敏摘要。**这里不会写入任何原始文本。**

    `code` 提供时优先:地图轮的“路线结构不合法”/“战略提案未过校验”不是 `degraded`,
    但同样是可读的失败终态。码必须来自 `TERMINAL_SUMMARIES` 闭集。
    """
    stamp = now or utcnow()
    if code is None:
        step, resolved_code = _terminal_from_result(
            degraded=degraded, degraded_reason=degraded_reason, stopped_reason=stopped_reason
        )
    else:
        resolved_code = code
        step = _step_for_code(code)
    previous = state.current_step
    state.current_step = step
    state.terminal_code = resolved_code
    state.safe_summary = safe_terminal_summary(resolved_code)
    state.last_progress_at = stamp
    if state.started_at is None:
        state.started_at = stamp
    if previous != step:
        state.trace_steps = [
            *(state.trace_steps or []),
            {"step": step.value, "at": stamp.isoformat()},
        ]
    if state.status is ReasoningStatus.EXPLORING:
        # 轨迹终态与业务状态互不覆盖:这里只在业务层没有给结论时补一个诚实状态。
        state.status = (
            ReasoningStatus.RESOLVED
            if step is AgentTraceStep.COMPLETED
            else ReasoningStatus.BLOCKED
        )


def start_map_trace(
    ctx: WorkspaceContext,
    *,
    trigger: str,
    source_message_id: uuid.UUID | None = None,
    context_node_id: uuid.UUID | None = None,
) -> ReasoningState:
    """为一个**地图轮**建一条真实轨迹。**调用方负责 add + commit。**

    它不另建表:写进的就是 `agent_loop_service` 用的那张 `reasoning_states`。
    `question` 存的是触发标签(闭集),不是用户原文。
    """
    state = ReasoningState(
        id=uuid.uuid4(),
        workspace_id=ctx.id,
        source_message_id=source_message_id,
        context_node_id=context_node_id,
        question=TRIGGER_LABELS.get(trigger, "推进当前目标"),
        status=ReasoningStatus.EXPLORING,
        next_action=ReasoningAction.SYNTHESIZE,
        facts_summary=[],
        assumptions=[],
        open_questions=[],
        trace_steps=[],
        trigger=trigger,
        attempt=1,
    )
    mark_step(state, AgentTraceStep.QUEUED)
    mark_step(state, AgentTraceStep.RESOLVING_CONTEXT)
    return state


__all__ = [
    "DEFAULT_TRACE_LIMIT",
    "MAX_TRACE_LIMIT",
    "STEP_LABELS",
    "TERMINAL_SUMMARIES",
    "TRIGGER_LABELS",
    "WAITING_MODEL_NOTICE_SECONDS",
    "build_turn_view",
    "load_trace",
    "load_turns",
    "mark_retry",
    "mark_step",
    "mark_terminal",
    "safe_terminal_summary",
    "start_map_trace",
    "trace_ui_enabled",
]
