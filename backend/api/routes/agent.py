"""目标推理 Agent 的地图读取(步骤 1)。

## 为什么是单独一组路由,而不是塞进 messages / questions

"系统自己发起一轮推理"与"用户发了一句话"是两件事。把它们挤进 `POST /messages`
意味着自动探索要伪造一条用户消息;挤进 questions 意味着探索必须挂在一个问题上。
显式的 `/agent/turn` 让**触发来源**成为请求的一部分(步骤 2 接入),前端不需要自己
拼状态。

所有正式计划写入仍然走既有 proposal → 用户确认 → 版本校验 → 事务。
"""

from __future__ import annotations

import json
import re
import urllib.parse
import uuid

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agent.runtime import Reasoner
from backend.api.dependencies.agent import get_reasoner
from backend.api.dependencies.workspace import get_workspace_context
from backend.api.routes.workspaces import turn_response
from backend.contracts.conversation import SendMessageResponse
from backend.contracts.reasoning import (
    AgentTurnRequest,
    AgentTurnResponse,
    GoalReasoningView,
    UpdateReasoningNodeRequest,
    V01FeedbackRequest,
    V1InteractionEventRequest,
)
from backend.contracts.trace import AgentTraceView
from backend.core.config import settings
from backend.db.session import get_db
from backend.services import agent_trace_service, reasoning_service
from backend.services.context import WorkspaceContext
from backend.services.errors import InvalidInput, TraceDisabled

router = APIRouter()


@router.get(
    "/{workspace_id}/reasoning",
    response_model=GoalReasoningView,
    summary="取当前目标推理地图",
)
async def read_reasoning_map(
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: AsyncSession = Depends(get_db),
) -> GoalReasoningView:
    """这个空间的目标推理地图。**只读,不创建会话。**

    新空间返回的是一个空地图(phase=strategic_exploration、没有节点),不是 404 ——
    "还没探索过"和"这个空间不存在"是两回事。真正的探索只发生在显式的
    `POST /agent/turn {trigger: space_entered}`(步骤 2)。
    """
    return await reasoning_service.load_map(db, ctx)


@router.post(
    "/{workspace_id}/agent/turn",
    response_model=AgentTurnResponse,
    summary="显式 Agent turn(自动探索 / 回答后重评 / 战略确认)",
)
async def run_agent_turn(
    payload: AgentTurnRequest,
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: AsyncSession = Depends(get_db),
    reasoner: Reasoner = Depends(get_reasoner),
) -> AgentTurnResponse:
    """一次显式 Agent turn。**幂等由 `idempotencyKey` + `input_version` 共同保证。**

    - `space_entered`:只在未探索、输入版本变化、或上次失败时真正跑模型;
      否则直接返回当前地图(不重复建节点)。
    - `retry`:显式重试上一次失败的探索。
    - `question_answered` / `node_selected` / `user_message` / `strategy_confirmation`:
      增量重评与战略收敛(步骤 4)。
    """
    return await reasoning_service.run_turn(db, ctx, reasoner, payload=payload)


@router.post(
    "/{workspace_id}/v01/feedback",
    response_model=AgentTurnResponse,
    summary="规划智能体 V0.1:记录一条执行反馈",
)
async def v01_feedback(
    payload: V01FeedbackRequest,
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: AsyncSession = Depends(get_db),
) -> AgentTurnResponse:
    """V0.1 周计划的执行反馈:完成 / 部分完成 / 未完成 / 延期。

    写的是任务节点的状态(不新建表、不改历史);完成率 < 60% 时由服务端
    把工作流推进到 REPLANNING,而不是在这里直接改计划。
    """
    from backend.services import v01_service  # 延迟 import,避免循环

    root = await reasoning_service.root_plan_node(db, ctx)
    session = await reasoning_service.get_session(db, ctx)
    if root is None or session is None or session.workflow_stage is None:
        raise InvalidInput("这个空间还没有开始 V0.1 规划流程。")
    trace = agent_trace_service.start_map_trace(
        ctx, trigger="progress_update", context_node_id=root.id
    )
    db.add(trace)
    await db.commit()
    return await v01_service.record_feedback(
        db,
        ctx,
        root,
        session,
        node_id=payload.node_id,
        outcome=payload.outcome,
        trace=trace,
    )


