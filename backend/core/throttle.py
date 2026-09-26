"""失败次数限流(进程内)。

## 为什么需要它

登录接口在没有限流的情况下就是一个密码猜测器:攻击者可以用一个常见的密码字典,
每秒试几十次,而服务端不会有任何表示。密码哈希再慢也只是把单次尝试的成本提高了
常数倍,改变不了"可以无限试"这个事实。

## 为什么是进程内,而不是 Redis

6379 上没有任何服务在跑,而这个仓库的约束明确反对"为了一个计数器引入一套基础设施"。
所以这里就是一个字典 + 时间戳队列。

**必须如实说明它的边界:每个 worker 各算各的。** uvicorn 起 4 个 worker 时,实际允许
的尝试次数是 4 倍上限;进程重启计数清零。它挡的是"脚本狂试密码",不是"分布式撞库"。
真要挡后者,得把计数放到一个所有 worker 都能看到的地方 —— 那是换成 Redis 之后的事,
现在不宣称已经具备。

## 为什么只记失败

因为成功之后本来就该清零,而"只记失败"顺带避免了"正常用户被自己的正常使用限流"
这种最让人恼火的误伤。
"""

from __future__ import annotations

import time
from collections import deque

#: 一个键最多留多少个失败时间戳。超过就说明这个键已经被打爆了,
#: 多留没有意义,只会让内存随攻击增长。
_MAX_TIMESTAMPS_PER_KEY = 64
#: 字典最多多少个键。防止有人用海量随机邮箱把内存撑爆 —— 限流器本身不该是一个
#: 新的内存耗尽入口。
_MAX_KEYS = 4096


class FailureThrottle:
    """滑动窗口的失败计数器。

    单线程 asyncio 下的原子性:`retry_after` / `record_failure` / `clear` 里没有任何
    `await`,所以在一次调用中间不会被别的协程插进来。**如果将来这个类被用在线程池里,
    必须先加锁** —— 这一点写在这里,是因为"看起来只是几个 dict 操作"最容易被搬去
    多线程环境。
    """

    def __init__(self, *, limit: int, window_seconds: float) -> None:
        self._limit = limit
        self._window = window_seconds
        self._failures: dict[str, deque[float]] = {}

    def _prune(self, key: str, now: float) -> deque[float]:
        stamps = self._failures.get(key)
        if stamps is None:
            stamps = deque(maxlen=_MAX_TIMESTAMPS_PER_KEY)
            self._failures[key] = stamps
        cutoff = now - self._window
        while stamps and stamps[0] <= cutoff:
            stamps.popleft()
        return stamps

    def _evict_if_needed(self) -> None:
        if len(self._failures) <= _MAX_KEYS:
            return
        # 丢掉最早活动的那一批键。用插入顺序近似"最久没被碰过" —— 对一个内存兜底
        # 来说够用,不值得为它维护一个真正的 LRU 链表。
        for key in list(self._failures)[: len(self._failures) - _MAX_KEYS]:
            self._failures.pop(key, None)

    def retry_after(self, key: str) -> float | None:
        """被限流时返回还需等待的秒数,否则返回 None。"""
        now = time.monotonic()
        stamps = self._prune(key, now)
        if len(stamps) < self._limit:
            return None
        # 窗口里最早那次失败滑出去的时候,就有名额了。
        return max(0.0, stamps[0] + self._window - now)

    def record_failure(self, key: str) -> None:
        now = time.monotonic()
        self._prune(key, now).append(now)
        self._evict_if_needed()

    def clear(self, key: str) -> None:
        self._failures.pop(key, None)

    def clear_all(self) -> None:
        """清空全部计数。

        存在的唯一理由是测试隔离:限流器是**进程级**状态,一个测试里故意打爆它,
        下一个测试就会莫名其妙地被 429。测试要能拿到干净的状态。
        """
        self._failures.clear()


#: 登录失败限流:同一个邮箱 15 分钟内失败 10 次就停一会儿。
#:
#: 数值是有意宽松的:正常用户打错三五次密码很常见,不该被拦。这个阈值挡的是脚本 ——
#: 10 次/15 分钟意味着穷举一个 8 位密码需要几万年。
LOGIN_THROTTLE = FailureThrottle(limit=10, window_seconds=15 * 60)
