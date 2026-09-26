"""`schedule_version` 与两次方案之间的差异。

版本号的用途是"用户预览过的正是被写入的那一份"。它有两个失败方向,都很难看:

- **太敏感** —— 输入没有实质变化它也变,用户看到"计划已经变了,请重新预览",而他什么
  也没改。所以他学会了无视这句话,于是真正的过期也照点不误。
- **太迟钝** —— 输入真的变了它不变,用户在一个过期方案上点"应用",系统照着旧输入写入。
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

from backend.scheduler.diff import changes_between, schedule_version
from backend.scheduler.schedule import simulate
from backend.scheduler.types import (
    CapacityProfile,
    ChurnSummary,
    ExistingSession,
    NodeStatus,
    NodeType,
    PlannedSession,
    Priority,
    ScheduleNode,
    ScheduleRequest,
    ScheduleResult,
)

from .conftest import TODAY

PROFILE = CapacityProfile(
    weekly_total_minutes=600,
    safety_factor=Decimal("1.00"),
    daily_max_minutes=120,
    default_buffer_minutes=10,
    min_session_minutes=15,
    max_session_minutes=60,
)
WORKSPACE = uuid.UUID("11111111-1111-1111-1111-111111111111")
NODE = uuid.UUID("22222222-2222-2222-2222-222222222222")


def _session(day: date, seq: int, minutes: int = 60) -> ExistingSession:
    return ExistingSession(
        id=uuid.uuid5(uuid.NAMESPACE_DNS, f"session-{day}-{seq}"),
        workspace_id=WORKSPACE,
        node_id=NODE,
        scheduled_date=day,
        planned_minutes=minutes,
        buffer_minutes=10,
        seq=seq,
    )


def _request(**overrides) -> ScheduleRequest:
    base: dict[str, object] = {"today": TODAY, "horizon_days": 14, "profile": PROFILE}
    base.update(overrides)
    return ScheduleRequest(**base)  # type: ignore[arg-type]


def test_version_ignores_the_order_of_the_inputs() -> None:
    """输入**内容相同、顺序不同**时,版本号必须一样。

    节点和场次从数据库里读出来时顺序由查询计划决定。版本号跟着顺序变的话,用户在看到
    预览之后、点"应用"之前,只要有任何一次查询返回了不同的顺序,就会被判为过期 ——
    而计划一个字都没变。
    """
    day = TODAY + timedelta(days=1)
    sessions = [_session(day, seq) for seq in (1, 2, 3)]

    forward = _request(existing=tuple(sessions))
    shuffled = _request(existing=tuple(reversed(sessions)))
    assert schedule_version(forward) == schedule_version(shuffled)


def test_version_is_stable_across_processes() -> None:
    """同一个输入,两次构造的版本号一样 —— 哈希里不能有随机的部分。

    用了 `hash()`、`id()` 或者在哈希里塞了对象的内存地址,这条就会炸。
    """
    assert schedule_version(_request()) == schedule_version(_request())


def test_version_changes_when_the_plan_changes() -> None:
    """真正改变计划的输入必须改变版本号。"""
    node = ScheduleNode(
        id=NODE,
        workspace_id=WORKSPACE,
        title="写文献综述",
        node_type=NodeType.TASK,
        status=NodeStatus.PENDING,
        priority=Priority.MEDIUM,
        estimate_minutes=120,
        deadline=None,
    )
    assert schedule_version(_request(nodes=(node,))) != schedule_version(
        _request(nodes=(replace(node, estimate_minutes=180),))
    )
    assert schedule_version(_request(nodes=(node,), plan_revision=1)) != schedule_version(
        _request(nodes=(node,), plan_revision=2)
    )
    assert schedule_version(_request(nodes=(node,))) != schedule_version(_request())
    # 视界是输入的一部分:它变了,排出来的东西就不同。
    assert schedule_version(_request(nodes=(node,))) != schedule_version(
        _request(nodes=(node,), horizon_days=28)
    )


def test_version_ignores_how_a_decimal_is_written() -> None:
    """`Decimal("0.8")` 与 `Decimal("0.80")` 是同一个数,版本号也要一样。

    规范化不做这一步的话,任何一次"把这个字段重新序列化一遍"的改动都会让所有用户
    手上一份还没应用的预览失效。
    """
    loose = replace(PROFILE, safety_factor=Decimal("0.8"))
    tight = replace(PROFILE, safety_factor=Decimal("0.80"))
    assert schedule_version(_request(profile=loose)) == schedule_version(_request(profile=tight))


def test_changes_between_counts_what_the_user_would_notice() -> None:
    """`changes_between` 数的是**用户会发现的变化**。

    一个场次从 60 分钟变成 60 分钟但内部序号挪了位,用户看不出来,就不该算成一次改动 ——
    否则"本次调整:移动 12 场"这句话会出现在一次什么都没变的刷新之后。
    """
    before = ScheduleResult(
        sessions=(
            PlannedSession(
                node_id=NODE,
                workspace_id=WORKSPACE,
                scheduled_date=TODAY + timedelta(days=1),
                planned_minutes=60,
                buffer_minutes=10,
                seq=1,
                session_id=uuid.uuid5(uuid.NAMESPACE_DNS, "a"),
            ),
            PlannedSession(
                node_id=NODE,
                workspace_id=WORKSPACE,
                scheduled_date=TODAY + timedelta(days=2),
                planned_minutes=60,
                buffer_minutes=10,
                seq=2,
                session_id=uuid.uuid5(uuid.NAMESPACE_DNS, "b"),
            ),
        )
    )
    # 第一场挪到后天,第二场原样;另加一场全新的。
    after = ScheduleResult(
        sessions=(
            PlannedSession(
                node_id=NODE,
                workspace_id=WORKSPACE,
                scheduled_date=TODAY + timedelta(days=3),
                planned_minutes=60,
                buffer_minutes=10,
                seq=1,
                session_id=uuid.uuid5(uuid.NAMESPACE_DNS, "a"),
            ),
            PlannedSession(
                node_id=NODE,
                workspace_id=WORKSPACE,
                scheduled_date=TODAY + timedelta(days=2),
                planned_minutes=60,
                buffer_minutes=10,
                seq=2,
                session_id=uuid.uuid5(uuid.NAMESPACE_DNS, "b"),
            ),
            PlannedSession(
                node_id=NODE,
                workspace_id=WORKSPACE,
                scheduled_date=TODAY + timedelta(days=4),
                planned_minutes=30,
                buffer_minutes=10,
                seq=3,
            ),
        )
    )

    churn = changes_between(before, after)
    assert churn == ChurnSummary(moved=1, created=1, canceled=0, kept=1)
    assert churn.describe() == "本次调整：移动 1 场，新增 1 场。"


def test_changes_between_says_nothing_changed_when_nothing_did() -> None:
    """什么都没变时,那句话必须是"本次没有改动任何安排"。

    这句话会被直接放到界面上。说错的方向只有一个是可接受的:宁可漏报,不可虚报 ——
    虚报会让用户开始怀疑每一个他看到的数字。
    """
    workspace = uuid.uuid4()
    node = uuid.uuid4()
    sessions = tuple(
        PlannedSession(
            node_id=node,
            workspace_id=workspace,
            scheduled_date=TODAY + timedelta(days=offset),
            planned_minutes=60,
            buffer_minutes=10,
            seq=offset,
            session_id=uuid.uuid5(uuid.NAMESPACE_DNS, f"x{offset}"),
        )
        for offset in (1, 2, 3)
    )
    result = ScheduleResult(sessions=sessions)

    churn = changes_between(result, result)
    assert churn.total_changes == 0
    assert churn.describe() == "本次没有改动任何安排。"
    assert churn.kept == 3


def _task(title: str, minutes: int, *, deadline: date | None = None, priority=Priority.MEDIUM) -> ScheduleNode:
    return ScheduleNode(
        id=uuid.uuid5(uuid.NAMESPACE_DNS, f"node-{title}"),
        workspace_id=WORKSPACE,
        title=title,
        node_type=NodeType.TASK,
        status=NodeStatus.PENDING,
        priority=priority,
        estimate_minutes=minutes,
        deadline=deadline,
    )


def _as_stored(result: ScheduleResult) -> tuple[ExistingSession, ...]:
    """把一次排期结果当成"库里已经存着的行",好喂给下一次排期。

    id 用 `uuid5` 从节点的 `(node_id, seq)` 推出来,于是它对同一个场次是**稳定**的 ——
    而 `changes_between` 正是按 id 认同一场次的。用 `uuid4` 的话每次都是新 id,比较函数
    会把所有场次都当成"新出现的",于是这个测试即使在重排乱动的情况下也会通过。
    """
    return tuple(
        ExistingSession(
            id=uuid.uuid5(uuid.NAMESPACE_DNS, f"session-{session.node_id}-{session.seq}"),
            workspace_id=session.workspace_id,
            node_id=session.node_id,
            scheduled_date=session.scheduled_date,
            planned_minutes=session.planned_minutes,
            buffer_minutes=session.buffer_minutes,
            seq=session.seq,
        )
        for session in result.sessions
    )


def _applied(request: ScheduleRequest) -> ScheduleResult:
    """"这份计划已经写进库了"的状态:再排一次,让场次带着 id 回来。

    为什么要两步:`changes_between` 按 `session_id` 认同一场。一次全新的排期里所有场次
    都是"还没有 id 的新场次"(`session_id is None`),拿它当"改动之前"的话,比较函数
    一个场次都认不出来 —— 于是永远报"新增了 N 场、移动 0 场"。而真实流程里"改动之前"
    的那一份**总是**库里已经有 id 的行,所以这里如实造出那个状态。

    这个坑值一个 helper:一个恒报"移动 0 场"的记账看起来永远是对的。
    """
    seed = simulate(request)
    return simulate(replace(request, existing=_as_stored(seed)))


def test_churn_of_a_real_replan_is_zero() -> None:
    """拿一次真实的、什么都没改的重排核对记账 —— 而不是手搓两份结果。

    手搓的结果只能证明"比较函数按我写的方式比较",证明不了"排期真的没动东西"。
    """
    node = _task("练习", 180)
    first = _applied(_request(nodes=(node,)))
    second = simulate(replace(_request(nodes=(node,)), existing=_as_stored(first)))

    churn = changes_between(first, second)
    assert churn.total_changes == 0, f"什么都没改,却报了改动:{churn.describe()}"
    assert churn.kept == 3


def test_churn_of_a_real_replan_reports_the_sessions_that_moved() -> None:
    """插进来一件高优先级的事之后,记账要说得出"移动了几场"。

    这是界面那句"本次调整:移动 3 场"的唯一来源。数错了,用户就不知道该不该重新看一眼
    自己的日历 —— 而这句话正是用来决定这件事的。
    """
    practice = _task("练习", 180)  # 3 场 60 分钟
    first = _applied(_request(nodes=(practice,)))

    urgent = _task("临时插入", 60, deadline=TODAY, priority=Priority.HIGH)
    second = simulate(
        replace(_request(nodes=(practice, urgent)), existing=_as_stored(first))
    )

    # 插进来的那件事抢走了今天,练习的三场各往后挪了一点。
    moved = {
        session.session_id
        for session in second.sessions
        if session.node_id == practice.id and session.session_id is not None
    }
    churn = changes_between(first, second)
    assert churn.created == 1, "新插入的那一场没有被算成新增"
    assert churn.moved >= 1, f"练习被挪动了,却没记账:{churn.describe()}"
    assert churn.kept + churn.moved == len(moved), (
        f"练习的 {len(moved)} 场里,记账说保留了 {churn.kept}、移动了 {churn.moved}"
    )
    # 原有场次一场都没被取消 —— 只是挪了地方。
    assert churn.canceled == 0
