"""站内提醒。

## 这是**站内**的,不是推送

提醒只在用户打开界面时出现。邮件、手机推送属于后续的独立能力 —— 那时候要解决投递
通道、退订、以及"半夜推送"这类问题,与这里的"什么时候该跟用户说一句话"是两件事。
把这层说成"已经具备离线提醒",用户会真的以为关掉浏览器也会收到。

## 每一条都由具体事件触发

不做"每天定时提醒学习":它在用户已经连续做了两周之后照样每天出现,然后就被无视了。
一个总是出现的提醒等于没有提醒。

## 提醒的内容是推导的,被存下来的只有用户的处置

库里没有"提醒表"。每次请求按事实重算(空间还空着、计划刚更新、几天没记录、到周末了、
某个阶段刚完成),`reminder_states` 里只记"用户关掉了哪条、让哪条过一会儿再说"。

这样做的实际好处是**没有失败模式**:如果提醒是一行数据,就必须有人在对的那一刻把它
写进去,于是需要定时任务,而任务没跑的时候提醒就永远不出现。推导则不会 —— 它的正确性
只取决于查询时的库状态。
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.dependencies.auth import AuthContext, get_auth_context
from backend.api.dependencies.clock import get_now
from backend.contracts.reminder import (
    DismissReminderRequest,
    RemindersResponse,
    ReminderStateView,
    SnoozeReminderRequest,
)
from backend.db.session import get_db
from backend.services import reminder_service

router = APIRouter()


@router.get("", response_model=RemindersResponse, summary="现在该显示的提醒")
async def list_reminders(
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
    now: datetime = Depends(get_now),
) -> RemindersResponse:
    """这个账号此刻该看到的提醒。**只读,不创建任何行。**

    跨全部活动空间 —— 提醒说的是"有事情在等你",而用户只有一个晚上。

    `quietHours` 一并返回:用户需要知道"现在没有提醒"是因为真的没有,还是因为处在
    免打扰时段而被压住了。这两件事在界面上长得一模一样,含义却相反,所以
    `suppressedCount` 会如实报出被压住的条数。

    `get_now` 是一个**可替换的依赖**(见 `api/dependencies/clock.py`)。它换掉的是
    "此刻是几点",不是免打扰这条规则 —— 22:00–08:00 该怎么压还是怎么压。
    """
    return await reminder_service.load(db, auth.user, now=now)


@router.post("/dismiss", response_model=ReminderStateView, summary="关掉一条提醒")
async def dismiss_reminder(
    payload: DismissReminderRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
) -> ReminderStateView:
    """关掉之后不再出现。**重复点没有副作用** —— 结果一样。

    键放在请求体里而不是路径上:`weekend:2026-W39` 这样的值不是一行资源的 id,
    而是由 kind + 时间片(+ 空间)拼出来的稳定标识。同一个键在下一个时间片会变成
    另一条提醒,所以"关掉"关的是**这一条**,不是"这类提醒以后都别出现"。

    想要后者的话,那是一个偏好设置,属于后续能力 —— 这里如实标注。
    """
    return await reminder_service.dismiss(db, auth.user, payload.key)


@router.post("/snooze", response_model=ReminderStateView, summary="让一条提醒过一会儿再说")
async def snooze_reminder(
    payload: SnoozeReminderRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
    now: datetime = Depends(get_now),
) -> ReminderStateView:
    """**"稍后"是一个真实的承诺。**

    到点之后它会重新出现,而不是被软化成"关掉"。这也是 `snoozedUntil` 必须落库的
    原因:只在内存里记一笔的话,刷新页面它就立刻回来了,用户会以为按钮坏了。

    `get_now` 与 `list_reminders` 用的是**同一个**依赖:落库的到期时刻和之后拿它比
    大小的"此刻"必须是同一个钟,否则"稍后 24 小时"会在两边各算一次。
    """
    return await reminder_service.snooze(
        db, auth.user, payload.key, hours=payload.hours, now=now
    )
