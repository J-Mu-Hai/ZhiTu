"""站内提醒。

## 这是**站内**提醒,不是推送

它只在用户打开界面时出现。邮件、手机推送属于后续的独立能力,那时候要解决的是
投递通道、退订、以及"半夜推送"这类问题 —— 与这里的"什么时候该跟用户说一句话"
是两件事。把这层包装成"已经具备离线提醒",用户会真的以为关掉浏览器也会收到。

## 每一条提醒都由**具体事件**触发

不做"每天定时提醒学习"这类时间驱动的东西:它会在用户已经连续做了两周的时候照样
每天出现,而那种提醒很快就会被无视 —— 一个总是出现的提醒等于没有提醒。这里的每
一条都对应一个真实发生过的事件:新建了空空间、刚生成了计划、连续几天没记录、
到周末了、一个阶段完成了。

## 免打扰时段来自用户自己的可用时段

用户如果说过"我平时晚上 19:00 到 22:00 有空",那 22:00 之后本来就不该被系统叫住。
没有可用时段时退回默认的 22:00–08:00。这是**推导**出来的,不是配置项 ——
一个可以配置的免打扰时段需要一整套设置界面,那是后续的事,这里如实标注。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import Field

from backend.contracts.common import ApiModel


class QuietHoursView(ApiModel):
    """当前的免打扰时段,以及此刻是不是正处在里面。"""

    active: bool = False
    #: 当日分钟数。`from_minute > to_minute` 表示这段跨过午夜(例如 22:00 -> 08:00)。
    from_minute: int = 22 * 60
    to_minute: int = 8 * 60
    description: str = ""
    #: 这个时段是从哪来的:用户的可用时段推导出来的,还是默认值。
    source: str = "default"


class ReminderView(ApiModel):
    """一条提醒。"""

    key: str
    #: 触发它的事件:`workspace_empty` / `plan_created` / `user_returned` /
    #: `repeated_skips` / `weekend` / `stage_completed`。
    kind: str
    title: str
    body: str
    workspace_id: uuid.UUID | None = None
    #: 这条提醒说的是哪一天的事。键里带着它,所以同一条提醒下周是新的一条。
    for_date: str = ""


class RemindersResponse(ApiModel):
    """当前该显示的提醒。"""

    reminders: list[ReminderView] = Field(default_factory=list)
    quiet_hours: QuietHoursView = Field(default_factory=QuietHoursView)
    #: 因为处在免打扰时段而被压住的提醒条数。**如实说出来** —— 用户不该把
    #: "现在没有提醒"读成"系统认为一切正常"。
    suppressed_count: int = 0
    note: str = ""


class DismissReminderRequest(ApiModel):
    """关掉一条提醒。

    键放在请求体里而不是路径上:它不是一行资源的 id,而是一个由 kind + 时间片 + 空间
    拼出来的稳定标识(`weekend:2026-W39`),把它塞进 URL 路径只会让"这个值是复合的"
    这件事变得不明显。
    """

    key: str = Field(min_length=3, max_length=160)


class SnoozeReminderRequest(ApiModel):
    """让一条提醒过一会儿再说。

    **"稍后"是一个真实的承诺**:到点之后它会重新出现,而不是被软化成"关掉"。
    """

    key: str = Field(min_length=3, max_length=160)
    hours: int = Field(default=24, ge=1, le=168)


class ReminderStateView(ApiModel):
    """一次处置的结果。"""

    key: str
    dismissed: bool = False
    snoozed_until: datetime | None = None


__all__ = [
    "DismissReminderRequest",
    "QuietHoursView",
    "ReminderStateView",
    "ReminderView",
    "RemindersResponse",
    "SnoozeReminderRequest",
]
