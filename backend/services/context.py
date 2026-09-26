"""请求上下文对象。

## 为什么仓储函数只收 `WorkspaceContext` 而不是裸 `workspace_id`

这是整个权限设计里唯一一处**靠类型系统而不是靠自觉**的地方。

如果函数签名是 `async def list_nodes(db, workspace_id: uuid.UUID)`,那么"调用前必须确认
这个空间属于当前用户"就只是一条写在文档里的纪律 —— 下一个接口、下一个我、或者半年后的
某次改动,只要漏了一次检查,就是一个跨账号读取。而且它不会报错,只会安静地返回别人的数据。

改成 `async def list_nodes(db, ctx: WorkspaceContext)` 之后,调用方**拿不出**一个
`WorkspaceContext`,除非它走过了 `load_workspace_context` —— 那个函数查库时就把
`owner_id == 当前用户` 写进了 WHERE,查不到就抛 `WorkspaceNotFound`。
想绕过校验,就得先绕过类型系统。

`WorkspaceContext` 定义在 services/ 而不是 api/:构造它的是 API 依赖,但**使用**它的是
服务层,所以类型必须住在服务层能 import 的地方(api/ 只做 HTTP,服务层不该 import 它)。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select

from backend.core.security import CurrentUser
from backend.db.models import Workspace
from backend.services.errors import WorkspaceNotFound


@dataclass(frozen=True, slots=True)
class WorkspaceContext:
    """一个已经确认属于当前用户的空间。"""

    workspace: Workspace
    user: CurrentUser

    @property
    def id(self) -> uuid.UUID:
        return self.workspace.id

    @property
    def owner_id(self) -> uuid.UUID:
        return self.workspace.owner_id

    @property
    def timezone(self) -> str:
        """这个空间用哪个时区判断"今天"。"""
        return self.workspace.timezone


async def load_workspace_context(
    db, user: CurrentUser, workspace_id: uuid.UUID
) -> WorkspaceContext:
    """按 id 取空间,**同时**校验归属。

    归属条件写在 SQL 的 WHERE 里,不是取出来之后再比一次:
    - 取出来再比,意味着"忘记比"是一种可能的代码路径;
    - 写进 WHERE,"忘记"这个选项根本不存在。

    "不存在"与"不属于当前用户"返回**同一个** `WorkspaceNotFound`(404)。若后者返回 403,
    任何人拿 id 逐个试就能测绘出系统里有哪些空间存在 —— 403 与 404 的区别本身就是信息。
    """
    result = await db.execute(
        select(Workspace).where(Workspace.id == workspace_id, Workspace.owner_id == user.user_id)
    )
    workspace = result.scalar_one_or_none()
    if workspace is None:
        raise WorkspaceNotFound("没有找到这个成长空间。")
    return WorkspaceContext(workspace=workspace, user=user)
