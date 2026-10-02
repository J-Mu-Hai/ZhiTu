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

## 归档与彻底删除:两件事,库里分得开

用户点一下垃圾桶,产品上那一下是**归档**(收起来,以后还能拿回来),而"删除"是
它的加强版。两者在库里都只是软删除,区别是 `purged_at` 那一列:

| | `archive`(默认) | `delete` |
| --- | --- | --- |
| `deleted_at` / `purged_at` | 打 / 留空 | 打 / 打 |
| `archive_batch_id` | 一次删除一个号 | 一次删除一个号 |
| 挂在上面的边 | **一行不动** | 物理删掉 |
| 场次 | 一行不动 | 一行不动 |
| 恢复 | 可以(`restore_node`) | 拒绝 |

"一次删除一个号"那一行是恢复**认哪一批**的依据 —— 恢复要认的是"当时是哪一下把它
带走的",不是"它下面现在有什么"(那会把用户更早单独收起来的子项也复活)。这个号
以前是"`deleted_at` 相等",换成显式的号的理由见 `PlanNode.archive_batch_id`。

归档**不动边**这一条是这一节存在的理由。以前这里无条件物理删掉挂在上面的
`dependencies` 行,而恢复入口一出现,那样做的后果就变成了"恢复回来的是一个一条前置
都没有的节点"—— 排期正是按前置算的,而用户不会知道自己拿到的是残的。
读路径本来就把"两端都活着"写进了 WHERE(见 `plan_service`),所以留着那些行不会让
界面上多出任何一条连线。

完整规则(节点与后代、三道恢复前校验、排期、界面)写在
`docs/10-NEXT-BATCH-SCOPE.md` 第 5 节。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.contracts.plan import (
    DEPENDS_ON,
    as_planning_level,
    child_level_conflicts,
    description_length_error,
    information_node_conflicts,
)
from backend.db.base import utcnow
from backend.db.locking import lock_workspace
from backend.db.models import (
    Dependency,
    DomainEvent,
    NodeRelation,
    PlanNode,
    PlanRevision,
    ScheduledSession,
    Workspace,
)
from backend.db.models.enums import (
    DependencyType,
    NodeOrigin,
    NodePurpose,
    NodeRelationType,
    NodeStatus,
    NodeType,
    Priority,
    RevisionActor,
    RevisionTrigger,
    ScheduledSessionStatus,
    WorkspaceStatus,
)
from backend.scheduler.calendar import daily_cap
from backend.services import plan_service, schedule_service
from backend.services.context import WorkspaceContext
from backend.services.errors import (
    ConcurrencyConflict,
    DependencyRejected,
    InformationNodeNotSchedulable,
    InvalidInput,
    NodeNotFound,
    NodePurged,
    ParentArchived,
    RelationNotFound,
    RelationRejected,
    RootNodeProtected,
)
from backend.services.proposal_validation import find_cycle
from backend.services.timeutil import today_in

#: 用户能直接改的字段。**白名单,不是黑名单** —— 黑名单的漏网之鱼是"用户改到了
#: 不该改的列",而白名单的漏网之鱼只是"某个字段暂时改不了",前者要修数据。
EDITABLE_FIELDS = frozenset(
    {
        "title",
        "description",
        "acceptance_criteria",
        "node_type",
        "purpose",
        "status",
        "priority",
        "estimate_minutes",
        "deadline",
        # 规划层级。可空 —— 传 null 是"清空层级"。它只表达语义,不进排期。
        "planning_level",
    }
)

#: 哪些字段算"正文" —— 它们改一次,`plan_nodes.content_version` 就 +1。
#:
#: `description` 是界面上那个"详细说明";`acceptance_criteria` 是同一类的自由文本,
#: 将来编辑器一并读写的很可能就是它,所以现在一起算进去 —— 只锁一半的话,
#: 那条丢失更新会从另一半溜回来。
BODY_FIELDS = frozenset({"description", "acceptance_criteria"})


def touch_content_version(node: PlanNode, fields: Iterable[str]) -> bool:
    """改了 `BODY_FIELDS` 里的字段就推进正文版本号;返回是否推进了。

    **全仓唯一推进 `content_version` 的地方。** 改一个已有节点的正文只有两条路:
    用户直接编辑(`update_node`)与 AI 提案确认(`proposal_service._apply`)——
    两条都必须走这里。各写一遍的代价不是"重复",是**迟早有一条忘了写**,而忘写
    的表现是**静默丢失更新**:客户端手里那个版本号还停在原地,它下一次保存就会把
    别人刚写的正文整段盖掉,而两边都不会看到冲突。

    这不是假设:提案路径原来正是漏的 —— `UpdateNodeAction` 里没有 `content_version`,
    `_apply` 只 `setattr`,于是"AI 改过的正文"在版本上等于没发生过。
    `tests/test_content_version_single_writer.py` 里有一条 AST 用例钉住"只有一个
    写入点",另有一条端到端用例钉住"提案改完正文,旧编辑器保存会被 409 拦住"。
    """
    if not BODY_FIELDS & set(fields):
        return False
    node.content_version = node.content_version + 1
    return True

