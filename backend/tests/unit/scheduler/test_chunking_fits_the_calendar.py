"""切分出来的场次,必须**装得进某一天**。

## 这个文件钉的是一个真的发生过的死局

默认档是"每周 600 分钟、安全系数 0.8" → 每天 `ceil(480/7) = 69` 分钟。而
`max_session_minutes` 的默认值是 120。于是:

    一个 300 分钟的任务 → 切 3 场,每场 100 分钟 + 10 分钟缓冲 = 占 110 分钟
    而任何一天都放不下 110 分钟
    → 一场都排不出来,dailyLoad 里一分钟都没有
    → 缺口报告说"截止时间之前的时间都排满了"

用户看到的是"我明明说了每周 10 小时,它却告诉我排满了",而他没有任何办法从这个
提示里看出真正的原因。**日上限是硬约束,那么场的长度就必须服从它。**

## 为什么默认档下"排不出东西"没有被别的测试发现

`conftest` 里的 `profile` 显式给了 `daily_max_minutes=120` 与 `max_session_minutes=60`,
两个数字是自洽的 —— 而自洽的测试档恰好绕开了默认档那组不自洽的默认值。所以这里
**刻意用 `CapacityProfile()` 的默认值本身**,一个数字都不覆盖。
"""

from __future__ import annotations

from datetime import date

from backend.scheduler.pack import split_sessions
from backend.scheduler.schedule import simulate
from backend.scheduler.types import CapacityProfile, NodeStatus, NodeType, ScheduleRequest

TODAY = date(2026, 9, 25)


def test_split_sessions_never_exceeds_the_daily_room() -> None:
    """每一场占的分钟数(含缓冲)都不超过"一天最多装得下多少"。"""
    profile = CapacityProfile()  # 每周 600 × 0.8 → 每天 69;单场上限 120;缓冲 10
    room = 69

    chunks = split_sessions(300, profile, day_room_minutes=room)

    assert chunks, "300 分钟的活必须能切出至少一场"
    for chunk in chunks:
        assert chunk.occupies_minutes <= room, (
            f"切出一场 {chunk.minutes} 分钟 + {chunk.buffer_minutes} 分钟缓冲 = "
            f"{chunk.occupies_minutes},而一天只有 {room} 分钟 —— 它排不进去"
        )
    # 切分仍然不能丢工时,也不能造出额外的工时。
    assert sum(chunk.minutes for chunk in chunks) == 300


def test_split_sessions_still_respects_the_user_maximum() -> None:
    """日池比 `max_session_minutes` 宽时,**用户的单场上限**才是那个更紧的约束。

    上一条测试只证明"不会超出日池";少了这一条,一个把 `maximum` 直接写成日池的实现
    也能通过 —— 而那会排出一场比用户说过的上限更长的安排。
    """
    profile = CapacityProfile(max_session_minutes=60, default_buffer_minutes=10)

    chunks = split_sessions(300, profile, day_room_minutes=600)

    assert max(chunk.minutes for chunk in chunks) <= 60


def test_default_profile_can_actually_schedule_a_task(node_factory) -> None:
    """默认档下,一个有工时的任务**真的能排出场次**。

    这是那个死局的回归测试:它会以"缺口里躺着一个 300 分钟、`dailyLoad` 全空"的方式
    失败,而不是以别的方式。
    """
    node = node_factory(estimate_minutes=300)
    request = ScheduleRequest(
        today=TODAY,
        horizon_days=28,
        profile=CapacityProfile(),
        nodes=(node,),
    )

    result = simulate(request)

    assert result.gaps == (), f"排不出来的原因: {[gap.reason_code for gap in result.gaps]}"
    assert result.sessions, "一个有工时的任务应当排出至少一场"
    assert sum(session.planned_minutes for session in result.sessions) == 300


