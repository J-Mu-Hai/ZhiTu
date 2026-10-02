"""这次分析是**在什么输入上**做的 —— 一份可以事后比较的记录。

## 它为什么必须存在

判断"这条分析是不是已经过期了"不能靠时间推测:昨天的分析和今天的数据完全可能一致,
一分钟前做的分析也可能已经作废。它只能靠**把当时那份输入记下来,和现在比一比**。

所以这个模块做两件事:

1. `capture` —— 把一次分析用到的输入(范围内的节点、祖先链、关系、时间预算、排期与
   执行)记成一份 `InputSnapshot`,它会随分析记录一起落库。
2. `to_payload` / `from_payload` —— 那份记录的**存储形状**。这是落库的契约:库里躺着
   的是历史行,字段一旦发布就不能随手改名,读的时候必须还能读。

## 什么算"变了"

- 范围内节点的**新增、删除、归档、恢复、移动、正文版本变化、标题变化、
  长笔记版本变化** → 算。
- 范围内节点的**关系边**(前置 / 关联 / 影响)增删 → 算。
- 本次分析用到的**时间预算**(每周可投入、个人容量、可用时段)变化 → 算。
- 范围内节点的**已排场次与执行记录**变化 → 算。
- **布局、视口、当前选中的节点**变化 → **不算**。它们回答的是"用户正在看哪儿",
  不是分析赖以成立的输入。把它们算进来的后果是用户拖一下画布就得到处点
  重新分析,而那个提示很快就没有人看了。

## 为什么还压了一层摘要

范围内的节点可能上百(逐个记进 JSON 会让这一列无谓地变大),所以逐个记的只有
**这一轮真的读到的那一批**;整个范围的结构另外压成一个摘要(规范化 JSON 的 blake2b)。
比较于是有两种结论:逐个记的那些能说出**具体变了什么**,只有摘要不同的至少能说
**范围内有变化** —— 而不是假装没看见。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select

from backend.db.models import (
    AvailabilityException,
    AvailabilityRule,
    Dependency,
    ExecutionRecord,
    NodeNote,
    NodeRelation,
    PlanningBrief,
    PlanNode,
    ScheduledSession,
    UserCapacityProfile,
)

#: 逐个记进快照的节点数上限。超过它时结构摘要仍然覆盖全部范围 ——
#: 逐个记是为了能说出"具体是谁变了",记不下不代表不比。
MAX_SNAPSHOT_NODES = 200

#: 快照里标题留多长。标题只用来把"变了什么"说清楚,不是用来重建内容。
TITLE_LIMIT = 80


@dataclass(frozen=True, slots=True)
class SnapshotNode:
    """范围内的一个节点在**那一刻**的样子。

    `content_version` 是重点:它是正文的版本号,正文改了它就变了。标题、位置、状态
    也一起记 —— 它们都在这份分析"读到过"的东西里。

    ## `note_version` 与 `content_version` 并排,不是一个

    300 字简述与 20,000 字长笔记是**两份不同的文本、两个不同的版本号**
    (`node_notes` 有自己的 `content_version`,理由见那个模型的 docstring:
    冲突检测的范围要和冲突的范围一样大)。所以快照里也是两个字段。

    少了 `note_version` 的后果不是"比较不准",是**整整一类编辑对失效判定隐形**:
    用户把一段长笔记从头写过,而所有基于旧笔记做的分析仍然显示「最新」。
    这不是一个漏报,它是一条没有报错、也没有日志的错误结论。

    语义与 `note_service.payload_of` 对齐:还没有写过笔记 = 第 0 版。于是"没有笔记"
    与"笔记是空的"在快照里是同一个值 —— 它们本来就是同一件事(见 `note_service.save`
    里那段"不给用户两件看不出区别的事")。
    """

    node_id: str
    parent_id: str | None
    depth: int
    status: str
    content_version: int
    title: str
    deleted: bool
    note_version: int = 0

    def to_payload(self) -> dict:
        return {
            "nodeId": self.node_id,
            "parentId": self.parent_id,
            "depth": self.depth,
            "status": self.status,
            "contentVersion": self.content_version,
            "title": self.title,
            "deleted": self.deleted,
            "noteVersion": self.note_version,
        }

    @classmethod
    def from_payload(cls, raw: dict) -> SnapshotNode:
        return cls(
            node_id=str(raw["nodeId"]),
            parent_id=None if raw.get("parentId") is None else str(raw["parentId"]),
            depth=int(raw.get("depth") or 0),
            status=str(raw.get("status") or ""),
            content_version=int(raw.get("contentVersion") or 0),
            title=str(raw.get("title") or ""),
            deleted=bool(raw.get("deleted")),
            # 老行里没有这个键(这一列是随本次改进加的)。缺了就是 0 —— 而 0 在这里
            # 恰好是正确的那一侧:它说的是"那份笔记没有变过",于是老分析不会因为
            # 一次升级而集体变成"已过期"。
            note_version=int(raw.get("noteVersion") or 0),
        )


@dataclass(frozen=True, slots=True)
class InputSnapshot:
    """一次分析的输入记录。**只记事实,不做判断。**"""

    #: 这次分析的作用范围起点。None 表示"整个空间"(那时候没有更窄的范围可说)。
    scope_root_id: str | None
    #: 本轮讨论的那个节点(用户正看着它)。None 表示用户没有指定。
    focus_node_id: str | None
    #: 这个空间当时存活的节点总数。用来发现"范围外新增的东西让这次分析该重来",
    #: 也用来如实说明"这一轮其实只读到了多少"。
    live_node_count: int
    #: 逐个记下的节点(**含范围内的祖先链**),按 id 排序,好比较。
    nodes: tuple[SnapshotNode, ...]
    #: 整个范围(含逐个记不下、含已删除的行)的结构摘要。
    structure_digest: str
    #: 范围涉及的关系边,规范化的字符串,排过序。
    edges: tuple[str, ...]
    #: 每周可投入(分钟)。None 表示当时**还没问到** —— 不是零。
    weekly_available_minutes: int | None
    #: 简报的版本号。用户改了口径而值恰好没变(240 → 240 但换了说法)也算变。
    brief_version: int | None
    #: 个人容量、可用时段、已排场次、执行记录各自的摘要。
    capacity_digest: str
    availability_digest: str
    schedule_digest: str
    execution_digest: str
    #: 逐个记的节点被 `MAX_SNAPSHOT_NODES` 截断过。
    truncated: bool

    def to_payload(self) -> dict:
        return {
            "scopeRootId": self.scope_root_id,
            "focusNodeId": self.focus_node_id,
            "liveNodeCount": self.live_node_count,
            "nodes": [node.to_payload() for node in self.nodes],
            "structureDigest": self.structure_digest,
            "edges": list(self.edges),
            "weeklyAvailableMinutes": self.weekly_available_minutes,
            "briefVersion": self.brief_version,
            "capacityDigest": self.capacity_digest,
            "availabilityDigest": self.availability_digest,
            "scheduleDigest": self.schedule_digest,
            "executionDigest": self.execution_digest,
            "truncated": self.truncated,
        }

    @classmethod
    def from_payload(cls, raw: dict) -> InputSnapshot:
        return cls(
            scope_root_id=None if raw.get("scopeRootId") is None else str(raw["scopeRootId"]),
            focus_node_id=None if raw.get("focusNodeId") is None else str(raw["focusNodeId"]),
            live_node_count=int(raw.get("liveNodeCount") or 0),
            nodes=tuple(SnapshotNode.from_payload(item) for item in raw.get("nodes") or ()),
            structure_digest=str(raw.get("structureDigest") or ""),
            edges=tuple(str(edge) for edge in raw.get("edges") or ()),
            weekly_available_minutes=raw.get("weeklyAvailableMinutes"),
            brief_version=raw.get("briefVersion"),
            capacity_digest=str(raw.get("capacityDigest") or ""),
            availability_digest=str(raw.get("availabilityDigest") or ""),
            schedule_digest=str(raw.get("scheduleDigest") or ""),
            execution_digest=str(raw.get("executionDigest") or ""),
            truncated=bool(raw.get("truncated")),
        )


def _digest(value: object) -> str:
    """规范化 JSON 的 blake2b,与 `proposal_service._hash` 同一个约定:
    键排序、非 ASCII 不转义、认不出来的类型按字符串处理。"""
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.blake2b(encoded.encode("utf-8"), digest_size=32).hexdigest()


def _node_key(node: PlanNode) -> tuple:
    """结构摘要里一个节点的那一份。**存活的与已删除的用同一个形状** ——
    归档、恢复、彻底删除都表现为这一份的变化。

    ## 为什么这里**没有** `content_version`

    因为它跟"结构"是两件事,而两者的失效范围不一样:

        结构变化(新增/删除/移动/归档)  整个范围都要算  -> 摘要覆盖全部
        内容变化(标题/正文/长笔记)     只有读到过的才算 -> 逐个记的那批覆盖

    把一个未被读取的分支的正文改动也算进来的后果,是用户改了别处一句话、这里
    每一条分析都变成"已过期" —— 那正是规范点名不要的行为("没有读取也不影响本次
    决策的无关分支,不应使所有分析一起失效")。

    **长笔记的版本号(`note_version`)属于"内容变化"这一栏**,所以它在
    `SnapshotNode` 上而不在这里 —— 同一条理由,一个字都不用改。

    反过来,节点**在不在、挂在谁下面、归档没有**必须是全范围的事实:靠"我读到的
    那 80 个"去判断"范围里有没有多出一个节点",在节点多起来的那天会开始漏。
    """
    return (
        str(node.id),
        str(node.parent_id) if node.parent_id else None,
        node.depth,
        node.status.value,
        node.deleted_at.isoformat() if node.deleted_at else None,
    )


def _digest_rows(rows: Iterable[Sequence[object]]) -> str:
    """一批"一行一个列表"的事实,按规范化 JSON 排序后取摘要。

    排序的键是那一行的 JSON 文本,不是那一行本身:同一列里混着 `None` 和整数时,
    Python 的列表比较会在第一个不同的位置上拿 `None` 和 `int` 比大小,直接抛
    `TypeError` —— 那会是一次"某个任务没填实际用时"引发的 500。
    """
    return _digest(sorted(json.dumps(row, sort_keys=True, ensure_ascii=False) for row in rows))


def _edge_key(kind: str, source: uuid.UUID, target: uuid.UUID, subtype: str) -> str:
    """一条边的规范化字符串。做成字符串而不是结构,是因为它要进 JSON 快照、
    要按字典序排序,而"两个字符串比一比"最不容易在比较那一侧写错。"""
    return f"{kind}:{subtype}:{source}>{target}"


async def _all_node_rows(db, workspace_id: uuid.UUID) -> list[PlanNode]:
    """这个空间的**全部**节点行,含已删除的。**没有 limit。**

    范围解析与结构摘要都必须看得见全部:靠"读到的前 80 个"去判断"范围里有没有
    多出一个节点",在节点多起来的那天会开始漏。
    """
    result = await db.execute(
        select(PlanNode)
        .where(PlanNode.workspace_id == workspace_id)
        .order_by(PlanNode.depth.asc(), PlanNode.order_index.asc(), PlanNode.created_at.asc())
    )
    return list(result.scalars())


def _scope_ids(root_id: uuid.UUID, rows: Sequence[PlanNode]) -> set[uuid.UUID]:
    """一棵子树(含根自己)。用显式栈不用递归 —— 深树会撞 Python 的递归深度,
    而那个报错和"收子节点"看起来毫无关系。"""
    children: dict[uuid.UUID, list[uuid.UUID]] = {}
    for node in rows:
        if node.parent_id is not None:
            children.setdefault(node.parent_id, []).append(node.id)

    collected: set[uuid.UUID] = set()
    pending = [root_id]
    while pending:
        current = pending.pop()
        if current in collected:
            continue
        collected.add(current)
        pending.extend(children.get(current, ()))
    return collected


def _ancestor_ids(focus_id: uuid.UUID | None, rows: Sequence[PlanNode]) -> list[uuid.UUID]:
    """从根到焦点的祖先链(不含焦点自己),顺序从根往下。"""
    if focus_id is None:
        return []
    parent_of = {node.id: node.parent_id for node in rows}
    chain: list[uuid.UUID] = []
    current = parent_of.get(focus_id)
    seen: set[uuid.UUID] = set()
    while current is not None and current not in seen:
        seen.add(current)
        chain.append(current)
        current = parent_of.get(current)
    chain.reverse()
    return chain


async def capture(
    db,
    ctx,
    *,
    scope_root_id: uuid.UUID | None,
    focus_node_id: uuid.UUID | None,
    window: Sequence[PlanNode],
    window_truncated: bool,
) -> InputSnapshot:
    """把这一刻的输入记下来。**只读。**

    `window` 是本轮真的送进模型的那批节点(含它们的正文),`window_truncated` 表示
    还有节点因为上限没被读到 —— 两者都会进快照,因为"我读到的是这些"正是这次分析
    的输入本身。
    """
    rows = await _all_node_rows(db, ctx.id)
    live = [node for node in rows if node.deleted_at is None]
    by_id = {node.id: node for node in rows}

    scope_id = scope_root_id if scope_root_id in by_id else None
    in_scope = _scope_ids(scope_id, rows) if scope_id is not None else {node.id for node in live}
    ancestors = _ancestor_ids(focus_node_id, rows)
    # 记哪些节点:这一轮读到的那些 + 范围里的结构(上限 MAX_SNAPSHOT_NODES)。
    detail_ids = {node.id for node in window}
    detail_ids.update(ancestors)
    coverage = set(in_scope)
    coverage.update(ancestors)
    coverage.update(detail_ids)

    detail_extra = sorted(
        (node for node in rows if node.id in coverage and node.id not in detail_ids),
        key=lambda node: (node.depth, node.order_index, str(node.id)),
    )
    room = max(0, MAX_SNAPSHOT_NODES - len(detail_ids))
    kept = [node for node in rows if node.id in detail_ids]
    kept.extend(detail_extra[:room])
    kept.sort(key=lambda node: (node.depth, node.order_index, str(node.id)))
    truncated = len(kept) < len(coverage)

    digest_rows = [node for node in rows if node.id in coverage]
    structure = _digest([_node_key(node) for node in digest_rows])

    note_versions = await _note_versions(db, ctx.id, [node.id for node in kept])

    return InputSnapshot(
        scope_root_id=None if scope_id is None else str(scope_id),
        focus_node_id=None if focus_node_id is None else str(focus_node_id),
        live_node_count=len(live),
        nodes=tuple(
            SnapshotNode(
                node_id=str(node.id),
                parent_id=None if node.parent_id is None else str(node.parent_id),
                depth=node.depth,
                status=node.status.value,
                content_version=node.content_version,
                title=(node.title or "")[:TITLE_LIMIT],
                deleted=node.deleted_at is not None,
                note_version=note_versions.get(node.id, 0),
            )
            for node in kept
        ),
        structure_digest=structure,
        edges=await _edges(db, ctx.id, coverage),
        weekly_available_minutes=await _weekly_minutes(db, ctx.id),
        brief_version=await _brief_version(db, ctx.id),
        capacity_digest=await _capacity_digest(db, ctx),
        availability_digest=await _availability_digest(db, ctx),
        schedule_digest=await _schedule_digest(db, ctx, in_scope),
        execution_digest=await _execution_digest(db, ctx, in_scope),
        truncated=truncated or window_truncated,
    )


async def _note_versions(
    db, workspace_id: uuid.UUID, node_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """这一批节点的长笔记版本号。**一次聚合查询,不是每节点一次。**

    没有行的节点不在这个字典里,由调用方当成 0 版(与
    `note_service.payload_of` 对"从来没写过"的处理一致)。

    这里刻意只读 `node_id` 与 `content_version` 两列 —— **不读正文**。快照会被
    序列化成一个 JSON 列,每个节点的 20,000 字笔记塞进去会让每一次分析都给
    `node_analyses` 添上一份几十万字符的副本,而比较用到的只是一个整数。
    """
    if not node_ids:
        return {}
    result = await db.execute(
        select(NodeNote.node_id, NodeNote.content_version).where(
            NodeNote.workspace_id == workspace_id, NodeNote.node_id.in_(node_ids)
        )
    )
    return dict(result.all())


async def _edges(db, workspace_id: uuid.UUID, coverage: set[uuid.UUID]) -> tuple[str, ...]:
    """范围涉及的关系边。**一端在范围里就算** —— 跨出范围的那一条也改变了
    范围内某个节点的处境,而这次分析正是基于它做的。"""
    found: list[str] = []

    dependencies = await db.execute(select(Dependency).where(Dependency.workspace_id == workspace_id))
    for edge in dependencies.scalars():
        if edge.predecessor_id in coverage or edge.successor_id in coverage:
            found.append(
                _edge_key("dep", edge.predecessor_id, edge.successor_id, edge.dep_type.value)
            )

    relations = await db.execute(
        select(NodeRelation).where(NodeRelation.workspace_id == workspace_id)
    )
    for edge in relations.scalars():
        if edge.source_node_id in coverage or edge.target_node_id in coverage:
            found.append(
                _edge_key(
                    "rel", edge.source_node_id, edge.target_node_id, edge.relation_type.value
                )
            )

    return tuple(sorted(found))


async def _weekly_minutes(db, workspace_id: uuid.UUID) -> int | None:
    brief = (
        await db.execute(select(PlanningBrief).where(PlanningBrief.workspace_id == workspace_id))
    ).scalar_one_or_none()
    return None if brief is None else brief.weekly_available_minutes


async def _brief_version(db, workspace_id: uuid.UUID) -> int | None:
    brief = (
        await db.execute(select(PlanningBrief).where(PlanningBrief.workspace_id == workspace_id))
    ).scalar_one_or_none()
    return None if brief is None else brief.version


async def _capacity_digest(db, ctx) -> str:
    """个人时间预算。**跨空间共享** —— 它在别处被改了,这里的分析同样作废。"""
    profile = (
        await db.execute(
            select(UserCapacityProfile).where(UserCapacityProfile.user_id == ctx.owner_id)
        )
    ).scalar_one_or_none()
    if profile is None:
        return _digest(None)
    return _digest(
        {
            "weeklyTotalMinutes": profile.weekly_total_minutes,
            "safetyFactor": str(profile.safety_factor),
            "dailyMaxMinutes": profile.daily_max_minutes,
            "defaultBufferMinutes": profile.default_buffer_minutes,
            "minSessionMinutes": profile.min_session_minutes,
            "maxSessionMinutes": profile.max_session_minutes,
            "weekStartWeekday": profile.week_start_weekday,
        }
    )


async def _availability_digest(db, ctx) -> str:
    """可用时段:周期规则与逐日例外。"""
    rules = await db.execute(
        select(AvailabilityRule)
        .where(AvailabilityRule.user_id == ctx.owner_id)
        .order_by(AvailabilityRule.weekday.asc(), AvailabilityRule.start_minute.asc())
    )
    exceptions = await db.execute(
        select(AvailabilityException)
        .where(AvailabilityException.user_id == ctx.owner_id)
        .order_by(AvailabilityException.on_date.asc())
    )
    return _digest(
        {
            "rules": [
                [
                    rule.weekday,
                    rule.start_minute,
                    rule.end_minute,
                    rule.source.value,
                    _iso(rule.effective_from),
                    _iso(rule.effective_to),
                ]
                for rule in rules.scalars()
            ],
            "exceptions": [
                [item.on_date.isoformat(), item.available_minutes, item.is_unavailable]
                for item in exceptions.scalars()
            ],
        }
    )


def _iso(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


async def _schedule_digest(db, ctx, in_scope: set[uuid.UUID]) -> str:
    """范围内节点的已排场次。`seq` 与时间戳不进摘要 —— 它们会因为一次无意义的
    重写而变,而那种变化不影响任何结论。"""
    if not in_scope:
        return _digest_rows(())
    rows = await db.execute(
        select(ScheduledSession).where(ScheduledSession.workspace_id == ctx.id)
    )
    return _digest_rows(
        [
            str(session.node_id),
            session.scheduled_date.isoformat(),
            session.planned_minutes,
            session.status.value,
            session.locked,
        ]
        for session in rows.scalars()
        if session.node_id in in_scope
    )


async def _execution_digest(db, ctx, in_scope: set[uuid.UUID]) -> str:
    """范围内节点的执行记录:实际做了多久、结果如何。"""
    if not in_scope:
        return _digest_rows(())
    rows = await db.execute(
        select(ExecutionRecord).where(ExecutionRecord.workspace_id == ctx.id)
    )
    return _digest_rows(
        [
            str(record.node_id),
            record.result.value,
            record.actual_minutes,
            None if record.completion_ratio is None else str(record.completion_ratio),
        ]
        for record in rows.scalars()
        if record.node_id in in_scope
    )


__all__ = [
    "MAX_SNAPSHOT_NODES",
    "InputSnapshot",
    "SnapshotNode",
    "capture",
]
