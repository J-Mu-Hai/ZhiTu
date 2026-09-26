"""空间归属依赖。

**所有接受 `{workspace_id}` 的路由都必须先过这一关**,拿到的 `WorkspaceContext`
才允许传给服务层。服务层的函数签名收的就是这个类型(见 services/context.py),
所以"忘了校验归属"不是一条会被忽略的疏漏,而是一段编译不过的代码。
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.dependencies.auth import AuthContext, get_auth_context
from backend.db.session import get_db
from backend.services.context import WorkspaceContext, load_workspace_context


async def get_workspace_context(
    workspace_id: uuid.UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WorkspaceContext:
    """按路径参数取空间,顺带校验它属于当前用户。

    已归档的空间**照常返回** —— 归档是用来隐藏的,不是用来锁死的。用户需要能重新
    打开它、改回 active。列表接口用 `includeArchived` 决定要不要列出来。
    """
    return await load_workspace_context(db, ctx.user, workspace_id)
