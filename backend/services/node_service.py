"""用户对计划的直接编辑。

## 为什么这条路不经过提案

产品上那两句话把写入分成了两类,而它们不该共用一个流程:

> AI 提案的"确认"由用户点击操作触发,不能由模型替用户调用。
> 用户自己勾选完成或编辑节点属于明确操作,可以直接保存。

勾掉一个任务就是用户表达"我做完了"的完整意思。对这类操作再要求一次"确认",是把
同一句话问两遍 —— 而用户会开始盲点确认,那正好毁掉前一类写入的严肃性。

## 但它和提案确认共用同一套版本记账

这是本文件最容易写错的地方。直接保存**不等于**绕过版本控制:

```
lock_workspace  ->  新版本号  ->  plan_revisions 一行  ->  domain_events 一行
```

少掉任何一样,计划的历史就会出现两条互不相认的线 —— 用户在复盘时看到的
"V4 改了什么"会和 `/plan` 里的现状对不上,而且没有任何东西会报错。所以
`_each_change` 是这三种操作的唯一入口:它把"守卫锁 + 版本记录 + 事件"捆在一起,
让"忘记记版本"变成一件需要主动绕过才能做到的事。

## 这里不接受的两件事,以及理由

1. **不能改 `parent_id`。** 移动节点会引入父环,而父环在前端就是递归渲染爆栈。
   提案那条路径靠"只允许向后引用"让父环在结构上不可能出现;这条路径不做移动,
   同样让它在结构上不可能出现。要做"移动到别的阶段",应该有独立的接口和它的
   子树重排语义,而不是混在"改个标题"的 PATCH 里。
2. **不能删根目标。** 空间存在的理由就是它。删掉之后这个空间在界面上什么都不剩,
   而 `workspaces` 那一行还在 —— 用户会以为自己删的是"整个空间"。

## 软删除,不是物理删除

`deleted_at` 打标记。历史版本的快照仍然引用着被删节点的 id,物理删除会让
"V3 里这个阶段叫什么"变成一条指向空气的记录。删除会**连带整棵子树** ——
父节点没了之后,它的子节点在界面上永远不可达,留着它们只会让用户在某处
看到一个自己找不到入口的任务。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.base import utcnow
from backend.db.locking import lock_workspace
from backend.db.models import Dependency, DomainEvent, PlanNode, PlanRevision, Workspace
from backend.db.models.enums import (
    DependencyType,
    NodeOrigin,
    NodeStatus,
    NodeType,
    Priority,
    RevisionActor,
    RevisionTrigger,
)
from backend.services import plan_service
from backend.services.context import WorkspaceContext
from backend.services.errors import (
    DependencyRejected,
    InvalidInput,
    NodeNotFound,
    RootNodeProtected,
)
from backend.services.proposal_validation import find_cycle

#: 用户能直接改的字段。**白名单,不是黑名单** —— 黑名单的漏网之鱼是"用户改到了
#: 不该改的列",而白名单的漏网之鱼只是"某个字段暂时改不了",前者要修数据。
EDITABLE_FIELDS = frozenset(
    {
        "title",
        "description",
        "acceptance_criteria",
        "node_type",
        "status",
        "priority",
        "estimate_minutes",
        "deadline",
    }
)

#: 字符串字段 -> 它对应的闭集枚举。闭集之外的值一律 400,不写进库。
#:
#: 不用 `sa.Enum` 的校验来兜底:那个报错的时机是 `commit()`,用户看到的是一句
#: 和"状态"毫无关系的数据库异常。在这里挡掉,报错才指向真正填错的那个字段。
_ENUM_FIELDS: dict[str, tuple[type, str]] = {
    "node_type": (NodeType, "节点类型"),
    "status": (NodeStatus, "状态"),
    "priority": (Priority, "优先级"),
}


@dataclass(frozen=True, slots=True)
class NodePatchRequest:
    """一次编辑请求,已经过结构校验。"""

    values: dict[str, object]


@dataclass(frozen=True, slots=True)
class EditResult:
    """一次直接编辑的结果。"""

    node: PlanNode
    revision_version: int
    #: 被删掉的节点数(含子树)。新增/修改时为 0。
    deleted_count: int = 0
    #: 连带删掉的依赖条数。
    removed_dependencies: int = 0


# ---------------------------------------------------------------------------------
# 读
# ---------------------------------------------------------------------------------
async def load_node(db: AsyncSession, ctx: WorkspaceContext, node_id: uuid.UUID) -> PlanNode:
    """取一个**属于这个空间且未被删除**的节点。

    归属条件写进 WHERE:`workspace_id` 不匹配就查不到,查不到就是 `NodeNotFound`。
    取出来再比一次归属的话,"忘记比"会成为一种可能的代码路径 —— 那样拿别人的节点
    id 就能改到别人的计划。
    """
    node = await db.scalar(
        select(PlanNode).where(
            PlanNode.id == node_id,
            PlanNode.workspace_id == ctx.id,
            PlanNode.deleted_at.is_(None),
        )
    )
    if node is None:
        raise NodeNotFound("这个节点不在当前空间里。")
    return node


async def load_live_nodes(db: AsyncSession, ctx: WorkspaceContext) -> list[PlanNode]:
    return await plan_service.load_all_nodes(db, ctx.id)


async def load_dependencies(db: AsyncSession, ctx: WorkspaceContext) -> list[Dependency]:
    return await plan_service.load_all_dependencies(db, ctx.id)


# ---------------------------------------------------------------------------------
# 写
# ---------------------------------------------------------------------------------
async def create_node(
    db: AsyncSession,
    ctx: WorkspaceContext,
    *,
    parent_id: uuid.UUID,
    title: str,
    node_type: str = NodeType.TASK.value,
    description: str | None = None,
    acceptance_criteria: str | None = None,
    priority: str = Priority.MEDIUM.value,
    estimate_minutes: int | None = None,
    deadline: date | None = None,
) -> EditResult:
    """在 `parent_id` 下面挂一个新节点。**用户自己建的,所以 `origin=user`。**"""
    clean_title = title.strip()
    parsed_type = _parse_enum("node_type", node_type)
    parsed_priority = _parse_enum("priority", priority)
    if not clean_title:
        raise InvalidInput("标题不能为空。")
    if len(clean_title) > 200:
        raise InvalidInput("标题不能超过 200 个字。")
    if estimate_minutes is not None and estimate_minutes <= 0:
        raise InvalidInput("预计工时必须是正数。")

    async with _each_change(db, ctx, trigger_detail=f"新建了「{clean_title}」") as change:
        # 父节点在**锁里面**读。放在锁外面读的话,"读到父节点 d=2 -> 另一个请求
        # 把父节点挪走了/删了 -> 我按旧数据写"就会算出错的 depth;而 depth 一旦
        # 和真实层级不符,不会有任何东西报错,只会让树在界面上缩成一团。
        parent = await load_node(db, ctx, parent_id)
        sibling_count = await db.scalar(
            select(func.count(PlanNode.id)).where(
                PlanNode.workspace_id == ctx.id,
                PlanNode.parent_id == parent.id,
                PlanNode.deleted_at.is_(None),
            )
        )
        node = PlanNode(
            id=uuid.uuid4(),
            workspace_id=ctx.id,
            parent_id=parent.id,
            title=clean_title,
            description=_clean(description),
            acceptance_criteria=_clean(acceptance_criteria),
            node_type=parsed_type,
            status=NodeStatus.PENDING,
            priority=parsed_priority,
            estimate_minutes=estimate_minutes,
            deadline=deadline,
            order_index=int(sibling_count or 0),
            # depth 由父节点推出来,不接受调用方传 —— 传进来的话,它就是第二个真相。
            depth=parent.depth + 1,
            origin=NodeOrigin.USER,
        )
        db.add(node)
        await db.flush()
        node.plan_revision_id = change.revision_id
        change.record(kind="node_created", node_id=node.id, payload={"title": node.title})
        return EditResult(node=node, revision_version=change.version)


async def update_node(
    db: AsyncSession,
    ctx: WorkspaceContext,
    node_id: uuid.UUID,
    patch: dict[str, object],
) -> EditResult:
    """改一个节点的字段。只改白名单里的列。"""
    unknown = set(patch) - EDITABLE_FIELDS
    if unknown:
        # 静默忽略是更坏的选择:用户改了"父节点",界面显示保存成功,而计划没动。
        raise InvalidInput(f"这些字段不能直接改:{'、'.join(sorted(unknown))}。")
    if not patch:
        raise InvalidInput("这次编辑没有带任何要改的字段。")

    values = {key: _parse_enum(key, value) for key, value in patch.items()}
    if "title" in values:
        title = str(values["title"]).strip()
        if not title:
            raise InvalidInput("标题不能为空。")
        if len(title) > 200:
            raise InvalidInput("标题不能超过 200 个字。")
        values["title"] = title
    for field in ("description", "acceptance_criteria"):
        if field in values:
            values[field] = _clean(values[field] if values[field] is None else str(values[field]))
    if values.get("estimate_minutes") is not None and int(values["estimate_minutes"]) <= 0:  # type: ignore[arg-type]
        raise InvalidInput("预计工时必须是正数。")

    async with _each_change(db, ctx, trigger_detail="") as change:
        node = await load_node(db, ctx, node_id)
        was_status = node.status
        # 详情要等读到节点才知道,而节点是在锁里面读的 —— 所以这句话只能在
        # 进入事务之后写。`_Change` 把 detail 做成可赋值的,正是为了这个。
        change.detail = _edit_detail(node.title, values)

        for field, value in values.items():
            setattr(node, field, value)
        # `completed_at` 与 `status` **必须一起改**。留着一个"已完成但没有完成时间"
        # 的行,复盘时就算不出"这个阶段实际花了多久" —— 而那是复盘唯一有用的数字。
        if "status" in values and node.status is not was_status:
            node.completed_at = utcnow() if node.status is NodeStatus.COMPLETED else None
        await db.flush()
        change.record(
            kind="node_updated",
            node_id=node.id,
            payload={"fields": sorted(values)},
        )
        return EditResult(node=node, revision_version=change.version)


async def delete_node(db: AsyncSession, ctx: WorkspaceContext, node_id: uuid.UUID) -> EditResult:
    """软删除一个节点**及其整棵子树**。

    子树是在 Python 侧用已加载的活节点走出来的,不用递归 CTE。理由是那一句 SQL 在
    SQLite 和 PostgreSQL 上写法不同,而这个仓库的可移植性红线明确要求两端都能跑;
    一个空间的节点数是几十到几百,一次全量加载的代价远小于维护两套 SQL。
    """
    async with _each_change(db, ctx, trigger_detail="") as change:
        root = await load_node(db, ctx, node_id)
        if root.parent_id is None:
            raise RootNodeProtected("根目标是这个空间存在的理由,不能删除。")

        # 子树在锁里面收。在锁外面收的话,另一个请求在这中间往子树里挂了一个新节点,
        # 那个节点会被留成一个父节点已被删除的孤儿 —— 它在界面上永远不可达,
        # 用户找不到任何入口去处理它。
        nodes = await load_live_nodes(db, ctx)
        subtree = _collect_subtree(nodes, root.id)
        change.detail = f"删除了「{root.title}」及其下面的 {len(subtree) - 1} 项"
        now = utcnow()

        await db.execute(
            update(PlanNode)
            .where(PlanNode.id.in_(subtree), PlanNode.workspace_id == ctx.id)
            .values(deleted_at=now)
            .execution_options(synchronize_session=False)
        )

        # 连带清掉挂在被删节点上的依赖。提案那条路径**不**这么做,因为它是在内存快照上
        # 校验的、本来就不会把死边算进去;这里直接改库,不清就会留下指向软删除节点的
        # 边 —— 投影时会被过滤掉(见 plan_service),于是"库里有、界面没有",
        # 排查时看到的是两个都说得通的事实。让它和界面一致。
        removed = await db.execute(
            delete(Dependency)
            .where(
                Dependency.workspace_id == ctx.id,
                (Dependency.predecessor_id.in_(subtree)) | (Dependency.successor_id.in_(subtree)),
            )
            .execution_options(synchronize_session=False)
        )

        change.record(
            kind="node_deleted",
            node_id=root.id,
            payload={"deleted": [str(item) for item in subtree]},
        )
        return EditResult(
            node=root,
            revision_version=change.version,
            deleted_count=len(subtree),
            removed_dependencies=int(removed.rowcount or 0),
        )


async def add_dependency(
    db: AsyncSession, ctx: WorkspaceContext, *, predecessor_id: uuid.UUID, successor_id: uuid.UUID
) -> Dependency:
    """手工加一条"前者完成后才能开始后者"。"""
    if predecessor_id == successor_id:
        raise InvalidInput("一个节点不能依赖它自己。")

    async with _each_change(db, ctx, trigger_detail="") as change:
        predecessor = await load_node(db, ctx, predecessor_id)
        successor = await load_node(db, ctx, successor_id)

        existing = await db.scalar(
            select(Dependency).where(
                Dependency.workspace_id == ctx.id,
                Dependency.predecessor_id == predecessor.id,
                Dependency.successor_id == successor.id,
            )
        )
        if existing is not None:
            # 幂等:这条边本来就在,不报错也不重复插。用户点两下不该得到一条 409。
            return existing

        # 环检测用的是提案那条路径上的同一个函数(见 proposal_validation.find_cycle),
        # 不是另写一份 —— 两份实现里只修一份的后果是"AI 加的依赖会成环,用户加的不会"。
        # 它也必须在锁里面做:两边同时加一条边,各自看都无环,合起来成环。
        edges = _edge_set(await load_dependencies(db, ctx))
        edges.add((predecessor.id, successor.id))
        cycle = find_cycle(edges)
        if cycle is not None:
            titles = {node.id: node.title for node in (predecessor, successor)}
            chain = " → ".join(f"「{titles.get(item, item)}」" for item in cycle)
            raise DependencyRejected(f"这条依赖会让计划出现环:{chain}")

        change.detail = f"「{predecessor.title}」完成后才能开始「{successor.title}」"
        dependency = Dependency(
            workspace_id=ctx.id,
            predecessor_id=predecessor.id,
            successor_id=successor.id,
            dep_type=DependencyType.FINISH_TO_START,
            lag_days=0,
        )
        db.add(dependency)
        await db.flush()
        change.record(kind="dependency_added", node_id=successor.id)
        return dependency


async def remove_dependency(
    db: AsyncSession, ctx: WorkspaceContext, *, predecessor_id: uuid.UUID, successor_id: uuid.UUID
) -> int:
    """去掉一条依赖。**不存在的依赖不算错** —— 目标是"这条边没了",而它已经没了。"""
    async with _each_change(db, ctx, trigger_detail="去掉了一条依赖") as change:
        result = await db.execute(
            delete(Dependency)
            .where(
                Dependency.workspace_id == ctx.id,
                Dependency.predecessor_id == predecessor_id,
                Dependency.successor_id == successor_id,
            )
            .execution_options(synchronize_session=False)
        )
        if int(result.rowcount or 0):
            change.record(kind="dependency_removed", node_id=successor_id)
        return int(result.rowcount or 0)


# ---------------------------------------------------------------------------------
# 版本记账
# ---------------------------------------------------------------------------------
class _Change:
    """一次直接编辑的事务作用域。

    它把四件事捆在一起,因为它们**必须同时发生或者同时不发生**:守卫锁、版本号前进、
    `plan_revisions` 一行、`domain_events` 一行。字段是属性而不是构造参数,是因为
    `version` 和 `revision_id` 要等到进入事务、读过当前版本号之后才知道。
    """

    def __init__(self, detail: str) -> None:
        self.version: int = 0
        self.revision_id: uuid.UUID | None = None
        self.events: list[dict[str, object]] = []
        #: 写进 `trigger_detail` 的那句话。可变,因为有些操作的描述要用到**锁里面**
        #: 才读得到的节点标题 —— 在进入事务之前根本写不出来。
        self.detail: str = detail

    def record(
        self, *, kind: str, node_id: uuid.UUID, payload: dict[str, object] | None = None
    ) -> None:
        self.events.append({"kind": kind, "node_id": node_id, "payload": payload})


@asynccontextmanager
async def _each_change(
    db: AsyncSession, ctx: WorkspaceContext, *, trigger_detail: str
) -> AsyncIterator[_Change]:
    """`async with _each_change(db, ctx, ...) as change:` —— 一次编辑的完整事务边界。

    进入时加锁并读版本号,退出时写版本记录、事件,把版本号推进一位,然后提交。
    块内抛异常则整体回滚,版本号不动 —— 一次失败的编辑不该在历史里留下一个空版本。
    """
    change = _Change(trigger_detail)

    # 先加锁再读版本号。反过来的话,"读到 v3 -> 另一个请求写成了 v4 -> 我写成 v3"
    # 会让两个变更共用同一个版本号,而 `uq_plan_revisions_workspace_id_version`
    # 那时才报错 —— 用户看到的是一句和计划毫无关系的唯一约束冲突。
    await lock_workspace(db, ctx.id)
    change.version = await _current_revision_version(db, ctx.id)

    try:
        yield change

        await db.flush()
        # 快照必须在 flush 之后取:本会话是 autoflush=False(见 db/session.py),
        # 不显式 flush 的话刚写进去的那一行不在快照里 —— 于是最新版本的
        # `plan_revisions.snapshot` 恰好缺掉它要记录的那次改动。
        snapshot = await plan_service.snapshot_payload(db, ctx.id)
        parent = await db.scalar(
            select(PlanRevision).where(
                PlanRevision.workspace_id == ctx.id,
                PlanRevision.version == change.version - 1,
            )
        )
        revision = PlanRevision(
            workspace_id=ctx.id,
            version=change.version,
            parent_revision_id=parent.id if parent else None,
            # 用户自己动的手,和 AI 提案要能分辨 —— 复盘时"哪些是 AI 排的、
            # 哪些是我改的"决定了该信任哪一部分。
            trigger_type=RevisionTrigger.USER_EDIT,
            trigger_detail=change.detail,
            actor=RevisionActor.USER,
            proposal_id=None,
            snapshot=snapshot,
            diff={
                "events": [
                    {
                        "kind": event["kind"],
                        "nodeId": str(event["node_id"]),
                        **(event["payload"] or {}),  # type: ignore[dict-item]
                    }
                    for event in change.events
                ]
            },
            created_at=utcnow(),
        )
        db.add(revision)
        await db.flush()
        change.revision_id = revision.id

        await db.execute(
            update(Workspace)
            .where(Workspace.id == ctx.id)
            .values(current_revision_version=change.version + 1)
            .execution_options(synchronize_session=False)
        )
        ctx.workspace.current_revision_version = change.version + 1

        for event in change.events:
            db.add(
                DomainEvent(
                    user_id=ctx.user.user_id,
                    workspace_id=ctx.id,
                    kind=str(event["kind"]),
                    ref_type="plan_node",
                    ref_id=event["node_id"],  # type: ignore[arg-type]
                    payload=event["payload"],  # type: ignore[arg-type]
                    created_at=utcnow(),
                )
            )

        await db.commit()
    except Exception:
        await db.rollback()
        raise


# ---------------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------------
def _collect_subtree(nodes: list[PlanNode], root_id: uuid.UUID) -> set[uuid.UUID]:
    """从 `root_id` 出发收集整棵子树。

    迭代式而不是递归:深树会在 Python 的默认递归深度上踩线,而那个报错
    (RecursionError)看起来和"删除节点"毫无关系。
    """
    children: dict[uuid.UUID | None, list[uuid.UUID]] = {}
    for node in nodes:
        children.setdefault(node.parent_id, []).append(node.id)

    collected: set[uuid.UUID] = set()
    stack = [root_id]
    while stack:
        current = stack.pop()
        if current in collected:
            continue
        collected.add(current)
        stack.extend(children.get(current, []))
    return collected


def _edge_set(dependencies: list[Dependency]) -> set[tuple[uuid.UUID, uuid.UUID]]:
    return {(dep.predecessor_id, dep.successor_id) for dep in dependencies}


def _parse_enum(field: str, value: object) -> object:
    """把 `node_type` / `status` / `priority` 的字符串解析成枚举。

    非枚举字段、以及值为 None(在 PATCH 里表示"清空")都原样返回。
    """
    entry = _ENUM_FIELDS.get(field)
    if entry is None or value is None:
        return value
    enum_cls, label = entry
    try:
        return enum_cls(value)  # type: ignore[operator]
    except ValueError:
        allowed = "、".join(member.value for member in enum_cls)  # type: ignore[attr-defined]
        raise InvalidInput(f"{label}「{value}」不是允许的值。可选:{allowed}。") from None


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None


def _edit_detail(title: str, values: dict[str, object]) -> str:
    """给版本历史写一句人能读懂的话。

    只列字段名,不列具体值 —— 值可能是几百字的说明,塞进 `trigger_detail` 之后
    版本列表就没法读了。想看具体值的人点进去看快照。
    """
    labels = {
        "title": "标题",
        "description": "说明",
        "acceptance_criteria": "验收标准",
        "node_type": "类型",
        "status": "状态",
        "priority": "优先级",
        "estimate_minutes": "预计工时",
        "deadline": "截止时间",
    }
    names = "、".join(labels.get(field, field) for field in sorted(values))
    return f"修改了「{title}」的{names}"


async def _current_revision_version(db: AsyncSession, workspace_id: uuid.UUID) -> int:
    value = await db.scalar(
        select(Workspace.current_revision_version).where(Workspace.id == workspace_id)
    )
    return int(value or 1)


__all__ = [
    "EDITABLE_FIELDS",
    "EditResult",
    "add_dependency",
    "create_node",
    "delete_node",
    "load_dependencies",
    "load_live_nodes",
    "load_node",
    "remove_dependency",
    "update_node",
]
