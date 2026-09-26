"""依赖图:拓扑序,以及"这件事最早能哪天开始"。

这一条对应产品规则 (a) 里的"依赖与截止":一个任务不可能排到它的前置任务**做完之前**。

## 早开始日是**算出来的**,不是问出来的

`earliest_start` 不是节点上的一个字段,而是"前置任务实际落在哪一天"推出来的。所以它
不能在读数据时算一次就算完 —— 排前面的任务往后挪了一天,后面整条链的早开始日都要跟着
动。`schedule.py` 因此在这个模块的帮助下**边排边维护**一张"某事哪天做完"的表。

## 排序必须完全确定

同一份输入排两次,输出要逐字节相同(`schedule_version` 靠它)。所以拓扑序里的并列项
一律用同一组显式键打破:`优先级 → 截止日 → 深度 → order_index → id`。

不这样做的话,并列项的先后取决于 `dict` 的插入顺序 —— 而插入顺序取决于 SQL 的返回
顺序,那取决于查询计划。表现是"同一份计划,今天排出来和明天排出来不一样",而没有任何
东西会报错。
"""

from __future__ import annotations

import heapq
import uuid
from datetime import date, timedelta

from backend.scheduler.errors import DependencyCycleError, UnknownNodeReferenceError
from backend.scheduler.types import (
    ScheduleDependency,
    ScheduleNode,
    priority_rank,
)

#: 没有截止日时的排序占位。用一个远未来的日期,好让"有截止日的排前面"这件事在
#: 比较时自然成立,而不必在比较函数里到处判 None。
_NO_DEADLINE = date.max


def sort_key(node: ScheduleNode) -> tuple:
    """排期顺序的**唯一**依据。所有并列项都在这条链上被打破。"""
    return (
        priority_rank(node.priority),
        node.deadline or _NO_DEADLINE,
        node.depth,
        node.order_index,
        str(node.id),
    )


def predecessors_of(
    dependencies: tuple[ScheduleDependency, ...],
) -> dict[uuid.UUID, tuple[ScheduleDependency, ...]]:
    """后继 -> 它的全部前置边。"""
    table: dict[uuid.UUID, list[ScheduleDependency]] = {}
    for dependency in dependencies:
        table.setdefault(dependency.successor_id, []).append(dependency)
    return {
        successor: tuple(sorted(edges, key=lambda edge: (edge.lag_days, str(edge.predecessor_id))))
        for successor, edges in table.items()
    }


def successors_of(
    dependencies: tuple[ScheduleDependency, ...],
) -> dict[uuid.UUID, tuple[uuid.UUID, ...]]:
    """前置 -> 它的全部后继 id。**波纹式修复靠它界定影响范围。**"""
    table: dict[uuid.UUID, list[uuid.UUID]] = {}
    for dependency in dependencies:
        table.setdefault(dependency.predecessor_id, []).append(dependency.successor_id)
    return {key: tuple(sorted(values, key=str)) for key, values in table.items()}


def validate_references(
    nodes: tuple[ScheduleNode, ...], dependencies: tuple[ScheduleDependency, ...]
) -> None:
    """依赖的两端都必须在本次请求的节点里。

    **不能"忽略掉不认识的边"就算完。** 一条被忽略的依赖意味着"后置任务排在了前置
    任务之前",而排出来的计划看起来完全合理 —— 用户没有任何办法发现。所以这里抛。
    """
    known = {node.id for node in nodes}
    for dependency in dependencies:
        for side, node_id in (
            ("predecessor", dependency.predecessor_id),
            ("successor", dependency.successor_id),
        ):
            if node_id not in known:
                raise UnknownNodeReferenceError(
                    f"依赖的 {side} 端 {node_id} 不在本次排期的节点集合里"
                )