def test_no_day_is_over_its_pool_with_the_default_profile(node_factory) -> None:
    """不变量:任何一天排进去的总量都不超过那天池子的容量。

    默认档下每天 69 分钟,**缓冲也算占用** —— 所以这条同时也在钉"60 分钟的活占用的是
    60 + 10",而那是"当天总占用 ≤ 上限"这条不变量在数据库里可复核的前提。
    """
    node = node_factory(estimate_minutes=300)
    request = ScheduleRequest(
        today=TODAY,
        horizon_days=28,
        profile=CapacityProfile(),
        nodes=(node,),
    )

    result = simulate(request)

    pools = {session.scheduled_date: 0 for session in result.sessions}
    for day in pools:
        load = sum(minutes for _, minutes in result.load_on(day).items())
        assert load <= 69, f"{day} 排了 {load} 分钟,而上限是 69"


def test_a_container_without_an_estimate_is_not_a_gap(node_factory) -> None:
    """**每个空间的根目标都是自动建的、天生没有工时。**

    把它报成缺口的话,每一份计划上都会永久挂着一条假警报 —— 而用户学会忽略整个缺口
    报告之后,那些真的排不下的也就一起被忽略了。
    """
    goal = node_factory(
        title="Python 学习",
        node_type=NodeType.GOAL,
        estimate_minutes=None,
        has_children=True,
    )
    child = node_factory(estimate_minutes=60)

    result = simulate(
        ScheduleRequest(
            today=TODAY,
            horizon_days=28,
            profile=CapacityProfile(),
            nodes=(goal, child),
        )
    )

    assert [gap.reason_code for gap in result.gaps] == [], "容器节点不该报缺口"


def test_a_leaf_without_an_estimate_is_still_a_gap(node_factory) -> None:
    """而一个**叶子**任务没有工时是真缺口:用户说了要做这件事,却没说要做多久。

    与上一条成对存在。少了它,"容器不报缺口"这条规则完全可以被实现成"不报缺口的
    规则被整体删掉了",而测试仍然是绿的。
    """
    leaf = node_factory(title="写文献综述", estimate_minutes=None, has_children=False)

    result = simulate(
        ScheduleRequest(
            today=TODAY,
            horizon_days=28,
            profile=CapacityProfile(),
            nodes=(leaf,),
        )
    )

    assert [gap.reason_code for gap in result.gaps] == ["NO_ESTIMATE"]


def test_a_container_with_an_estimate_is_still_scheduled(node_factory) -> None:
    """容器**自己填了工时**时,那部分是它自己的活,照排不误。

    这条划出了上面那条规则的边界:被忽略的只是"容器的工时为**空**",不是"容器"。
    多写这一条是因为把规则做宽一点的诱惑很大(比如"有孩子的节点一律不排"),而那会
    静默丢掉用户同意过的工作量。
    """
    stage = node_factory(
        title="阶段一",
        node_type=NodeType.STAGE,
        estimate_minutes=60,
        has_children=True,
    )

    result = simulate(
        ScheduleRequest(
            today=TODAY,
            horizon_days=28,
            profile=CapacityProfile(),
            nodes=(stage,),
        )
    )

    assert [gap.reason_code for gap in result.gaps] == []
    assert sum(session.planned_minutes for session in result.sessions) == 60


def test_a_closed_container_is_never_scheduled(node_factory) -> None:
    """已完成/已归档的容器不排。**顺带钉住 `has_children` 没有把这条顺序搞反** ——
    "不排"的判断在"没有工时"之前,所以一个没有工时的**已完成**节点既不该排,
    也不该报缺口。"""
    done = node_factory(
        node_type=NodeType.GOAL,
        estimate_minutes=None,
        has_children=True,
        status=NodeStatus.COMPLETED,
    )

    result = simulate(
        ScheduleRequest(
            today=TODAY,
            horizon_days=28,
            profile=CapacityProfile(),
            nodes=(done,),
        )
    )

    assert result.gaps == ()
    assert result.sessions == ()
