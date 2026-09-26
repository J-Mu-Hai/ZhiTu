"""依赖图:拓扑序要**确定**,而且不许把认不出来的边当成不存在。"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from backend.scheduler.errors import DependencyCycleError, UnknownNodeReferenceError
from backend.scheduler.graph import (
    descendants,
    earliest_start,
    predecessors_of,
    sort_key,
    successors_of,
    topological_order,
)
from backend.scheduler.types import Priority, ScheduleDependency, ScheduleNode

from .conftest import TODAY, make_node


def _fixed(seed: int, **kwargs) -> ScheduleNode:
    """一个 id 确定的节点。用 `uuid5` 而不是 `uuid4`,好让排序断言写得死。"""
    return make_node(node_id=uuid.uuid5(uuid.NAMESPACE_DNS, f"zhitu-test-{seed}"), title=f"节点{seed}", **kwargs)


def _edge(predecessor: ScheduleNode, successor: ScheduleNode, lag_days: int = 0) -> ScheduleDependency:
    return ScheduleDependency(
        predecessor_id=predecessor.id, successor_id=successor.id, lag_days=lag_days
    )


def test_topological_order_puts_predecessors_first() -> None:
    a, b, c = _fixed(1), _fixed(2), _fixed(3)
    order = topological_order((a, b, c), (_edge(a, b), _edge(b, c)))
    assert order.index(a.id) < order.index(b.id) < order.index(c.id)


def test_parallel_branches_order_is_deterministic() -> None:
    """互不依赖的节点之间的先后由 `sort_key` 决定,不取决于输入顺序。

    输入顺序来自 SQL 的返回顺序,那取决于查询计划。"同一份计划今天排出来和明天排出来
    不一样"就是这样产生的 —— 而且没有任何东西会报错,用户只会觉得计划老是变。
    """
    nodes = tuple(_fixed(index) for index in range(6))
    assert topological_order(nodes, ()) == topological_order(tuple(reversed(nodes)), ())


def test_priority_and_deadline_break_ties() -> None:
    """并排时先按优先级、再按截止日。没有截止日的排在所有有截止日的之后。"""
    low = _fixed(10, priority=Priority.LOW)
    high = _fixed(11, priority=Priority.HIGH)
    early = _fixed(12, deadline=TODAY + timedelta(days=1))
    late = _fixed(13, deadline=TODAY + timedelta(days=30))

    assert sort_key(high) < sort_key(low), "高优先级没有排在前面"
    assert sort_key(early) < sort_key(late), "早截止的没有排在前面"
    assert sort_key(early) < sort_key(_fixed(14))


def test_cycle_is_an_error_not_a_guess() -> None:
    """成环时抛错,而不是"尽力排一个顺序"。

    猜一个顺序的后果是某个任务排在了它的前置之前,而排出来的计划看起来完全合理 ——
    用户没有任何办法发现,直到他发现自己连着两天在啃同一本没读过的教材。
    """
    a, b, c = _fixed(20), _fixed(21), _fixed(22)
    with pytest.raises(DependencyCycleError):
        topological_order((a, b, c), (_edge(a, b), _edge(b, c), _edge(c, a)))


def test_unknown_edge_is_an_error_not_ignored() -> None:
    """一条指向不存在节点的依赖必须抛错,不能静默丢掉。

    丢掉的效果是"后置任务排在了前置之前",而且它连环都算不上 —— 计划看起来毫无异常。
    """
    a = _fixed(30)
    ghost = uuid.uuid5(uuid.NAMESPACE_DNS, "不存在的节点")
    neither = ScheduleDependency(predecessor_id=ghost, successor_id=a.id)
    nor = ScheduleDependency(predecessor_id=a.id, successor_id=ghost)

    with pytest.raises(UnknownNodeReferenceError):
        topological_order((a,), (neither,))
    with pytest.raises(UnknownNodeReferenceError):
        topological_order((a,), (nor,))


def test_earliest_start_is_the_day_after_the_predecessor_finishes() -> None:
    """finish-to-start:前置做完的**第二天**才能开始,lag 再往后顺延。

    写成同一天的话,"读完第 1 章"和"读完第 2 章"会在同一天各占一段,用户分不出先后 ——
    而他明确说过要按顺序读。
    """
    a, b = _fixed(40), _fixed(41)
    finish = {a.id: TODAY}

    earliest, waiting = earliest_start(
        b.id, predecessors=predecessors_of((_edge(a, b),)), finish_days=finish, fallback=TODAY
    )
    assert earliest == TODAY + timedelta(days=1)
    assert waiting is True

    earliest_lag, _ = earliest_start(
        b.id,
        predecessors=predecessors_of((_edge(a, b, lag_days=3),)),
        finish_days=finish,
        fallback=TODAY,
    )
    assert earliest_lag == TODAY + timedelta(days=4)

    # 前置还没有完成日:不拖住后继,而且**不算"在等前置"** —— 报"要等前置"而其实没等,
    # 会让用户去查一条根本没问题的依赖。
    free, waiting_free = earliest_start(
        b.id, predecessors=predecessors_of((_edge(a, b),)), finish_days={}, fallback=TODAY
    )
    assert free == TODAY
    assert waiting_free is False


def test_descendants_is_the_whole_downstream() -> None:
    """波纹式修复的范围:前置动了,受影响的是它的**全部**下游。

    只看直接后继会漏掉孙辈 —— 修复停在一半,而计划里留下了一处违反依赖的地方,
    没有任何东西会报错。
    """
    a, b, c, d = _fixed(50), _fixed(51), _fixed(52), _fixed(53)
    successors = successors_of((_edge(a, b), _edge(b, c), _edge(c, d)))
    assert descendants(a.id, successors) == {b.id, c.id, d.id}
    assert descendants(c.id, successors) == {d.id}
    assert descendants(d.id, successors) == set()