#: 字符串字段 -> 它对应的闭集枚举。闭集之外的值一律 400,不写进库。
#:
#: 不用 `sa.Enum` 的校验来兜底:那个报错的时机是 `commit()`,用户看到的是一句
#: 和"状态"毫无关系的数据库异常。在这里挡掉,报错才指向真正填错的那个字段。
_ENUM_FIELDS: dict[str, tuple[type, str]] = {
    "node_type": (NodeType, "节点类型"),
    "purpose": (NodePurpose, "用途"),
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
    #: 连带**物理**删掉的依赖条数。只有 `mode=delete`(彻底删除)会大于 0 ——
    #: 归档不动边,所以那个数字在归档时恒为 0,而这不是"什么都没删"的意思。
    removed_dependencies: int = 0
    #: 这一次删除是不是可恢复的。
    restorable: bool = True


#: `DELETE /nodes/{id}?mode=` 的两个取值。**默认 `archive`。**
#:
#: 命名上刻意用两个词而不是"软删/硬删":`archive` 是用户做的事(收起来,以后还能拿回来),
#: `delete` 是"不可恢复";两者在库里都是**软删除**(都打 `deleted_at`,行都留着),
#: 区别只在 `purged_at` 那一列。用"软/硬"命名会让人以为 `archive` 是不写库的,
#: 或者以为 `delete` 会物理删行 —— 两个都是错的。
ARCHIVE_MODE = "archive"
PURGE_MODE = "delete"
MODES = (ARCHIVE_MODE, PURGE_MODE)


@dataclass(frozen=True, slots=True)
class ArchiveImpact:
    """归档(或彻底删除)一个节点之前,先算清楚这一下会带走什么。

    数字由后端算,不由前端从它手上那份计划里推:前端手里那份可能是几分钟前的,
    而这份是"此刻"的。用户在确认框里看到的数字和实际发生的事对不上,
    比不给数字更糟 —— 他会照着那个数字做决定。
    """

    node_id: uuid.UUID
    title: str
    #: 后代节点数(不含自己)。
    descendants: int
    #: 会跟着消失的 `related_to` / `influences` 关系条数。
    relations: int
    #: 会跟着消失的「前置 → 后续」依赖条数。
    dependencies: int
    #: 挂在被归档子树上的场次条数与分钟数。
    sessions: int
    session_minutes: int
    #: 还有几场在今天之前(归档再恢复的话,那几场已经过期了)。
    overdue_sessions: int


@dataclass(frozen=True, slots=True)
class OverbookedDay:
    """恢复之后某一天超了上限。"""

    day: date
    planned_minutes: int
    daily_cap: int

    @property
    def over_by(self) -> int:
        return max(0, self.planned_minutes - self.daily_cap)


@dataclass(frozen=True, slots=True)
class RestoreResult:
    """一次恢复的结果。

    **带排期报告是必须的,不是锦上添花。** 恢复会把归档期间冻结的场次一次性放回日历:
    它们可能已经过期,也可能和归档之后新排的挤在同一天。静默恢复等于替用户交了一份
    他自己没有看过的日程 —— 所以这里把"回来几场、过期几场、哪几天超了"如实带出去,
    由界面说清楚,让用户决定要不要重排。
    """

    node: PlanNode
    revision_version: int
    restored_count: int
    restored_sessions: int
    restored_minutes: int
    overdue_sessions: int
    overbooked_days: tuple[OverbookedDay, ...]
    #: 跟着重新可见的关系与依赖条数。它们**没有**被归档删掉过(归档一行边都不动),
    #: 所以这里说的是"重新可见",不是"重新创建" —— 界面拿它解释"为什么边也回来了"。
    relations_visible: int = 0


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
    purpose: str = NodePurpose.PLANNING.value,
    description: str | None = None,
    acceptance_criteria: str | None = None,
    priority: str = Priority.MEDIUM.value,
    estimate_minutes: int | None = None,
    deadline: date | None = None,
    planning_level: str | None = None,
) -> EditResult:
    """在 `parent_id` 下面挂一个新节点。**用户自己建的,所以 `origin=user`。**"""
    clean_title = title.strip()
    parsed_type = _parse_enum("node_type", node_type)
    parsed_purpose = _parse_enum("purpose", purpose)
    parsed_priority = _parse_enum("priority", priority)
    parsed_level = as_planning_level(planning_level)
    if planning_level is not None and parsed_level is None:
        raise InvalidInput(
            "规划层级只能是 strategy / phase / month / week / day。"
        )
    if not clean_title:
        raise InvalidInput("标题不能为空。")
    if len(clean_title) > 200:
        raise InvalidInput("标题不能超过 200 个字。")
    if estimate_minutes is not None and estimate_minutes <= 0:
        raise InvalidInput("预计工时必须是正数。")

    # 新节点没有"存量",所以 300 码点这条规则在这里就是一条普通的上限
    # (豁免只对**已经超长的老正文**成立,见 `description_length_error`)。
    # 检查的是 `_clean` 之后的值 —— 那才是真正会写进库的那个字符串,否则
    # "300 个字加一个换行"会在校验时是 301、存下来却是 300。
    clean_description = _clean(description)
    too_long = description_length_error(None, clean_description)
    if too_long is not None:
        raise InvalidInput(too_long)
    # 手工建和 AI 提案走**同一个谓词**(§2.5)。两条路各写一份判断的话,
    # 迟早有一条漏掉,而漏掉的表现是"我自己建的能带工时,AI 建的不行"。
    conflicts = information_node_conflicts(
        parsed_purpose, estimate_minutes, deadline, parsed_level
    )
    if conflicts is not None:
        raise InvalidInput(conflicts)

    async with _each_change(db, ctx, trigger_detail=f"新建了「{clean_title}」") as change:
        # 父节点在**锁里面**读。放在锁外面读的话,"读到父节点 d=2 -> 另一个请求
        # 把父节点挪走了/删了 -> 我按旧数据写"就会算出错的 depth;而 depth 一旦
        # 和真实层级不符,不会有任何东西报错,只会让树在界面上缩成一团。
        parent = await load_node(db, ctx, parent_id)
        # 父子层级只能从粗到细。父节点在锁里读到,判断就在锁里做 —— 放外面的话
        # "读到父层级 -> 别人改了它 -> 我按旧值写"会留下一条反向的层级链。
        level_conflict = child_level_conflicts(parent.planning_level, parsed_level)
        if level_conflict is not None:
            raise InvalidInput(level_conflict)
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
            description=clean_description,
            acceptance_criteria=_clean(acceptance_criteria),
            node_type=parsed_type,
            purpose=parsed_purpose,
            planning_level=parsed_level,
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
    *,
    expected_content_version: int | None = None,
) -> EditResult:
    """改一个节点的字段。只改白名单里的列。

    `expected_content_version` 是**正文的乐观锁**(可选)。给了就一定比对:对不上抛
    `ConcurrencyConflict`(409),这一次编辑**整个不生效** —— 不是"只改别的字段"。
    半个请求生效比整个失败更难查:用户看到"保存失败",刷新却发现工时变了。

    比对在 `_each_change` **里面**做,也就是在拿到工作区锁之后。放到锁外面的话,
    "查完 -> 别人写 -> 我写"这三步里的中间那一步谁都拦不住,而锁存在的意义正是
    让这三步变成一件事。

    ## 只有正文会推进版本号

    推进版本的只有 `description` / `acceptance_criteria` 这两个字段 —— 它们才是
    "正文",也才是客户端整份读回来、整份写回去的那份数据(丢失更新就发生在这里)。
    改标题、优先级、工时**不动版本号**:那些字段是逐字段 PATCH 的,两个标签页分别
    改标题和正文互不覆盖,不该因为对方动过就被判成冲突 —— 那样用户会看到
    "你手上那份旧了",而他改的那个字段根本没人碰过。
    """

    unknown = set(patch) - EDITABLE_FIELDS
    if unknown:
        # 静默忽略是更坏的选择:用户改了"父节点",界面显示保存成功,而计划没动。
        raise InvalidInput(f"这些字段不能直接改:{'、'.join(sorted(unknown))}。")
    if not patch:
        raise InvalidInput("这次编辑没有带任何要改的字段。")

    values = {key: _parse_enum(key, value) for key, value in patch.items()}
    if "planning_level" in values:
        raw_level = values["planning_level"]
        parsed_level = as_planning_level(raw_level)
        if raw_level is not None and parsed_level is None:
            raise InvalidInput(
                "规划层级只能是 strategy / phase / month / week / day。"
            )
        values["planning_level"] = parsed_level
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

        if expected_content_version is not None and node.content_version != expected_content_version:
            # 消息里给的是**两个数字**,不说"请刷新重试"这种没有信息量的话:
            # 界面上要能告诉用户"你手上是第 3 版,库里已经是第 5 版" —— 那是他判断
            # "要不要用我这份覆盖"的唯一依据。`details` 里的 `content_version`
            # 是**此刻库里那一版**,客户端可以拿它直接重发。
            raise ConcurrencyConflict(
                f"这份正文在别处被改过了(你手上是第 {expected_content_version} 版,"
                f"库里已经是第 {node.content_version} 版),所以这次没有写进去。",
                content_version=node.content_version,
                expected_content_version=expected_content_version,
            )

        if "description" in values:
            # **这条检查在锁里面,而这个位置是必须的。** 300 码点是**一条条件
            # 规则**:存量已经超过上限的说明继续想写多长写多长(那些是上限出现
            # 之前用户唯一的表达方式,对它们收口等于追溯性地宣布他已经写下的东西
            # 不合法)。要比的那一份"存量"只有读到节点才知道,而节点是在锁里读的。
            too_long = description_length_error(node.description, values["description"])  # type: ignore[arg-type]
            if too_long is not None:
                raise InvalidInput(too_long)

        # 用途、工时、截止**一起**看:只改其中一个也能造出"信息主题带着 90 分钟"
        # 这种状态(比如把一个有工时的任务改成信息主题)。取的是**改完之后**的
        # 值,不是 patch 里出现的字段 —— 判"结果合不合法",不判"这次动了几个字段"。
        conflicts = information_node_conflicts(
            values.get("purpose", node.purpose),
            values.get("estimate_minutes", node.estimate_minutes),  # type: ignore[arg-type]
            values.get("deadline", node.deadline),  # type: ignore[arg-type]
            values.get("planning_level", node.planning_level),
        )
        if conflicts is not None:
            raise InvalidInput(conflicts)

        # 改了规划层级就要与**父节点和直属子节点**都相容。只判父不判子的话,
        # 把一个 phase 改成 week 会在它下面留下一串更粗的 phase 孩子 —— 而那正是
        # "从粗到细"这条规则要防的反向链。
        if "planning_level" in values:
            target_level = values["planning_level"]
            if node.parent_id is not None:
                parent = await db.get(PlanNode, node.parent_id)
                parent_level = (
                    parent.planning_level
                    if parent is not None and parent.deleted_at is None
                    else None
                )
                parent_conflict = child_level_conflicts(parent_level, target_level)
                if parent_conflict is not None:
                    raise InvalidInput(parent_conflict)
            children = await db.execute(
                select(PlanNode.planning_level).where(
                    PlanNode.workspace_id == ctx.id,
                    PlanNode.parent_id == node.id,
                    PlanNode.deleted_at.is_(None),
                )
            )
            for (child_level,) in children.all():
                child_conflict = child_level_conflicts(target_level, child_level)
                if child_conflict is not None:
                    raise InvalidInput(child_conflict)

        for field, value in values.items():
            setattr(node, field, value)
        # 正文改了才推进版本号 —— 规则与提案路径共用同一个函数,见上面那一节。
        touch_content_version(node, values)
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


