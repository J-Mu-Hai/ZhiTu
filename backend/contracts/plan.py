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

#: 画布上"前置"这个类型的名字 —— **接口上的名字,不是库里的名字**。
#: 库里它是 `DependencyType.FINISH_TO_START`,存在 `dependencies` 表;而
#: `related_to` / `influences` 存在 `node_relations` 表。三种类型在接口上共用一个
#: `relationType` 字段,所以"这个字符串对应哪张表"这件事必须有**一处**定义。
#:
#: 定义在契约层而不是某个服务里,是因为 `node_service` 与 `plan_service` 都要用它,
#: 而让其中一个 import 另一个会形成循环(写与读互相依赖)。
DEPENDS_ON = "depends_on"


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
    #: 正文的版本号。客户端保存正文时把它原样带回来,对不上就是 409
    #: `CONCURRENCY_CONFLICT` —— 说明另一个标签页改过同一段正文。
    #:
    #: 它是**节点级**的,不是空间级(`revision_version` 是空间级)。用空间版本号做
    #: 正文冲突检测,会让"另一个标签页勾掉了一个任务"变成"我的正文保存失败"。
    content_version: int = 1


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


class RelationPayload(ApiModel):
    """画布上的一条边。**三种类型共用一个形状。**

    ## 这是一份投影,不是第二份存储

    `depends_on` 来自 `dependencies` 表(它就是那张表的行),`related_to` 与
    `influences` 来自 `node_relations` 表。在这里合并成一种形状,是因为画布只需要
    "有哪些边、连的是谁、是什么类型" —— 让前端自己去 join 两张表,等于把"哪种类型
    存在哪里"这个知识复制到前端,而它是会变的。

    存储层**不合并**(见 `db/models/enums.py::NodeRelationType`):`depends_on` 参与
    排期,另外两种不参与。投影可以合并,规则不可以。

    ## 方向

    `source_id -> target_id`。对 `depends_on` 来说就是**前置 -> 后续**,与
    `DependencyPayload.predecessor_id -> successor_id` 同向,也与数据库里存的
    `dependencies` 行列同向 —— **全仓只有这一个方向约定,任何一层都不做反转**。

    `related_to` 是**无向**的:它存哪一头在前是按 UUID 排的,不代表方向。前端画虚线
    无箭头,不要去解释 `source` 是"主语"。
    """

    id: uuid.UUID
    relation_type: str
    source_id: uuid.UUID
    target_id: uuid.UUID
    #: 用户写在这条边上的说明。`depends_on` 恒为 None —— `dependencies` 表没有说明
    #: 列,而"补一个空串"会让界面上出现一条看不出是"没写"还是"写了空"的边。
    note: str | None = None
    #: 谁连的。**`depends_on` 恒为 None**:`dependencies` 表没有 `origin` 列。
    #: 猜一个 "user" 会让 AI 提案加的依赖看起来像用户自己连的 —— 而这正是
    #: `NodeOrigin` 这个枚举存在的理由(见 `ModelSource` 的注释,同一条纪律)。
    origin: str | None = None
    #: 只有 `depends_on` 有:前置完成后还要等几天。另外两种恒为 None。
    lag_days: int | None = None


class CreateRelationRequest(ApiModel):
    """连一条边。

    **默认类型由前端决定,服务端不做"没写就是前置"的猜测。** `relation_type` 必填:
    把用户随手拖的一条线解释成"任务前置"会改变排期结果,那是产品里最不该由默认值
    决定的一件事。
    """

    source_id: uuid.UUID
    target_id: uuid.UUID
    relation_type: str
    note: str | None = None


class UpdateRelationRequest(ApiModel):
    """改一条边。**"没传"与"传了 null"是两件事**,同 `UpdateNodeRequest`。

    `relation_type` 只能在 `related_to` 与 `influences` 之间换 —— 换成/换出
    `depends_on` 会跨表(那是排期输入),这一版拒绝并给出明确原因,不静默丢说明。
    """

    relation_type: str | None = None
    #: 传 null 是"把说明清空",不传是"不改说明"。
    note: str | None = None


class LayoutPositionPayload(ApiModel):
    """一个节点在画布上的位置。单位是画布坐标,不是屏幕像素。"""

    node_id: uuid.UUID
    x: float
    y: float


class ScopeViewportPayload(ApiModel):
    """一个层级(总空间或某个子空间)的平移与缩放。"""

    scope_node_id: uuid.UUID
    zoom: float
    pan_x: float
    pan_y: float


class PutLayoutRequest(ApiModel):
    """把这一次看到的布局整体提交上来。

    ## 为什么是"整份提交"而不是逐条 PATCH

    拖动一个节点会连续产生几十个中间位置,逐条发请求意味着几十次写入和几十个
    并发冲突。整份提交让"最后一次赢"成为语义本身,不需要按时间戳做仲裁 ——
    而按时间戳仲裁在客户端时钟不准时是错的。

    ## 它**不删除**没出现在这份请求里的位置

    "整 scope 覆盖"听起来像"没提交的就是不要了",但位置是按 `(用户, 空间, 节点)`
    存的,不按 scope 分。前端在某个子空间里只能看到那一层的节点,如果这里做
    全量替换,提交一次就会**删掉其他所有层级的位置** —— 而它看起来完全正常,
    直到用户返回上一层发现节点全叠在一起了。
    """

    positions: list[LayoutPositionPayload] = Field(default_factory=list)
    viewports: list[ScopeViewportPayload] = Field(default_factory=list)


