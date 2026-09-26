"""计划的**唯一**载荷。

## 为什么只有一个 `/plan` 接口,而不是 path / timeline / tasks 各一个

前端有四个视图:路径、时间线、任务、周计划。它们看上去像是四种数据,实际上是同一份
计划被投影了四次。给每个视图配一个接口,就等于承诺了"四份数据一直一致" —— 而它们
不一致的时候不会有任何东西报错,只会出现"任务列表里有这一条、路径图上没有"。

所以只有一个载荷,四个视图都读它。视图之间的差异全部在前端完成。

## `revisionVersion` 为什么要出现在载荷里

它是"你看到的这份计划是哪个版本"。用户看了一会儿、计划在另一个标签页被改过、
再回来点确认 —— 客户端拿这个版本号和提案的 `baseRevisionVersion` 一比,就能在
发请求之前发现"你看的那份已经旧了"。少了这个字段,这件事只能在服务端发现,
而那时用户已经做了确认这个动作,体验是"我点了确认,它说不行"。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import Field

from backend.contracts.common import ApiModel


class PlanNodePayload(ApiModel):
    """一个计划节点。**不含任何排期字段** —— "哪天做"在 sessions 里。"""

    id: uuid.UUID
    parent_id: uuid.UUID | None = None
    title: str
    description: str | None = None
    acceptance_criteria: str | None = None
    node_type: str
    status: str
    priority: str
    #: 预计工时(分钟)。是排期的输入,不是排期结果。
    estimate_minutes: int | None = None
    deadline: date | None = None
    depth: int = 0
    order_index: int = 0
    origin: str
    completed_at: datetime | None = None
    created_at: datetime


class SessionPayload(ApiModel):
    """一个排期场次 —— "哪天做"的**唯一**表示。

    一个节点要 8 小时就有 8 行这个,而 `plan_nodes` 里始终只有一行。这不是实现细节,
    是产品规则:把「写文献综述」为了填满日历复制成 8 个同名任务,用户要勾 8 次完成、
    进度百分比失去意义,复盘时也看不出它们其实是同一件事。

    `start_minute` 为空是**一等状态**,不是缺失值:用户说过"我每天大概两小时",没说
    过"我 19:00 开始"。凭空指定一个时钟时间会让他在自己的日历里看到一个自己没同意过
    的时间表。
    """

    id: uuid.UUID
    node_id: uuid.UUID
    workspace_id: uuid.UUID
    #: 冗余带上。一个空间里几十个节点时,界面为了显示一排场次要来回查表,而这条
    #: 载荷本来就已经把所有节点一起发过去了 —— 服务端 join 一次的代价比它小。
    node_title: str
    scheduled_date: date
    planned_minutes: int
    #: 缓冲也计入当天占用。存在行上,所以"当天总占用 ≤ 上限"随时能从数据库复核。
    buffer_minutes: int = 0
    actual_minutes: int | None = None
    seq: int = 0
    status: str = "planned"
    #: 用户锁定后,重排绝不会自动移动它。
    locked: bool = False
    lock_reason: str | None = None
    origin: str = "scheduler"
    start_minute: int | None = None
    end_minute: int | None = None
    completed_at: datetime | None = None


class DependencyPayload(ApiModel):
    id: uuid.UUID
    predecessor_id: uuid.UUID
    successor_id: uuid.UUID
    dep_type: str
    lag_days: int = 0


class BriefView(ApiModel):
    """当前已知的规划条件。

    `missing` 是服务端算出来的"还缺哪些条件",不是前端猜的。界面据此提示
    "还差每周时间预算",与模型是否可用无关 —— 模型挂了也该显示。

    定义在 plan.py 而不是 conversation.py:`missing` 是用来判断"能不能排计划"的,
    它属于计划这一侧的事实;对话只是恰好也在响应里带上它。
    """

    version: int = 0
    goal: str | None = None
    deadline: str | None = None
    weekly_available_minutes: int | None = None
    current_level: str | None = None
    success_criteria: str | None = None
    constraints: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)


class PlanPayload(ApiModel):
    """一个空间的完整计划。路径 / 时间线 / 任务 / 周计划都读这一份。"""

    workspace_id: uuid.UUID
    revision_version: int
    nodes: list[PlanNodePayload] = Field(default_factory=list)
    dependencies: list[DependencyPayload] = Field(default_factory=list)
    brief: BriefView = Field(default_factory=BriefView)

    #: 排期场次。**已取消与已搬走的场次不在这里** —— 它们是墓碑,仍在库里,复盘时
    #: 查得到,但"我的计划是什么"这个问题里不该出现一个已经不存在的安排。
    sessions: list[SessionPayload] = Field(default_factory=list)
    #: 计划里有多少个节点、多少个已完成。由服务端算 —— 前端数 `nodes.length` 的话,
    #: 一旦将来按需分页,这两个数字就会悄悄变成"当前这一页的数量"。
    total_nodes: int = 0
    completed_nodes: int = 0


class CreateNodeRequest(ApiModel):
    """用户手工新建一个节点。

    **没有 `parentRef`/`localId` 这套记号。** 那套东西是给模型用的 —— 模型看不见
    真实 UUID,所以需要服务端替它把记号翻译回 id;而用户点的是界面上那个真实的
    节点,客户端手里本来就有 UUID。硬套同一套契约只会让前端多做一次无意义的映射。
    """

    parent_id: uuid.UUID
    title: str
    node_type: str = "task"
    description: str | None = None
    acceptance_criteria: str | None = None
    priority: str = "medium"
    estimate_minutes: int | None = None
    deadline: date | None = None


class UpdateNodeRequest(ApiModel):
    """用户改一个节点。

    **每个字段都可选,而且"没传"和"传了 null"是两件事。** 前者表示不改,后者表示
    清空(`deadline` 从有日期改成没有日期)。用 `model_dump(exclude_unset=True)` 取
    差异,所以两者不会混起来 —— 混起来的后果是"我只改了标题,说明被清空了"。
    """

    title: str | None = None
    description: str | None = None
    acceptance_criteria: str | None = None
    node_type: str | None = None
    status: str | None = None
    priority: str | None = None
    estimate_minutes: int | None = None
    deadline: date | None = None


class CreateDependencyRequest(ApiModel):
    predecessor_id: uuid.UUID
    successor_id: uuid.UUID


class NodeEditResponse(ApiModel):
    """一次直接编辑的结果:改完之后的那个节点,加上新的计划版本号。

    **带上版本号是必须的。** 客户端本地缓存着计划,编辑成功后它要么重取整份计划、
    要么就地打补丁;无论哪种,它都需要知道"现在是多少版" —— 否则下一次它拿本地
    那份旧计划去比对 `baseRevisionVersion`,会以为自己看到的是最新的。
    """

    node: PlanNodePayload
    revision_version: int
    #: 只对删除有意义:这次一共删掉了几个节点(含子树)、几条依赖。
    deleted_count: int = 0
    removed_dependencies: int = 0


__all__ = [
    "BriefView",
    "CreateDependencyRequest",
    "CreateNodeRequest",
    "DependencyPayload",
    "NodeEditResponse",
    "PlanNodePayload",
    "PlanPayload",
    "SessionPayload",
    "UpdateNodeRequest",
]