async def delete_node(
    db: AsyncSession,
    ctx: WorkspaceContext,
    node_id: uuid.UUID,
    *,
    mode: str = ARCHIVE_MODE,
) -> EditResult:
    """删除一个节点**及其整棵子树**。默认是**归档**(可恢复),`mode="delete"` 是彻底删除。

    子树是在 Python 侧用已加载的活节点走出来的,不用递归 CTE。理由是那一句 SQL 在
    SQLite 和 PostgreSQL 上写法不同,而这个仓库的可移植性红线明确要求两端都能跑;
    一个空间的节点数是几十到几百,一次全量加载的代价远小于维护两套 SQL。

    ## 归档**不动边**,彻底删除才动

    这里原来无条件物理删掉挂在被删节点上的 `dependencies` 行,注释给的理由是"不清就会
    留下指向软删除节点的边,投影时被过滤掉,于是库里有界面没有"。**那个理由不成立**:
    `plan_service.load_all_dependencies` / `load_all_node_relations` 本来就把"两端都活着"
    写进了 WHERE,界面上从来就看不到那些边。那一段硬删唯一的实际效果是**让恢复拿不回
    依赖** —— 用户点"恢复"之后拿到的是一个一条前置都没有的节点,而排期正是按前置算的。
    所以归档改成一行不动,彻底删除那条路照旧物理删(它本来就不给恢复)。

    场次同理:归档一行不碰(见 `plan_service.load_all_sessions` 那侧的过滤),
    彻底删除也不碰 —— 它是历史,不该从复盘里消失。

    为什么彻底删除也**不物理删行**:`plan_revisions.snapshot` 里引用着这些 id,
    物理删除会让历史版本指向空气。"彻底"指的是不可恢复,不是从历史里抹掉。
    """
    if mode not in MODES:
        raise InvalidInput(f"删除方式只能是 {ARCHIVE_MODE} 或 {PURGE_MODE}。")

    purging = mode == PURGE_MODE

    async with _each_change(db, ctx, trigger_detail="") as change:
        root = await load_node(db, ctx, node_id)
        if root.parent_id is None:
            raise RootNodeProtected("根目标是这个空间存在的理由,不能删除。")

        # 子树在锁里面收。在锁外面收的话,另一个请求在这中间往子树里挂了一个新节点,
        # 那个节点会被留成一个父节点已被删除的孤儿 —— 它在界面上永远不可达,
        # 用户找不到任何入口去处理它。
        nodes = await load_live_nodes(db, ctx)
        subtree = _collect_subtree(nodes, root.id)
        change.detail = (
            f"彻底删除了「{root.title}」及其下面的 {len(subtree) - 1} 项"
            if purging
            else f"归档了「{root.title}」及其下面的 {len(subtree) - 1} 项(可以恢复)"
        )
        now = utcnow()

        values: dict[str, object] = {"deleted_at": now, "archive_batch_id": uuid.uuid4()}
        if purging:
            values["purged_at"] = now
        await db.execute(
            update(PlanNode)
            .where(PlanNode.id.in_(subtree), PlanNode.workspace_id == ctx.id)
            .values(**values)
            .execution_options(synchronize_session=False)
        )

        removed_count = 0
        if purging:
            # 只有这一条路上才真的删边。`NodeRelation` 一起删,理由和 `Dependency` 一样 ——
            # 归档那条路都不删,是因为它们还可能被恢复;这条路上没有"以后"。
            removed = await db.execute(
                delete(Dependency)
                .where(
                    Dependency.workspace_id == ctx.id,
                    (Dependency.predecessor_id.in_(subtree))
                    | (Dependency.successor_id.in_(subtree)),
                )
                .execution_options(synchronize_session=False)
            )
            await db.execute(
                delete(NodeRelation)
                .where(
                    NodeRelation.workspace_id == ctx.id,
                    (NodeRelation.source_node_id.in_(subtree))
                    | (NodeRelation.target_node_id.in_(subtree)),
                )
                .execution_options(synchronize_session=False)
            )
            removed_count = int(removed.rowcount or 0)

        # 事件名仍然是 `node_deleted`(它确实是一次删除),两种删除的区别放进 payload ——
        # 下游按 kind 过滤的地方不会因为多了一个 kind 而漏掉这一类事件。
        change.record(
            kind="node_deleted",
            node_id=root.id,
            payload={
                "deleted": [str(item) for item in subtree],
                "restorable": not purging,
            },
        )
        return EditResult(
            node=root,
            revision_version=change.version,
            deleted_count=len(subtree),
            removed_dependencies=removed_count,
            restorable=not purging,
        )


