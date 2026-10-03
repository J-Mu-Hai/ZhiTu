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
from backend.db.models.enums import PLANNING_LEVEL_RANK, PlanningLevel

#: 画布上"前置"这个类型的名字 —— **接口上的名字,不是库里的名字**。
#: 库里它是 `DependencyType.FINISH_TO_START`,存在 `dependencies` 表;而
#: `related_to` / `influences` 存在 `node_relations` 表。三种类型在接口上共用一个
#: `relationType` 字段,所以"这个字符串对应哪张表"这件事必须有**一处**定义。
#:
#: 定义在契约层而不是某个服务里,是因为 `node_service` 与 `plan_service` 都要用它,
#: 而让其中一个 import 另一个会形成循环(写与读互相依赖)。
DEPENDS_ON = "depends_on"

# ---------------------------------------------------------------------------------
# 长度限制与用途规则
#
# 这两样放契约层而不是某个服务里,是因为**每一条都要被不止一处用**,而两处各写一遍
# 的代价不是"重复",是迟早只改一处 —— 那时候同一份输入在一条路上被拒、在另一条路上
# 被存下来,而用户看不出区别。
# ---------------------------------------------------------------------------------

#: 简短说明(「简述」)的码点上限。§2.1。
#:
#: **计数单位是 Unicode 码点,而 Python 的 `len()` 就是码点数** —— 这不是巧合,
#: 是这个上限能选在这里的理由。JS 那边必须用 `[...s].length`(见
#: `apps/web/src/lib/codepoints.ts`),因为 `s.length` 数的是 UTF-16 码元,
#: 一个星平面 emoji 会被数成 2 —— 那正是 §2.1 点名的错配。码点是唯一一条
#: 两边能按构造相等的口径(`Intl.Segmenter` 数的是字素,`👨‍👩‍👧` 是 1 个字素、
#: 5 个码点)。
MAX_DESCRIPTION_CODEPOINTS = 300

#: 长正文(节点笔记)的码点上限。§2.2。
#:
#: 它比 `MAX_DESCRIPTION_CODEPOINTS` 大两个数量级,而且**存的不是同一个地方**
#: (见 `db/models/note.py`):简述进 `plan_nodes.description`(每次改计划都被
#: 快照进版本账本),长正文进 `node_notes`。把长文塞进节点行会让每一行版本都背着
#: 每个节点的全文。
MAX_NOTE_CODEPOINTS = 20_000


def description_length_error(existing: str | None, incoming: str | None) -> str | None:
    """这次写入的说明是不是太长。返回**给用户看的那句话**,或者 `None`(可以写)。

    ## 上限是一条条件规则,这是用户选的

    存量已经超过上限的说明**继续想写多长写多长**;上限只落在"新写的说明"上。
    理由是这批上限是后来才有的:那些在它之前写下的长说明是用户当时唯一的表达方式,
    对它们收口等于**追溯性地宣布他已经写下的东西不合法** —— 而他会因此无法保存
    任何一次修改,连改个错别字都不行。

    代价要说清楚:同一个节点上"旧的能继续长、新的卡在 300"。它不是一把尺子,
    但它是确定的、服务端执行的,而且有一条可走的出路(改到 300 以内之后,
    从此按 300 算)。

    两个数都报给用户(上限与这一份的实际长度) —— 只说"太长了"等于让他自己数。
    """
    if incoming is None:
        return None
    limit = MAX_DESCRIPTION_CODEPOINTS
    if existing is not None and len(existing) > limit:
        return None
    if len(incoming) <= limit:
        return None
    return f"说明最多 {limit} 个字(按 Unicode 码点计),这一份有 {len(incoming)} 个。"


