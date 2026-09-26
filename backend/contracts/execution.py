"""执行反馈与「今天」。

## `saved` 为什么是一个显式字段

用户录一次"今天做完了 60 分钟",他需要知道**这一条到底进没进库**。数据库不可用时
服务端返回 503,响应体里带 `saved: false`;成功时 200 带 `saved: true`。两者形状
一致,客户端只需要读一个布尔值,不需要靠状态码去猜。

这不是形式主义:谎报成功会让那条记录永久消失,而用户以为它在。所以成功响应里也
带着这个字段 —— 一个只在失败时出现的字段,客户端很容易在成功路径上忘了处理它。

## `needsCheckIn` 与 `result is None` 的分工

`result` 为空表示**没有任何记录**。它与 `result == "skipped"` 是两件完全不同的事:
后者是用户说了"这场我没做",前者是我们**不知道**。

`needsCheckIn` 把这件事变成一句可执行的话:这一场已经过去了、而且我们不知道结果,
界面应当**问**,而不是把它悄悄算成"没完成"。把"未记录"当成"未完成"会让复盘从第一
天起就在算一个用户从没确认过的偏差,而他纠正不了。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import Field

from backend.contracts.common import ApiModel
from backend.contracts.plan import SessionPayload

#: 单次反馈的分钟数上限。一天只有 1440 分钟,而这个字段是"这场做了多久" ——
#: 一个手滑多打了两个零的值会让当天的占用统计彻底失真。
MAX_ACTUAL_MINUTES = 1440


class RecordExecutionRequest(ApiModel):
    """一次执行反馈。**只追加**,不会修改这条记录。"""

    #: completed | partial | skipped | failed。闭集校验在服务层做,理由与
    #: `node_service._parse_enum` 相同:这里报错要指向真正填错的那个字段。
    result: str
    #: 客户端生成一次,**重试时复用同一个**。作用与提案确认、排期应用上的那一份
    #: 完全一样:网络超时重发不该产生第二条记录。
    idempotency_key: str = Field(min_length=8, max_length=64)

    started_at: datetime | None = None
    ended_at: datetime | None = None
    actual_minutes: int | None = Field(default=None, ge=0, le=MAX_ACTUAL_MINUTES)
    #: 0.0 ~ 1.0。部分完成时给出"做到哪儿了"。
    completion_ratio: float | None = Field(default=None, ge=0.0, le=1.0)
    #: 为什么没做完 / 没做。这是"记录阻碍"那条路的落点 ——
    #: 用户说得出原因的时候,复盘才可能给出有用的调整。
    delay_reason: str | None = Field(default=None, max_length=1000)
    user_feedback: str | None = Field(default=None, max_length=2000)


class ExecutionRecordView(ApiModel):
    """落库之后的那条记录,原样回给客户端。"""

    id: uuid.UUID
    session_id: uuid.UUID | None = None
    node_id: uuid.UUID
    workspace_id: uuid.UUID
    result: str
    actual_minutes: int | None = None
    completion_ratio: float | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    delay_reason: str | None = None
    user_feedback: str | None = None
    created_at: datetime


class RecordExecutionResponse(ApiModel):
    """一次反馈的结果。

    `session` 是写完之后的**那一行场次**,不是客户端发上来的东西 —— 界面据此显示
    "已完成 / 还在进行"这类状态时,看到的是库里的真相。
    """

    #: 这条记录有没有真的落库。**成功时也是显式的 true**,见模块开头。
    saved: bool = True
    record: ExecutionRecordView
    session: SessionPayload | None = None
    #: 这次响应是不是重放之前那一次的。用户双击时第二次是 true。
    replayed: bool = False
    #: 记录之后,这个节点还剩几场没过、几场已经完成。给界面一句"还剩 2 次"。
    node_remaining_sessions: int = 0
    node_completed_sessions: int = 0


class ExecutionHistoryResponse(ApiModel):
    """一个场次的反馈历史。**只读,新的在前。**

    `session` 与记录一起返回,是因为用户打开"这场做过什么"时真正想知道的是
    "现在这场算不算做完了" —— 那是场次行的状态,不是某一条记录的内容。
    """

    session: SessionPayload | None = None
    records: list[ExecutionRecordView] = Field(default_factory=list)


class TodayItemView(ApiModel):
    """今天要做的一件事。"""

    session_id: uuid.UUID
    node_id: uuid.UUID
    workspace_id: uuid.UUID
    workspace_title: str
    node_title: str
    planned_minutes: int
    buffer_minutes: int = 0
    seq: int = 0
    start_minute: int | None = None
    end_minute: int | None = None
    status: str = "planned"
    locked: bool = False

    #: 用户对这场反馈过什么。**None 表示没有记录,不表示没完成。**
    result: str | None = None
    actual_minutes: int | None = None
    delay_reason: str | None = None
    #: 有没有反馈过。与 `result is None` 同义,单独给出来是因为它比一个可空字段
    #: 更难在客户端被漏判成"假值 = 没做"。
    recorded: bool = False


class TodayWorkspaceView(ApiModel):
    """一个活动空间今天的安排。按空间分组是为了让界面能直接显示"哪个目标占的"。"""

    workspace_id: uuid.UUID
    title: str
    items: list[TodayItemView] = Field(default_factory=list)


class CheckInQuestion(ApiModel):
    """一句**提问**,不是一句结论。

    没有记录不等于没完成 —— 用户可能是做完了忘了记,也可能是那天临时有事。
    系统能给的最准确的东西就是这句话本身,以及一个"记一下"的入口。
    """

    session_id: uuid.UUID
    node_id: uuid.UUID
    workspace_id: uuid.UUID
    node_title: str
    scheduled_date: date
    days_ago: int
    planned_minutes: int
    question: str


class TodayResponse(ApiModel):
    """跨空间的「今天」。**聚合这一个账号的全部活动空间。**"""

    today: date
    timezone: str
    workspaces: list[TodayWorkspaceView] = Field(default_factory=list)

    planned_minutes: int = 0
    #: 已经反馈过的那些场次里,用户实际花掉的分钟数合计。**只统计有记录的** ——
    #: 把没记录的算成 0 会让"今天投入了多久"在下午就变成一个假数字。
    actual_minutes: int = 0
    item_count: int = 0
    recorded_count: int = 0

    #: 今天已经过去、但还没有任何记录的那些场次 —— 它们构成上面那批提问。
    check_in_questions: list[CheckInQuestion] = Field(default_factory=list)
    #: 服务端拼好的一句话,如实说明"没有记录"意味着什么。
    note: str = ""


__all__ = [
    "CheckInQuestion",
    "ExecutionHistoryResponse",
    "ExecutionRecordView",
    "RecordExecutionRequest",
    "RecordExecutionResponse",
    "TodayItemView",
    "TodayResponse",
    "TodayWorkspaceView",
]
