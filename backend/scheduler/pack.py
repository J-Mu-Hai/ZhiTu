"""把一个任务的工时切成若干**场次**。

## 一个任务永远是**一行** `PlanNode`,不是 N 行

这就是产品文档里"长任务切分"那条规则的全部含义。切分产生的是 `ScheduledSession`,不是
新的任务。反过来做(为了填满日历把「写文献综述」复制成 8 个同名任务)会带来三个后果:
用户要勾 8 次完成、进度百分比变得没有意义、而复盘时看不出"这 8 条其实是同一件事"。

## 切得均匀,不要留碎片

480 分钟按 120 分钟切是 4 场;按"能塞就塞"切也是 4 场。但如果切法不留心,一段
500 分钟的活会变成 120+120+120+120+20 —— 最后那 20 分钟是一场几乎无法开始的安排
(打开文档、进入状态、收尾)。所以这里**先算场次数,再等分**,并且保证每场不低于
`min_session_minutes`(除非整件事本来就比它还短)。

## 缓冲每一场都带

`default_buffer_minutes` 加在**每一场**上,包括这个任务的最后一场。统一处理是为了让
"这一天占了多少"可预测 —— 而"最后一场不算缓冲"会让同一天的总占用取决于那是不是某个
任务的收尾,用户没法自己算。缓冲计入每日池(见 `capacity.py`)。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from backend.scheduler.types import CapacityProfile


@dataclass(frozen=True)
class Chunk:
    """要排的一场。`seq` 是它在**本任务内**的序号,从 1 开始。"""

    minutes: int
    buffer_minutes: int
    seq: int

    @property
    def occupies_minutes(self) -> int:
        return self.minutes + self.buffer_minutes


def split_sessions(
    remaining_minutes: int,
    profile: CapacityProfile,
    *,
    seq_offset: int = 0,
    day_room_minutes: int | None = None,
) -> tuple[Chunk, ...]:
    """把 `remaining_minutes` 切成若干场。

    `seq_offset` 用于"这个任务已经有几场冻结在场了"的情形:新的场次序号接着往下排,
    这样 `(node_id, scheduled_date, seq)` 这个唯一键在"同一天既有旧的又有新的"时
    不会撞车。

    ## `day_room_minutes`:一天最多装得下多少

    这是**整个排期里最大的那一天的池子**,由调用方从时间池算出来。切出来的每一场都必须
    装得进某一天,所以 `minutes + buffer` 不能超过它。

    少了这个上限会发生什么,值得写下来,因为它曾经真的发生过:默认预算(每周 600 分钟、
    安全系数 0.8)平摊到每天是 69 分钟,而 `max_session_minutes` 的默认值是 120 ——
    于是一个 300 分钟的任务被切成 3 场 100 分钟的活,每场连缓冲要占 110 分钟,而**任何
    一天都放不下 110 分钟**。结果是这个任务一场也排不出来,缺口报告说"截止时间之前的
    时间都排满了",而 `dailyLoad` 里一分钟都没有。用户看到的是"我明明说了每周 10 小时,
    它却说我排满了"。

    传这个参数的意义是让切分**看得见它要挤进的那个日历**:日上限是硬约束,那么场的
    长度就必须服从它,而不是反过来。
    """
    remaining = int(remaining_minutes)
    if remaining <= 0:
        return ()

    minimum = max(1, profile.min_session_minutes)
    maximum = max(minimum, profile.max_session_minutes)
    buffer_minutes = max(0, profile.default_buffer_minutes)
    if day_room_minutes is not None:
        # 池子给的是"这一天一共多少分钟",而场次占的是 `净工时 + 缓冲`。所以这里减掉
        # 缓冲才是净工时的上限。下界仍取 `minimum`:一天连一场最小的（含缓冲）都装不下
        # 时,排不进去是**正确**的结论,把它切得比下限还小只会排出一场用户没法开始的
        # 安排。那种情形会以 `BELOW_MIN_SESSION` 缺口报到用户面前。
        maximum = max(minimum, min(maximum, int(day_room_minutes) - buffer_minutes))

    if remaining <= maximum:
        # 整件事一场就能做完。**哪怕它短于 `min_session_minutes` 也一样** ——
        # 那是"这件事本来就这么小",不是"切坏了"。把它藏起来不排的话,用户会看到
        # 一个在任务列表里、却永远不出现在日历上的任务。
        return (Chunk(minutes=remaining, buffer_minutes=buffer_minutes, seq=seq_offset + 1),)

    # 每场不超过 `maximum` → 至少这么多场。**`maximum` 是硬约束**:排出一场比用户说过
    # 的上限更长的安排,他会直接跳过它 —— 那比多切一场糟得多。
    #
    # 这里**不**去满足"每场不低于 `minimum`":两个下限/上限是会冲突的(活 250 分钟、
    # 下限 100、上限 120 —— 切 2 场每场 125 超上限,切 3 场每场 83 低于下限,无解)。
    # 冲突时让步的只能是下限:低于下限的场次用户照样能做,只是不理想。
    count = math.ceil(remaining / maximum)

    base, extra = divmod(remaining, count)
    return tuple(
        Chunk(
            minutes=base + (1 if index < extra else 0),
            buffer_minutes=buffer_minutes,
            seq=seq_offset + index + 1,
        )
        for index in range(count)
        # 余数摊到最前面的几场 —— 这个顺序是确定的,不依赖任何遍历顺序。
    )


def already_delivered(
    *,
    done_minutes: int,
    reported_minutes: int,
    estimate_minutes: int | None,
    committed_minutes: int = 0,
) -> int:
    """这个任务**已经有着落**的分钟数。

    三个来源,而它们的合并方式不一样 —— 这不是随手写的:

    - `done_minutes`:状态为 `done` 的场次(用户勾过完成)。
    - `reported_minutes`:用户**直接对任务**报告的进度(他没对某一场勾完成,而是说
      "这个任务整体做了一半")。它与 `done_minutes` 之间取**较大值**而不是相加 ——
      同一段进度被两处各记一次时,相加会得到"一个 100 分钟的任务做完了 160 分钟",
      于是它再也不需要排期,而它其实没做完。
    - `committed_minutes`:锁定在未来、不许挪动的场次。它们**已经占着**那一天,所以
      是**相加** —— 与 `done_minutes` 不会重叠,因为已完成的场次走的是上面那一路。

      少了这一项的话,一个 60 分钟、其中有 60 分钟被用户钉在周三的任务会被**再排**
      60 分钟:日历上同一件事占了两倍的时间,而用户会以为自己要做两小时。
    """
    total = max(0, done_minutes) + max(0, committed_minutes)
    total = max(total, max(0, reported_minutes))
    if estimate_minutes is None:
        return total
    return min(estimate_minutes, total)


__all__ = ["Chunk", "already_delivered", "split_sessions"]
