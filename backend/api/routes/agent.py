"""目标推理 Agent 的地图读取(步骤 1)。

## 为什么是单独一组路由,而不是塞进 messages / questions

"系统自己发起一轮推理"与"用户发了一句话"是两件事。把它们挤进 `POST /messages`
意味着自动探索要伪造一条用户消息;挤进 questions 意味着探索必须挂在一个问题上。
显式的 `/agent/turn` 让**触发来源**成为请求的一部分(步骤 2 接入),前端不需要自己
拼状态。

所有正式计划写入仍然走既有 proposal → 用户确认 → 版本校验 → 事务。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
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
)
from backend.db.session import get_db
from backend.services import reasoning_service
from backend.services.context import WorkspaceContext

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
