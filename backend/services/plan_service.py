"""计划的读取投影。

## 一个序列化函数,两处使用

`node_to_dict` / `session_to_dict` 同时服务于两个消费者:

1. `GET /plan` 的接口载荷 —— 路径 / 时间线 / 任务 / 周计划 / 日计划全都读它。
2. 提案确认时写进 `plan_revisions.snapshot` 的那份快照 —— 用户回头看"V3 是什么样"
   时读的是它。

两者共用一份字段清单,是因为它们回答的是同一个问题:**"此刻这个计划长什么样"**。
分别写两遍的话,新加一个字段时很容易只改一边 —— 于是版本历史里少一个字段,
而这件事没有任何东西会报错,只会在几个月后有人想对比两个版本时才发现。

## "要做什么"和"哪天做"在同一份载荷里,但是两组字段

`node_to_dict` 里**没有**任何排期字段:截止日不是安排,它是用户或模型定下的意图。
真正的安排全部在 `session_to_dict` 的输出里,一个节点可以对应多行。把两者混成一组
字段的话,一个 8 小时的任务在界面上就只有一个日期,而那 8 小时其实分布在四天。

## 软删除的节点不出现在这里,但它们还在库里

`deleted_at` 不为空的节点被过滤掉,可它们并**没有**被物理删除 —— 历史版本的快照里
仍然引用着它们的 id。所以"删掉一个阶段"不等于"这个阶段从没存在过",这一点在
复盘时是有意义的。依赖同理:软删除不会级联清理依赖行,所以这里必须显式过滤
两端都还活着的边,否则界面上会出现指向不存在节点的连线。

## 已取消 / 已搬走的场次也不在这里

它们是**墓碑**:行还在,状态表明"这场没有发生过"或"它搬到了另一天"。复盘要能查到
它们,但"我的计划是什么"这个问题里不该出现一个已经不存在的安排 —— 不然用户会在
日历上看到一个他上周取消掉的安排,而没有任何一处说明它已经取消了。
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.contracts.plan import (
    BriefView,
    DependencyPayload,
    PlanNodePayload,
    PlanPayload,
    SessionPayload,
)
from backend.db.models import Dependency, PlanNode, ScheduledSession
from backend.db.models.enums import NodeStatus, ScheduledSessionStatus
from backend.services import brief_service
from backend.services.context import WorkspaceContext

#: 只作为历史存在的场次状态。它们仍在库里(复盘要查),但不再是"计划的一部分"。
_TOMBSTONE_STATUSES = (ScheduledSessionStatus.CANCELED, ScheduledSessionStatus.MOVED)


def node_to_dict(node: PlanNode) -> dict[str, object]:
    """节点的规范表示。**这是字段清单的唯一出处。**"""
    return {
        "id": node.id,
        "parent_id": node.parent_id,
        "title": node.title,
        "description": node.description,
        "acceptance_criteria": node.acceptance_criteria,
        "node_type": node.node_type.value,
        "status": node.status.value,
        "priority": node.priority.value,
        "estimate_minutes": node.estimate_minutes,
        "deadline": node.deadline,
        "depth": node.depth,
        "order_index": node.order_index,
        "origin": node.origin.value,
        "completed_at": node.completed_at,
        "created_at": node.created_at,
    }


def dependency_to_dict(dep: Dependency) -> dict[str, object]:
    return {
        "id": dep.id,
        "predecessor_id": dep.predecessor_id,
        "successor_id": dep.successor_id,
        "dep_type": dep.dep_type.value,
        "lag_days": dep.lag_days,
    }


def session_to_dict(session: ScheduledSession, node_title: str) -> dict[str, object]:
    """场次的规范表示。**这是排期字段清单的唯一出处。**

    `node_title` 由调用方传进来而不是在这里 `session.node.title` 取一次:那样每渲染
    一行都会触发一次懒加载,而在 async SQLAlchemy 里,一次没预料到的懒加载抛的是
    `MissingGreenlet` —— 一个看起来与"计划视图"毫无关系的 500。调用方本来就已经把
    节点全部加载在手上了(见 `build_plan`),连一次查询都不必多加。
    """
    return {
        "id": session.id,
        "node_id": session.node_id,
        "workspace_id": session.workspace_id,
        "node_title": node_title,
        "scheduled_date": session.scheduled_date,
        "planned_minutes": session.planned_minutes,
        "buffer_minutes": session.buffer_minutes,
        "actual_minutes": session.actual_minutes,
        "seq": session.seq,
        "status": session.status.value,
        "locked": session.locked,
        "lock_reason": session.lock_reason,
        "origin": session.origin.value,
        "start_minute": session.start_minute,
        "end_minute": session.end_minute,
        "completed_at": session.completed_at,
    }


async def load_all_nodes(db: AsyncSession, workspace_id: uuid.UUID) -> list[PlanNode]:
    """这个空间里全部**未被软删除**的节点。

    与 `turn_context.load_nodes` 的区别是**这里没有上限**。送给模型的那份可以截断
    (阶段和目标比第 80 个任务更能说明问题),但投影给用户的计划不能少一条 ——
    "界面上少了一个节点"和"数据库里没有它"在用户看来是同一件事。
    """
    result = await db.execute(
        select(PlanNode)
        .where(PlanNode.workspace_id == workspace_id, PlanNode.deleted_at.is_(None))
        .order_by(PlanNode.depth.asc(), PlanNode.order_index.asc(), PlanNode.created_at.asc())
    )
    return list(result.scalars())


async def load_all_dependencies(
    db: AsyncSession, workspace_id: uuid.UUID
) -> list[Dependency]:
    """两端都还活着的依赖关系。"""
    live = select(PlanNode.id).where(
        PlanNode.workspace_id == workspace_id, PlanNode.deleted_at.is_(None)
    )
    result = await db.execute(
        select(Dependency)
        .where(
            Dependency.workspace_id == workspace_id,
            Dependency.predecessor_id.in_(live),
            Dependency.successor_id.in_(live),
        )
        .order_by(Dependency.created_at.asc())
    )
    return list(result.scalars())


async def load_all_sessions(
    db: AsyncSession, workspace_id: uuid.UUID
) -> list[ScheduledSession]:
    """这个空间里的排期场次,按日期与次序排好。**墓碑不在其中。**"""
    result = await db.execute(
        select(ScheduledSession)
        .where(
            ScheduledSession.workspace_id == workspace_id,
            ScheduledSession.status.not_in(_TOMBSTONE_STATUSES),
        )
        .order_by(
            ScheduledSession.scheduled_date.asc(),
            ScheduledSession.seq.asc(),
            ScheduledSession.created_at.asc(),
        )
    )
    return list(result.scalars())


async def build_plan(db: AsyncSession, ctx: WorkspaceContext) -> PlanPayload:
    """一个空间的完整计划。**只读。**

    节点与场次在同一次查询里各取一遍,然后在这里 join —— `session_to_dict` 需要
    节点标题,而按行去取 `session.node.title` 会在 async 上下文里抛 `MissingGreenlet`。
    """
    nodes = await load_all_nodes(db, ctx.id)
    dependencies = await load_all_dependencies(db, ctx.id)
    sessions = await load_all_sessions(db, ctx.id)
    brief = await brief_service.load_brief(db, ctx.id)

    titles = {node.id: node.title for node in nodes}

    return PlanPayload(
        workspace_id=ctx.id,
        revision_version=ctx.workspace.current_revision_version,
        nodes=[PlanNodePayload.model_validate(node_to_dict(node)) for node in nodes],
        dependencies=[
            DependencyPayload.model_validate(dependency_to_dict(dep)) for dep in dependencies
        ],
        brief=BriefView.model_validate(brief_service.known_summary(brief)),
        sessions=[
            SessionPayload.model_validate(
                session_to_dict(session, titles.get(session.node_id, ""))
            )
            for session in sessions
        ],
        total_nodes=len(nodes),
        completed_nodes=sum(1 for node in nodes if node.status is NodeStatus.COMPLETED),
    )


async def snapshot_payload(db: AsyncSession, workspace_id: uuid.UUID) -> dict[str, object]:
    """写进 `plan_revisions.snapshot` 的那份快照。与接口载荷同一套字段。

    **场次也要进快照。** 只存节点与依赖的话,"V3 是什么样"回答不了"那时候哪几天要
    做多久" —— 而排期是计划的一半。少存这一半不会有任何东西报错,只会在几个月后
    有人想对比两个版本时才发现,而那正是这份快照存在的理由。
    """
    nodes = await load_all_nodes(db, workspace_id)
    dependencies = await load_all_dependencies(db, workspace_id)
    sessions = await load_all_sessions(db, workspace_id)
    titles = {node.id: node.title for node in nodes}
    return {
        "nodes": [_jsonable(node_to_dict(node)) for node in nodes],
        "dependencies": [_jsonable(dependency_to_dict(dep)) for dep in dependencies],
        "sessions": [
            _jsonable(session_to_dict(session, titles.get(session.node_id, "")))
            for session in sessions
        ],
    }


def _jsonable(payload: dict[str, object]) -> dict[str, object]:
    """把 uuid / date / datetime 转成字符串。

    存进 JSON 列的值必须是纯 JSON 类型。SQLAlchemy 的 JSON 序列化器遇到 uuid 会
    直接抛错,而那个报错的时机是 `commit()` —— 用户看到的是"确认失败",日志里是
    一段和计划毫无关系的序列化异常。在这里转掉,问题就不会出现在提交那一刻。
    """
    converted: dict[str, object] = {}
    for key, value in payload.items():
        if isinstance(value, uuid.UUID):
            converted[key] = str(value)
        elif hasattr(value, "isoformat"):
            converted[key] = value.isoformat()  # type: ignore[union-attr]
        else:
            converted[key] = value
    return converted


__all__ = [
    "build_plan",
    "dependency_to_dict",
    "load_all_dependencies",
    "load_all_nodes",
    "load_all_sessions",
    "node_to_dict",
    "session_to_dict",
    "snapshot_payload",
]