def information_node_conflicts(
    purpose: object,
    estimate_minutes: int | None,
    deadline: date | None,
    planning_level: object | None = None,
) -> str | None:
    """信息用途的节点和工时/截止不能同时存在。返回原因,或者 `None`。

    §4.1:信息主题不需要具备工时、完成勾选或截止日期。它回答的是"我了解到什么"
    (「我排名 38」),不是"我要做什么" —— 而工时与截止是**排期的输入**。让一个
    信息主题带着 90 分钟进排期,得到的是一个用户没打算做的日程;而它一旦排进去,
    日历上就分不出哪些是"要做的事"、哪些只是"知道的情况"。

    ## 为什么在契约层

    两条完全不同的路都要拦它,而它们必须拦得一模一样:

    - 手工编辑(`node_service.create_node` / `update_node`)→ 400 `INVALID_INPUT`;
    - AI 提案(`proposal_validation._create`)→ 提案级错误码。

    放服务里就要让 `proposal_validation` import 服务层,而那个模块的纪律是
    "纯函数、一次写入都没有"(`proposal_validation` 的模块 docstring)。放这里
    两边都只 import 契约,谁也不欠谁。

    ## 不传 `purpose` 的调用方

    `purpose` 收的是 `object` 而不是 `NodePurpose`:两个调用方手里一个是枚举、
    一个是动作里那个字段,而"值是不是 `information`"只跟字符串相等有关。
    用 `getattr(value, "value", value)` 取值,于是枚举与字符串两条路都成立 ——
    少写这一个转换,调用方就会各自写一次,而写漏的那次会让规则静默失效。
    """
    if getattr(purpose, "value", purpose) != "information":
        return None
    fields: list[str] = []
    if estimate_minutes is not None:
        fields.append("预计工时")
    if deadline is not None:
        fields.append("截止日期")
    if planning_level is not None:
        fields.append("规划层级")
    if not fields:
        return None
    what = "和".join(fields)
    # **两条出路都给**。这句话会被渲染三处(创建表单、详情编辑器、提案预览),
    # 而这三处用户想做的事不一定是同一件:有人是想把它变成信息主题,有人是
    # 手滑点错了用途。只说"不能带工时",他得自己猜该改哪一边。
    return (
        f"信息主题不进排期,所以不能带{what}。"
        f"要给它排期,就把用途改回「行动」;要让它当信息主题,就把{what}清掉。"
    )


#: 规划层级的中文名。**只在这里定义一处** —— 手工表单、提案摘要、前端标签读它。
PLANNING_LEVEL_LABELS: dict[str, str] = {
    "strategy": "战略",
    "phase": "阶段",
    "month": "月",
    "week": "周",
    "day": "日",
}


def as_planning_level(value: object | None) -> PlanningLevel | None:
    """把字符串/枚举/None 统一成 `PlanningLevel | None`。非法值返回 `None`。

    手工路径收的是字符串,AI 动作收的是枚举。两处都要"值是不是合法层级"这一个
    判断,所以收在契约层做一次 —— 分两处写就会漂移。
    """
    if value is None:
        return None
    if isinstance(value, PlanningLevel):
        return value
    try:
        return PlanningLevel(str(value))
    except ValueError:
        return None


def child_level_conflicts(parent_level: object | None, child_level: object | None) -> str | None:
    """父子的规划层级只能**从粗到细**(或同级)。返回原因,或 `None`。

    - `None` 表示"没指定层级" —— 它既不算粗也不算细,与任何层级都相容(存量兼容)。
    - 允许跳过中间层级:strategy → week 合法,不强制每个用户都先有 month/day。
    - 禁止 strategy 挂在 week/day 下,也禁止 week/day 成为 strategy 的父节点。

    手工建/改与 AI 提案都调它 —— 两条路必须拦得一模一样(同 `information_node_conflicts`)。
    """
    parent = as_planning_level(parent_level)
    child = as_planning_level(child_level)
    if parent is None or child is None:
        return None
    if PLANNING_LEVEL_RANK[child] < PLANNING_LEVEL_RANK[parent]:
        return (
            f"父节点是{PLANNING_LEVEL_LABELS[parent.value]}层,不能挂一个更粗的"
            f"{PLANNING_LEVEL_LABELS[child.value]}层子节点。规划层级只能从粗到细。"
        )
    return None