async def load_archived_node(
    db: AsyncSession, ctx: WorkspaceContext, node_id: uuid.UUID
) -> PlanNode:
    """取一个**已被归档**的节点。`load_node` 的反面。

    归属条件同样写进 WHERE(理由见 `load_node`)。查不到一律 `NodeNotFound` ——
    "这个节点不是你的"和"它没被归档过"返回同一个错误,不给拿 id 试探的人当路标。
    """
    node = await db.scalar(
        select(PlanNode).where(
            PlanNode.id == node_id,
            PlanNode.workspace_id == ctx.id,
            PlanNode.deleted_at.is_not(None),
        )
    )
    if node is None:
        raise NodeNotFound("这个节点不在当前空间里,或者它没有被归档过。")
    return node


async def archive_impact(
    db: AsyncSession, ctx: WorkspaceContext, node_id: uuid.UUID
) -> ArchiveImpact:
    """这一下会带走什么。**只读,不加锁。**

    不加锁是刻意的:它是给"你确定吗"那个对话框用的,而为了画一个对话框去抢工作区锁,
    会让"两个人在同一秒里删不同的东西"变成其中一个人看到一句莫名其妙的等待超时。
    代价是这份数字可能比真正执行的那一刻早几秒 —— 界面上的措辞因此是"会带走",
    而不是"已经带走"。
    """
    root = await load_node(db, ctx, node_id)
    nodes = await load_live_nodes(db, ctx)
    subtree = _collect_subtree(nodes, root.id)

    dependency_count = await _count_edges(db, ctx, subtree, relation=False)
    relation_count = await _count_edges(db, ctx, subtree, relation=True)
    sessions = list(
        await db.scalars(
            select(ScheduledSession).where(
                ScheduledSession.workspace_id == ctx.id,
                ScheduledSession.node_id.in_(subtree),
                ScheduledSession.status.not_in(plan_service.TOMBSTONE_SESSION_STATUSES),
            )
        )
    )
    today = today_in(ctx.timezone)
    return ArchiveImpact(
        node_id=root.id,
        title=root.title,
        descendants=len(subtree) - 1,
        relations=relation_count,
        dependencies=dependency_count,
        sessions=len(sessions),
        session_minutes=sum(session.planned_minutes for session in sessions),
        overdue_sessions=sum(1 for session in sessions if session.scheduled_date < today),
    )


@dataclass(frozen=True, slots=True)
class ArchivedEntry:
    """归档列表里的一行。"""

    node: PlanNode
    archived_at: datetime
    descendants: int
    sessions: int
    #: 能不能恢复。父节点在**另一次**归档里、或者父节点被彻底删除过,都会挡住它 ——
    #: 界面据此把那一条的按钮置灰并写明原因,而不是让用户点一下撞一句错误。
    restorable: bool
    blocked_reason: str | None


