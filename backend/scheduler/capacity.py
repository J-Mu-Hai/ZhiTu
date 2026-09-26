"""池子的记账,以及可行性闸门。

## 全用户只有一个池子

`PoolTracker` 里没有"每个空间一个池子"这种东西,只有 `日期 -> 已用分钟`。按空间分开
记账的话,两个空间会各自以为自己能占满同一周的晚上,而用户只有一个晚上 —— 这个错误在
界面上看不出来,因为两个空间各自的计划都"排得下",只有合起来看才超。

`load_on` 仍然按空间分开,**但那是审计轨迹,不是池子**:它用来回答"这周满了,是什么
占满的"。用户看到的必须是"因为「考研」占了 4 小时",而不是一个 480 这个数字。

## 缓冲计入占用

一场 60 分钟、缓冲 10 分钟的任务在当天占 70 分钟。把缓冲排除在外的话,"当天总占用 ≤
上限"这条不变量就只能信任算法的内部状态 —— 而它存在 `scheduled_sessions` 的行上,
本来就是为了让人能随时从数据库直接复核。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date

from backend.scheduler.calendar import DayPools, week_start_of
from backend.scheduler.errors import BindingConstraint


@dataclass(frozen=True)
class Room:
    """某一天还能放多少分钟,以及**真正卡住的是哪一个约束**。

    第二项是规则 (h) 要求点名的东西。只说"排不下",用户能做的只有盲猜;说"卡在每日
    上限上",他立刻知道该去调哪一个数字。
    """

    minutes: int
    binding: BindingConstraint

    @property
    def is_empty(self) -> bool:
        return self.minutes <= 0


@dataclass
class PoolTracker:
    """一次排期过程中的池子。**可变是刻意的,但它只活在一次 `simulate()` 里。**

    做成可变对象而不是"每排一场就返回一份新池子",是因为排 60 个任务会产生 60 份
    池子的拷贝,而它们除了最新那份都立刻被丢掉。纯度由"输入不被修改、输出只由输入
    决定"来保证,不由"过程中没有可变状态"来保证 —— 后者在这个规模上只是性能税。
    """

    pools: DayPools
    week_start_weekday: int = 0

    #: `日期 -> 已用分钟`(含缓冲)。
    _daily_used: dict[date, int] = field(default_factory=dict)
    #: `日期 -> (空间 -> 分钟)`。**审计轨迹**,不参与任何判断。
    _daily_by_workspace: dict[date, dict[uuid.UUID, int]] = field(default_factory=dict)
    #: `那一周的起始日 -> 已用分钟`。
    _weekly_used: dict[date, int] = field(default_factory=dict)

    # ---------------------------------------------------------------- 读
    def day_used(self, day: date) -> int:
        return self._daily_used.get(day, 0)

    def daily_remaining(self, day: date) -> int:
        return max(0, self.pools.pool(day) - self.day_used(day))

    def week_used(self, day: date) -> int:
        return self._weekly_used.get(week_start_of(day, self.week_start_weekday), 0)

    def weekly_remaining(self, day: date) -> int:
        return max(0, self.pools.weekly_budget - self.week_used(day))

    def room(self, day: date) -> Room:
        """这一天还能放多少。

        两个约束取小,并且**如实说出是哪一个更紧**:两者都是"这周/这天排满了"的
        直接来源,而用户面对它们要做的决定不同(调每日上限 vs 调每周预算)。
        """
        daily = self.daily_remaining(day)
        weekly = self.weekly_remaining(day)
        if daily <= weekly:
            return Room(minutes=daily, binding=BindingConstraint.DAILY_MAX)
        return Room(minutes=weekly, binding=BindingConstraint.WEEKLY_BUDGET)

    def load_on(self, day: date) -> dict[uuid.UUID, int]:
        return dict(self._daily_by_workspace.get(day, {}))

    # ---------------------------------------------------------------- 写
    def take(self, day: date, workspace_id: uuid.UUID, minutes: int) -> None:
        """占用。**调用方必须先问过 `room`** —— 这里不检查,越界由不变量断言兜住。

        不在这里静默截断成"能放多少放多少":截断会让一个排不下的任务变成"排下了一部分
        而且没人告诉你",而用户要的恰恰是知道还差多少。
        """
        self._daily_used[day] = self.day_used(day) + minutes
        bucket = self._daily_by_workspace.setdefault(day, {})
        bucket[workspace_id] = bucket.get(workspace_id, 0) + minutes
        week = week_start_of(day, self.week_start_weekday)
        self._weekly_used[week] = self._weekly_used.get(week, 0) + minutes

    def release(self, day: date, workspace_id: uuid.UUID, minutes: int) -> None:
        """归还。用于"先试排、发现违反依赖再撤回"这一类回退。"""
        self._daily_used[day] = max(0, self.day_used(day) - minutes)
        bucket = self._daily_by_workspace.get(day)
        if bucket is not None:
            bucket[workspace_id] = max(0, bucket.get(workspace_id, 0) - minutes)
            if bucket[workspace_id] == 0:
                del bucket[workspace_id]
        week = week_start_of(day, self.week_start_weekday)
        self._weekly_used[week] = max(0, self._weekly_used.get(week, 0) - minutes)

    def audit_trail(self) -> tuple[tuple[date, tuple[tuple[uuid.UUID, int], ...]], ...]:
        """交给 `ScheduleResult.daily_load` 的那份快照。排序保证同一输入同一输出。"""
        return tuple(
            (day, tuple(sorted(bucket.items(), key=lambda pair: str(pair[0]))))
            for day, bucket in sorted(self._daily_by_workspace.items())
        )


@dataclass(frozen=True)
class Feasibility:
    """闸门结论:"这些工时在截止日之前放得下吗"。**不是"能排多满",是"能不能"。**

    两种"放不下"要分清:

    - `shortfall_minutes` —— 日子够,分钟数不够(用户每周只有 4 小时,而计划要 12 小时)。
      出路是[少做点]或[延期]。
    - `late_minutes` —— 分钟数够,但**截止日之前**放不下(前 3 周都在考试)。
      出路是[延期]。
    """

    required_minutes: int
    capacity_minutes: int
    shortfall_minutes: int
    binding: BindingConstraint | None
    detail: dict[str, object] = field(default_factory=dict)

    @property
    def fits(self) -> bool:
        return self.shortfall_minutes <= 0


def assess_feasibility(
    *,
    required_minutes: int,
    pools: DayPools,
    last_day: date | None = None,
) -> Feasibility:
    """把"要求做的分钟数"和"视界内(或截止日之前)能拿出的分钟数"比一比。

    只用于**告诉用户结果**和生成选项:排期本身不依赖它做决定,它逐场次地排,排不下
    就记缺口。先算闸门再决定排不排的话,一条算错的闸门会让整个计划排不出来,而用户
    看到的是一句笼统的"排不下"。

    ## 一周能拿出的不是"7 个每日池相加",而是再被周预算削一次

    两个约束是独立的:**每天**不超过每日池,而且**那一周**不超过周预算。所以一周的
    实际容量是 `min(那一周各天池子之和, 周预算)`,整条视界再把各周加起来。

    少了这层 `min` 的话,闸门会把容量报成 7 × 每日池 —— 用户说"每周 8 小时、每天最多
    4 小时"时,它算出"一周有 28 小时",于是对着一个根本排不下的计划说"没问题"。
    而用户是在看到缺口之前唯一一次听到结论的机会;这一次说错了,他接下来做的事
    (增加投入 / 少做点)全是基于一个错的数字。

    周界与 `week_start_of` 一致。视界截断在某一周中间时,那一周仍然按**完整的**周预算
    算:周一到周三确实可以花掉整周的预算,所以按比例缩水反而会低估。
    """
    days = pools.days
    if last_day is not None:
        days = tuple(day for day in days if day <= last_day)

    weekly: dict[date, int] = {}
    for day in days:
        week = week_start_of(day)
        weekly[week] = weekly.get(week, 0) + pools.pool(day)
    per_week = {week: min(total, pools.weekly_budget) for week, total in weekly.items()}
    capacity = sum(per_week.values())

    # 真正卡住的那个约束,而不是永远说"容量不足"。
    binding: BindingConstraint | None = None
    if required_minutes > capacity:
        if last_day is not None:
            binding = BindingConstraint.DEADLINE
        elif any(total > pools.weekly_budget for total in weekly.values()):
            binding = BindingConstraint.WEEKLY_BUDGET
        else:
            binding = BindingConstraint.DAILY_MAX

    return Feasibility(
        required_minutes=required_minutes,
        capacity_minutes=capacity,
        shortfall_minutes=max(0, required_minutes - capacity),
        binding=binding,
        detail={
            "days": len(days),
            "lastDay": last_day.isoformat() if last_day else None,
            "weeklyBudget": pools.weekly_budget,
            "dailyCap": pools.daily_cap,
            # 按周分开,好让界面说得出"是第几周装不下",而不只是一个总数。
            "weeks": [
                {"weekStart": week.isoformat(), "pools": total, "usable": per_week[week]}
                for week, total in sorted(weekly.items())
            ],
        },
    )


__all__ = ["Feasibility", "PoolTracker", "Room", "assess_feasibility"]
