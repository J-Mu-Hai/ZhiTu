"""成长空间。

## 新建一个空间到底创建了什么

**恰好两行:一条 workspaces,一条 plan_nodes(根目标)。**

没有对话、没有消息、没有提案、没有排期、没有计划版本。这不是"还没实现",这是产品
要求:新空间必须是空的,第一句话必须由用户说出来。

之前的行为正好相反 —— 新建空间会克隆一整套保研 Demo(24 个节点 + 示例对话),用户
打开自己新建的空间,看到的是一段自己从没说过的话。`test_workspace_isolation.py`
就是钉住这件事的:节点数必须等于 1,其余三张表必须是 0。

## 关于 `current_revision_version`

计数器语义:**下一次变更将得到的版本号**。新空间是 1,表示"这个空间还没有任何一次
计划变更记录;第一次变更(用户编辑或确认提案)会成为 v1"。

所以这里**不**写 `plan_revisions` 行。计划本身要求"零提案、零排期",而凭空写一条
"初始版本"意味着我要现在就定下快照的 JSON 形状 —— 那是阶段 4 决定的事,提前编一个
只会给阶段 4 留一个必须兼容的历史包袱。没有那行数据,`plan_revisions` 就是空的,
而"当前计划"由 `plan_nodes` 直接回答;版本历史从第一次真正的变更开始。
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.contracts.workspace import WorkspaceCounts, WorkspaceDetail, WorkspaceSummary
from backend.core.security import CurrentUser
from backend.db.base import utcnow
from backend.db.models import (
    Conversation,
    NodeOrigin,
    NodeStatus,
    NodeType,
    PlanNode,
    Proposal,
    ScheduledSession,
    Workspace,
    WorkspaceStatus,
)
from backend.services.context import WorkspaceContext
from backend.services.errors import InvalidInput

ROOT_GOAL_MAX = 200


async def create_workspace(
    db: AsyncSession,
    *,
    user: CurrentUser,
    title: str,
    intent: str,
    goal: str | None = None,
) -> tuple[Workspace, PlanNode]:
    """建空间 + 建唯一的根目标节点。返回值里没有任何别的东西,这是刻意的。"""
    title = title.strip()
    if not title:
        raise InvalidInput("空间名称不能为空。")

    # 根目标的标题:用户写下的目标优先;没说就用空间名。
    # 两者都来自用户自己的输入 —— 系统不替他想一个目标出来。
    root_title = (goal or title).strip() or title
    if len(root_title) > ROOT_GOAL_MAX:
        raise InvalidInput(f"目标不能超过 {ROOT_GOAL_MAX} 个字符。")

    workspace = Workspace(
        owner_id=user.user_id,
        title=title,
        intent=intent.strip(),
        status=WorkspaceStatus.ACTIVE,
        # 继承用户时区,而不是各自默认。空间与用户在"今天是哪一天"上不一致的话,
        # 跨空间的每日时间池会算出两个不同的"今天"。
        timezone=user.timezone,
    )
    db.add(workspace)
    await db.flush()  # 拿到 workspace.id

    root = PlanNode(
        workspace_id=workspace.id,
        parent_id=None,  # 根节点没有父节点
        title=root_title,
        node_type=NodeType.GOAL,
        status=NodeStatus.PENDING,
        origin=NodeOrigin.USER,
        depth=0,
        order_index=0,
        # plan_revision_id 保持 NULL:这个节点不属于任何一次"变更",它是起点。
    )
    db.add(root)
    await db.commit()
    return workspace, root


def build_summary(workspace: Workspace) -> WorkspaceSummary:
    """ORM -> 契约。

    显式逐个列出字段,不用 `model_validate(workspace)` 图省事。理由不是洁癖:
    `intent` 在库里可空、在契约里是字符串,必须在这里收敛(NULL -> ""),而
    `model_validate` 遇到 NULL 会直接抛校验错。显式写出来的第二个好处是 ——
    契约将来多一个必填字段时,这里当场报错,而不是安静地少返回一个字段。
    """
    return WorkspaceSummary(
        id=workspace.id,
        title=workspace.title,
        intent=workspace.intent or "",
        created_at=workspace.created_at,
    )


async def build_detail(db: AsyncSession, workspace: Workspace) -> WorkspaceDetail:
    """比 summary 多出状态、时区、版本号与四张表的真实计数。"""
    summary = build_summary(workspace)
    return WorkspaceDetail(
        id=summary.id,
        title=summary.title,
        intent=summary.intent,
        created_at=summary.created_at,
        status=workspace.status,
        timezone=workspace.timezone,
        current_revision_version=workspace.current_revision_version,
        archived_at=workspace.archived_at,
        updated_at=workspace.updated_at,
        counts=await count_workspace_rows(db, workspace.id),
    )


async def list_workspaces(
    db: AsyncSession, user: CurrentUser, *, include_archived: bool = False
) -> list[Workspace]:
    stmt = select(Workspace).where(Workspace.owner_id == user.user_id)
    if not include_archived:
        stmt = stmt.where(Workspace.status == WorkspaceStatus.ACTIVE)
    result = await db.execute(stmt.order_by(Workspace.updated_at.desc()))
    return list(result.scalars())


async def count_workspace_rows(db: AsyncSession, workspace_id: uuid.UUID) -> WorkspaceCounts:
    """四张表各一次 count。

    这些数字必须是查出来的。界面上曾经显示的是 `spaces.length * 8` —— 一个凭空
    乘出来的"任务数"。只要它还出现在屏幕上,就必须来自一次真实查询。
    """
    async def count(model, *extra_where) -> int:
        stmt = select(func.count()).select_from(model).where(model.workspace_id == workspace_id)
        for clause in extra_where:
            stmt = stmt.where(clause)
        return int(await db.scalar(stmt) or 0)

    return WorkspaceCounts(
        # 软删除的节点不算"空间里有多少任务"。
        nodes=await count(PlanNode, PlanNode.deleted_at.is_(None)),
        conversations=await count(Conversation),
        proposals=await count(Proposal),
        scheduled_sessions=await count(ScheduledSession),
    )


async def update_workspace(
    db: AsyncSession,
    ctx: WorkspaceContext,
    *,
    title: str | None = None,
    intent: str | None = None,
    status: WorkspaceStatus | None = None,
) -> Workspace:
    """改标题 / 意向 / 归档状态。

    **没有删除。** 空间级联删除会把整棵计划树和全部历史一起抹掉,而 PlanNode 的自引用
    外键是 RESTRICT(级联删除会静默抹掉子树,所以当初刻意选了 RESTRICT)。归档是
    可逆的、用户能理解的、且不丢数据的那条路;真要删除,得先有导出与二次确认,
    那是后续独立能力,不是顺手加的一个 DELETE 路由。
    """
    workspace = ctx.workspace
    if title is not None:
        workspace.title = title.strip()
    if intent is not None:
        workspace.intent = intent.strip()
    if status is not None and status != workspace.status:
        workspace.status = status
        # archived_at 与 status 必须一起改:留着一个"已归档但没有归档时间"的行,
        # 或者反过来,都会让排查历史的人得到互相矛盾的两个答案。
        workspace.archived_at = utcnow() if status == WorkspaceStatus.ARCHIVED else None
    await db.commit()
    return workspace