async def list_archive(db: AsyncSession, ctx: WorkspaceContext) -> list[ArchivedEntry]:
    """这个空间里归档过什么。新的在前。

    只列**每一次归档的根**:同一批被带走的子孙不单独出现(点那一行的"恢复"会把它们
    一起带回来,列出来只会让用户以为要一个一个恢复)。

    父节点在另一次归档里的那些仍然列出来,但标成不可恢复(见 `ArchivedEntry`)——
    它们确实还在归档里,从列表里藏起来的话,用户会以为它被删了。
    """
    archived = list(
        await db.scalars(
            select(PlanNode).where(
                PlanNode.workspace_id == ctx.id,
                PlanNode.deleted_at.is_not(None),
                # 彻底删除过的不进这个列表:它没有"恢复"这个动作可言。
                PlanNode.purged_at.is_(None),
            )
        )
    )
    by_id = {node.id: node for node in archived}
    # 批次号为空时退回**它自己一个批次**(用 id 当键),而不是按时间戳归堆:归堆会把
    # 第一行之外的那些当成"同一批的后代"从列表里藏掉,而藏起来是看不见的。
    # 这个退化路径不该出现 —— 迁移给每一行归档都补了号(见 `PlanNode.archive_batch_id`)。
    def batch_key(node: PlanNode) -> uuid.UUID:
        return node.archive_batch_id or node.id

    by_batch: dict[uuid.UUID, list[PlanNode]] = {}
    for node in archived:
        by_batch.setdefault(batch_key(node), []).append(node)

    # 父亲不在归档里的那些,要单独查一次才分得清"父亲活着"(不挡)和"父亲被彻底
    # 删除了"(挡住,而且**没有**"先恢复父亲"这条路可走)。一次查询,不做 N+1。
    outside_ids = {
        node.parent_id for node in archived if node.parent_id is not None
    } - set(by_id)
    outside: dict[uuid.UUID, PlanNode] = {}
    if outside_ids:
        outside = {
            row.id: row
            for row in await db.scalars(
                select(PlanNode).where(
                    PlanNode.id.in_(outside_ids), PlanNode.workspace_id == ctx.id
                )
            )
        }

    session_counts = await _session_counts(db, ctx, set(by_id))
    entries: list[ArchivedEntry] = []
    for node in archived:
        parent = by_id.get(node.parent_id) if node.parent_id is not None else None
        if parent is not None and batch_key(parent) == batch_key(node):
            # 和它同一批走的,列的是那一批的根。
            continue
        blocked = None
        if node.parent_id is not None:
            if parent is not None:
                blocked = "PARENT_ARCHIVED"
            else:
                outer = outside.get(node.parent_id)
                if outer is None or outer.deleted_at is not None:
                    blocked = (
                        "PARENT_PURGED"
                        if outer is not None and outer.purged_at is not None
                        else "PARENT_ARCHIVED"
                    )
        subtree = _collect_subtree(by_batch[batch_key(node)], node.id)
        entries.append(
            ArchivedEntry(
                node=node,
                archived_at=node.deleted_at,
                descendants=len(subtree) - 1,
                sessions=sum(session_counts.get(item, 0) for item in subtree),
                restorable=blocked is None,
                blocked_reason=blocked,
            )
        )
    entries.sort(key=lambda entry: (entry.archived_at, entry.node.title), reverse=True)
    return entries


