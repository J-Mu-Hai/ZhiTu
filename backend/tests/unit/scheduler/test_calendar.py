"""每日池子怎么算出来,以及 `safety_factor` **只乘一次**。"""

from __future__ import annotations

import math
from datetime import date, timedelta
from decimal import Decimal

import pytest

from backend.scheduler.calendar import (
    build_day_pools,
    daily_cap,
    day_supply,
    week_start_of,
    weekly_budget,
)
from backend.scheduler.capacity import assess_feasibility
from backend.scheduler.errors import BindingConstraint
from backend.scheduler.types import AvailabilityWindow, CapacityProfile, DayException

from .conftest import TODAY


def test_safety_factor_applied_once() -> None:
    """`safety_factor` 只乘一次:每日池是从**那一个乘积**平摊出来的,不是再乘一次。

    这条测试刻意从两个方向夹:算术上比对数字,行为上比对"一整周能不能排下等于周预算的
    工作量"。只比对数字的话,一个把每日池改成 `weekly_total × factor² / 7` 的实现仍然
    可以通过 —— 只要它的 `weekly_budget()` 没变。而用户看到的是"我这周明明有 5 小时,
    它非说只有 2.5 小时",于是去增加投入,正好是最不该做的那个动作。
    """
    profile = CapacityProfile(
        weekly_total_minutes=600,
        safety_factor=Decimal("0.50"),
        daily_max_minutes=None,
    )

    assert weekly_budget(profile) == 300
    # 300 ÷ 7 天摊到每天 ≈ 43,而**不是** 600 × 0.5² ÷ 7 ≈ 22。
    assert daily_cap(profile) == math.ceil(300 / 7)

    pools = build_day_pools(start=TODAY, horizon_days=7, profile=profile)
    capacity = sum(pools.pool(day) for day in pools.days)
    # 平摊会把除不尽的零头往上取,所以容量略微大于周预算,但绝不能小。
    assert weekly_budget(profile) <= capacity <= weekly_budget(profile) + 7

    # 行为验证:要求"整整一个周预算"的工作量,必须在**一周之内**排得下。
    # 二次乘算的实现会算出 154 分钟的容量,于是这里报出一个不存在的缺口。
    feasibility = assess_feasibility(required_minutes=weekly_budget(profile), pools=pools)
    assert feasibility.fits, (
        f"一周装不下自己的周预算:{feasibility.capacity_minutes} < "
        f"{feasibility.required_minutes} —— 安全系数被乘了两次?"
    )


def test_daily_cap_and_weekly_budget_are_independent_constraints() -> None:
    """两个约束各自生效,取小的那个 —— 用户给了每日上限时周预算才可能是紧的那一个。

    "每天 4 小时"听起来很宽,但 4×7=28 小时远超"每周 8 小时"的意愿。逐天看,每天
    240 分钟确实都空着;合起来看,这一周只有 480 分钟可用。如果闸门只把每日池相加,
    它会对一个排不下的计划说"没问题" —— 而那是用户唯一一次听到结论的机会。
    """
    generous_daily = CapacityProfile(
        weekly_total_minutes=480,
        safety_factor=Decimal("1.00"),
        daily_max_minutes=240,
    )
    Monday = TODAY + timedelta(days=(0 - TODAY.weekday()) % 7)
    pools = build_day_pools(start=Monday, horizon_days=7, profile=generous_daily)

    assert pools.daily_cap == 240
    assert weekly_budget(generous_daily) == 480
    # 每日池相加是 7 × 240 = 1680,但可用的只有周预算 480。
    assert sum(pools.pool(day) for day in pools.days) == 7 * 240
    feasibility = assess_feasibility(required_minutes=7 * 240, pools=pools)
    assert feasibility.capacity_minutes == 480, "闸门把 7 个每日池直接相加了"
    assert not feasibility.fits
    assert feasibility.binding is BindingConstraint.WEEKLY_BUDGET
    assert feasibility.shortfall_minutes == 7 * 240 - 480