class LayoutPayload(ApiModel):
    """这个用户在这个空间里的全部布局。**按用户取,不按空间共享。**"""

    positions: list[LayoutPositionPayload] = Field(default_factory=list)
    viewports: list[ScopeViewportPayload] = Field(default_factory=list)


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
    #: 画布上所有的边,三种类型一起(见 `RelationPayload`)。它**包含**
    #: `dependencies` 的那一份 —— 前者是"画什么",后者是"排期读什么"。
    #: 一次请求拿全,是因为画布在一次渲染里就要用到全部三种线。
    relations: list[RelationPayload] = Field(default_factory=list)
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
    #: **只有彻底删除才会大于 0。** 归档一行边都不动(`?mode=archive`),所以这个数字
    #: 为 0 不代表"什么都没删"。客户端不要拿它当"删干净了"的证据。
    removed_dependencies: int = 0
    #: 这一次删除能不能用 `POST /nodes/{id}/restore` 拿回来。
    restorable: bool = True


class ArchiveImpactPayload(ApiModel):
    """归档/彻底删除**之前**的影响范围。给确认框用的那一份数字。

    由后端算而不是前端拿本地计划推:前端手里那份可能是几分钟前的。用户在确认框里
    看到的数字和实际发生的事对不上,比不给数字更糟 —— 他会照着那个数字做决定。
    """

    node_id: uuid.UUID
    title: str
    #: 会一起被归档的后代节点数(不含自己)。
    descendants: int
    #: 会从画布上消失的 `related_to` / `influences` 关系条数。
    relations: int
    #: 会从画布上消失的「前置 → 后续」依赖条数。
    dependencies: int
    #: 挂在这一支上的排期场次条数与分钟数。归档之后它们不再出现在计划里
    #: (恢复时会原样回来)。
    sessions: int
    session_minutes: int
    #: 其中已经过了日期、还标着"待做"的那几场 —— 恢复之后它们需要用户自己处理。
    overdue_sessions: int


class OverbookedDayPayload(ApiModel):
    """恢复之后,哪一天超了每日上限。"""

    day: date
    planned_minutes: int
    daily_cap: int
    over_by: int


class RestoreResponse(ApiModel):
    """一次恢复的结果。

    **排期那几个数字不是锦上添花。** 恢复会把归档期间冻结的场次一次性放回日历,
    它们可能已经过期、也可能和归档之后新排的挤在同一天。这份报告就是"不许静默恢复"
    的那一半:界面必须把"回来几场、过期几场、哪几天超了"说出来。
    """

    node: PlanNodePayload
    revision_version: int
    #: 一共恢复回来几个节点(含子树)。
    restored_count: int
    restored_sessions: int
    restored_minutes: int
    #: 其中日期已过、状态仍是"待做/进行中"的场次。
    overdue_sessions: int
    #: 恢复之后超了每日上限的那些天。**这一栏比的是每日上限,没有重跑可用时段** ——
    #: 用户那天本来就没有可用时段时会少报(见 `node_service._restore_schedule_report`)。
    overbooked_days: list[OverbookedDayPayload] = []
    #: 跟着一起回来的关系与依赖条数。它们**没有**被归档删掉过,所以这里说的是
    #: "重新可见",不是"重新创建"。给界面用来解释"为什么边也回来了"。
    relations_visible: int = 0


class ArchivedNodePayload(ApiModel):
    """归档列表里的一行。

    列表里只出现**一次归档的根**:同一批被带走的子孙不单独列(点那一行的"恢复"
    就会把它们一起带回来)。父节点在**另一次**归档里的时候,它仍然单独列出来 ——
    那时点它会得到一句"先恢复上层",而不是从列表里凭空消失。
    """

    node: PlanNodePayload
    archived_at: datetime
    #: 这一次归档带走的子孙数。
    descendants: int
    #: 跟着一起收起来的场次数。
    sessions: int
    #: 能不能恢复。`false` 时原因在 `blocked_reason` 里 —— 界面据此把那一条的按钮
    #: 置灰并写明原因,而不是让用户点一下撞一句错误。
    restorable: bool = True
    #: `PARENT_ARCHIVED`(先恢复上层)或 `PARENT_PURGED`(上层被彻底删除了,回不来)。
    blocked_reason: str | None = None


__all__ = [
    "ArchiveImpactPayload",
    "ArchivedNodePayload",
    "BriefView",
    "CreateDependencyRequest",
    "CreateNodeRequest",
    "CreateRelationRequest",
    "DependencyPayload",
    "LayoutPayload",
    "LayoutPositionPayload",
    "NodeEditResponse",
    "OverbookedDayPayload",
    "PlanNodePayload",
    "PlanPayload",
    "PutLayoutRequest",
    "RelationPayload",
    "RestoreResponse",
    "ScopeViewportPayload",
    "SessionPayload",
    "UpdateNodeRequest",
    "UpdateRelationRequest",
]
