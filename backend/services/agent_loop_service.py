"""服务端控制的有界工具循环。

## 它做什么

把「一次模型调用直接产出 reply / questions / actions」升级成:

```
build context
  -> 模型调用(可选择 read tool / ask user / stop / 最终 actions)
  -> 服务端执行只读工具(白名单、预算受限)
  -> 把工具结果回填到下一次模型上下文
  -> 重复,直到 stop / 预算耗尽 / 失败
  -> 只把**最终一轮**的输出交给既有 proposal 链路
```

## 三条硬规则

1. **中间轮的 actions 绝不进 proposal。** 只有终止轮(没有工具请求的那一轮)的
   `actions` 会被 `conversation_service` 拿走。预算耗尽时即使最后一轮带了 actions,
   也会被剥掉并标成 `budget_exhausted`。
2. **不绕过服务层。** 工具执行走 `agent_tools` 注册表,工具只读、不写任何业务数据。
3. **失败/超预算不丢数据。** 用户消息与已回答问题在这之前已经提交;这里把
   `ReasoningState` 置 `blocked` 并保留已经写入的工具记录与事实摘要。

## 为什么不是"多 Agent"

整个循环里只有一个 `Reasoner`。Planner / Investigator / Critic / Synthesizer 是
**同一模型在不同轮次承担的不同窄任务**(见 planning 提示词),不是并行进程,也不是
多智能体通信。省下的复杂度全部用在"可恢复、可审计、预算受限"上。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, replace

from sqlalchemy.ext.asyncio import AsyncSession

from backend.agent.runtime.base import ReasoningResult, ToolExchange, TurnContext
from backend.core.config import settings
from backend.db.base import utcnow
from backend.db.models import ReasoningState, ToolCallRecord
from backend.db.models.enums import (
    AgentTraceStep,
    DegradedReason,
    ModelSource,
    ReasoningAction,
    ReasoningStatus,
    ToolCallStatus,
)
from backend.services import agent_tools, agent_trace_service, research_service
from backend.services.context import WorkspaceContext

logger = logging.getLogger(__name__)

#: 一轮对话循环的预算。**服务端常量,不交给模型。**
MAX_MODEL_CALLS = 3
MAX_TOOL_CALLS = 4

#: 终止那一轮的 stopReason 到 state 状态的映射。
_TERMINAL_STATUS = {
    "ready_to_propose": ReasoningStatus.RESOLVED,
    "need_user_answer": ReasoningStatus.RESOLVED,
    "insufficient_evidence": ReasoningStatus.BLOCKED,
    "failed": ReasoningStatus.BLOCKED,
    "budget_exhausted": ReasoningStatus.BLOCKED,
}


@dataclass(frozen=True, slots=True)
class LoopOutcome:
    """一次有界循环的结果。`result` 是**唯一**允许进入 proposal 的那一份。"""

    result: ReasoningResult
    state: ReasoningState
    model_calls: int
    tool_calls: int
    stopped_reason: str
    #: 本轮真实执行的公开研究引用记录(见 `research_service.research_record`)。
    #: **只含服务端验证过的来源**,没有查过时为 None。
    research: dict | None = None


def _tool_record(
    state: ReasoningState,
    exchange: ToolExchange,
    sanitized: dict,
    *,
    sequence: int,
    ok: bool,
) -> ToolCallRecord:
    if ok:
        status = ToolCallStatus.OK
    elif exchange.status == "rejected":
        status = ToolCallStatus.REJECTED
    else:
        status = ToolCallStatus.ERROR
    error = None
    if not ok:
        raw_error = exchange.summary.get("error") if isinstance(exchange.summary, dict) else None
        error = str(raw_error) if raw_error else "工具没有给出结果。"
    now = utcnow()
    return ToolCallRecord(
        workspace_id=state.workspace_id,
        reasoning_state=state,
        sequence=sequence,
        tool_name=exchange.tool_name,
        sanitized_arguments=sanitized,
        status=status,
        result_summary=exchange.summary if ok else None,
        error_summary=error,
        created_at=now,
        finished_at=now,
    )


def _seed_state(
    ctx: WorkspaceContext,
    *,
    source_message_id: uuid.UUID | None,
    context_node_id: uuid.UUID | None,
    user_message: str,
) -> ReasoningState:
    return ReasoningState(
        id=uuid.uuid4(),
        workspace_id=ctx.id,
        source_message_id=source_message_id,
        context_node_id=context_node_id,
        question=user_message.strip()[:500] or "推进当前目标",
        status=ReasoningStatus.EXPLORING,
        next_action=ReasoningAction.SYNTHESIZE,
        facts_summary=[],
        assumptions=[],
        open_questions=[],
    )


def _prefer_research(current: dict | None, incoming: dict) -> dict:
    """保留第一次拿到来源的成功记录。

    一轮里可能请求多次 `research_public`(只有第一次会真的出网)。后续的限额/拦截
    记录**不能遮掉**已经拿到的来源;而已有来源时,后续成功也应保留第一次的那份。
    """
    if current is None:
        return incoming
    if not current.get("citations") and incoming.get("citations"):
        return incoming
    return current


def _absorb_result(state: ReasoningState, result: ReasoningResult) -> None:
    """把模型这一轮的**可审计摘要**写进 state。**不写原始 CoT。**"""
    facts: list[dict] = list(state.facts_summary)
    for claim in result.brief_claims:
        if getattr(claim, "is_user_stated", False):
            facts.append(
                {
                    "text": f"{claim.field}={claim.value}",
                    "source": "user",
                    "at": utcnow().isoformat(),
                }
            )
    for exchange in getattr(result, "tool_exchanges", ()):  # pragma: no cover - 兼容
        facts.append({"text": exchange.tool_name, "source": "tool", "at": utcnow().isoformat()})
    analysis = result.analysis
    assumptions: list[dict] = list(state.assumptions)
    open_questions: list[dict] = list(state.open_questions)
    if analysis is not None:
        for item in analysis.known:
            facts.append({"text": item, "source": "model_inference", "at": utcnow().isoformat()})
        for item in analysis.assumptions:
            assumptions.append({"text": item, "source": "assumption", "at": utcnow().isoformat()})
        for item in analysis.unknowns:
            open_questions.append({"text": item, "source": "model_inference", "at": utcnow().isoformat()})
    for question in result.questions:
        open_questions.append(
            {"text": question.question, "source": "model_inference", "at": utcnow().isoformat()}
        )
    # 只保留最近的一批,state 是摘要不是日志。
    state.facts_summary = facts[-40:]
    state.assumptions = assumptions[-20:]
    state.open_questions = open_questions[-20:]


async def run_turn(
    db: AsyncSession,
    ctx: WorkspaceContext,
    reasoner,
    *,
    turn: TurnContext,
    source_message_id: uuid.UUID | None,
    context_node_id: uuid.UUID | None,
    trigger: str = "user_message",
    attempt: int = 1,
) -> LoopOutcome:
    """跑一次有界循环。

    ## 事务口径(阶段 9 起)

    此前这里是"**不 commit** —— 调用方在同一个事务里提交"。为了满足运行轨迹"模型请求
    仍在等待时就能读到 waiting_model",现在把**轨迹行本身**在发模型请求之前单独提交。
    这样做同时解决了一个旧约束:第一次模型调用发生在一个**没有写锁**的事务里,用户
    在另一个标签页改计划不会撞 `database is locked`。业务写入(助手消息、提案、问题)
    仍由调用方在最后统一提交。
    """
    state = _seed_state(
        ctx,
        source_message_id=source_message_id,
        context_node_id=context_node_id,
        user_message=turn.user_message,
    )
    state.trigger = trigger
    state.attempt = attempt
    # 上下文在调用方进入本函数**之前**就已经建好(见 `conversation_service`),
    # 所以这里从 queued 直接推到 resolving_context,再推到 waiting_model。
    started = utcnow()
    agent_trace_service.mark_step(state, AgentTraceStep.RESOLVING_CONTEXT, now=started)
    state.started_at = started
    db.add(state)

    exchanges: list[ToolExchange] = []
    model_calls = 0
    tool_calls = 0
    #: 本轮已发生的真实公网检索次数(受 `RESEARCH_MAX_CALLS_PER_TURN` 限制)。
    research_calls = 0
    #: 本轮公开研究的引用记录(服务端验证)。
    research_signal: dict | None = None
    final: ReasoningResult | None = None

    try:
        for iteration in range(MAX_MODEL_CALLS):
            current_turn = replace(turn, tool_exchanges=tuple(exchanges))
            model_calls += 1
            # **发请求前就写 waiting_model 并提交。** 这样模型卡住时,诊断入口能读到
            # "正在等模型"和最后一次心跳,而不是一片空白。
            agent_trace_service.mark_step(state, AgentTraceStep.WAITING_MODEL)
            state.model_calls_used = model_calls
            await db.commit()
            # `Reasoner.reason` 对上游失败永不抛异常(见 base 模块契约)。
            result = await reasoner.reason(current_turn)

            if result.degraded:
                # 模型不可用:保留 state=blocked,不伪造结论/工具结果。
                state.status = ReasoningStatus.BLOCKED
                state.next_action = ReasoningAction.STOP
                _absorb_result(state, result)
                agent_trace_service.mark_terminal(
                    state,
                    degraded=True,
                    degraded_reason=result.degraded_reason,
                    stopped_reason="failed",
                )
                await db.commit()
                return LoopOutcome(
                    result=result,
                    state=state,
                    model_calls=model_calls,
                    tool_calls=tool_calls,
                    stopped_reason="failed",
                    research=research_signal,
                )

            tool_budget_left = tool_calls < MAX_TOOL_CALLS
            can_continue = iteration < MAX_MODEL_CALLS - 1
            if result.tool_requests and tool_budget_left and can_continue:
                state.next_action = ReasoningAction.READ_TOOL
                for request in result.tool_requests:
                    if tool_calls >= MAX_TOOL_CALLS:
                        break
                    if request.name == "research_public" and research_calls >= settings.research_max_calls_per_turn:
                        # 每轮默认最多一次真实公网检索:超出的直接拒,不发请求。
                        exchange = ToolExchange(
                            tool_name="research_public",
                            status="rejected",
                            summary={"error": "每一轮最多允许一次真实公网检索。"},
                        )
                        sanitized = {}
                        ok = False
                        research_signal = _prefer_research(
                            research_signal, research_service.uncited_record("limited")
                        )
                    else:
                        agent_trace_service.mark_step(state, AgentTraceStep.RUNNING_TOOL)
                        await db.commit()
                        execution = await agent_tools.execute_tool(
                            db, ctx, current_turn, name=request.name, arguments=request.arguments
                        )
                        exchange = execution.exchange
                        sanitized = execution.sanitized_arguments
                        ok = execution.ok
                        if request.name == "research_public":
                            research_calls += 1
                            # 成功/缓存带 citations;隐私拦截与工具错误没有 provider 结果,
                            # 按 blocked / failed 如实记录。
                            record = execution.research or research_service.uncited_record(
                                "blocked" if exchange.status == "rejected" else "failed"
                            )
                            research_signal = _prefer_research(research_signal, record)
                    tool_calls += 1
                    exchanges.append(exchange)
                    db.add(
                        _tool_record(
                            state, exchange, sanitized, sequence=tool_calls, ok=ok
                        )
                    )
                    # 工具事实以 `tool` 来源进入 state。
                    if ok:
                        state.facts_summary = [
                            *state.facts_summary,
                            {
                                "text": f"{exchange.tool_name}: {request.reason or '读取事实'}",
                                "source": "tool",
                                "at": utcnow().isoformat(),
                            },
                        ]
                state.tool_calls_used = tool_calls
                _absorb_result(state, result)
                continue

            # ---- 终止轮 ----
            if result.tool_requests:
                # 想调工具但预算/轮次不允许:剥掉 actions,标成预算耗尽。
                result = replace(result, actions=(), stop_reason="budget_exhausted")
            reason = result.stop_reason or (
                "need_user_answer" if result.questions else "ready_to_propose"
            )
            agent_trace_service.mark_step(state, AgentTraceStep.VALIDATING_OUTPUT)
            state.status = _TERMINAL_STATUS.get(reason, ReasoningStatus.RESOLVED)
            state.next_action = (
                ReasoningAction.ASK_USER
                if reason == "need_user_answer"
                else ReasoningAction.SYNTHESIZE
            )
            if state.status is ReasoningStatus.RESOLVED:
                state.conclusion = (result.reply or "").strip()[:1000]
            _absorb_result(state, result)
            final = result
            break

        if final is None:  # pragma: no cover - 循环结构上保证不会走到
            final = ReasoningResult(
                reply="这一轮没有得出结论。",
                source=ModelSource.UNAVAILABLE,
                degraded=True,
                degraded_reason=DegradedReason.MODEL_UNAVAILABLE,
            )
            state.status = ReasoningStatus.BLOCKED
            state.next_action = ReasoningAction.STOP

        agent_trace_service.mark_terminal(
            state,
            degraded=bool(final.degraded),
            degraded_reason=final.degraded_reason,
            stopped_reason=final.stop_reason or "ready_to_propose",
        )
        await db.commit()
        return LoopOutcome(
            result=final,
            state=state,
            model_calls=model_calls,
            tool_calls=tool_calls,
            stopped_reason=final.stop_reason or "ready_to_propose",
            research=research_signal,
        )
    except Exception:
        # 真实 Reasoner 按契约不抛;这里只兜"契约被违反"的结构性 bug。**必须重新抛**
        # 让调用方看到失败,同时把轨迹停在可解释的 failed,而不是永远停在 waiting_model。
        try:
            agent_trace_service.mark_terminal(
                state,
                degraded=True,
                degraded_reason=None,
                stopped_reason="failed",
            )
            state.terminal_code = "INTERNAL"
            state.safe_summary = agent_trace_service.safe_terminal_summary("INTERNAL")
            await db.commit()
        except Exception:  # pragma: no cover - 兜底提交失败不该遮住原始异常
            logger.exception("写入 Agent 轨迹失败")
        raise


__all__ = [
    "MAX_MODEL_CALLS",
    "MAX_TOOL_CALLS",
    "LoopOutcome",
    "run_turn",
]