@router.post(
    "/{workspace_id}/agent/v1/strategy/align",
    response_model=AgentTurnResponse,
    summary="规划智能体 V1:确认“战略理解”(进入正式战略确认前的中间确认)",
)
async def v1_align_strategy_understanding(
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: AsyncSession = Depends(get_db),
) -> AgentTurnResponse:
    from backend.services import v1_service

    session = await _v1_session(ctx, db)
    return await v1_service.advance_v1_workflow(
        db, ctx, session, event="strategy_understanding_confirmed"
    )


@router.post(
    "/{workspace_id}/agent/v1/timeline/align",
    response_model=AgentTurnResponse,
    summary="规划智能体 V1:对齐时间节奏(认可默认 / 提出调整)",
)
async def v1_align_timeline(
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: AsyncSession = Depends(get_db),
    reasoner: Reasoner = Depends(get_reasoner),
    answer: str = Query(default="", max_length=2000),
    accepted: bool = Query(default=False),
) -> AgentTurnResponse:
    from backend.services import v1_service

    session = await _v1_session(ctx, db)
    return await v1_service.advance_v1_workflow(
        db,
        ctx,
        session,
        reasoner,
        event="timeline_alignment_confirmed",
        payload={"answer": answer, "accepted": accepted},
    )


@router.post(
    "/{workspace_id}/agent/v1/strategy/confirm",
    response_model=AgentTurnResponse,
    summary="规划智能体 V1:确认战略逻辑(进入时间架构的准备状态)",
)
async def v1_confirm_strategy(
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: AsyncSession = Depends(get_db),
    reasoner: Reasoner = Depends(get_reasoner),
) -> AgentTurnResponse:
    """用户确认阶段一的战略逻辑,并立即生成**待确认**的粗时间架构草案(P3)。

    确认本身不写正式计划;粗时间架构先落成提案,用户确认后才写入阶段。
    """
    from backend.services import v1_service  # 延迟 import,避免循环

    session = await reasoning_service.get_session(db, ctx)
    if session is None or not v1_service.is_v1(session):
        raise InvalidInput("这个空间还没有开始 V1 规划流程。")
    # R1:所有 V1 入口都汇入唯一编排器,由它决定下一阶段。
    return await v1_service.advance_v1_workflow(
        db, ctx, session, reasoner, event="strategy_confirmed"
    )


async def _v1_session(ctx, db):
    from backend.services import v1_service

    session = await reasoning_service.get_session(db, ctx)
    if session is None or not v1_service.is_v1(session):
        raise InvalidInput("这个空间还没有开始 V1 规划流程。")
    return session


@router.post(
    "/{workspace_id}/agent/v1/weekly/generate",
    response_model=AgentTurnResponse,
    summary="规划智能体 V1:生成本周计划与下周预览",
)
async def v1_generate_weekly(
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: AsyncSession = Depends(get_db),
) -> AgentTurnResponse:
    from backend.services import v1_service

    session = await _v1_session(ctx, db)
    trace = agent_trace_service.start_map_trace(
        ctx, trigger="space_entered", context_node_id=session.root_plan_node_id
    )
    db.add(trace)
    await db.commit()
    return await v1_service.advance_v1_workflow(
        db, ctx, session, event="weekly_refinement_requested", trace=trace
    )


@router.post(
    "/{workspace_id}/agent/v1/daily/generate",
    response_model=AgentTurnResponse,
    summary="规划智能体 V1:把本周计划拆成少量工作日工作块",
)
async def v1_generate_daily(
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: AsyncSession = Depends(get_db),
) -> AgentTurnResponse:
    from backend.services import v1_service

    session = await _v1_session(ctx, db)
    return await v1_service.advance_v1_workflow(
        db, ctx, session, event="daily_refinement_requested"
    )


@router.post(
    "/{workspace_id}/agent/v1/review",
    response_model=AgentTurnResponse,
    summary="规划智能体 V1:周末回顾入口(准备未来重规划草案)",
)
async def v1_review(
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: AsyncSession = Depends(get_db),
) -> AgentTurnResponse:
    from backend.services import v1_service

    session = await _v1_session(ctx, db)
    return await v1_service.advance_v1_workflow(db, ctx, session, event="weekly_review_due")


