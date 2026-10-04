"""首页「本周计划 / 本日计划」的**跨空间聚合**载荷。

首页要同时展示这个账号**全部活动空间**的本周计划与今天安排。逐个空间读 `/plan`
会有两个问题:请求数随空间数增长(前端无法控制),而且每个空间各画一份,首页要
自己 join 起来 —— 那等于把"哪份是当前计划"的判断抄到前端第二遍。

所以服务端一次查完、投影成下面这一份。它**只收正式、活跃、未归档的 `PlanNode`
与排期场次**:proposal 还没确认,库里根本没有提案里的节点;历史版本是 `archived`
状态,在这里被排除。首页因此不可能把未确认或已替换的东西画成"当前计划"。

## 为什么工作块与任务共用 `AgendaTodayItemView`

用户问的是"今天要做什么",而不是"今天要做什么工作块、又有什么任务"。两者是同一
件事的两种来源:排了场次的(有几点、多久)和只标了截止日的(还没排)。分开两种形状
会让界面必须写两套渲染,而"同一件事被画两遍"正是要避免的事。用 `kind` 区分来源,
界面只排一次序。

## `targets` 为什么也在这里

首页的「添加」表单要让用户选空间、阶段(以及该阶段当前周的周计划)。这份清单和上面
的聚合来自同一次读取 —— 让前端再对每个空间发一次请求,就把 N+1 从计划搬到了表单上。
"""

from __future__ import annotations

import uuid
from datetime import date

from pydantic import Field

from backend.contracts.common import ApiModel


class AgendaSessionView(ApiModel):
    """一个任务在当前周里的排期场次。**可为空** —— 没有具体日期的任务也要出现。"""

    id: uuid.UUID
    scheduled_date: date
    planned_minutes: int
    start_minute: int | None = None
    end_minute: int | None = None
    seq: int = 0
    status: str = "planned"


class AgendaWeekTaskView(ApiModel):
    """一周计划下的一个未归档任务(或里程碑)。"""

    node_id: uuid.UUID
    title: str
    node_type: str
    status: str
    deadline: date | None = None
    estimate_minutes: int | None = None
    priority: str = "medium"
    #: 该任务在本周(或更远)的场次。空数组 = 没有具体日期,但它**仍然属于本周计划**。
    sessions: list[AgendaSessionView] = Field(default_factory=list)


class AgendaWeekPlanView(ApiModel):
    """某个空间、某个阶段下当前自然周的活跃周计划。"""

    workspace_id: uuid.UUID
    workspace_title: str
    plan_node_id: uuid.UUID
    plan_title: str
    #: 所属阶段(周计划的父节点)。用户要知道这件事挂在哪个阶段下面。
    stage_id: uuid.UUID | None = None
    stage_title: str | None = None
    tasks: list[AgendaWeekTaskView] = Field(default_factory=list)


class AgendaTodayItemView(ApiModel):
    """今天的一件事。`kind` 区分它来自已排场次还是只有截止日的任务。"""

    #: `block`(已有排期场次)/ `task`(只标了今天截止,还没排)。
    kind: str
    #: `block` 才有;`task` 恒为 None。
    session_id: uuid.UUID | None = None
    node_id: uuid.UUID
    workspace_id: uuid.UUID
    workspace_title: str
    title: str
    stage_id: uuid.UUID | None = None
    stage_title: str | None = None
    scheduled_date: date
    #: `task` 的来源没有场次,这里退化为该节点的预计工时(可空)。
    planned_minutes: int | None = None
    start_minute: int | None = None
    end_minute: int | None = None
    #: 场次状态(`block` 才有)。任务的完成状态在 `node_status` 里。
    session_status: str | None = None
    #: 节点整体状态(`pending` / `doing` / `completed`)。勾选完成写的就是它。
    node_status: str = "pending"
    #: 该场次最近一次执行反馈;`None` 表示没有记录,不表示没做。
    result: str | None = None
    recorded: bool = False


class AgendaWeekSessionView(ApiModel):
    """某一周里的一场已排场次。顶部「本周时间线」按天铺开的就是它。

    它与 `AgendaTodayItemView` 的区别是粒度:一个任务可以有多场,这里逐场返回。
    """

    id: uuid.UUID
    node_id: uuid.UUID
    workspace_id: uuid.UUID
    workspace_title: str
    node_title: str
    scheduled_date: date
    planned_minutes: int
    start_minute: int | None = None
    end_minute: int | None = None
    status: str = "planned"


class AgendaPhaseTargetView(ApiModel):
    """添加表单里的一个可选阶段,以及它当前周的活跃周计划(可空)。"""

    stage_id: uuid.UUID
    stage_title: str
    week_plan_id: uuid.UUID | None = None
    week_plan_title: str | None = None


class AgendaWorkspaceTargetView(ApiModel):
    workspace_id: uuid.UUID
    workspace_title: str
    phases: list[AgendaPhaseTargetView] = Field(default_factory=list)


class TodayPlansResponse(ApiModel):
    """首页下方两个页签的全部数据。**只读,一次查完。**"""

    today: date
    timezone: str
    #: 当前自然周(周一 ~ 周日),按用户时区算。
    week_start: date
    week_end: date
    #: 全部活动空间的当前周活跃周计划(含没有日期的任务)。
    week_plans: list[AgendaWeekPlanView] = Field(default_factory=list)
    #: 全部活动空间今天的安排:已排场次 + 今天截止但还没排的任务。
    today_items: list[AgendaTodayItemView] = Field(default_factory=list)
    #: 这一周的全部已排场次(逐场)。顶部「本周时间线」用它,不再逐空间拉 `/plan`。
    week_sessions: list[AgendaWeekSessionView] = Field(default_factory=list)
    #: 添加表单的可选目标(空间 -> 阶段 -> 当前周周计划)。
    targets: list[AgendaWorkspaceTargetView] = Field(default_factory=list)


__all__ = [
    "AgendaPhaseTargetView",
    "AgendaSessionView",
    "AgendaTodayItemView",
    "AgendaWeekPlanView",
    "AgendaWeekSessionView",
    "AgendaWeekTaskView",
    "AgendaWorkspaceTargetView",
    "TodayPlansResponse",
]