def test_overlapping_windows_are_merged_not_summed() -> None:
    """周三 19:00-21:00 与 20:00-22:00 合起来是 3 小时,不是 4 小时。

    相加会多出一小时,而那一小时会变成一个**排得下、但用户其实没空**的安排。这种
    错误在界面上完全看不出来:那一场看起来和其他场次一模一样。
    """
    wednesday = TODAY + timedelta(days=(2 - TODAY.weekday()) % 7)
    profile = CapacityProfile(weekly_total_minutes=600, safety_factor=Decimal("1.00"), daily_max_minutes=1440)
    windows = (
        AvailabilityWindow(weekday=wednesday.weekday(), start_minute=19 * 60, end_minute=21 * 60),
        AvailabilityWindow(weekday=wednesday.weekday(), start_minute=20 * 60, end_minute=22 * 60),
    )

    supply = day_supply(wednesday, profile, windows, {})
    assert supply.minutes == 180, f"重叠时段被相加了:{supply.detail}"

    # 不重叠的两段仍然要相加,否则这条规则就从"合并重叠"变成了"只留第一段"。
    apart = (
        AvailabilityWindow(weekday=wednesday.weekday(), start_minute=9 * 60, end_minute=10 * 60),
        AvailabilityWindow(weekday=wednesday.weekday(), start_minute=19 * 60, end_minute=21 * 60),
    )
    assert day_supply(wednesday, profile, apart, {}).minutes == 180


def test_exception_beats_weekly_window() -> None:
    """某一天的例外要盖过周期性时段,而且"整天不可用"要真的归零。

    优先级写反的话,用户标记的"这周三出差"会被"每周三晚上有空"覆盖 —— 而他在飞机上
    看到手机里排着三场任务。
    """
    wednesday = TODAY + timedelta(days=(2 - TODAY.weekday()) % 7)
    profile = CapacityProfile(weekly_total_minutes=600, safety_factor=Decimal("1.00"), daily_max_minutes=120)
    windows = (AvailabilityWindow(weekday=wednesday.weekday(), start_minute=19 * 60, end_minute=21 * 60),)

    off = day_supply(wednesday, profile, windows, {wednesday: DayException(on_date=wednesday, is_unavailable=True)})
    assert off.minutes == 0
    assert off.binding is BindingConstraint.AVAILABILITY

    # 例外给的是"空闲",每日上限给的是"意愿",两者取小。
    short = day_supply(
        wednesday, profile, windows, {wednesday: DayException(on_date=wednesday, available_minutes=300)}
    )
    assert short.minutes == 120, "例外绕过了每日上限"


def test_days_outside_the_horizon_have_no_capacity() -> None:
    """视界之外的日子池子是 0,而不是"没限制"。

    当成没限制的话,一个没有截止日的任务会被排到明年 —— 而用户看到的是"从 2027 年
    3 月开始做第一件事"。
    """
    profile = CapacityProfile(weekly_total_minutes=600, safety_factor=Decimal("1.00"), daily_max_minutes=120)
    pools = build_day_pools(start=TODAY, horizon_days=3, profile=profile)

    assert len(pools.days) == 3
    assert pools.pool(TODAY + timedelta(days=3)) == 0
    assert pools.supply(TODAY + timedelta(days=3)).binding is BindingConstraint.HORIZON


@pytest.mark.parametrize(
    ("day", "expected_monday"),
    [
        (date(2026, 9, 21), date(2026, 9, 21)),  # 周一
        (date(2026, 9, 25), date(2026, 9, 21)),  # 周五
        (date(2026, 9, 27), date(2026, 9, 21)),  # 周日 —— 归到本周,不是下周
        (date(2026, 9, 28), date(2026, 9, 28)),  # 下周一
    ],
)
def test_week_starts_on_monday(day: date, expected_monday: date) -> None:
    """周界是周一。周日必须归到**它所在的那一周**,不能算成下一周的开头。"""
    assert week_start_of(day, 0) == expected_monday