@router.post(
    "/{workspace_id}/agent/v1/feedback",
    response_model=AgentTurnResponse,
    summary="规划智能体 V1:记录一条执行反馈",
)
async def v1_feedback(
    payload: V01FeedbackRequest,
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: AsyncSession = Depends(get_db),
) -> AgentTurnResponse:
    from backend.services import v1_service

    session = await _v1_session(ctx, db)
    return await v1_service.advance_v1_workflow(
        db,
        ctx,
        session,
        event="execution_feedback",
        payload={"node_id": payload.node_id, "outcome": payload.outcome},
    )


@router.post(
    "/{workspace_id}/agent/v1/interaction",
    status_code=204,
    summary="规划智能体 V1:记录居中专注模式的打开 / 收起",
)
async def v1_interaction_event(
    payload: V1InteractionEventRequest,
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: AsyncSession = Depends(get_db),
) -> Response:
    from backend.services import v1_service

    session = await _v1_session(ctx, db)
    await v1_service.record_interaction_event(
        db, ctx, session, interaction_id=payload.interaction_id, event=payload.event
    )
    return Response(status_code=204)


@router.get(
    "/{workspace_id}/agent/v1/audit-export",
    summary="导出 V1 规划决策审计记录(JSON / Markdown)",
)
async def v1_audit_export(
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: AsyncSession = Depends(get_db),
    fmt: str = Query(default="markdown", alias="format", pattern="^(json|markdown)$"),
) -> Response:
    """导出本空间**可审阅的**规划决策记录。

    - 仅 V1 空间可导出;
    - 仅在 `AGENT_AUDIT_EXPORT` 打开时允许(默认生产关闭);
    - 只包含已清洗的可审阅内容 —— 不含思维链、系统提示词、密钥或内部异常。
    """
    from backend.services import audit_service  # 延迟 import,避免循环

    if not settings.agent_audit_export:
        raise InvalidInput("决策审计导出没有开启。")
    session = await reasoning_service.get_session(db, ctx)
    if session is None or session.v1_stage is None:
        raise InvalidInput("这个空间还没有可导出的 V1 规划记录。")

    data = await audit_service.export_json(db, ctx, session)
    if not data["events"]:
        # 空导出不伪造文件 —— 如实告诉用户还没有记录。
        raise InvalidInput("本空间尚未产生可导出的规划记录。")

    # 文件名带空间名与时间戳。HTTP 头只能用 latin-1,所以:英文/数字的可用文件名 +
    # `filename*=UTF-8''` 的完整名(中文空间名不会让响应编码失败)。
    stamp = audit_service.utcnow().strftime("%Y%m%d-%H%M%S")
    raw_base = f"agent-audit-{ctx.workspace.title or 'space'}-{stamp}"
    ascii_base = (
        re.sub(r"[^0-9A-Za-z_.-]+", "-", raw_base).strip("-.")
        or f"agent-audit-{str(ctx.id)[:8]}-{stamp}"
    )
    ext = "json" if fmt == "json" else "md"
    disposition = (
        f'attachment; filename="{ascii_base}.{ext}"; '
        f"filename*=UTF-8''{urllib.parse.quote(raw_base + '.' + ext)}"
    )
    if fmt == "json":
        return Response(
            content=json.dumps(data, ensure_ascii=False, indent=2),
            media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": disposition},
        )
    content = await audit_service.export_markdown(db, ctx, session)
    return Response(
        content=content,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": disposition},
    )


@router.post(
    "/{workspace_id}/agent/v1/direction/select",
    response_model=AgentTurnResponse,
    summary="规划智能体 V1:选择一个候选方向",
)
async def v1_select_direction(
    key: str = Query(min_length=1, max_length=48),
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: AsyncSession = Depends(get_db),
    reasoner: Reasoner = Depends(get_reasoner),
) -> AgentTurnResponse:
    from backend.services import v1_service

    session = await _v1_session(ctx, db)
    return await v1_service.advance_v1_workflow(
        db, ctx, session, reasoner, event="candidate_direction_selected", payload={"key": key}
    )