class PlanNodePayload(ApiModel):
    """一个计划节点。**不含任何排期字段** —— "哪天做"在 sessions 里。"""

    id: uuid.UUID
    parent_id: uuid.UUID | None = None
    title: str
    description: str | None = None
    acceptance_criteria: str | None = None
    node_type: str
    #: 用途轴(`planning` / `information`)。与 `node_type` **正交** ——
    #: "这是什么事"和"这件事要不要占日历"是两个问题。
    #: 见 `db/models/enums.py` 的 `NodePurpose`。
    purpose: str = "planning"
    #: 规划层级(`strategy` / `phase` / `month` / `week` / `day`)。
    #: **`None` = 没指定** —— 存量节点与不需要层级的节点都是它,前端不得显示
    #: 任何误导性的层级标签。它只表达语义层级,不代表已排期。
    planning_level: str | None = None
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
    #: 正文冲突检测,会让“另一个标签页勾掉了一个任务”变成“我的正文保存失败”。
    content_version: int = 1
    #: 规划智能体重构 V1(P2):固定分析容器键(current_state / true_intent / …)。
    #: None = 非 V1 节点。前端据此渲染事实/假设标记与讨论数。
    v1_key: str | None = None
    #: 模型对该容器的可审阅判断:`{judgment, known_facts[], assumptions[], evidence[],
    #: importance_reason, uncertainty, status, discussion_count}`。None = 还没有判断。
    v1_analysis: dict | None = None


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
    #:
    #: **两者都只数 `purpose == planning` 的节点**(§2.5)。它们回答的是"我还有多少事
    #: 要做",而一个信息主题(「我排名 38」)没有"做不做完"这回事 —— 把它算进去,
    #: 进度条会永远差那么几格,而用户找不到那几格是什么。于是它们**不等于**
    #: `len(nodes)`,这两个数各自要按用途过滤。
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
    #: 默认 `planning`:不传就是"这是个要排期的东西",与加这一列之前的语义一致。
    #: 双击空白处建出来的"主题/方向"传 `information`(见 `PathView.tsx`)。
    purpose: str = "planning"
    description: str | None = None
    acceptance_criteria: str | None = None
    priority: str = "medium"
    estimate_minutes: int | None = None
    deadline: date | None = None
    #: 规划层级。**可空** —— 不传就是 unspecified。information 节点不允许带它。
    planning_level: str | None = None


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
    #: 可以把一个节点改成信息主题,也可以改回来。改过去不需要先清掉工时/截止 ——
    #: 清理由调用方决定(见 `information_node_conflicts`),不是隐式副作用。
    purpose: str | None = None
    status: str | None = None
    priority: str | None = None
    estimate_minutes: int | None = None
    deadline: date | None = None
    #: 规划层级。传 null 是"清空层级"(回到 unspecified),不传是"不改"。
    planning_level: str | None = None
    #: **正文的乐观锁,不是要写的字段 —— 它是一个前置条件。**
    #:
    #: 客户端把读到的那一版 `PlanNodePayload.content_version` 原样带回来;对不上
    #: 就是 409 `CONCURRENCY_CONFLICT`,这一次编辑整个不生效。不带(或带 null)表示
    #: "不检查" —— 内部调用方(提案确认、排期)走的就是这条路,它们本来就持有锁。
    #:
    #: 它**不在** `EDITABLE_FIELDS` 里:那是"能被写进列的字段"的白名单,而这个是
    #: "你手上那一份是不是还够新"的断言。混进去的后果是它会被当成一次赋值,于是
    #: 谁都能把版本号改成任意数字 —— 锁就没了。
    content_version: int | None = None


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
    overbooked_days: list[OverbookedDayPayload] = Field(default_factory=list)
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


class NotePayload(ApiModel):
    """一个节点的**长正文**当前是什么。§2.2。

    ## 为什么它不在 `PlanPayload` 里

    节点行上的 `description` 是**简述**(300 码点,进版本账本),这里的长正文最多
    20,000 码点,而且不进 `plan_revisions.snapshot`。把它塞进计划载荷的后果是每次
    读计划、每次改计划、每一行版本记录都背着全部节点的全文 —— 而四个视图里没有
    一个需要正文。

    于是客户端是**按需**取它:点开某个节点的笔记编辑器时才 GET 这一条。
    """

    node_id: uuid.UUID
    body: str = ""
    #: 笔记自己的乐观锁。**与 `PlanNodePayload.content_version` 是两个号** ——
    #: 共用会变成"有人改了 300 字的简述 → 你 20,000 字的笔记保存失败",而冲突检测
    #: 的范围必须和冲突的范围一样大(`db/models/plan.py` 里那条注释)。
    #:
    #: **从来没有写过笔记的节点是 `0`**,而不是 404。
    content_version: int = 0
    #: 最后一次写入的时刻。没写过就是 `null`。
    updated_at: datetime | None = None


class PutNoteRequest(ApiModel):
    """写一个节点的长正文。

    `expected_content_version` 与 `UpdateNodeRequest.content_version` 是同一个
    设计:**前置条件,不是要写的字段**。客户端把 GET 到的那一版原样带回来,对不上
    就是 409 `CONCURRENCY_CONFLICT`,这一次写入整个不生效;不带(或带 `null`)表示
    "不检查",给内部调用方(提案确认)用 —— 它们本来就持有工作空间锁。
    """

    body: str = ""
    expected_content_version: int | None = None


class NoteEditResponse(ApiModel):
    """一次笔记写入的结果。

    **这里刻意没有 `revision_version`**(而节点编辑的响应里一定有)。笔记不是计划的
    一部分:它的写入不新增 `plan_revisions` 行、不推进 `revision_version`。带上一个
    不会因为这次写入而改变的版本号,客户端很容易读成"计划刚变了" —— 而它接下来
    就会拿这个号去比对提案的 `baseRevisionVersion`。
    """

    note: NotePayload


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
    "NoteEditResponse",
    "NotePayload",
    "OverbookedDayPayload",
    "PlanNodePayload",
    "PlanPayload",
    "PutLayoutRequest",
    "PutNoteRequest",
    "RelationPayload",
    "RestoreResponse",
    "ScopeViewportPayload",
    "SessionPayload",
    "UpdateNodeRequest",
    "UpdateRelationRequest",
]
