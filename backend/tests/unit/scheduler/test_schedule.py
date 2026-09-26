"""`simulate()` 的行为。这是本模块风险最高的地方,所以断言尽量钉在**产品规则**上。

每个测试的名字对应一条规则,失败时的信息说的是"用户会遇到什么",不是"哪个变量不对"。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict
from datetime import date, timedelta
from decimal import Decimal

import pytest

from backend.scheduler.calendar import build_day_pools, week_start_of
from backend.scheduler.errors import BindingConstraint, ScheduleErrorCode
from backend.scheduler.schedule import recovery_options, simulate
from backend.scheduler.types import (
    CapacityProfile,
    ExecutionFact,
    ExecutionResult,
    ExistingSession,
    NodeStatus,
    NodeType,
    PlannedSession,
    Priority,
    ScheduleDependency,
    ScheduleNode,
    ScheduleRequest,
    SessionStatus,
)

from .conftest import TODAY


#: 下一个周一。用"从今天推出来的那个周一"而不是写死日期:写死的话,某天改了
#: `TODAY`,这个测试的语义会**悄悄**从"一周视界"变成"跨两周",而它仍然通过。
def _next_monday(day: date) -> date:
    return week_start_of(day, 0) + timedelta(days=7)


def _task(
    workspace_id: uuid.UUID,
    minutes: int | None,
    *,
    deadline: date | None = None,
    priority: Priority = Priority.MEDIUM,
    status: NodeStatus = NodeStatus.PENDING,
    title: str = "任务",
    node_id: uuid.UUID | None = None,
) -> ScheduleNode:
    return ScheduleNode(
        id=node_id or uuid.uuid4(),
        workspace_id=workspace_id,
        title=title,
        node_type=NodeType.TASK,
        status=status,
        priority=priority,
        estimate_minutes=minutes,
        deadline=deadline,
    )


def _request(
    profile: CapacityProfile,
    nodes: tuple[ScheduleNode, ...],
    *,
    today: date = TODAY,
    horizon_days: int = 14,
    dependencies: tuple[ScheduleDependency, ...] = (),
    existing: tuple[ExistingSession, ...] = (),
    executions: tuple[ExecutionFact, ...] = (),
    plan_revision: int = 0,
) -> ScheduleRequest:
    return ScheduleRequest(
        today=today,
        horizon_days=horizon_days,
        profile=profile,
        nodes=nodes,
        dependencies=dependencies,
        existing=existing,
        executions=executions,
        plan_revision=plan_revision,
    )


# ---------------------------------------------------------------------------------
# 规则 (d):长任务切分。**一个 PlanNode,N 个 ScheduledSession。**
# ---------------------------------------------------------------------------------
def test_long_task_becomes_many_sessions_but_stays_one_node(profile: CapacityProfile) -> None:
    """480 分钟切成 8 场,而计划里**只有一行任务**。

    反过来做(为了填满日历把「写文献综述」复制成 8 个同名任务)会带来三个后果:用户要
    勾 8 次完成、进度百分比变得没有意义、复盘时看不出这 8 条是同一件事。
    """
    workspace = uuid.uuid4()
    node = _task(workspace, 480, title="写文献综述")
    request = _request(profile, (node,), horizon_days=14)

    result = simulate(request)

    assert len(result.sessions) == 8
    assert {session.node_id for session in result.sessions} == {node.id}
    # 请求里的节点仍然是那一个 —— 切分不改节点集合。
    assert len(request.nodes) == 1
    assert sum(session.planned_minutes for session in result.sessions) == 480
    assert result.churn.created == 8
    assert not result.gaps
    # 一个任务不能被排成"同一天做八段":那说明切分之后没按天摊开。
    assert len({session.scheduled_date for session in result.sessions}) == 8


# ---------------------------------------------------------------------------------
# 规则 (c):全用户只有一个池子
# ---------------------------------------------------------------------------------
@pytest.mark.parametrize("other_workspace_is_present", [True, False])
def test_two_workspaces_share_one_pool(other_workspace_is_present: bool) -> None:
    """两个空间争的是同一个晚上,而用户只有一个晚上。

    一周视界、周预算 300 分钟、每日不设限。B 自己排得下(180 ≤ 300);加上 A 的 180 分钟
    就超了 —— 如果池子是按空间各算一份,两个空间会各自以为自己能占满这一周,而那正好是
    "两份看起来都排得下的计划,合起来超了"这个错误。
    """
    profile = CapacityProfile(
        weekly_total_minutes=300,
        safety_factor=Decimal("1.00"),
        daily_max_minutes=300,
        default_buffer_minutes=0,
        min_session_minutes=15,
        max_session_minutes=60,
    )
    monday = _next_monday(TODAY)
    workspace_a, workspace_b = uuid.uuid4(), uuid.uuid4()
    node_a = _task(workspace_a, 180, title="空间 A 的任务")
    node_b = _task(workspace_b, 180, title="空间 B 的任务")
    nodes = (node_a, node_b) if other_workspace_is_present else (node_b,)

    result = simulate(_request(profile, nodes, today=monday, horizon_days=7))

    # B 单独排:这一周装得下,没有缺口。
    if not other_workspace_is_present:
        assert result.unscheduled_minutes == 0, "一个空间的工作量自己就排不下?"

    # 不论哪种情形,不变量都必须成立:每天的跨空间总量不超过当天池子。
    pools = build_day_pools(start=monday, horizon_days=7, profile=profile)
    for day, buckets in result.daily_load:
        total = sum(minutes for _, minutes in buckets)
        assert total <= pools.pool(day), f"{day} 排了 {total} 分钟,池子只有 {pools.pool(day)}"

    if other_workspace_is_present:
        assert result.unscheduled_minutes > 0, (
            "两个空间各 180 分钟、周预算只有 300 —— 共享池下必须有缺口;"
            "没有缺口说明每个空间都拿到了自己的一份池子。"
        )
        # 审计轨迹按空间分开:用户要能回答"这周满了,是什么占满的"。
        workspaces_seen = {
            workspace for _, buckets in result.daily_load for workspace, _ in buckets
        }
        assert workspaces_seen == {workspace_a, workspace_b}


def test_every_day_respects_its_own_pool(profile: CapacityProfile) -> None:
    """每日上限是硬约束,包括缓冲。多个任务争同一天时也不能被顶破。"""
    workspace = uuid.uuid4()
    nodes = tuple(_task(workspace, 90, title=f"任务{index}") for index in range(6))
    result = simulate(_request(profile, nodes, horizon_days=10))

    pools = build_day_pools(start=TODAY, horizon_days=10, profile=profile)
    for day, buckets in result.daily_load:
        total = sum(minutes for _, minutes in buckets)
        assert total <= pools.pool(day), f"{day} 排了 {total} 分钟,上限 {pools.pool(day)}"
        # 每日上限是 120 分钟;一场 90 分钟 + 10 缓冲 = 100,所以一天最多一场。
        assert total <= 120


# ---------------------------------------------------------------------------------
# 规则 (a):依赖与截止
# ---------------------------------------------------------------------------------
def test_a_successor_never_starts_before_its_predecessor_finishes(profile: CapacityProfile) -> None:
    """前置做完的第二天,后继才能开始。"""
    workspace = uuid.uuid4()
    first = _task(workspace, 60, title="读第 1 章")
    second = _task(workspace, 60, title="读第 2 章")
    dependency = ScheduleDependency(predecessor_id=first.id, successor_id=second.id)

    result = simulate(_request(profile, (first, second), dependencies=(dependency,)))

    first_days = [s.scheduled_date for s in result.sessions if s.node_id == first.id]
    second_days = [s.scheduled_date for s in result.sessions if s.node_id == second.id]
    assert first_days and second_days
    assert min(second_days) > max(first_days), (
        f"第 2 章排在了第 1 章之前:{second_days} vs {first_days}"
    )


def test_a_deadline_that_has_passed_is_reported_not_ignored(profile: CapacityProfile) -> None:
    """截止日已经过去时,如实报缺口,而不是"排到未来某天"了事。

    静默往后挪会让用户以为这件事还来得及 —— 而他需要知道的是"这个截止日已经不可能了",
    好去决定是放弃它还是改期。
    """
    workspace = uuid.uuid4()
    node = _task(workspace, 60, deadline=TODAY - timedelta(days=1), title="已经过期的事")
    result = simulate(_request(profile, (node,)))

    assert not result.sessions, "过期的事被排到了未来"
    assert result.unscheduled_minutes == 60
    assert result.gaps[0].reason_code == ScheduleErrorCode.DEADLINE_ALREADY_PASSED
    assert result.gaps[0].binding_constraint == BindingConstraint.DEADLINE
    assert result.truncated is False


def test_a_task_that_cannot_fit_before_its_deadline_is_never_silently_truncated(
    profile: CapacityProfile,
) -> None:
    """容量不足时:每分钟都要有账,`truncated` 恒为 False。

    "排了 3 场,剩下的悄悄丢掉"是最坏的结果 —— 用户以为计划是完整的。所以这里断言
    "排进去的 + 报缺口的 == 原工时",一分不多一分不少。
    """
    workspace = uuid.uuid4()
    node = _task(workspace, 600, deadline=TODAY + timedelta(days=2), title="赶不完的报告")
    result = simulate(_request(profile, (node,), horizon_days=30))

    scheduled = sum(session.planned_minutes for session in result.sessions)
    assert scheduled + result.unscheduled_minutes == 600
    assert result.unscheduled_minutes > 0
    assert result.truncated is False
    assert result.gaps, "排不下却没有缺口记录"
    for gap in result.gaps:
        assert gap.reason_code
        assert gap.binding_constraint
        assert gap.detail, "缺口没有说清是被什么卡住的"


# ---------------------------------------------------------------------------------
# 规则 (f)(g):已完成与已锁定的场次不许被移动
# ---------------------------------------------------------------------------------
def test_a_completed_session_is_neither_moved_nor_canceled(profile: CapacityProfile) -> None:
    """已完成的场次只贡献占用量,绝不出现在 move / cancel 里。"""
    workspace = uuid.uuid4()
    node = _task(workspace, 120, title="练习")
    done_id = uuid.uuid4()
    done = ExistingSession(
        id=done_id,
        workspace_id=workspace,
        node_id=node.id,
        scheduled_date=TODAY - timedelta(days=2),
        planned_minutes=60,
        buffer_minutes=10,
        seq=1,
        status=SessionStatus.DONE,
    )
    result = simulate(
        _request(
            profile,
            (node,),
            existing=(done,),
            executions=(
                ExecutionFact(
                    node_id=node.id,
                    session_id=done_id,
                    result=ExecutionResult.COMPLETED,
                    actual_minutes=60,
                ),
            ),
        )
    )

    assert done_id not in result.cancelations
    assert all(move.session_id != done_id for move in result.moves)
    # 已经做完 60 分钟,所以只剩 60 要排 —— 不是又排了一遍 120。
    assert sum(session.planned_minutes for session in result.sessions) == 60


def test_a_session_in_the_past_is_frozen_even_if_still_planned(profile: CapacityProfile) -> None:
    """日期已经过去、但状态还是 `planned` 的场次也要冻结。

    用户上周没做的事不会因为"状态还是计划中"就变得可以搬到这周 —— 搬走等于把他没做成
    的痕迹擦掉,而复盘时那正是要看的东西。
    """
    workspace = uuid.uuid4()
    node = _task(workspace, 60)
    stale = ExistingSession(
        id=uuid.uuid4(),
        workspace_id=workspace,
        node_id=node.id,
        scheduled_date=TODAY - timedelta(days=1),
        planned_minutes=60,
        seq=1,
    )
    result = simulate(_request(profile, (node,), existing=(stale,)))

    assert stale.id not in result.cancelations
    assert all(move.session_id != stale.id for move in result.moves)
    # 它仍然排在今天或之后的新场次里(过去的那一场没有满足这个节点)。
    assert all(session.scheduled_date >= TODAY for session in result.sessions)


def test_a_locked_session_is_kept_in_place_and_named_in_the_gap() -> None:
    """锁定的场次钉在原地;放不下时点名说"是因为锁",而不是笼统地"排满了"。

    用户对"这周满了"能做的事取决于他知道是谁占的。锁定场次是唯一一件**只差他一个
    决定**就能腾出空间的事,所以归因给它而不是"增加投入",是这个模块最该做对的地方。
    """
    profile = CapacityProfile(
        weekly_total_minutes=600,
        safety_factor=Decimal("1.00"),
        daily_max_minutes=60,
        default_buffer_minutes=0,
        min_session_minutes=15,
        max_session_minutes=60,
    )
    workspace = uuid.uuid4()
    locked_node = _task(workspace, 60, title="用户钉住的")
    other = _task(workspace, 60, title="排不下的")
    locked = ExistingSession(
        id=uuid.uuid4(),
        workspace_id=workspace,
        node_id=locked_node.id,
        scheduled_date=TODAY,
        planned_minutes=60,
        seq=1,
        locked=True,
        lock_reason="这天的安排我不想动",
    )
    result = simulate(_request(profile, (locked_node, other), existing=(locked,), horizon_days=1))

    assert locked.id not in result.cancelations
    assert all(move.session_id != locked.id for move in result.moves)
    assert result.unscheduled_minutes == 60
    # 这一天被锁定场次占满了 —— 缺口应当归因到锁定上。
    assert result.gaps[0].binding_constraint == BindingConstraint.LOCKED_SESSIONS


# ---------------------------------------------------------------------------------
# 最小改动重排
# ---------------------------------------------------------------------------------
def test_replan_after_one_completion_moves_zero_sessions(profile: CapacityProfile) -> None:
    """用户勾完第一场之后,**其余三场一场都不动**。

    这是"每天改计划、系统失去稳定性"那一条的直接回归:从头重排会把整个日历重洗一遍,
    而用户看到的是"我什么都没改,计划却全变了"。
    """
    workspace = uuid.uuid4()
    node = _task(workspace, 240, title="写文献综述")
    request = _request(profile, (node,), horizon_days=14)

    first = simulate(request)
    assert len(first.sessions) == 4

    # 把第一版的结果当成库里已经存在的行:第一场已完成,其余三场是计划中。
    stored: dict[int, uuid.UUID] = {}
    existing: list[ExistingSession] = []
    for session in first.sessions:
        session_id = uuid.uuid4()
        stored[session.seq] = session_id
        existing.append(
            ExistingSession(
                id=session_id,
                workspace_id=workspace,
                node_id=node.id,
                scheduled_date=session.scheduled_date,
                planned_minutes=session.planned_minutes,
                buffer_minutes=session.buffer_minutes,
                seq=session.seq,
                status=SessionStatus.DONE if session.seq == 1 else SessionStatus.PLANNED,
            )
        )

    second = simulate(
        _request(
            profile,
            (node,),
            horizon_days=14,
            existing=tuple(existing),
            executions=(
                ExecutionFact(
                    node_id=node.id,
                    session_id=stored[1],
                    result=ExecutionResult.COMPLETED,
                    actual_minutes=60,
                ),
            ),
        )
    )

    assert second.moves == (), f"重排挪动了安排:{second.moves}"
    assert second.cancelations == (), f"重排取消了安排:{second.cancelations}"
    assert second.churn.describe() == "本次没有改动任何安排。"

    # 三场都还在原来的日子上,而且复用原来的行(id 不变)—— 不是"原地新建一份"。
    before = {s.seq: s.scheduled_date for s in first.sessions if s.seq > 1}
    after = {s.seq: s.scheduled_date for s in second.sessions}
    assert after == before
    assert {s.session_id for s in second.sessions} == {stored[2], stored[3], stored[4]}
    # 已完成的那一场不在输出里 —— 它不需要被"重新写入",而它的记录必须原样留着。
    assert all(s.session_id != stored[1] for s in second.sessions)


def test_replan_only_moves_what_it_has_to(profile: CapacityProfile) -> None:
    """前置挪动时,只动受影响的那条链 —— 别的任务原地不动。

    朴素做法(每次从头重排)会把 20 个任务的日历整个重洗,而用户只改了其中一个的截止日。
    """
    workspace = uuid.uuid4()
    chain_a = _task(workspace, 60, title="链 A 第一步")
    chain_b = _task(workspace, 60, title="链 A 第二步")
    unrelated = _task(workspace, 60, title="别的任务")
    dependency = ScheduleDependency(predecessor_id=chain_a.id, successor_id=chain_b.id)

    first = simulate(_request(profile, (chain_a, chain_b, unrelated), dependencies=(dependency,)))
    existing = tuple(
        ExistingSession(
            id=uuid.uuid4(),
            workspace_id=workspace,
            node_id=session.node_id,
            scheduled_date=session.scheduled_date,
            planned_minutes=session.planned_minutes,
            buffer_minutes=session.buffer_minutes,
            seq=session.seq,
            status=SessionStatus.PLANNED,
        )
        for session in first.sessions
    )

    # 同样的输入(问题没变)—— 输出必须一场都不动。
    second = simulate(
        _request(
            profile,
            (chain_a, chain_b, unrelated),
            dependencies=(dependency,),
            existing=existing,
        )
    )
    assert second.moves == ()
    assert second.cancelations == ()
    assert second.churn.kept == len(existing)


# ---------------------------------------------------------------------------------
# 确定性
# ---------------------------------------------------------------------------------
def test_same_input_gives_byte_identical_output(profile: CapacityProfile) -> None:
    """同一份输入排两次,序列化之后**逐字节相同**。

    `schedule_version` 校验"用户预览过的正是被写入的那一份",而它成立的前提是排期本身
    是确定的。这里比对的是输入**内容相同但对象不同**的两次运行 —— 也就是真实的
    "预览"和"应用"两次调用。
    """
    workspace = uuid.uuid4()
    node_a = _task(workspace, 240, title="甲", priority=Priority.HIGH)
    node_b = _task(workspace, 180, title="乙", priority=Priority.MEDIUM, deadline=TODAY + timedelta(days=20))
    node_c = _task(workspace, 90, title="丙", priority=Priority.LOW)
    dependency = ScheduleDependency(predecessor_id=node_a.id, successor_id=node_c.id)

    def build() -> ScheduleRequest:
        return _request(profile, (node_a, node_b, node_c), dependencies=(dependency,), horizon_days=21)

    def fingerprint(result) -> str:
        return json.dumps(
            {
                "sessions": [
                    [str(s.node_id), s.scheduled_date.isoformat(), s.planned_minutes, s.seq]
                    for s in result.sessions
                ],
                "moves": [str(m.session_id) for m in result.moves],
                "cancelations": [str(c) for c in result.cancelations],
                "gaps": [
                    [str(g.node_id), g.unscheduled_minutes, g.reason_code, g.binding_constraint]
                    for g in result.gaps
                ],
                "daily_load": [
                    [day.isoformat(), [[str(ws), minutes] for ws, minutes in buckets]]
                    for day, buckets in result.daily_load
                ],
                "churn": asdict(result.churn),
                "schedule_version": result.schedule_version,
            },
            sort_keys=True,
            ensure_ascii=False,
        )

    first = simulate(build())
    second = simulate(build())
    assert fingerprint(first) == fingerprint(second)
    assert first.schedule_version == second.schedule_version


def test_simulate_does_not_mutate_its_input(profile: CapacityProfile) -> None:
    """排期不改输入。输入是 frozen dataclass,但里面装的元组同样不许被换掉。"""
    workspace = uuid.uuid4()
    node = _task(workspace, 120)
    request = _request(profile, (node,))
    before = json.dumps(asdict(request), sort_keys=True, default=str)

    simulate(request)

    assert json.dumps(asdict(request), sort_keys=True, default=str) == before


def test_schedule_version_changes_when_the_plan_changes(profile: CapacityProfile) -> None:
    """输入变一点,版本号就要变 —— 否则"计划已经变了,请重新预览"永远不会出现。

    这条是反向的:`schedule_version` 恒定的实现会让用户在过期的基础上点"应用"。
    """
    workspace = uuid.uuid4()
    node = _task(workspace, 120)

    base = simulate(_request(profile, (node,), plan_revision=1))
    revised = simulate(_request(profile, (node,), plan_revision=2))
    assert base.schedule_version != revised.schedule_version

    # 计划本身也变了(用户把估算从 120 改成 180)—— 版本号同样要变。
    longer = simulate(
        _request(profile, (_task(workspace, 180, node_id=node.id),), plan_revision=1)
    )
    assert longer.schedule_version != base.schedule_version


# ---------------------------------------------------------------------------------
# 规则 (h):缺口与三条出路
# ---------------------------------------------------------------------------------
def test_recovery_options_are_measured_not_asserted() -> None:
    """三条出路的 `resolves_gap` 是**跑出来的**:代价是真跑一遍 `simulate()`。

    这个场景里卡住的是**截止日**:每天 60 分钟、4 天就到截止,300 分钟的工作只能排 4 场。
    于是:

    - 少做 60 分钟 → 真的排得下 → `resolves_gap=True`
    - 往后延(把视界拉长)→ 截止日没动 → 还是排不下 → `resolves_gap=False`
    - 增加每周投入 → 每日上限没动,4 天就是 4 天 → 还是排不下 → `resolves_gap=False`

    断言它成立的话,后面两条会带着"这能解决"的标签送到用户面前,而用户会照着它去改自己
    的时间预算 —— 改完还是排不下,而他已经为此挤掉了别的事。
    """
    profile = CapacityProfile(
        weekly_total_minutes=420,
        safety_factor=Decimal("1.00"),
        daily_max_minutes=60,
        default_buffer_minutes=0,
        min_session_minutes=15,
        max_session_minutes=60,
    )
    workspace = uuid.uuid4()
    node = _task(workspace, 300, deadline=TODAY + timedelta(days=3), title="紧的报告")
    request = _request(profile, (node,), horizon_days=30)

    result = simulate(request)
    assert result.unscheduled_minutes == 60

    options = {option.kind: option for option in recovery_options(request, result)}
    assert set(options) == {"REDUCE_SCOPE", "EXTEND_DEADLINE", "INCREASE_INPUT"}

    assert options["REDUCE_SCOPE"].resolves_gap is True
    assert options["REDUCE_SCOPE"].remaining_unscheduled_minutes == 0
    assert options["EXTEND_DEADLINE"].resolves_gap is False, "截止日没动,延期不该声称能解决"
    assert options["EXTEND_DEADLINE"].remaining_unscheduled_minutes == 60
    assert options["INCREASE_INPUT"].resolves_gap is False, "每日上限没动,多投入不该声称能解决"
    assert options["INCREASE_INPUT"].remaining_unscheduled_minutes == 60

    # 每条建议都要说得出它改了什么参数,否则界面上无法让用户确认。
    for option in options.values():
        assert option.params, f"{option.kind} 没说清它要改什么"
        assert option.description


def test_no_gaps_means_no_recovery_options(profile: CapacityProfile) -> None:
    """排得下的时候不给建议 —— 一个无条件的建议等于噪音。"""
    workspace = uuid.uuid4()
    node = _task(workspace, 60)
    request = _request(profile, (node,))

    assert recovery_options(request, simulate(request)) == ()


def test_a_task_without_an_estimate_is_reported_not_guessed(profile: CapacityProfile) -> None:
    """没有预计工时就是没有 —— 不许替用户猜一个数字排进去。

    猜一个 60 分钟排进去,用户会在日历上看到一件他从没说过要做一小时的事,而那个数字
    之后会被当成他自己定的。
    """
    workspace = uuid.uuid4()
    node = _task(workspace, None, title="不知道要多久")
    result = simulate(_request(profile, (node,)))

    assert not result.sessions
    assert [gap.reason_code for gap in result.gaps] == [ScheduleErrorCode.NO_ESTIMATE]
    assert result.gaps[0].unscheduled_minutes == 0
    assert result.truncated is False


def test_a_completed_node_needs_no_future_sessions(profile: CapacityProfile) -> None:
    """已完成的任务不再占用未来的时间,而且它的既有场次被取消而不是删除。

    删除的话复盘时就看不到"这里原本排过" —— 而"计划本来是这么排的、后来做完了"正是
    复盘要看的东西。
    """
    workspace = uuid.uuid4()
    node = _task(workspace, 120, status=NodeStatus.COMPLETED, title="已经做完的")
    leftover = ExistingSession(
        id=uuid.uuid4(),
        workspace_id=workspace,
        node_id=node.id,
        scheduled_date=TODAY + timedelta(days=1),
        planned_minutes=60,
        seq=1,
    )
    result = simulate(_request(profile, (node,), existing=(leftover,)))

    assert not result.sessions
    assert result.cancelations == (leftover.id,)
    assert not result.daily_load, "已完成的节点不该占用任何一天的池子"


def test_daily_load_is_an_audit_trail_that_matches_the_sessions(profile: CapacityProfile) -> None:
    """`daily_load` 与 `sessions` 必须对得上 —— 它是给用户看"这天为什么满了"的依据。

    对不上的话,界面上会显示一张和自己排的场次矛盾的图,而用户唯一能做的就是不再相信它。
    """
    workspace = uuid.uuid4()
    nodes = tuple(_task(workspace, 100, title=f"任务{index}") for index in range(4))
    result = simulate(_request(profile, nodes, horizon_days=10))

    from_sessions: dict[date, int] = {}
    for session in result.sessions:
        from_sessions[session.scheduled_date] = (
            from_sessions.get(session.scheduled_date, 0)
            + session.planned_minutes
            + session.buffer_minutes
        )
    from_load = {
        day: sum(minutes for _, minutes in buckets) for day, buckets in result.daily_load
    }
    assert from_load == from_sessions


def test_reusing_a_session_keeps_its_identity_and_origin(profile: CapacityProfile) -> None:
    """被复用的场次带着原来的 id 和来源回去 —— 写入方才能把它当成"同一条记录"。

    `session_id` 丢了的话,应用阶段会把它当新场次插进去,于是用户的时间线上凭空多出
    一场重复的安排。
    """
    workspace = uuid.uuid4()
    node = _task(workspace, 60, title="练习")
    existing = ExistingSession(
        id=uuid.uuid4(),
        workspace_id=workspace,
        node_id=node.id,
        scheduled_date=TODAY + timedelta(days=1),
        planned_minutes=60,
        buffer_minutes=0,
        seq=1,
        start_minute=19 * 60,
        end_minute=20 * 60,
    )
    result = simulate(_request(profile, (node,), existing=(existing,)))
    (session,) = result.sessions

    assert isinstance(session, PlannedSession)
    assert session.session_id == existing.id
    assert session.scheduled_date == existing.scheduled_date
    # 用户给过的时钟时段要保留 —— 抹掉它等于把"19:00 开始"改成"什么时候都行"。
    assert (session.start_minute, session.end_minute) == (19 * 60, 20 * 60)