def topological_order(
    nodes: tuple[ScheduleNode, ...], dependencies: tuple[ScheduleDependency, ...]
) -> tuple[uuid.UUID, ...]:
    """依赖在前、后继在后的一个确定顺序。成环则抛 `DependencyCycleError`。

    只用依赖边来定序,**不用父子层级**:父子是"属于"关系,不是"先后"关系。把
    parent 也当成一条边的话,一个阶段下的所有任务会被强行串成一条链,而它们本来
    可以并行。
    """
    validate_references(nodes, dependencies)
    by_id = {node.id: node for node in nodes}
    indegree = {node.id: 0 for node in nodes}
    outgoing: dict[uuid.UUID, list[uuid.UUID]] = {node.id: [] for node in nodes}

    for dependency in dependencies:
        indegree[dependency.successor_id] += 1
        outgoing[dependency.predecessor_id].append(dependency.successor_id)

    # 用堆而不是队列:入度为 0 的一组节点里,谁先排要由 `sort_key` 说了算。
    # 普通 BFS 的顺序取决于节点在输入里的位置 —— 而那取决于 SQL 的返回顺序。
    ready = [(sort_key(by_id[node_id]), str(node_id), node_id) for node_id, degree in indegree.items() if degree == 0]
    heapq.heapify(ready)

    order: list[uuid.UUID] = []
    while ready:
        _, _, node_id = heapq.heappop(ready)
        order.append(node_id)
        for successor in sorted(outgoing[node_id], key=str):
            indegree[successor] -= 1
            if indegree[successor] == 0:
                heapq.heappush(ready, (sort_key(by_id[successor]), str(successor), successor))

    if len(order) != len(by_id):
        stuck = sorted(str(node_id) for node_id, degree in indegree.items() if degree > 0)
        raise DependencyCycleError(
            "依赖成环,无法定出先后顺序。涉及节点:" + "、".join(stuck[:10])
        )
    return tuple(order)


def earliest_start(
    node_id: uuid.UUID,
    *,
    predecessors: dict[uuid.UUID, tuple[ScheduleDependency, ...]],
    finish_days: dict[uuid.UUID, date],
    fallback: date,
) -> tuple[date, bool]:
    """这个节点最早能哪天开始。返回 `(日期, 是否需要等前置)`。

    第二项是给缺口报告用的:"排不进 9 月 30 日"和"要等前置任务做到 10 月 8 日"是
    两种不同的解释,而用户能做的事完全不同 —— 前者可以少做点或延期,后者只能去
    看那条依赖是不是建错了。

    前置**还没有** `finish_days` 记录时按 `fallback` 处理:它要么排在更后面(拓扑序
    保证了不会),要么根本不参与排期(没工时、或已完成)—— 后者不该拖住后继。
    """
    edges = predecessors.get(node_id, ())
    if not edges:
        return fallback, False

    waiting = False
    latest = fallback
    for edge in edges:
        finished = finish_days.get(edge.predecessor_id)
        if finished is None:
            continue
        # finish-to-start + lag:前置做完的**第二天**才能开始,再顺延 lag 天。
        candidate = finished + timedelta(days=1 + max(0, edge.lag_days))
        if candidate > latest:
            latest = candidate
            waiting = True
    return latest, waiting


def descendants(
    node_id: uuid.UUID, successors: dict[uuid.UUID, tuple[uuid.UUID, ...]]
) -> set[uuid.UUID]:
    """这个节点的全部下游(不含自己)。

    **波纹式修复用它界定影响范围**:前置动了一天,只有它的下游需要重新检查 ——
    而不是整条视界重排一遍。朴素做法(从头重排)会重洗整个日历,那正是产品文档里
    "用户每天改计划、系统失去稳定性"的来源。
    """
    seen: set[uuid.UUID] = set()
    stack = list(successors.get(node_id, ()))
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(successors.get(current, ()))
    return seen


__all__ = [
    "descendants",
    "earliest_start",
    "predecessors_of",
    "sort_key",
    "successors_of",
    "topological_order",
    "validate_references",
]
