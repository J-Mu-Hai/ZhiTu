"""目标推理 Agent 的地图读取(步骤 1)。

## 为什么是单独一组路由,而不是塞进 messages / questions

"系统自己发起一轮推理"与"用户发了一句话"是两件事。把它们挤进 `POST /messages`
意味着自动探索要伪造一条用户消息;挤进 questions 意味着探索必须挂在一个问题上。
显式的 `/agent/turn` 让**触发来源**成为请求的一部分(步骤 2 接入),前端不需要自己
拼状态。

所有正式计划写入仍然走既有 proposal → 用户确认 → 版本校验 → 事务。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.dependencies.workspace import get_workspace_context
from backend.contracts.reasoning import GoalReasoningView
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


__all__ = ["router"]
