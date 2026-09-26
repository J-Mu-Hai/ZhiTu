"""排期单测的公共构件。**不需要数据库,不需要模型 key,不需要 HTTP。**

这里刻意不依赖 `backend/tests/conftest.py` 里那套 app_client / db fixture:排期是纯函数,
给它挂上数据库反而会让人以为它需要数据库。它要的是"一份输入",而输入全部是 dataclass ——
所以这里的 fixture 就是几个构造器。
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest

from backend.scheduler.types import (
    CapacityProfile,
    NodeStatus,
    NodeType,
    Priority,
    ScheduleDependency,
    ScheduleNode,
)

#: 固定的一天。**绝不用 `date.today()`** —— "今天"是输入的一部分,用真的今天会让
#: 测试在某个特定日期开始失败,而失败的样子和代码改动完全无关。
TODAY = date(2026, 9, 25)


@pytest.fixture
def today() -> date:
    return TODAY


@pytest.fixture
def profile() -> CapacityProfile:
    """一个说得出数字的默认档:每天最多 120 分钟,缓冲 10 分钟。

    `daily_max_minutes` 显式给出来,是为了让"每日上限"和"每周预算"这两个约束在测试里
    分得开 —— 不给的话每日池是从周预算平摊出来的,两者永远成比例,测不出区别。
    """
    return CapacityProfile(
        weekly_total_minutes=600,
        safety_factor=Decimal("1.00"),
        daily_max_minutes=120,
        default_buffer_minutes=10,
        min_session_minutes=15,
        max_session_minutes=60,
    )


def make_node(
    *,
    title: str = "任务",
    estimate_minutes: int | None = 60,
    deadline: date | None = None,
    priority: Priority = Priority.MEDIUM,
    status: NodeStatus = NodeStatus.PENDING,
    node_type: NodeType = NodeType.TASK,
    workspace_id: uuid.UUID | None = None,
    node_id: uuid.UUID | None = None,
    has_children: bool = False,
) -> ScheduleNode:
    return ScheduleNode(
        id=node_id or uuid.uuid4(),
        workspace_id=workspace_id or uuid.uuid4(),
        title=title,
        node_type=node_type,
        status=status,
        priority=priority,
        estimate_minutes=estimate_minutes,
        deadline=deadline,
        has_children=has_children,
    )


@pytest.fixture
def node_factory():
    return make_node


@pytest.fixture
def dep_factory():
    def _make(predecessor: ScheduleNode, successor: ScheduleNode, lag_days: int = 0):
        return ScheduleDependency(
            predecessor_id=predecessor.id,
            successor_id=successor.id,
            lag_days=lag_days,
        )

    return _make
