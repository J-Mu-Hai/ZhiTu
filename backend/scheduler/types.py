"""排期算法的输入与输出形状。**纯数据,只 import 标准库。**

## 为什么这里自己声明了一套枚举

`backend/db/models/enums.py` 是领域枚举的唯一定义处,排期也确实要用到其中几个取值
(`pending` / `doing` / `done`…)。但**不能从那里 import**:Python 导入一个子模块会先
执行它的全部父包,而 `backend/db/__init__.py` 导入了 `session`,那里会建 sqlalchemy
引擎。于是"排期不碰数据库"会变成"排期不碰数据库,但它 import 的每一层都在碰"。

所以这里重新声明取值相同的 `StrEnum`,由测试保证两边不漂移
(`tests/unit/scheduler/test_purity.py` 逐成员比对)。用测试而不是注释来保证一致 ——
注释不会被执行。

## 为什么 `ScheduleRequest` 装的是**一个用户的全部空间**

规则"每天投入不超过个人预算"是按人算的,不是按空间算的。如果排期逐空间运行,两个
空间就会各自以为自己能占满同一周的晚上,而用户只有一个晚上。所以请求里带的是这个用户
所有的节点、依赖和既有场次,池子只有一个,`daily_load` 里按空间分开记账**只是为了
能回答"这周满了,是什么占满的"**。

## 为什么全是 frozen dataclass

排期的输出要和"用户预览过的那一版"逐字节一致(`schedule_version` 校验)。可变对象
会让"算完之后某个步骤顺手改了输入"变成一条无人察觉的路径,而它的表现是版本号对不上
却查不出原因。frozen 让这件事在赋值那一刻就炸。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum


# ---------------------------------------------------------------------------------
# 取值枚举(与 backend.db.models.enums 一致,由测试比对)
# ---------------------------------------------------------------------------------
class NodeStatus(StrEnum):
    PENDING = "pending"
    DOING = "doing"
    COMPLETED = "completed"
    ARCHIVED = "archived"


class NodeType(StrEnum):
    GOAL = "goal"
    CAPABILITY = "capability"
    STAGE = "stage"
    TASK = "task"
    MILESTONE = "milestone"


class Priority(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class SessionStatus(StrEnum):
    PLANNED = "planned"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    SKIPPED = "skipped"
    MOVED = "moved"
    CANCELED = "canceled"


class SessionOrigin(StrEnum):
    SCHEDULER = "scheduler"
    USER = "user"
    AI = "ai"


class ExecutionResult(StrEnum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    SKIPPED = "skipped"
    FAILED = "failed"


#: 已经发生过、不可能再被重排改动的场次状态。这三者的行是**历史**,不是候选。
FROZEN_STATUSES: frozenset[SessionStatus] = frozenset(
    {SessionStatus.DONE, SessionStatus.SKIPPED, SessionStatus.CANCELED}
)

#: 优先级 → 排序权重。数值大的先排。写成常量表而不是 `if priority == "high"`,
#: 是因为排序键里要用它做减法,而三向比较很容易在新增一档时写漏一个分支。
PRIORITY_RANK: dict[Priority, int] = {
    Priority.HIGH: 0,
    Priority.MEDIUM: 1,
    Priority.LOW: 2,
}


def priority_rank(priority: Priority | str) -> int:
    """未知取值排在最前面(最保守:宁可先排它,也不要静默把它排到最后)。"""
    try:
        return PRIORITY_RANK[Priority(priority)]
    except ValueError:
        return 0


# ---------------------------------------------------------------------------------
# 输入
# ---------------------------------------------------------------------------------
@dataclass(frozen=True)
class ScheduleNode:
    """参与排期的一个节点。**只带排期需要的字段。**"""

    id: uuid.UUID
    workspace_id: uuid.UUID
    title: str
    node_type: NodeType
    status: NodeStatus
    priority: Priority
    #: 预计工时(分钟)。为空表示"用户没说过要做多久" —— 见 `errors.NoEstimate`。
    estimate_minutes: int | None
    deadline: date | None
    order_index: int = 0
    depth: int = 0
    #: 它下面还有没有节点。**只有一件事靠它判断**:一个没有工时的节点该不该报成缺口。
    #: 容器节点(目标、阶段)的活是它下面的节点干的,自己没填工时是正常的;叶子节点
    #: 没填工时才是真的排不出来。本模块拿不到父子关系(`ScheduleNode` 刻意不带
    #: `parent_id` —— 排期不需要知道谁挂在谁下面),所以这个事实由调用方算好带进来。
    has_children: bool = False

    @property
    def is_open(self) -> bool:
        """还需要排期吗。已完成/已归档的节点不排。"""
        return self.status in (NodeStatus.PENDING, NodeStatus.DOING)


@dataclass(frozen=True)
class ScheduleDependency:
    predecessor_id: uuid.UUID
    successor_id: uuid.UUID
    lag_days: int = 0


@dataclass(frozen=True)
class CapacityProfile:
    """个人时间预算。**跨所有空间共享。**

    `safety_factor` 在 `calendar.weekly_budget` 里**只乘一次**:可行性闸门与每日池
    用的都是那一个乘积。文档里 `WeeklyAvailableMinutes × RemainingWeeks × SafetyFactor`
    是可行性闸门那一侧,如果每日池再乘一次,系统会报出根本不存在的缺口 —— 而用户看到
    "这周排不下"时唯一的反应是增加投入,那正好是最不该给的建议。
    """

    weekly_total_minutes: int = 600
    safety_factor: Decimal = Decimal("0.80")
    daily_max_minutes: int | None = None
    default_buffer_minutes: int = 10
    min_session_minutes: int = 15
    max_session_minutes: int = 120
    #: 0 = 周一,与 `date.weekday()` 一致。
    week_start_weekday: int = 0


@dataclass(frozen=True)
class AvailabilityWindow:
    """周期性可用时段,按周几描述。用户没给过具体时段时,请求里这个元组是空的。"""

    weekday: int
    start_minute: int
    end_minute: int
    #: 生效区间(含两端)。为空表示不限。排期是跨若干周的,而"这学期周三晚上有空"
    #: 这种说法本身就有起止 —— 把它当成永久的会让寒假之后仍然按周三排。
    effective_from: date | None = None
    effective_to: date | None = None

    @property
    def minutes(self) -> int:
        return max(0, self.end_minute - self.start_minute)

    def covers(self, day: date) -> bool:
        if self.effective_from is not None and day < self.effective_from:
            return False
        return self.effective_to is None or day <= self.effective_to


@dataclass(frozen=True)
class DayException:
    """对某个具体日期的覆盖。`is_unavailable` 为真时忽略 `available_minutes`。"""

    on_date: date
    available_minutes: int | None = None
    is_unavailable: bool = False


@dataclass(frozen=True)
class ExistingSession:
    """已经在库里的一个场次。**输入,不是候选。**

    `status` 落在 `FROZEN_STATUSES` 里、或 `locked`、或日期已经过去 —— 三者任一为真,
    这场就是既成事实:它照样占用当天池子,但绝不被移到别处。
    """

    id: uuid.UUID
    workspace_id: uuid.UUID
    node_id: uuid.UUID
    scheduled_date: date
    planned_minutes: int
    buffer_minutes: int = 0
    start_minute: int | None = None
    end_minute: int | None = None
    seq: int = 0
    status: SessionStatus = SessionStatus.PLANNED
    locked: bool = False
    lock_reason: str | None = None
    origin: SessionOrigin = SessionOrigin.SCHEDULER

    @property
    def occupies_minutes(self) -> int:
        """这场在当天池子里占多少。**缓冲也计入** —— 不把它算进去的话,
        "当天总占用 ≤ 上限"这条不变量在数据库里就复核不出来。"""
        return max(0, self.planned_minutes) + max(0, self.buffer_minutes)


@dataclass(frozen=True)
class ExecutionFact:
    """一条执行反馈的只读投影。

    排期**只读**它:`execution_records` 是 append-only 的事实记录,排期不会因为
    "重排"而改掉任何一条。它唯一的用途是把"这个任务实际完成了多少"算进已完成工时。
    """

    node_id: uuid.UUID
    session_id: uuid.UUID | None
    result: ExecutionResult
    actual_minutes: int | None = None
    completion_ratio: Decimal | None = None


# ---------------------------------------------------------------------------------
# 请求
# ---------------------------------------------------------------------------------
@dataclass(frozen=True)
class ScheduleRequest:
    """一次排期的**全部**输入。排期不读别的东西。

    `today` 是参数而不是从时钟取的 —— 理由见包文档。

    `horizon_days` 是"我算到多久以后"。它不是用户的截止日,而是**算法愿意往后看多远**:
    一个没有截止日的任务必须有一个尽头,否则它会被排到三年后,而用户看到的是一份
    2030 年才开始做第一件事的计划。
    """

    today: date
    horizon_days: int
    profile: CapacityProfile
    nodes: tuple[ScheduleNode, ...] = ()
    dependencies: tuple[ScheduleDependency, ...] = ()
    existing: tuple[ExistingSession, ...] = ()
    executions: tuple[ExecutionFact, ...] = ()
    windows: tuple[AvailabilityWindow, ...] = ()
    exceptions: tuple[DayException, ...] = ()
    #: 这次排期基于的计划版本。它不参与计算,只进 `schedule_version` 的哈希 ——
    #: 计划一变,用户在旧版本上预览的那份方案就该失效。
    plan_revision: int = 0


# ---------------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------------
@dataclass(frozen=True)
class PlannedSession:
    """算法希望存在的一场。`session_id` 为空表示这是新的一场。"""

    node_id: uuid.UUID
    workspace_id: uuid.UUID
    scheduled_date: date
    planned_minutes: int
    buffer_minutes: int
    seq: int
    start_minute: int | None = None
    end_minute: int | None = None
    session_id: uuid.UUID | None = None
    origin: SessionOrigin = SessionOrigin.SCHEDULER
    locked: bool = False


@dataclass(frozen=True)
class SessionMove:
    """把某一场从 A 天挪到 B 天。**每一个都是一次"用户会发现的变化"**,所以要记账。"""

    session_id: uuid.UUID
    node_id: uuid.UUID
    from_date: date
    to_date: date
    seq: int


@dataclass(frozen=True)
class CapacityGap:
    """排不进去的那部分。**不静默截断,以结构化的事实呈现。**

    `resolves_gap` 不在这里 —— 选项由 `schedule.recovery_options` 实测得出,
    因为"这个选项能不能补上缺口"必须真的重跑一遍才算数,而不是断言它成立。
    """

    #: 哪个空间的缺口。为空表示这个缺口是跨空间的(池子被别的空间占满了)。
    workspace_id: uuid.UUID | None
    node_id: uuid.UUID | None
    node_title: str
    unscheduled_minutes: int
    reason_code: str
    binding_constraint: str
    #: 那一天/那一周是哪个约束卡的,给界面做可视化用。
    detail: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class RecoveryOption:
    """容量不足时给用户的一条出路。

    `resolves_gap` 是**跑出来的**:把这个选项的参数代回 `simulate()` 重算一遍,
    缺口真的消失才算数。断言它成立的话,一个算错的建议会带着"这能解决"的标签
    送到用户面前。
    """

    kind: str
    label: str
    description: str
    resolves_gap: bool
    #: 重算之后还剩多少分钟排不进去(0 表示真的解决了)。
    remaining_unscheduled_minutes: int
    params: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ChurnSummary:
    """这次重排到底动了多少东西。**要说给用户听。**

    "本次调整:移动 2 场,新增 3 场" 和 "已重新规划" 是两种完全不同的告知。后者
    会让用户第二天打开时发现整个日历都变了却不知道为什么 —— 那正是"每天改计划、
    系统失去稳定性"的来源。
    """

    moved: int = 0
    created: int = 0
    canceled: int = 0
    kept: int = 0

    @property
    def total_changes(self) -> int:
        return self.moved + self.created + self.canceled

    def describe(self) -> str:
        if self.total_changes == 0:
            return "本次没有改动任何安排。"
        parts: list[str] = []
        if self.moved:
            parts.append(f"移动 {self.moved} 场")
        if self.created:
            parts.append(f"新增 {self.created} 场")
        if self.canceled:
            parts.append(f"取消 {self.canceled} 场")
        return "本次调整：" + "，".join(parts) + "。"


@dataclass(frozen=True)
class ScheduleResult:
    """一次排期的完整结果。**纯数据。**"""

    #: 本次排出来的场次:**既有场次里可以动的那些**(原样保留的或挪了地方的)+ 新增的。
    #:
    #: **冻结的场次不在这里** —— 已完成 / 已跳过 / 已取消 / 已锁定 / 日期已过去的那些
    #: 只作为占用量进入计算,不出现在输出里。于是"`sessions` 里没有"的意思是
    #: **别动它**,而不是"删掉它"。写入方必须按这个契约走:`sessions` 里的逐条
    #: 更新或插入,`cancelations` 里的改成取消,**没被提到的行一律不碰**。
    #:
    #: 这条契约是为了保住数据:如果写入方把 `sessions` 当成"完整的期望集合",它就会
    #: 顺手删掉用户上周勾过的那些完成记录 —— 而那正是本模块最不该造成的事。反过来说,
    #: 冻结场次全部列进来的话,"哪些是我的历史"就取决于写入方有没有正确识别状态,
    #: 而状态是它自己刚读进来的,读错了就要毁数据;不列进来则没有这个失败模式。
    sessions: tuple[PlannedSession, ...] = ()
    #: 应该被取消的既有场次 id(不是删除 —— 行还在,状态改成 canceled)。
    #: 只有"还没发生的、可动的"场次会出现在这里(见 `sessions` 的契约)。
    cancelations: tuple[uuid.UUID, ...] = ()
    #: 被移动的场次。记账用,界面靠它说"移动了 2 场"。
    moves: tuple[SessionMove, ...] = ()
    gaps: tuple[CapacityGap, ...] = ()
    #: 审计轨迹:`日期 -> 空间 -> 分钟`。跨空间共享池是分不开账的,所以这里分开记,
    #: 让"这周满了"能被回答成"是什么占满的"。
    daily_load: tuple[tuple[date, tuple[tuple[uuid.UUID, int], ...]], ...] = ()
    churn: ChurnSummary = ChurnSummary()
    #: 入数据规范化后的哈希。`POST /schedule/apply` 拿它校验"用户预览过的正是被写入的"。
    schedule_version: str = ""
    #: 恒为 False。存在的意义是**让"永不静默截断"是一条可断言的性质**,而不是一句承诺。
    truncated: bool = False

    def load_on(self, day: date) -> dict[uuid.UUID, int]:
        """某一天各空间占用的分钟数。测试与界面都用它,免得各自解一遍元组。"""
        for when, entries in self.daily_load:
            if when == day:
                return dict(entries)
        return {}

    @property
    def unscheduled_minutes(self) -> int:
        return sum(gap.unscheduled_minutes for gap in self.gaps)
