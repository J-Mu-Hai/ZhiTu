"""日期算术,以及**每天那个分钟池**。

## `safety_factor` 只乘一次,就在这个文件里

    weekly_budget = weekly_total_minutes × safety_factor

这个乘积同时是每日上限的推导来源和可行性闸门的输入。文档里
`WeeklyAvailableMinutes × RemainingWeeks × SafetyFactor` 是可行性闸门那一侧;如果
每日池再乘一次,系统会报出一批**根本不存在的缺口** —— 而用户看到"这周排不下"时唯一
的反应是增加投入,那恰好是最不该给的建议。所以这里就只有这一个乘积,别处一律引用它。

## 池子是按**人**算的,不是按空间

`build_day_pools` 返回的是一个 `date -> 分钟` 的表,与空间无关。两个空间争的是同一个
晚上,这是产品规则 (c) 能成立的前提。按空间各算一份池子的话,两个空间会各自以为
自己能占满整晚,而用户只有一个晚上 —— 而且这个错误在界面上看不出来:两个空间各自的
计划都"排得下",只有合起来看才超。

## 没给具体时段时,按"某天多少分钟"排

`start_minute` 为 `None` 是一等状态。用户说过"我每天大概两小时",没说过"我 19:00
开始" —— 凭空指定 19:00 只会制造无谓的日历抖动,而且他在预览里会看到一个自己没同意过
的时间表。所以池子算的是**分钟数**,时钟时间留空。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from backend.scheduler.errors import BindingConstraint
from backend.scheduler.types import (
    AvailabilityWindow,
    CapacityProfile,
    DayException,
)

#: 一天的分钟数。写成常量而不是到处 1440,是为了让"这个数字是什么意思"有个落点。
DAY_MINUTES = 1440


def iter_days(start: date, count: int) -> tuple[date, ...]:
    """从 `start` 起连续的 `count` 天(含 `start`)。`count <= 0` 时返回空。"""
    return tuple(start + timedelta(days=offset) for offset in range(max(0, count)))


def week_start_of(day: date, week_start_weekday: int = 0) -> date:
    """`day` 所在那一周的起点。

    默认 0 = 周一,与 `date.weekday()` 一致。**不用"周日开头"的直觉**:`weekday()`
    把周日算成 6,而以周日为界的话,周日会被归到上周的末尾 —— 用户说"这周还剩几天"
    时想的是从现在到周日,不是从上周一到(上)周日。
    """
    offset = (day.weekday() - week_start_weekday) % 7
    return day - timedelta(days=offset)


def weekly_budget(profile: CapacityProfile) -> int:
    """每周可投入的分钟数。**`safety_factor` 在整份算法里只乘这一次。**"""
    total = Decimal(max(0, profile.weekly_total_minutes))
    factor = Decimal(profile.safety_factor)
    return int((total * factor).to_integral_value(rounding="ROUND_FLOOR"))


def daily_cap(profile: CapacityProfile) -> int:
    """单日上限。

    用户给了 `daily_max_minutes` 就用它;没给就把周预算平摊到 7 天 —— 后者不是"又乘了
    一次安全系数",它就是同一个 `weekly_budget` 除以 7。两个约束各自独立生效:平摊出来
    的每日池 × 7 恰好等于周预算,而用户显式给了每日上限时(比如 4 小时/天 × 7 天 =
    28 小时 > 周预算 8 小时),**周预算才是真正卡住的那个**。
    """
    if profile.daily_max_minutes is not None:
        return max(0, profile.daily_max_minutes)
    return math.ceil(weekly_budget(profile) / 7)


@dataclass(frozen=True)
class DaySupply:
    """某一天池子的来源。缺口报告要靠它说出"为什么这天只有 0 分钟"。"""

    minutes: int
    source: str
    #: 这一天真正卡住的约束。`minutes` 为 0 时它尤其重要。
    binding: BindingConstraint
    detail: dict[str, object]


def _window_minutes_on(
    windows: tuple[AvailabilityWindow, ...], day: date
) -> tuple[int, tuple[AvailabilityWindow, ...]]:
    active = tuple(window for window in windows if window.weekday == day.weekday() and window.covers(day))
    return sum(window.minutes for window in active), active


def day_supply(
    day: date,
    profile: CapacityProfile,
    windows: tuple[AvailabilityWindow, ...],
    exceptions: dict[date, DayException],
) -> DaySupply:
    """这一天用户**最多**能投入多少分钟,以及这个数字是谁定的。

    优先级:例外(整天不可用) > 例外(当天可用分钟数) > 周期性可用时段 > 平摊的默认值。
    四条都会再被 `daily_cap` 削一次 —— 用户说"这周三晚上有 5 小时空",不等于他愿意
    一天投入 5 小时;每日上限是**意愿**,可用时段是**空闲**,两者取小。

    **重叠时段的合并在这里做,不在调用方做。** 放在外面的版本有个安静的失败模式:
    `build_day_pools` 合了,而任何别处直接调用本函数的代码没合,于是同一天多算出一段
    根本不存在的时间 —— 排出来的那一场用户没空做,而它看起来和其他场次一模一样。
    把正确性绑在"调用方记得先合"上,迟早会漏。
    """
    cap = daily_cap(profile)
    windows = _merged_windows(windows, day)

    exception = exceptions.get(day)
    if exception is not None and exception.is_unavailable:
        return DaySupply(
            minutes=0,
            source="exception",
            binding=BindingConstraint.AVAILABILITY,
            detail={"reason": "这一天被标记为不可用", "date": day.isoformat()},
        )
    if exception is not None and exception.available_minutes is not None:
        available = max(0, exception.available_minutes)
        minutes = min(available, cap)
        return DaySupply(
            minutes=minutes,
            source="exception",
            binding=(
                BindingConstraint.AVAILABILITY if minutes < available else BindingConstraint.DAILY_MAX
            ),
            detail={
                "reason": "这一天有单独的可用时间",
                "date": day.isoformat(),
                "availableMinutes": available,
                "dailyCap": cap,
            },
        )

    window_minutes, active = _window_minutes_on(windows, day)
    if active:
        minutes = min(window_minutes, cap)
        return DaySupply(
            minutes=minutes,
            source="availability",
            binding=(
                BindingConstraint.AVAILABILITY if minutes < window_minutes else BindingConstraint.DAILY_MAX
            ),
            detail={
                "reason": "按每周固定时段",
                "date": day.isoformat(),
                "windowMinutes": window_minutes,
                "dailyCap": cap,
                "windows": [w.start_minute for w in active],
            },
        )

    # 没有例外、也没有具体时段 —— 用平摊的每日上限。**这不是"随便给的默认值"**:
    # 用户明确说过每周能投入多少,而没说的只是"哪几天"。按天平分是最不假设的分配。
    return DaySupply(
        minutes=cap,
        source="weekly_spread",
        binding=BindingConstraint.WEEKLY_BUDGET if profile.daily_max_minutes is None else BindingConstraint.DAILY_MAX,
        detail={
            "reason": "按每周预算平摊到每天",
            "date": day.isoformat(),
            "weeklyBudget": weekly_budget(profile),
            "dailyCap": cap,
        },
    )


@dataclass(frozen=True)
class DayPools:
    """整条视界上每天的池子。**排期过程中只读它,不就地修改。**"""

    supplies: tuple[tuple[date, DaySupply], ...]
    weekly_budget: int
    daily_cap: int

    @property
    def days(self) -> tuple[date, ...]:
        return tuple(day for day, _ in self.supplies)

    def pool(self, day: date) -> int:
        for when, supply in self.supplies:
            if when == day:
                return supply.minutes
        # 视界之外的日子池子是 0,不是"没限制"。把它当没限制会让算法悄悄排到明年。
        return 0

    def supply(self, day: date) -> DaySupply:
        for when, supply in self.supplies:
            if when == day:
                return supply
        return DaySupply(
            minutes=0,
            source="outside_horizon",
            binding=BindingConstraint.HORIZON,
            detail={"reason": "超出了本次排期的视界", "date": day.isoformat()},
        )

    def week_budget_for(self, day: date, week_start_weekday: int = 0) -> int:
        return self.weekly_budget

    def total_capacity(self) -> int:
        """整条视界上能干活的分钟数。可行性闸门用它。"""
        return sum(supply.minutes for _, supply in self.supplies)


def build_day_pools(
    *,
    start: date,
    horizon_days: int,
    profile: CapacityProfile,
    windows: tuple[AvailabilityWindow, ...] = (),
    exceptions: tuple[DayException, ...] = (),
) -> DayPools:
    """把视界内每一天的池子算出来。

    重叠时段由 `day_supply` 自己合并 —— 这里不预处理,免得出现"两条路径算得不一样"。
    """
    by_date = {item.on_date: item for item in exceptions}
    supplies = tuple(
        (day, day_supply(day, profile, windows, by_date))
        for day in iter_days(start, horizon_days)
    )
    return DayPools(supplies=supplies, weekly_budget=weekly_budget(profile), daily_cap=daily_cap(profile))


def _merged_windows(
    windows: tuple[AvailabilityWindow, ...], day: date
) -> tuple[AvailabilityWindow, ...]:
    """把这一天的时段**合并重叠部分**后再交给 `day_supply`。

    实现方式是把同一天的区间排序后合并,再把合并结果表示成新的 `AvailabilityWindow`
    (它们已经只属于这一天,`weekday` 只用于过滤,所以这里原样带上)。
    """
    active = sorted(
        (w for w in windows if w.weekday == day.weekday() and w.covers(day)),
        key=lambda w: (w.start_minute, w.end_minute),
    )
    if len(active) < 2:
        return tuple(active)

    merged: list[tuple[int, int]] = []
    for window in active:
        if merged and window.start_minute <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], window.end_minute))
        else:
            merged.append((window.start_minute, window.end_minute))

    first = active[0]
    return tuple(
        AvailabilityWindow(
            weekday=first.weekday,
            start_minute=start_minute,
            end_minute=end_minute,
            effective_from=first.effective_from,
            effective_to=first.effective_to,
        )
        for start_minute, end_minute in merged
    )


__all__ = [
    "DAY_MINUTES",
    "DayPools",
    "DaySupply",
    "build_day_pools",
    "daily_cap",
    "day_supply",
    "iter_days",
    "week_start_of",
    "weekly_budget",
]
