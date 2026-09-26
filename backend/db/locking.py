"""跨数据库的行级守卫写。

SQLite **静默忽略** `SELECT ... FOR UPDATE` —— 不报错,只是不加锁。所以"用 FOR UPDATE
保证并发安全"在本地是一条看起来成立、实际不成立的规则:本地测试全绿,线上才出现并发
问题。这正是本地/线上分叉里最难发现的一类。

这里换成一次**守卫写**(guard write):在同一行上做一次 UPDATE。

- PostgreSQL:UPDATE 会在这行上加行锁,持到事务结束。后到的事务阻塞在同一个行上,
  于是"读版本号 -> 比较 -> 写入"这一串真正变成原子的。
- SQLite:UPDATE 让事务从读升级为写,拿到数据库级的 RESERVED 锁;后到的事务按
  `busy_timeout=5000`(见 db/session.py)等待,而不是直接失败。

代价是 `updated_at` 会被这次写更新。这个代价可以接受,而且语义上是对的 ——
守卫写发生的时刻,本来就是这个空间即将被修改的时刻。

**注意这不代替唯一约束。** 守卫写解决的是"两个请求同时读到 v3",而"同一个幂等键
被执行两次"由 `proposal_decisions` 的唯一约束解决。两者是两道独立的闸门,都要有:
守卫写挡并发,唯一约束挡重复提交(包括跨进程、跨重启的重复提交)。
"""

from __future__ import annotations

import uuid

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.base import utcnow
from backend.db.models import User, Workspace


async def lock_workspace(db: AsyncSession, workspace_id: uuid.UUID) -> None:
    """在这个空间的行上加一把跨数据库都真实存在的写锁。

    调用它必须**已经处在事务中**(否则 UPDATE 立即自动提交,锁当场释放,等于没加)。
    路由层的会话默认不开自动提交,服务层的确认流程也刻意把 commit 留到最后一次,
    所以这一点是成立的。
    """
    await db.execute(
        update(Workspace)
        .where(Workspace.id == workspace_id)
        .values(updated_at=utcnow())
        # 这条 UPDATE 不改变任何业务字段,不需要把变化同步回会话里的 ORM 对象。
        # 不加这一句,SQLAlchemy 会尝试在 Python 侧求值 WHERE 条件来同步会话。
        .execution_options(synchronize_session=False)
    )


async def lock_user(db: AsyncSession, user_id: uuid.UUID) -> None:
    """在这个**用户**的行上加一把写锁。

    ## 为什么排期不能只锁空间

    时间池是**按人**算的,不是按空间算的(见 `scheduler/calendar.py`)。两个空间争的
    是同一个晚上,而 `lock_workspace` 只锁住其中一个空间的行 —— 于是两个空间各自的
    "应用排期"会同时通过空间锁,各自以为整周的晚上都是自己的,两边都把自己排满。
    这个错误在界面上完全看不出来:两个空间各自的计划都"排得下",只有合起来看才超。

    锁在人这一行上,同一账号的两次排期必然排队,无论它们从哪个空间发起。行只有一行,
    所以也不存在"锁顺序"造成的死锁。

    代价与 `lock_workspace` 同:`users.updated_at` 会被这次写更新。语义上也说得通 ——
    守卫写发生的时刻,本来就是这个账号的安排即将被修改的时刻。
    """
    await db.execute(
        update(User)
        .where(User.id == user_id)
        .values(updated_at=utcnow())
        .execution_options(synchronize_session=False)
    )


__all__ = ["lock_user", "lock_workspace"]
