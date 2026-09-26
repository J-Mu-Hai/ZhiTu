"""切分:一个任务切成若干场,以及"已经做了多少"的算法。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from backend.scheduler.pack import already_delivered, split_sessions
from backend.scheduler.types import CapacityProfile


def _profile(**kwargs) -> CapacityProfile:
    """切分只关心这几个字段,所以这里给一份最小的档,免得测试和 conftest 的默认值
    纠缠 —— 改 conftest 的每日上限不该让切分测试的期望值跟着变。"""
    base: dict[str, object] = {
        "weekly_total_minutes": 600,
        "safety_factor": Decimal("1.00"),
        "min_session_minutes": 15,
        "max_session_minutes": 60,
        "default_buffer_minutes": 10,
    }
    base.update(kwargs)
    return CapacityProfile(**base)  # type: ignore[arg-type]


def test_long_task_splits_into_even_sessions() -> None:
    """480 分钟、上限 60 → 8 场,每场 60。切出来的分钟数合起来必须正好等于原工时。

    少一分钟意味着用户永远做不完那件事(剩下的零头排不进任何一场),多一分钟意味着
    凭空多出一段他没有的时间。
    """
    chunks = split_sessions(480, _profile())
    assert len(chunks) == 8
    assert [chunk.minutes for chunk in chunks] == [60] * 8
    assert sum(chunk.minutes for chunk in chunks) == 480
    assert [chunk.seq for chunk in chunks] == list(range(1, 9))


def test_remainder_is_spread_not_dumped_in_a_fragment() -> None:
    """500 分钟不能切成 120+120+120+120+20。

    最后那 20 分钟是一场几乎无法开始的安排(打开文档、进入状态、收尾),而用户在
    日历上看到的是"周五还要做 20 分钟"。先算场次数再等分,碎片就不存在。
    """
    chunks = split_sessions(500, _profile(max_session_minutes=120))
    minutes = [chunk.minutes for chunk in chunks]

    assert sum(minutes) == 500
    assert max(minutes) <= 120
    assert max(minutes) - min(minutes) <= 1, f"切得不均:{minutes}"


def test_max_session_is_a_hard_constraint() -> None:
    """`max_session_minutes` 是硬约束:宁可多切一场,也不排一场超长的。

    排出一场比用户说过的上限更长的安排,他会直接跳过它 —— 那比多切一场糟得多。
    """
    for total, maximum in [(250, 60), (121, 120), (1000, 90), (61, 60)]:
        chunks = split_sessions(total, _profile(max_session_minutes=maximum))
        assert all(chunk.minutes <= maximum for chunk in chunks), (total, maximum, chunks)
        assert sum(chunk.minutes for chunk in chunks) == total


def test_a_short_task_is_still_one_session() -> None:
    """整件事不到 `min_session_minutes` 时,仍然排成一场。

    按"低于下限就不排"处理的话,用户会在任务列表里看到一个永远不出现在日历上的任务 ——
    而它本来只要十分钟。
    """
    chunks = split_sessions(10, _profile(min_session_minutes=30))
    assert len(chunks) == 1
    assert chunks[0].minutes == 10


def test_every_chunk_carries_the_buffer() -> None:
    """缓冲加在**每一场**上,包括这个任务的最后一场。

    "最后一场不算缓冲"会让同一天的总占用取决于那是不是某个任务的收尾,用户自己算不
    出来 —— 而他正需要那个数字来判断今天还放不放得下别的事。
    """
    chunks = split_sessions(180, _profile(default_buffer_minutes=10))
    assert [chunk.buffer_minutes for chunk in chunks] == [10, 10, 10]
    assert [chunk.occupies_minutes for chunk in chunks] == [70, 70, 70]


def test_nothing_to_do_yields_no_sessions() -> None:
    """零工时切不出场次 —— 不是"切出一场 0 分钟"。"""
    assert split_sessions(0, _profile()) == ()
    assert split_sessions(-5, _profile()) == ()


@pytest.mark.parametrize(
    ("done_minutes", "reported_minutes", "expected"),
    [
        # 同一段进度被两处各记一次:取较大值,不能相加。
        (60, 60, 60),
        (60, 100, 100),
        (100, 60, 100),
        (0, 0, 0),
    ],
)
def test_delivered_takes_the_max_not_the_sum(
    done_minutes: int, reported_minutes: int, expected: int
) -> None:
    """一个任务的已完成工时**取较大值**,不相加。

    相加会得到"一个 100 分钟的任务做完了 160 分钟",于是它再也不需要排期 —— 而它其实
    没做完。用户会在复盘时发现计划说"已完成",而他自己知道那件事还没动。
    """
    assert (
        already_delivered(
            done_minutes=done_minutes, reported_minutes=reported_minutes, estimate_minutes=200
        )
        == expected
    )


def test_delivered_never_exceeds_the_estimate() -> None:
    """报过量也不能超出估算 —— 否则 `remaining` 变负数,任务被当成做完了。

    用户多报了几分钟(他可能连查资料的时间一起算上了),系统不该因此认为整个任务完成。
    """
    assert already_delivered(done_minutes=500, reported_minutes=0, estimate_minutes=300) == 300


def test_delivered_without_an_estimate_falls_back_to_the_max() -> None:
    """没有估算时不做截断 —— 没有天花板可以截。

    这里返回 0 会让一个已经做了两小时的任务被当成没开始,于是它整个再排一遍。
    """
    assert already_delivered(done_minutes=90, reported_minutes=0, estimate_minutes=None) == 90
