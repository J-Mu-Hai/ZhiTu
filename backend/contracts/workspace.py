"""成长空间的 HTTP 契约。

字段与前端既有的 `WorkspaceSummary`(`id / title / intent / createdAt`)对齐 ——
空间列表页因此不必跟着改。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import Field, field_validator

from backend.contracts.common import ApiModel
from backend.db.models.enums import NodeStatus, NodeType, WorkspaceStatus

TITLE_MAX = 120
INTENT_MAX = 500


class CreateWorkspaceRequest(ApiModel):
    title: str = Field(min_length=1, max_length=TITLE_MAX)
    intent: str = Field(default="", max_length=INTENT_MAX)
    #: 根目标节点的标题。不传时用 title —— 用户先把空间命名成"Python 学习",
    #: 再说"三个月内做出一个项目",这两句话哪个是目标由用户决定,不由系统替他挑。
    goal: str | None = Field(default=None, max_length=200)

    @field_validator("title")
    @classmethod
    def _check_title(cls, value: str) -> str:
        title = value.strip()
        if not title:
            raise ValueError("空间名称不能为空。")
        return title

    @field_validator("intent", "goal")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        return value.strip() if isinstance(value, str) else value


class UpdateWorkspaceRequest(ApiModel):
    """PATCH。只更新传了的字段。

    刻意**没有** `current_revision_version` —— 版本号只能由确认提案或用户编辑经由
    服务层推进,不开放给客户端直接设置,否则"提案过期检测"就形同虚设。
    """

    title: str | None = Field(default=None, min_length=1, max_length=TITLE_MAX)
    intent: str | None = Field(default=None, max_length=INTENT_MAX)
    status: WorkspaceStatus | None = None

    @field_validator("title")
    @classmethod
    def _check_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        title = value.strip()
        if not title:
            raise ValueError("空间名称不能为空。")
        return title


class WorkspaceCounts(ApiModel):
    """空间里各类实体的真实行数。

    存在的理由之一:空间概览页此前显示的是 `spaces.length * 8` 这种凭空乘出来的数字。
    任何要显示的统计都必须来自一次真实的 count 查询。
    """

    nodes: int
    conversations: int
    proposals: int
    scheduled_sessions: int


class WorkspaceSummary(ApiModel):
    id: uuid.UUID
    title: str
    #: 数据库列可空,但契约取字符串 —— 空串与未填在界面上没有区别,而前端既有类型
    #: `WorkspaceSummary.intent: string` 也是非空的。服务层负责把 NULL 收敛成 ""。
    intent: str
    created_at: datetime


class WorkspaceDetail(WorkspaceSummary):
    status: WorkspaceStatus
    timezone: str
    current_revision_version: int
    archived_at: datetime | None = None
    updated_at: datetime
    counts: WorkspaceCounts


class RootNode(ApiModel):
    """新建空间时唯一被创建的那个根目标节点。

    把它单列出来返回,是为了让前端能立刻拿到 id 去关联后续对话 —— 并且让"新空间里
    到底有什么"这件事在响应里一目了然:一个根目标,别无他物。

    **这个响应里没有会话、没有提案、没有排期**,因为创建过程不会产生它们。
    """

    id: uuid.UUID
    title: str
    node_type: NodeType
    status: NodeStatus
    depth: int
    deadline: date | None = None


class WorkspaceCreated(ApiModel):
    workspace: WorkspaceDetail
    root_node: RootNode
