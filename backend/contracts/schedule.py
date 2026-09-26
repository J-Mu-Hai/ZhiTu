"""排期的两份载荷:预览与应用。

Python 侧一律 snake_case,线格式由 `ApiModel` 的 alias 生成器转成 camelCase ——
与 `plan.py`、`proposal.py` 同一套约定。

## 预览返回的 `scheduleVersion` 是应用时的门票

中间隔着一次界面渲染、一次点击和一次网络往返。这段时间里计划可能被别的东西改了
(用户自己勾了个完成、另一个标签页确认了一份提案)。`scheduleVersion` 是**输入**的
指纹,应用时服务端拿此刻的输入再算一遍,对不上就 409 让用户重新预览。

为什么是"重新预览"而不是"照样应用":用户点头的是他在屏幕上看到的那一份。输入变了
之后重排出来的可能是另一份(任务挪到了别的日子),而用户没有看过它。这与提案路径上
`STALE_BASE_REVISION` 是同一条纪律。

## 为什么预览里要带 `capacityMinutes`

缺口报告最容易做砸的地方不是算出"这周满了",而是让用户理解**为什么**满了。只给一个
`unscheduledMinutes` 的话,用户能做的只有盲猜,而他能做的三件事(少做点 / 延期 /
多投入)分别对应不同的约束。所以每一天都带上"排了多少"和"这一天本来有多少" ——
两者放在一起,"这周满了"才是一句可核对的话,而不是一堵数字墙。

`byWorkspace` 分开记账的理由:用户问"是谁占的"时,答案必须是空间名。写不出这个答案
的话,他会以为排期系统自己把时间吃掉了。
"""

from __future__ import annotations

import uuid
from datetime import date

from pydantic import Field

from backend.contracts.common import ApiModel


class PlannedSessionView(ApiModel):
    """排期算法希望存在的一场。

    `session_id` 为空表示这一场是**新加的**;非空表示它对应库里已经有的一行(可能挪了
    日子,也可能原样没动)。界面上"新增 3 场 / 移动 2 场"这句摘要就靠它区分。

    `node_title` 冗余带上:预览是独立的一份载荷,界面不该为了显示"周三做《写文献综述》"
    再去拉一次节点表。
    """

    session_id: uuid.UUID | None = None
    node_id: uuid.UUID
    workspace_id: uuid.UUID
    node_title: str
    scheduled_date: date
    planned_minutes: int
    #: 缓冲也计入当天占用。单独给出来,是为了让"这天占了多少"能在界面上被复核。
    buffer_minutes: int = 0
    seq: int = 0
    start_minute: int | None = None
    end_minute: int | None = None
    origin: str = "scheduler"
    locked: bool = False


class ScheduleGapView(ApiModel):
    """排不进去的那部分。**结构化的事实,不是一句"排不下"。**

    `bindingConstraint` 是"哪一道闸门卡住的",用户能做的三件事各自对应一个取值:
    每日上限 -> 少做点,周预算 -> 少做点或多投入,可用时段 -> 换个时间,
    锁定场次 -> 解开一个锁,截止日 -> 延期。给错约束的代价由用户承担。
    """

    workspace_id: uuid.UUID | None = None
    node_id: uuid.UUID | None = None
    node_title: str = ""
    unscheduled_minutes: int = 0
    reason_code: str
    binding_constraint: str
    detail: dict = Field(default_factory=dict)


class RecoveryOptionView(ApiModel):
    """一条出路。`resolvesGap` 是**重跑一遍算出来的**,不是断言的。"""

    kind: str
    label: str
    description: str
    resolves_gap: bool = False
    remaining_unscheduled_minutes: int = 0
    params: dict = Field(default_factory=dict)


class DailyLoadView(ApiModel):
    """某一天被占了多少、本来有多少。"""

    date: date
    planned_minutes: int = 0
    #: 这一天用户最多能投入多少。为 0 说明这天被标成了不可用,或者超出了视界。
    capacity_minutes: int = 0
    by_workspace: dict[str, int] = Field(default_factory=dict)


class ScheduleChurnView(ApiModel):
    """这次排期动了多少东西。**要说给用户听。**"""

    moved: int = 0
    created: int = 0
    canceled: int = 0
    kept: int = 0
    #: 服务端拼好的一句话("本次调整：移动 2 场，新增 3 场。")。让前端拼的话,
    #: 三种语言习惯会各自长出来一份,而这个句子的措辞是这个产品的一部分。
    description: str = ""


class SchedulePreviewResponse(ApiModel):
    """预览一份排期。**只读,不产生任何写入。**"""

    #: 把这份结果写进库时要带上的门票。
    schedule_version: str = ""
    today: date
    horizon_days: int = 0
    #: 这次排期覆盖了哪些空间。**是"哪些"而不是"哪一个"** —— 时间池按人算,
    #: 所以一份排期天然横跨用户的全部活动空间。界面必须如实说出来。
    scope_workspace_ids: list[uuid.UUID] = Field(default_factory=list)

    #: 这份排期是建立在"每周多少分钟"上的(`weekly_total_minutes × safety_factor`)。
    #: 用户问"为什么只排了这么点儿"时,这是唯一能回答它的数字。
    weekly_budget_minutes: int = 0

    sessions: list[PlannedSessionView] = Field(default_factory=list)
    gaps: list[ScheduleGapView] = Field(default_factory=list)
    options: list[RecoveryOptionView] = Field(default_factory=list)
    daily_load: list[DailyLoadView] = Field(default_factory=list)
    churn: ScheduleChurnView = Field(default_factory=ScheduleChurnView)

    total_planned_minutes: int = 0
    unscheduled_minutes: int = 0
    #: 恒为 false。存在的意义是让"永不静默截断"是一条可断言的性质:排不下的部分
    #: 一定出现在 `gaps` 里,而不是安静地消失。
    truncated: bool = False


class ScheduleApplyRequest(ApiModel):
    """应用一份排期。"""

    schedule_version: str = Field(min_length=1, max_length=64)
    #: 由调用方生成一次,**重试时复用同一个**。换一个键就等于告诉后端"这是另一次
    #: 应用",网络超时重发会变成写两遍 —— 同一个任务在同一天多出两场。
    idempotency_key: str = Field(min_length=8, max_length=64)


class ScheduleAppliedView(ApiModel):
    """实际写进去了多少。

    `created` / `updated` / `canceled` 这几个数字取自数据库回报的 `rowcount`,
    不是算法说它想改多少。两者不一致时用户看到的是真实发生的那个数 —— 比如某一行
    在写入前被状态保护挡下来了,报告里就不会算成"已取消"。
    """

    created: int = 0
    updated: int = 0
    moved: int = 0
    canceled: int = 0
    kept: int = 0
    #: 这次写入碰了几个空间。
    workspaces: int = 0
    #: 应用之后,全账号还有多少分钟没排下。应用不会让它变少 —— 应用的正是同一份输入。
    unscheduled_minutes: int = 0


class ScheduleApplyResponse(ApiModel):
    schedule_version: str
    applied: ScheduleAppliedView
    churn: ScheduleChurnView = Field(default_factory=ScheduleChurnView)
    #: 这次响应是不是重放之前那一次的。用户双击"应用"时第二次是 true。
    replayed: bool = False


__all__ = [
    "DailyLoadView",
    "PlannedSessionView",
    "RecoveryOptionView",
    "ScheduleAppliedView",
    "ScheduleApplyRequest",
    "ScheduleApplyResponse",
    "ScheduleChurnView",
    "ScheduleGapView",
    "SchedulePreviewResponse",
]