@router.post(
    "/{workspace_id}/agent/v1/goal/confirm",
    response_model=AgentTurnResponse,
    summary="规划智能体 V1:确认目标定义,进入问题结构",
)
async def v1_confirm_goal_definition(
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: AsyncSession = Depends(get_db),
    reasoner: Reasoner = Depends(get_reasoner),
) -> AgentTurnResponse:
    from backend.services import v1_service

    session = await _v1_session(ctx, db)
    return await v1_service.advance_v1_workflow(
        db, ctx, session, reasoner, event="goal_definition_confirmed"
    )


@router.post(
    "/{workspace_id}/agent/v1/strategy/continue",
    response_model=AgentTurnResponse,
    summary="规划智能体 V1:继续形成战略路径(responseMode=none 的兑底 CTA)",
)
async def v1_continue_strategy(
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: AsyncSession = Depends(get_db),
    reasoner: Reasoner = Depends(get_reasoner),
) -> AgentTurnResponse:
    from backend.services import v1_service

    session = await _v1_session(ctx, db)
    # 继续形成战略路径是 problem_structure 的自动推进分支,交给编排器统一决定。
    return await v1_service.advance_v1_workflow(db, ctx, session, reasoner, trigger="space_entered")


@router.post(
    "/{workspace_id}/agent/v1/direction/reopen",
    response_model=AgentTurnResponse,
    summary="规划智能体 V1:重新选择起点(回到 goal_reframe)",
)
async def v1_reopen_direction(
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: AsyncSession = Depends(get_db),
) -> AgentTurnResponse:
    from backend.services import v1_service

    session = await _v1_session(ctx, db)
    return await v1_service.advance_v1_workflow(
        db, ctx, session, event="direction_reselection_requested"
    )


@router.get(
    "/{workspace_id}/agent/trace",
    response_model=AgentTraceView,
    summary="取最近 Agent turn 的脱敏运行轨迹(本地诊断)",
)
async def read_agent_trace(
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=agent_trace_service.DEFAULT_TRACE_LIMIT, ge=1, le=agent_trace_service.MAX_TRACE_LIMIT),
) -> AgentTraceView:
    """最近若干次 Agent turn 的运行轨迹。

    **按 workspace 授权** —— `get_workspace_context` 已经保证"不属于你的空间"
    返回 404,跨空间读取在这里没有入口。返回的是脱敏投影(见 `contracts/trace.py`),
    不是 ORM 原始 JSON。

    开关关闭时返回 404,与"这个空间不存在"同一个形状 —— 不暴露诊断入口的存在。
    轨迹本身始终由服务端真实边界写入,这里只控制**能不能读到**。
    """
    if not agent_trace_service.trace_ui_enabled():
        raise TraceDisabled("本地诊断入口没有开启。")
    return await agent_trace_service.load_trace(db, ctx, limit=limit)


@router.patch(
    "/{workspace_id}/reasoning/nodes/{node_id}",
    response_model=GoalReasoningView,
    summary="用户编辑一个推理地图节点(标题 / 原文)",
)
async def update_reasoning_node(
    node_id: uuid.UUID,
    payload: UpdateReasoningNodeRequest,
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: AsyncSession = Depends(get_db),
) -> GoalReasoningView:
    """用户改标题或原文。**只改这两列** —— 其余字段由 Agent 维护,且改过的标题
    之后 Agent 不再覆盖(见 `reasoning_service.update_node`)。"""
    return await reasoning_service.update_node(
        db,
        ctx,
        node_id,
        title=payload.title,
        user_description=payload.user_description,
        status=payload.status,
    )


@router.post(
    "/{workspace_id}/agent/strategy/refine",
    response_model=SendMessageResponse,
    summary="细化已确认战略(生成阶段/里程碑/周计划提案)",
)
async def refine_strategy(
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: AsyncSession = Depends(get_db),
    reasoner: Reasoner = Depends(get_reasoner),
) -> SendMessageResponse:
    """**只有已确认战略存在时**才说得上细化阶段。它走的是完整对话工作流,
    在对话里留下一条用户消息与一条助手消息;计划变更仍然要用户确认。"""
    outcome = await reasoning_service.refine_strategy(db, ctx, reasoner)
    return await turn_response(db, ctx, outcome)


__all__ = ["router"]