async def _session_counts(
    db: AsyncSession, ctx: WorkspaceContext, node_ids: set[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """这些节点各挂着几场还没作废的排期。一次查询数完,不按节点逐条查。"""
    if not node_ids:
        return {}
    rows = await db.execute(
        select(ScheduledSession.node_id, func.count())
        .where(
            ScheduledSession.workspace_id == ctx.id,
            ScheduledSession.node_id.in_(node_ids),
            ScheduledSession.status.not_in(plan_service.TOMBSTONE_SESSION_STATUSES),
        )
        .group_by(ScheduledSession.node_id)
    )
    return {row[0]: int(row[1]) for row in rows}


async def _count_edges(
    db: AsyncSession, ctx: WorkspaceContext, subtree: set[uuid.UUID], *, relation: bool
) -> int:
    """挂在子树上的边有几条。依赖与用户画的关系各数一遍(它们在不同的表里)。"""
    if relation:
        condition = (NodeRelation.source_node_id.in_(subtree)) | (
            NodeRelation.target_node_id.in_(subtree)
        )
        table = NodeRelation
    else:
        condition = (Dependency.predecessor_id.in_(subtree)) | (
            Dependency.successor_id.in_(subtree)
        )
        table = Dependency
    count = await db.scalar(
        select(func.count()).select_from(table).where(table.workspace_id == ctx.id, condition)
    )
    return int(count or 0)


async def restore_node(
    db: AsyncSession, ctx: WorkspaceContext, node_id: uuid.UUID
) -> RestoreResult:
    """把一个归档的节点恢复回来 —— **连同当时一起被归档的那一支**。

    ## 认人的依据是批次号,不是"整棵子树",也不是时间戳

    一次归档给整棵子树上的是**同一个** `archive_batch_id`。恢复据此把"这一次带走的"
    认出来,而不是无条件地把现在这棵子树都清掉:那样会把**更早以前单独归档过**的子孙
    一起复活,而用户点的是这一行 —— 他以为只回来了一项,实际回来了一片,那一片里可能
    有他当时特意收起来的东西。

    **这个键以前是 `deleted_at` 相等**,换成了显式的号:时间戳相等要靠"两次归档不落在
    同一个微秒上"成立,那是精度的性质而不是写下来的约束,而它失效时的症状正好是这个
    功能最不该有的错(见 `PlanNode.archive_batch_id` 那段)。

    ## 三道校验,一道都不许静默通过

    1. 彻底删除过的不给恢复(`NodePurged`)—— 见 `delete_node` 的说明。
    2. 父节点还在归档里不给恢复(`ParentArchived`)—— 一个活节点挂在归档节点下面,
       在界面上永远不可达:父节点不出现,子节点就没人能导航到。
    3. 恢复之后重新验环。按现在的写入规则这**不该发生**(归档期间两端都不可见,
       加不了边),所以它是"万一发生了,不许静默"的断言,不是预期路径。

    ## 排期不自动改

    场次跟着回来(行一行没动),代价是那几场可能已经过期、或者和归档之后新排的挤在
    同一天。这里**只报不改**:静默重排等于替用户改计划,而他看到的会是"我恢复了一下,
    怎么别的安排也跟着动了"。
    """
    async with _each_change(db, ctx, trigger_detail="") as change:
        root = await load_archived_node(db, ctx, node_id)
        if root.purged_at is not None:
            raise NodePurged(f"「{root.title}」是被彻底删除的,恢复不了。")

        if root.parent_id is not None:
            parent = await db.scalar(
                select(PlanNode).where(
                    PlanNode.id == root.parent_id, PlanNode.workspace_id == ctx.id
                )
            )
            if parent is not None and parent.purged_at is not None:
                # 上层被**彻底删除**了 —— 这一项没有"先恢复父亲"那条路可走。
                # 报 `ParentArchived` 会给出一个用户照做不了的提示。
                raise NodePurged(
                    f"「{parent.title}」是被彻底删除的,它下面的这一项也回不来了。"
                )
            if parent is None or parent.deleted_at is not None:
                # 父行没了(理论上不该发生:RESTRICT 挡住级联,节点从不物理删)时,
                # 报"上层还在归档里"仍然是**可照做**的那句话;报内部错误不是。
                title = parent.title if parent is not None else "上层"
                raise ParentArchived(f"「{title}」还在归档里,先恢复它,再恢复这一项。")

        archived = list(
            await db.scalars(
                select(PlanNode).where(
                    PlanNode.workspace_id == ctx.id, PlanNode.deleted_at.is_not(None)
                )
            )
        )
        # 同一批(同一个批次号)且**不是被彻底删除的**那些。遍历只在这批里做:
        # 同一批的后代一定也在这一批里(见上面"认人的依据")。
        #
        # 批次号为空只可能是"库里的归档行没经过那次回填"(迁移给每一行都补过号),
        # 这时退化成"只恢复它自己":恢复得**少**是看得见的(子项还留在归档列表里),
        # 恢复得多则是静默的,而那正是这里最不能出的错。
        batch = (
            [root]
            if root.archive_batch_id is None
            else [
                node
                for node in archived
                if node.archive_batch_id == root.archive_batch_id and node.purged_at is None
            ]
        )
        restore_ids = _collect_subtree(batch, root.id)
        restore_ids.add(root.id)

        # 环检测用的边集合 = 恢复**之后**活着的那些边。恢复的边一直都在库里
        # (归档没有删它们),所以判据就是"两端都在(活 ∪ 要恢复)"。
        live_nodes = await load_live_nodes(db, ctx)
        alive_after = {node.id for node in live_nodes} | restore_ids
        edges = {
            (row[0], row[1])
            for row in await db.execute(
                select(Dependency.predecessor_id, Dependency.successor_id).where(
                    Dependency.workspace_id == ctx.id
                )
            )
            if row[0] in alive_after and row[1] in alive_after
        }
        cycle = find_cycle(edges)
        if cycle is not None:
            titles = {node.id: node.title for node in (*live_nodes, *archived)}
            chain = " → ".join(f"「{titles.get(item, item)}」" for item in cycle)
            raise DependencyRejected(
                f"恢复之后计划里会出现环({chain}),所以这一项没有恢复。"
                "请先调整相关的依赖,再试一次。"
            )

        await db.execute(
            update(PlanNode)
            .where(PlanNode.id.in_(restore_ids), PlanNode.workspace_id == ctx.id)
            # 批次号一起清掉,让"活着 ⇒ 没有批次"这条不变式成立:留着一个已经作废的
            # 批次号,下一个人读库时会以为它还在某一次归档里。
            .values(deleted_at=None, archive_batch_id=None)
            .execution_options(synchronize_session=False)
        )

        report = await _restore_schedule_report(db, ctx, restore_ids)
        # 挂在恢复范围上的边,恢复之前不可能"两端都活着",所以它们就是跟着重新可见的
        # 那些 —— 归档没有删过它们,这里说的是"重新可见"而不是"重新创建"。
        relations_visible = await _count_edges(db, ctx, restore_ids, relation=True) + (
            await _count_edges(db, ctx, restore_ids, relation=False)
        )
        change.detail = (
            f"恢复了「{root.title}」及其下面的 {len(restore_ids) - 1} 项"
            if len(restore_ids) > 1
            else f"恢复了「{root.title}」"
        )
        change.record(
            kind="node_restored",
            node_id=root.id,
            payload={
                "restored": [str(item) for item in restore_ids],
                "sessions": report.restored_sessions,
                "overdue": report.overdue_sessions,
            },
        )
        # 返回的那个节点要反映**恢复之后**的状态。用 `root` 对象本身不行:上面的
        # `update()` 走的是 SQL,这个 ORM 实例还带着 `deleted_at` —— 响应里会带着一个
        # 早已过期的删除时间(`synchronize_session=False` 就是为了不惊动会话里的对象)。
        # 批次号同理:它和 `deleted_at` 是同一条 SQL 一起清的,对象上也得跟着清,
        # 否则这个实例和它对应的那一行说的不是同一件事。
        root.deleted_at = None
        root.archive_batch_id = None
        return RestoreResult(
            node=root,
            revision_version=change.version,
            restored_count=len(restore_ids),
            restored_sessions=report.restored_sessions,
            restored_minutes=report.restored_minutes,
            overdue_sessions=report.overdue_sessions,
            overbooked_days=report.overbooked_days,
            relations_visible=relations_visible,
        )


def _reject_information_endpoints(predecessor: PlanNode, successor: PlanNode) -> None:
    """依赖的两端都不能是信息主题。理由见 `InformationNodeNotSchedulable`。"""
    for role, node in (("前置", predecessor), ("后继", successor)):
        if node.purpose is NodePurpose.INFORMATION:
            raise InformationNodeNotSchedulable(
                f"「{node.title}」是信息主题,不参与排期,不能作为{role}节点。"
                "要让它参与排期,先把它的用途改成「行动」。"
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
        _reject_information_endpoints(predecessor, successor)

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
# 关系(related_to / influences)
#
# 与上面的 `add_dependency` 分工:**`depends_on` 走那一组,另外两种走这一组。**
# 两者共用同一个 `_each_change`(锁 + 版本 + 事件),因为对用户来说它们都是"我改了计划";
# 但**不共用同一张表** —— 那边是排期的输入,这边不是。理由见
# `db/models/enums.py::NodeRelationType`。
#
# 接口层把三种边投影成同一个形状(`contracts/plan.py::RelationPayload`),那条分支
# 在 `plan_service.relation_of` 里,**只有那一处**。
# ---------------------------------------------------------------------------------
#: 能写进 `node_relations` 的类型。
_RELATION_TYPES: dict[str, NodeRelationType] = {
    NodeRelationType.RELATED_TO.value: NodeRelationType.RELATED_TO,
    NodeRelationType.INFLUENCES.value: NodeRelationType.INFLUENCES,
}

#: 三种类型合起来的全部取值。用在报错信息里 —— 用户填错了要能看到全部合法值,
#: 包括他其实该用的那个 `depends_on`。
ALL_RELATION_TYPES = frozenset({*_RELATION_TYPES, DEPENDS_ON})

#: 写进 `plan_revisions.trigger_detail` 的那句话里用的动词。
_RELATION_VERB: dict[NodeRelationType, str] = {
    NodeRelationType.RELATED_TO: "关联了",
    NodeRelationType.INFLUENCES: "会影响",
}


def _relation_kind(value: str) -> NodeRelationType:
    """接口上的类型 -> 存储层的类型。**只接受 `node_relations` 里的那两种。**

    `depends_on` 传进来也是 400,但调用方应当先自己把它分派给 `add_dependency` ——
    走到这里说明调用方漏了那一步,那是代码问题,所以这句报错是给开发看的。
    """
    kind = _RELATION_TYPES.get(value)
    if kind is None:
        raise InvalidInput(
            f"关系类型只能是 {' / '.join(sorted(ALL_RELATION_TYPES))},收到的是「{value}」。"
        )
    return kind


def _endpoints(kind: NodeRelationType, source: uuid.UUID, target: uuid.UUID) -> tuple[
    uuid.UUID, uuid.UUID
]:
    """这一对节点该按什么顺序存。

    `related_to` 是**无向**的:用户从 A 拖到 B 和从 B 拖到 A 是同一条边。按 UUID 的
    整数序排成固定顺序,两种画法才会落到同一行上 —— 否则唯一约束只能拦住"同一方向
    连两次",而同一对节点会并存两行,画布上就是两条重叠的虚线。

    `influences` 有向,原样存。

    按 `int` 而不是按字符串排:UUID 的序就是它的整数序,字符串序是实现细节。
    """
    if kind is NodeRelationType.INFLUENCES:
        return source, target
    return tuple(sorted((source, target), key=lambda value: value.int))  # type: ignore[return-value]


async def add_relation(
    db: AsyncSession,
    ctx: WorkspaceContext,
    *,
    source_id: uuid.UUID,
    target_id: uuid.UUID,
    relation_type: str,
    note: str | None = None,
) -> NodeRelation | Dependency:
    """连一条边。

    `depends_on` 在这里转给 `add_dependency` —— 环检测、幂等、排期语义都在那条路径上,
    不复制一份。另外两种写 `node_relations`。

    返回的是**两种表里的行之一**,投影成统一的接口形状是 `plan_service.relation_of`
    的事:服务层不该知道接口长什么样。
    """
    if source_id == target_id:
        raise InvalidInput("一个节点不能和自己连关系。")

    if relation_type == DEPENDS_ON:
        if (note or "").strip():
            # `dependencies` 没有说明列。静默丢掉用户刚写的一段话,比拒绝这次请求糟得多。
            raise InvalidInput(
                "前置关系没有说明这一栏,写在这里会丢掉。要记录解释,请另连一条"
                "「相关」或「影响」。"
            )
        return await add_dependency(
            db, ctx, predecessor_id=source_id, successor_id=target_id
        )

    kind = _relation_kind(relation_type)

    async with _each_change(db, ctx, trigger_detail="") as change:
        source = await load_node(db, ctx, source_id)
        target = await load_node(db, ctx, target_id)
        first, second = _endpoints(kind, source.id, target.id)

        existing = await db.scalar(
            select(NodeRelation).where(
                NodeRelation.workspace_id == ctx.id,
                NodeRelation.source_node_id == first,
                NodeRelation.target_node_id == second,
                NodeRelation.relation_type == kind,
            )
        )
        if existing is not None:
            # 幂等:这条边本来就在,不报错也不重复插。**也不覆盖说明** ——
            # 第二次没写说明时,第一次写的那句应该还在。
            return existing

        change.detail = f"「{source.title}」{_RELATION_VERB[kind]}「{target.title}」"
        relation = NodeRelation(
            workspace_id=ctx.id,
            source_node_id=first,
            target_node_id=second,
            relation_type=kind,
            note=(note or None),
            origin=NodeOrigin.USER,
        )
        db.add(relation)
        await db.flush()
        change.record(
            kind="relation_added",
            node_id=second,
            payload={"sourceId": str(first), "relationType": kind.value},
        )
        return relation


async def load_relation(
    db: AsyncSession, ctx: WorkspaceContext, relation_id: uuid.UUID
) -> NodeRelation | Dependency:
    """按 id 找一条边,**两张表都找**。

    接口上三种边共用一个 id 空间(见 `RelationPayload`),所以按 id 删/改的时候,
    服务端得知道这个 id 属于哪张表。两张表的 id 都是 UUID,不会撞车。

    查不到就是 `RelationNotFound` —— 与节点、空间同一条纪律:**不存在**和
    **属于别人**返回同一个错误,否则拿 id 逐个试就能测出别人空间里有什么。
    """
    relation = await db.scalar(
        select(NodeRelation).where(
            NodeRelation.id == relation_id, NodeRelation.workspace_id == ctx.id
        )
    )
    if relation is not None:
        return relation
    dependency = await db.scalar(
        select(Dependency).where(
            Dependency.id == relation_id, Dependency.workspace_id == ctx.id
        )
    )
    if dependency is not None:
        return dependency
    raise RelationNotFound("这条关系不在当前空间里。")


async def update_relation(
    db: AsyncSession, ctx: WorkspaceContext, relation_id: uuid.UUID, values: dict[str, object]
) -> NodeRelation:
    """改一条边:换类型(`related_to` <-> `influences`)或改说明。

    **换不成 `depends_on`,这一版明确拒绝。** 那意味着把这一行搬进 `dependencies`
    表 —— 它会开始参与排期,而那张表**没有说明列**,搬过去用户刚写的那段解释就没了。
    宁可不做,也不要静默丢掉别人写的东西。(搬家的另一半代价是环检测:一条 `related_to`
    可以成环,变成前置就必须先查环。)

    前置关系这里也改不了,理由同上 —— 它连说明都没有,能被改的只有 `lag_days`,
    而那是排期参数,不该出现在画布的关系编辑器里。
    """
    note = values.get("note")
    if isinstance(note, str):
        note = note.strip() or None

    relation = await load_relation(db, ctx, relation_id)
    if isinstance(relation, Dependency):
        raise RelationRejected(
            "前置关系不能在画布上改 —— 它决定排期,而且没有说明这一栏。"
            "想去掉它就在边上点删除;想留一段解释,另连一条「相关」或「影响」。"
        )

    next_type = values.get("relation_type")
    if next_type == DEPENDS_ON:
        raise RelationRejected(
            "这一版不支持把一条关系改成前置关系 —— 那会改变排期,而前置关系没有说明"
            "这一栏,现在写在这条边上的解释会丢掉。要去掉它再重新连一条前置。"
        )
    if next_type is not None and next_type == relation.relation_type.value:
        # 类型没变,不是错误,也别让它进下面的"换类型"分支去查重。
        next_type = None

    async with _each_change(db, ctx, trigger_detail="改了关系的说明") as change:
        if next_type is not None:
            kind = _relation_kind(str(next_type))
            first, second = _endpoints(kind, relation.source_node_id, relation.target_node_id)
            # 换完之后唯一约束可能撞上一条已经存在的边(比如 A 和 B 之间既有「相关」
            # 又有「影响」,把「影响」也改成「相关」)。**在写之前查** ——
            # 让唯一约束在 commit 时抛错的话,用户看到的是一句和关系毫无关系的数据库异常。
            clash = await db.scalar(
                select(NodeRelation.id).where(
                    NodeRelation.workspace_id == ctx.id,
                    NodeRelation.source_node_id == first,
                    NodeRelation.target_node_id == second,
                    NodeRelation.relation_type == kind,
                    NodeRelation.id != relation.id,
                )
            )
            if clash is not None:
                raise InvalidInput("这两个节点之间已经有一条同样的关系了。")
            relation.source_node_id = first
            relation.target_node_id = second
            relation.relation_type = kind
            change.detail = "把一条关系换成了另一种"

        if "note" in values:
            relation.note = note  # type: ignore[assignment]

        await db.flush()
        change.record(
            kind="relation_updated",
            node_id=relation.target_node_id,
            payload={"sourceId": str(relation.source_node_id)},
        )
        return relation


async def remove_relation(db: AsyncSession, ctx: WorkspaceContext, relation_id: uuid.UUID) -> bool:
    """删一条边。**不删节点。**

    前置关系转给 `remove_dependency`(那一条已经有"不存在的依赖不算错"的语义),
    另外两种从 `node_relations` 删。
    """
    relation = await load_relation(db, ctx, relation_id)
    if isinstance(relation, Dependency):
        await remove_dependency(
            db, ctx, predecessor_id=relation.predecessor_id, successor_id=relation.successor_id
        )
        return True

    target_id = relation.target_node_id
    source_id = relation.source_node_id
    async with _each_change(db, ctx, trigger_detail="去掉了一条关系") as change:
        result = await db.execute(
            delete(NodeRelation)
            .where(NodeRelation.id == relation.id, NodeRelation.workspace_id == ctx.id)
            .execution_options(synchronize_session=False)
        )
        if int(result.rowcount or 0):
            change.record(
                kind="relation_removed",
                node_id=target_id,
                payload={"sourceId": str(source_id)},
            )
        return bool(result.rowcount or 0)


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
@dataclass(frozen=True, slots=True)
class _ScheduleReport:
    """恢复报告的排期那一半。放在私有类型里,是因为它是 `RestoreResult` 的中间产物。"""

    restored_sessions: int
    restored_minutes: int
    overdue_sessions: int
    overbooked_days: tuple[OverbookedDay, ...]


async def _restore_schedule_report(
    db: AsyncSession, ctx: WorkspaceContext, restore_ids: set[uuid.UUID]
) -> _ScheduleReport:
    """恢复之后排期那边会变成什么样。**只读,一行都不改。**

    两件事各算一遍,而它们的口径不同,这不是笔误:

    - **过期**:只看被恢复的这些场次自己。一场安排在昨天的"待做"是用户恢复之后
      必须自己处理的东西(要么补做,要么挪走)。
    - **超上限**:要**按人**重算。容量不是按空间的(见 `schedule_service` 开头:
      两个空间争的是同一个晚上),所以那一天超没超,得把这个人的全部活动空间里
      那一天的场次加起来看。只算被恢复的那几天,不是全量重排。

    一句实话:**这一栏比的是每日上限(`daily_cap`),没有重跑可用时段那一段。**
    用户那天本来就没有可用时段(比如请了假)时,这里会**少报**。宁可少报也不假装
    自己重排过一遍 —— 真排不下的那天,排期器下次预览时会自己说出来。
    """
    today = today_in(ctx.timezone)
    sessions = list(
        await db.scalars(
            select(ScheduledSession).where(
                ScheduledSession.workspace_id == ctx.id,
                ScheduledSession.node_id.in_(restore_ids),
                ScheduledSession.status.not_in(plan_service.TOMBSTONE_SESSION_STATUSES),
            )
        )
    )
    overdue = sum(
        1
        for session in sessions
        if session.scheduled_date < today
        and session.status
        in (ScheduledSessionStatus.PLANNED, ScheduledSessionStatus.IN_PROGRESS)
    )
    minutes = sum(session.planned_minutes for session in sessions)

    upcoming = sorted({session.scheduled_date for session in sessions if session.scheduled_date >= today})
    if not upcoming:
        return _ScheduleReport(len(sessions), minutes, overdue, ())

    rows = await db.execute(
        select(ScheduledSession.scheduled_date, func.sum(ScheduledSession.planned_minutes))
        .join(PlanNode, PlanNode.id == ScheduledSession.node_id)
        .join(Workspace, Workspace.id == ScheduledSession.workspace_id)
        .where(
            ScheduledSession.user_id == ctx.user.user_id,
            ScheduledSession.scheduled_date.in_(upcoming),
            ScheduledSession.status.not_in(plan_service.TOMBSTONE_SESSION_STATUSES),
            PlanNode.deleted_at.is_(None),
            Workspace.status == WorkspaceStatus.ACTIVE,
        )
        .group_by(ScheduledSession.scheduled_date)
    )
    active_workspaces = tuple(
        await db.scalars(
            select(Workspace.id).where(
                Workspace.owner_id == ctx.user.user_id,
                Workspace.status == WorkspaceStatus.ACTIVE,
            )
        )
    )
    cap = daily_cap(
        await schedule_service.capacity_profile(db, ctx.user.user_id, active_workspaces)
    )
    overbooked = tuple(
        OverbookedDay(day=row[0], planned_minutes=int(row[1] or 0), daily_cap=cap)
        for row in rows
        if int(row[1] or 0) > cap
    )
    return _ScheduleReport(len(sessions), minutes, overdue, overbooked)


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
    "ALL_RELATION_TYPES",
    "EDITABLE_FIELDS",
    "EditResult",
    "add_dependency",
    "add_relation",
    "create_node",
    "delete_node",
    "load_dependencies",
    "load_live_nodes",
    "load_node",
    "load_relation",
    "remove_dependency",
    "remove_relation",
    "update_node",
    "update_relation",
]
