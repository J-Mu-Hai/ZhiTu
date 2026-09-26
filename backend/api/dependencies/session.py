"""场次归属依赖。

## 为什么它和 `get_workspace_context` 是两个依赖

`/api/sessions/{session_id}/...` 这条路径上**没有 `workspace_id`** —— 用户是从
「今天」里点开某一场的,他手上只有场次 id。

服务层的函数收的却是 `WorkspaceContext`(见 services/context.py,那是"归属校验不可
绕过"的实现方式)。所以这里必须先把"这一场属于谁"查出来,再据此把上下文拼出来:

```
session_id ──查(WHERE user_id = 当前用户)──> session.workspace_id ──> WorkspaceContext
```

**中间的归属条件写在 SQL 里**,不是查出来之后再比一次 —— 取出来再比意味着"忘记比"
是一条可能的代码路径。查不到时抛 `SessionNotFound`(404),与"这个 id 不存在"同一个
响应:403 与 404 的区别本身就是信息,拿 id 逐个试就能测出系统里有哪些场次。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.dependencies.auth import AuthContext, get_auth_context
from backend.db.models import ScheduledSession
from backend.db.session import get_db
from backend.services.context import WorkspaceContext, load_workspace_context
from backend.services.errors import SessionNotFound


@dataclass(frozen=True, slots=True)
class SessionContext:
    """一个已经确认属于当前用户的场次,以及它所在的空间。"""

    ctx: WorkspaceContext
    session: ScheduledSession


async def get_session_context(
    session_id: uuid.UUID,
    auth: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SessionContext:
    result = await db.execute(
        select(ScheduledSession).where(
            ScheduledSession.id == session_id,
            ScheduledSession.user_id == auth.user.user_id,
        )
    )
    session = result.scalar_one_or_none()
    if session is None:
        raise SessionNotFound("没有找到这场安排。")

    # 空间本身可能已经被归档 —— 归档是用来隐藏的,不是用来锁死的,所以这里照常放行
    # (与 `get_workspace_context` 的取舍一致)。归档空间里的历史记录仍然要能读。
    ctx = await load_workspace_context(db, auth.user, session.workspace_id)
    return SessionContext(ctx=ctx, session=session)
