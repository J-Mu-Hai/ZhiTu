"""把一次对话轮次需要的全部上下文组装出来。

## 这个文件修的是产品最核心的一个缺陷

原来的前端在 `send()` 里只发了四个字段(空间 id、当前节点 id、视图名、消息正文),
**既不带历史也不带计划**。所以后端那个 agent 看不到用户真实的空间里有什么,
只能看到它自己上一轮提议过的东西。用户说"把刚才那个阶段往后挪一周",
模型没有"刚才那个阶段"可指 —— 它只能猜。

现在这里把这几样东西一起送进去:

1. **今天是几号**(按用户时区算,不是 UTC)
2. **这个空间里真实存在的计划节点**,以及它们**之间的关系**
3. **这个空间已经确认的条件,以及还没问到的**
4. **作用范围**:用户在哪个子空间里、点着哪个节点、哪些是范围外只读的
5. **这次分析的输入快照**(见 `services/input_snapshot.py`),供"过期了吗"事后比较

## 第二版补的是"看不见正文"

第一版把节点的标题、类型、状态送进去了,但**没有正文**。于是用户在自己写的正文里
写下"只能周末做""没有设备""已经会 C 语言",模型一条都看不到 —— 它只会照着标题
猜,然后把用户刚刚写过的事情再问一遍。第二版按"离这一轮有多近"分层读:

    焦点            正文全文(用户正写着的那一页)
    祖先链          最近的几层 + 最上面那条(根目标是最硬的约束)给正文
    焦点的直属子节点 前若干个给正文
    范围内其它节点   只给标题
    范围外           只给标题,且**只读**

"读了多少"不是内部细节:`body_read` 一路带到渲染层,和"这个节点没有正文"分开呈现。

## "今天是几号"为什么必须在这里算

`UtcDateTime` 把所有时间戳统一成 UTC 存,那是为了存储正确。但"今天"不是时间戳,
是一个**时区相关的事实**:UTC 的 2026-09-25T22:00 在东八区已经是 09-26。
如果用 UTC 日期去算"还剩几周",东八区用户在晚上 8 点以后看到的每一份周计划都会
错一天 —— 而错一天在"这周还剩几天可以安排"这个问题上就是实打实的错误。
所以日期字符串按用户时区现算,`db/base.py` 里那条"哪一天绝不由时间戳推导"就是这条。
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, timedelta

from sqlalchemy import func, select

from backend.agent.prompts.planning import (
    ANCESTOR_BODIES_NEAREST,
    MAX_AVAILABILITY_ROWS,
    MAX_CHILD_BODIES,
    MAX_EXCEPTION_ROWS,
    MAX_EXECUTION_ROWS,
    MAX_SESSION_ROWS,
)
from backend.agent.runtime.base import (
    HISTORY_TURNS,
    LAYER_ANCESTOR,
    LAYER_CHILD,
    LAYER_FOCUS,
    LAYER_OUTSIDE,
    LAYER_SCOPE,
    AvailableWindowView,
    ExecutionFactView,
    PlanNodeView,
    RelationView,
    SessionFactView,
    TimeView,
    TurnContext,
)
from backend.db.models import (
    AvailabilityException,
    AvailabilityRule,
    Dependency,
    ExecutionRecord,
    Message,
    NodeNote,
    NodeRelation,
    PlanNode,
    ScheduledSession,
    UserCapacityProfile,
)
from backend.db.models.enums import NodeType
from backend.scheduler.calendar import build_day_pools, daily_cap, weekly_budget
from backend.scheduler.capacity import assess_feasibility
from backend.scheduler.schedule import OCCUPYING_STATUSES
from backend.scheduler.types import (
    AvailabilityWindow,
    DayException,
)
from backend.scheduler.types import (
    NodeStatus as SchedulerNodeStatus,
)
from backend.services import input_snapshot, schedule_service
from backend.services.brief_service import load_brief, to_known_conditions
from backend.services.context import WorkspaceContext
from backend.services.errors import InvalidInput
from backend.services.timeutil import today_in

logger = logging.getLogger(__name__)

#: 送进模型的节点数上限。超过之后按"离根近的优先"截断 ——
#: 阶段和目标比第 40 个任务更能说明这个空间在干什么。
MAX_NODES = 80


async def load_note_budget(
    db, workspace_id: uuid.UUID, focus_node_id: uuid.UUID | None
) -> tuple[dict[uuid.UUID, int], uuid.UUID | None, str | None]:
    """长笔记的预算:**每个节点有多少字** + 焦点节点那份正文。§2.2。

    一次聚合查询加一次单行查询 —— **不是每节点一次**。N+1 在本地那几十个节点上
    完全看不出来,而它会随着空间长大按节点数放大,且症状是"聊久了越来越慢",
    没有一个地方会报错。

    长度用 SQL 的 `length()` 数,不是把正文取回来再 `len()`:这个数只用来告诉模型
    "那里有一片多大",而把最多 80 份、每份两万字的正文取回内存只为数个数,是为一个
    展示用整数付一次全量的代价。两者在这里是**同一个定义**:SQLite 与 PostgreSQL 的
    `length()` 对字符串数的都是**码点**(不是字节,也不是 UTF-16 码元),与 Python 的
    `len()` 一致 —— 这正是契约里 `MAX_DESCRIPTION_CODEPOINTS` 那段说的那条计数规则。
    **权威仍然是 Python 那一侧**(`note_service._checked` 的拒写),这里只是同一个数的
    另一种读法。

    正文只给**焦点节点**那一份:其余节点看到的是那几个事实(有笔记、多大、本次没读)。
    返回 `(每个节点的字数, 焦点节点 id 或 None, 焦点那份正文或 None)`。
    """
    # 只收有正文的行 —— "长度为 0"与"没有这一行"是同一件事(见 `note_service.save`
    # 里"不给用户两件看不出区别的事"那段),所以 0 字不进这张表,`notes_present`
    # 随之是假。让两种"没有"在这里合成一种,是为了让下游的判断只有一条。
    rows = await db.execute(
        select(NodeNote.node_id, func.length(NodeNote.body)).where(
            NodeNote.workspace_id == workspace_id
        )
    )
    chars = {node_id: length for node_id, length in rows.all() if length}

    if focus_node_id is None or focus_node_id not in chars:
        return chars, None, None
    body = await db.scalar(
        select(NodeNote.body).where(
            NodeNote.workspace_id == workspace_id, NodeNote.node_id == focus_node_id
        )
    )
    return chars, focus_node_id, body


def _weekday_cn(value) -> str:
    return "一二三四五六日"[value.weekday()]


async def load_nodes(db, workspace_id: uuid.UUID) -> list[PlanNode]:
    """取这个空间里没有被软删除的节点,按层级和排序号排列。

    **记号(`n1..nK`)的编号顺序就是这里的排序**,所以这个顺序不能改:提案确认那一步
    会拿同样的排序重新编号,两处不一致的话,模型说的 `n3` 在确认时会指向另一个节点。
    """
    result = await db.execute(
        select(PlanNode)
        .where(PlanNode.workspace_id == workspace_id, PlanNode.deleted_at.is_(None))
        .order_by(PlanNode.depth.asc(), PlanNode.order_index.asc(), PlanNode.created_at.asc())
        .limit(MAX_NODES)
    )
    return list(result.scalars())


async def count_live_nodes(db, workspace_id: uuid.UUID) -> int:
    """这个空间里存活节点的**总数**。只是为了让"我这次没读全"能被说出来。"""
    total = await db.scalar(
        select(func.count())
        .select_from(PlanNode)
        .where(PlanNode.workspace_id == workspace_id, PlanNode.deleted_at.is_(None))
    )
    return int(total or 0)


async def load_history(
    db, conversation_id: uuid.UUID, *, exclude_message_id: uuid.UUID | None = None
) -> list[tuple[str, str]]:
    """最近若干轮对话,按时间正序。

    `exclude_message_id` 用来排除掉"刚刚这一轮的用户消息"—— 它会作为
    `user_message` 单独出现,同时又在历史里出现一次的话,模型会以为用户说了两遍。
    """
    stmt = select(Message).where(Message.conversation_id == conversation_id)
    if exclude_message_id is not None:
        stmt = stmt.where(Message.id != exclude_message_id)
    result = await db.execute(stmt.order_by(Message.seq.desc()).limit(HISTORY_TURNS))
    rows = list(result.scalars())
    rows.reverse()
    # 系统消息不进对话历史:它们是内部标记,模型看到会当成用户说的话。
    return [(m.role.value, m.content) for m in rows if m.role.value in ("user", "assistant")]


# ---------------------------------------------------------------------------------
# 时间底盘
# ---------------------------------------------------------------------------------
def _occupying_values() -> frozenset[str]:
    """仍然占着时间的那几种场次状态。

    **从排期器那里取,不在这里重写一遍。** 那一组常量(`OCCUPYING_STATUSES`)决定了
    哪些场次占着当天的时间池;这里自己列一份的话,两边对"这一周还剩多少"给出不同答案,
    而用户看到的是"AI 说排得下、排期预览说排不下"。比的是 `value` 字符串,因为两边
    声明的枚举是各自独立的(理由见 `scheduler/types.py` 顶部)。
    """
    return frozenset(item.value for item in OCCUPYING_STATUSES)


async def load_time_view(
    db,
    ctx: WorkspaceContext,
    *,
    today: date,
    handles: dict[uuid.UUID, str],
) -> TimeView:
    """读这个人的时间底盘。**只读,一行都不写。**

    ## 为什么"读"也要成段地写在这里,而不是散在渲染层

    时间那一段里的每一个数字都要和排期预览对得上:每周多少分钟来自 `capacity_profile`
    的三档回落,`safety_factor` 只在 `weekly_budget` 里乘一次,视界来自 `horizon_days`。
    这几处各写一份的后果不是报错,是**两个都自称按同一套预算算的数字对不上** ——
    而用户会相信 AI 那一份,因为它说得更像人话。所以这里只做取数 + 调用那几处,
    一个算术都不自己写。

    ## 挂在哪几个节点上的场次才看得见

    场次与执行记录都按**本轮拿得到的记号**过滤:节点的记号是模型唯一能指涉的东西,
    没有记号的节点(超出 `MAX_NODES`、或已归档)列出来它也没法引用。没列出来的不静默
    丢掉 —— 总数照给,渲染层会说"共 N 场,上面列了 M 场"。
    """
    user_id = ctx.user.user_id
    workspace_ids = await schedule_service.active_workspace_ids(db, user_id)
    profile = await schedule_service.capacity_profile(db, user_id, workspace_ids)
    profile_row = await db.scalar(
        select(UserCapacityProfile).where(UserCapacityProfile.user_id == user_id)
    )

    window_rows = list(
        await db.scalars(
            select(AvailabilityRule)
            .where(AvailabilityRule.user_id == user_id)
            .order_by(AvailabilityRule.weekday.asc(), AvailabilityRule.start_minute.asc())
        )
    )
    windows = tuple(
        AvailabilityWindow(
            weekday=row.weekday,
            start_minute=row.start_minute,
            end_minute=row.end_minute,
            effective_from=row.effective_from,
            effective_to=row.effective_to,
        )
        for row in window_rows
    )

    # 视界:**和排期预览同一套规则**,连"最远的截止日"都是同一次 `max()` 的意思。
    # 查的是这个人全部活动空间里最远的那个截止日 —— 时间池是按人算的,只看当前空间
    # 会让 AI 报出一个比排期预览更小的容量。
    furthest = await db.scalar(
        select(func.max(PlanNode.deadline)).where(
            PlanNode.workspace_id.in_(workspace_ids),
            PlanNode.deleted_at.is_(None),
        )
    )
    horizon = schedule_service.horizon_days(today, furthest)
    horizon_last = today + timedelta(days=horizon - 1)

    exception_rows = list(
        await db.scalars(
            select(AvailabilityException)
            .where(
                AvailabilityException.user_id == user_id,
                # 视界之外的例外影响不了这次判断,却会把那一段撑长。
                AvailabilityException.on_date >= today,
                AvailabilityException.on_date <= horizon_last,
            )
            .order_by(AvailabilityException.on_date.asc())
        )
    )
    exceptions = tuple(
        DayException(
            on_date=row.on_date,
            available_minutes=row.available_minutes,
            is_unavailable=row.is_unavailable,
        )
        for row in exception_rows
    )

    pools = build_day_pools(
        start=today,
        horizon_days=horizon,
        profile=profile,
        windows=windows,
        exceptions=exceptions,
    )
    # `required_minutes=0` 是有意的:这里要的只是**容量那一侧**,一次减法都不做。
    # 需求那一侧(待做任务的预计工时合计)在下面由 `_plan_workload` 单独给出 —— 两个
    # 数字都摆出来,但**相减、下结论仍然不在这里做**:那要看安全系数、缓冲和前置关系
    # 能不能排得开,是「排期」预览的活。
    capacity_minutes = assess_feasibility(
        required_minutes=0, pools=pools, last_day=furthest
    ).capacity_minutes

    sessions, sessions_total = await _load_sessions(db, ctx, handles)
    executions, executions_total = await _load_executions(db, ctx, handles)
    other_workspaces = await _count_other_sessions(db, user_id, ctx.id, workspace_ids)
    task_minutes, tasks_without_estimate = await _plan_workload(db, ctx)

    return TimeView(
        horizon_days=horizon,
        horizon_last_day=None if furthest is None else furthest.isoformat(),
        horizon_at_limit=horizon >= schedule_service.MAX_HORIZON_DAYS,
        capacity_minutes=capacity_minutes,
        weekly_total_minutes=profile.weekly_total_minutes,
        weekly_budget_minutes=weekly_budget(profile),
        safety_factor=str(profile.safety_factor),
        daily_cap_minutes=daily_cap(profile),
        min_session_minutes=profile.min_session_minutes,
        max_session_minutes=profile.max_session_minutes,
        default_buffer_minutes=profile.default_buffer_minutes,
        capacity_configured=profile_row is not None,
        windows=tuple(
            AvailableWindowView(
                weekday=row.weekday, start_minute=row.start_minute, end_minute=row.end_minute
            )
            for row in window_rows[:MAX_AVAILABILITY_ROWS]
        ),
        windows_total=len(window_rows),
        exceptions=tuple(
            (item.on_date.isoformat(), item.available_minutes, item.is_unavailable)
            for item in exceptions[:MAX_EXCEPTION_ROWS]
        ),
        exceptions_total=len(exceptions),
        sessions=sessions,
        sessions_total=sessions_total,
        sessions_other_workspaces=other_workspaces,
        executions=executions,
        executions_total=executions_total,
        open_task_minutes=task_minutes,
        open_tasks_without_estimate=tasks_without_estimate,
        # 按 id 逐个排除当前空间,不写成 `len(...) - 1` —— 那个写法默认当前空间一定在
        # 这批活动空间里。它通常确实在,但"通常"不是一个可以拿来算数的事实。
        other_active_workspaces=len([item for item in workspace_ids if item != ctx.id]),
    )


async def _load_sessions(
    db, ctx: WorkspaceContext, handles: dict[uuid.UUID, str]
) -> tuple[tuple[SessionFactView, ...], int]:
    """这个空间排过的场次:能指涉的列出来,一共多少照实说。

    排序按日期 —— 用户问"这周还排得下吗"时,三个月后的那几场帮不上忙,而截断砍掉的是
    尾巴。
    """
    rows = list(
        await db.scalars(
            select(ScheduledSession)
            .where(ScheduledSession.workspace_id == ctx.id)
            .order_by(ScheduledSession.scheduled_date.asc(), ScheduledSession.seq.asc())
        )
    )
    listed = tuple(
        SessionFactView(
            handle=handles[row.node_id],
            day=row.scheduled_date.isoformat(),
            minutes=row.planned_minutes,
            status=row.status.value,
            locked=row.locked,
        )
        for row in rows
        if row.node_id in handles
    )
    return listed[:MAX_SESSION_ROWS], len(rows)


async def _load_executions(
    db, ctx: WorkspaceContext, handles: dict[uuid.UUID, str]
) -> tuple[tuple[ExecutionFactView, ...], int]:
    """执行记录。**最近的那些优先** —— 它们才是"上次实际花了多久"的答案。"""
    rows = list(
        await db.scalars(
            select(ExecutionRecord)
            .where(ExecutionRecord.workspace_id == ctx.id)
            .order_by(ExecutionRecord.created_at.desc())
        )
    )
    listed = tuple(
        ExecutionFactView(
            handle=handles[row.node_id],
            result=row.result.value,
            actual_minutes=row.actual_minutes,
            completion_ratio=None if row.completion_ratio is None else str(row.completion_ratio),
        )
        for row in rows
        if row.node_id in handles
    )
    return listed[:MAX_EXECUTION_ROWS], len(rows)


async def _plan_workload(db, ctx: WorkspaceContext) -> tuple[int, int]:
    """这个空间还没做完的任务:预计工时合计,以及其中几个没填预计工时。

    ## 为什么这个加法由服务端做

    让模型自己去加几十个节点的 `estimateMinutes`,它会算错 —— 而**算错的方向看不出来**,
    它给的理由听起来和算对的时候一模一样。两个数字都由服务端给出来,"够不够"这一步
    就建立在两个可核对的事实上,而不是一次无声的算术。

    ## 为什么它仍然不是结论

    它只是**这个空间**待做任务的预计工时。别的空间的待做任务不在这里面(所以另外印一行
    说明这个人还有几个活动空间),已完成的不算,而"安全系数、缓冲、前置关系能不能排得开"
    一个都没进来。这些留给「排期」预览 —— 那里才是唯一的结论。

    ## "还没做完"的口径

    用排期器自己声明的那两个状态(`scheduler.types.NodeStatus` 的待做/进行中,见
    `ScheduleNode.is_open`),不在这里另立一套。自己写一份的话,AI 说的"还要做多少"
    和排期器算的会慢慢分家。
    """
    open_statuses = tuple(
        item.value for item in (SchedulerNodeStatus.PENDING, SchedulerNodeStatus.DOING)
    )
    where = (
        PlanNode.workspace_id == ctx.id,
        PlanNode.deleted_at.is_(None),
        PlanNode.node_type == NodeType.TASK,
        PlanNode.status.in_(open_statuses),
    )
    minutes = await db.scalar(
        select(func.coalesce(func.sum(PlanNode.estimate_minutes), 0)).where(*where)
    )
    missing = await db.scalar(
        select(func.count())
        .select_from(PlanNode)
        .where(*where, PlanNode.estimate_minutes.is_(None))
    )
    return int(minutes or 0), int(missing or 0)


async def _count_other_sessions(
    db,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
    workspace_ids: tuple[uuid.UUID, ...],
) -> int:
    """**别的空间**还排着多少场仍然占时间的安排。

    这个数是"时间池按人算"那条产品规则唯一的可见形式。没有它,模型会以为每个子空间
    各有一份每周预算 —— 于是对一个被别的空间占满的星期说"这里还很空"。
    """
    others = tuple(item for item in workspace_ids if item != workspace_id)
    if not others:
        return 0
    total = await db.scalar(
        select(func.count())
        .select_from(ScheduledSession)
        .where(
            ScheduledSession.user_id == user_id,
            ScheduledSession.workspace_id.in_(others),
            ScheduledSession.status.in_(sorted(_occupying_values())),
        )
    )
    return int(total or 0)


# ---------------------------------------------------------------------------------
# 范围
# ---------------------------------------------------------------------------------
class _Scope:
    """一次轮次的作用范围:**读什么、能改什么**。

    这不是一个可以省掉的中间层。范围要回答三个不同的问题(见 `TurnContext` 的注释):
    用户在哪个空间里、他在看哪个节点、模型能改哪些节点。三者混成一个的话,
    "用户点了一个节点"就会顺手把可改范围缩到他脚底下,而那是两件事。
    """

    __slots__ = ("ancestors", "children", "focus_id", "in_scope", "layer_of", "scope_id")

    def __init__(self) -> None:
        self.scope_id: uuid.UUID | None = None
        self.focus_id: uuid.UUID | None = None
        #: 范围内的节点 id(含范围起点自己)。
        self.in_scope: set[uuid.UUID] = set()
        #: 焦点的祖先链,从根往下。
        self.ancestors: list[uuid.UUID] = []
        #: 焦点的直属子节点 id。
        self.children: list[uuid.UUID] = []
        #: 节点 id -> 它属于哪一层。
        self.layer_of: dict[uuid.UUID, str] = {}


async def load_scope(
    db,
    ctx: WorkspaceContext,
    nodes: list[PlanNode],
    *,
    scope_root_id: uuid.UUID | None,
    focus_node_id: uuid.UUID | None,
) -> _Scope:
    """解出这次的作用范围。**只读。**

    ## 范围起点给错了怎么办

    给了 `scope_root_id` 但它不是这个空间里存活的节点 -> 直接拒绝(`InvalidInput`)。
    悄悄放宽成"整个空间"是最坏的处理:用户以为 AI 只在自己看的那一支里动,
    实际上它拿到了整棵树 —— 而这种错误不会有任何迹象,直到某天某个范围外的分支
    被改了。宁可让他看到一句"这个范围起点不对"。
    """
    scope = _Scope()
    live = {node.id: node for node in nodes}
    parent_of = {node.id: node.parent_id for node in nodes}

    if scope_root_id is not None:
        if scope_root_id not in live:
            # 少数情况下它确实存在、只是没落进这一批(节点超过 MAX_NODES 时)。
            # 这时也拒绝:基于"我没读到"去做范围判断,才是真正危险的那件事。
            exists = await db.scalar(
                select(PlanNode.id).where(
                    PlanNode.id == scope_root_id,
                    PlanNode.workspace_id == ctx.id,
                    PlanNode.deleted_at.is_(None),
                )
            )
            if exists is not None:
                raise InvalidInput(
                    "这一层节点太多,一次读不完,当前这一轮还进不了这个子空间。"
                    "可以先在它上层继续聊,或把它下面的分支分开建。"
                )
            raise InvalidInput("这个范围起点不在当前空间里。")
        scope.scope_id = scope_root_id
        scope.in_scope = _subtree(scope_root_id, nodes)
    else:
        scope.in_scope = set(live)

    # 焦点必须落在范围内。范围外的东西可以看得见,但不该是本轮讨论的对象:
    # 用户在别的分支上点过一个节点、又导航回了上层时,这个判断避免把"上一处的选择"
    # 当成"这一轮在聊什么"。
    if focus_node_id is not None and focus_node_id in scope.in_scope:
        scope.focus_id = focus_node_id
        chain: list[uuid.UUID] = []
        seen: set[uuid.UUID] = set()
        current = parent_of.get(focus_node_id)
        while current is not None and current not in seen:
            seen.add(current)
            chain.append(current)
            current = parent_of.get(current)
        chain.reverse()
        scope.ancestors = chain
        scope.children = [node.id for node in nodes if node.parent_id == focus_node_id]

    # 分层看的是**它在这一轮里扮演什么角色**,不是它在不在范围内 —— 范围外与否由
    # `in_scope` 单独表达(渲染成「只读」)。所以祖先优先于范围外:一个在范围之上的
    # 根目标,对这一轮的意义是"最硬的那条约束",把它归到"范围外"那一堆里,
    # 模型就看不到它为什么是约束了。
    ancestors = set(scope.ancestors)
    children = set(scope.children)
    for node in nodes:
        if node.id == scope.focus_id:
            scope.layer_of[node.id] = LAYER_FOCUS
        elif node.id in ancestors:
            scope.layer_of[node.id] = LAYER_ANCESTOR
        elif node.id in children:
            scope.layer_of[node.id] = LAYER_CHILD
        elif node.id in scope.in_scope:
            scope.layer_of[node.id] = LAYER_SCOPE
        else:
            scope.layer_of[node.id] = LAYER_OUTSIDE
    return scope


def _subtree(root_id: uuid.UUID, nodes: list[PlanNode]) -> set[uuid.UUID]:
    """一棵子树(含根自己)。显式栈,不递归 —— 深树会撞 Python 的递归深度,
    而那个报错和"收子节点"看起来毫无关系。"""
    children: dict[uuid.UUID, list[uuid.UUID]] = {}
    for node in nodes:
        if node.parent_id is not None:
            children.setdefault(node.parent_id, []).append(node.id)

    collected: set[uuid.UUID] = set()
    pending = [root_id]
    while pending:
        current = pending.pop()
        if current in collected:
            continue
        collected.add(current)
        pending.extend(children.get(current, ()))
    return collected


def _bodies_read(scope: _Scope, order: dict[uuid.UUID, int]) -> set[uuid.UUID]:
    """**哪些节点这次读到了正文。**

    这是一处产品判断,所以写在这里、写成一段能被读懂的规则,而不是散在查询条件里:

    - 焦点:全读。用户在写的那一页,优先。
    - 祖先链:最上面那条(空间的根目标,最硬的约束)+ 最近的 `ANCESTOR_BODIES_NEAREST`
      条。中间那些只给标题 —— 它们离这一轮太远,展开只会把焦点冲淡。
    - 直属子节点:按排序号取前 `MAX_CHILD_BODIES` 个。拆解要看的是这一层的构成,
      而不是把它整段抄一遍。

    没读到的不会被静默略过:渲染层会把这些节点一个个列出来说"本次没有读它的正文"。
    """
    read: set[uuid.UUID] = set()
    if scope.focus_id is not None:
        read.add(scope.focus_id)
    if scope.ancestors:
        read.update(scope.ancestors[:1])  # 最上面那一条:空间的根目标
        read.update(scope.ancestors[-ANCESTOR_BODIES_NEAREST:])
    children = sorted(scope.children, key=lambda node_id: order.get(node_id, 0))
    read.update(children[:MAX_CHILD_BODIES])
    return read


# ---------------------------------------------------------------------------------
# 关系
# ---------------------------------------------------------------------------------
async def load_edges(db, workspace_id: uuid.UUID) -> list[tuple[str, str, uuid.UUID, uuid.UUID, str | None]]:
    """这个空间里全部的关系边:`(kind, type, source, target, note)`。

    `dependencies`(前置)与 `node_relations`(关联/影响)是两张表,但对模型来说都是
    "这两个节点之间有一条线"。**合成一份读出来、但不合成一种语义** —— kind 一路带到
    渲染层,前置与关联在那里分开说。
    """
    edges: list[tuple[str, str, uuid.UUID, uuid.UUID, str | None]] = []

    dependencies = await db.execute(
        select(Dependency).where(Dependency.workspace_id == workspace_id)
    )
    for edge in dependencies.scalars():
        edges.append(
            ("dep", edge.dep_type.value, edge.predecessor_id, edge.successor_id, None)
        )

    relations = await db.execute(
        select(NodeRelation).where(NodeRelation.workspace_id == workspace_id)
    )
    for edge in relations.scalars():
        edges.append(
            ("rel", edge.relation_type.value, edge.source_node_id, edge.target_node_id, edge.note)
        )
    return edges


def _relation_views(
    edges: list[tuple[str, str, uuid.UUID, uuid.UUID, str | None]],
    handles: dict[uuid.UUID, str],
    visible: set[uuid.UUID],
) -> tuple[tuple[RelationView, ...], int]:
    """把边翻译成记号形式。**两端都要有记号才列得出来。**

    列不出来的那些如实计数返回:关系的缺席和数据的缺席在模型眼里长得一样,
    而"这两个节点没关系"和"我没看见它们的关系"会导致完全不同的计划。
    """
    views: list[RelationView] = []
    hidden = 0
    for kind, relation_type, source, target, note in edges:
        source_handle = handles.get(source)
        target_handle = handles.get(target)
        if (
            source_handle is None
            or target_handle is None
            or source not in visible
            or target not in visible
        ):
            hidden += 1
            continue
        views.append(
            RelationView(
                source=source_handle,
                target=target_handle,
                kind=kind,
                relation_type=relation_type,
                note=note,
            )
        )
    views.sort(key=lambda view: (view.kind, view.source, view.target))
    return tuple(views), hidden


# ---------------------------------------------------------------------------------
# 组装
# ---------------------------------------------------------------------------------
async def build_turn_context(
    db,
    ctx: WorkspaceContext,
    *,
    conversation_id: uuid.UUID,
    user_message: str,
    exclude_message_id: uuid.UUID | None = None,
    context_node_id: uuid.UUID | None = None,
    current_view: str | None = None,
    scope_root_id: uuid.UUID | None = None,
) -> TurnContext:
    """组装一次轮次的上下文。**只读,不写任何东西。**"""
    today = today_in(ctx.timezone)
    nodes = await load_nodes(db, ctx.id)
    live_total = await count_live_nodes(db, ctx.id)
    history = await load_history(db, conversation_id, exclude_message_id=exclude_message_id)
    brief = await load_brief(db, ctx.id)
    scope = await load_scope(
        db, ctx, nodes, scope_root_id=scope_root_id, focus_node_id=context_node_id
    )

    # 记号在这里、也只在这里分配。`load_nodes` 的排序是确定的(depth, order_index,
    # created_at),所以同一个库状态下 n1..nK 每次都对得上同一批节点 ——
    # 提案校验时用的是同一份映射,不需要把 id 序列化成字符串再反解。
    handles = tuple((f"n{index}", str(node.id)) for index, node in enumerate(nodes, start=1))
    handle_of = {uuid.UUID(node_id): handle for handle, node_id in handles}
    order = {node.id: node.order_index for node in nodes}
    read_bodies = _bodies_read(scope, order)
    note_chars, note_focus_id, note_body = await load_note_budget(db, ctx.id, scope.focus_id)
    scope_node = next((node for node in nodes if node.id == scope.scope_id), None)
    focus_node = next((node for node in nodes if node.id == scope.focus_id), None)

    views = tuple(
        PlanNodeView(
            handle=handle,
            title=node.title,
            node_type=node.node_type.value,
            purpose=node.purpose.value,
            planning_level=node.planning_level.value if node.planning_level else None,
            status=node.status.value,
            depth=node.depth,
            deadline=node.deadline.isoformat() if node.deadline else None,
            estimate_minutes=node.estimate_minutes,
            parent_handle=handle_of.get(node.parent_id) if node.parent_id else None,
            description=node.description,
            acceptance_criteria=node.acceptance_criteria,
            body_read=node.id in read_bodies,
            notes_present=node.id in note_chars,
            notes_chars=note_chars.get(node.id, 0),
            note_body=note_body if node.id == note_focus_id else None,
            layer=scope.layer_of.get(node.id, LAYER_SCOPE),
            in_scope=node.id in scope.in_scope,
        )
        for (handle, _), node in zip(handles, nodes, strict=True)
    )

    edges, hidden_edges = _relation_views(
        await load_edges(db, ctx.id), handle_of, {node.id for node in nodes}
    )
    # 可改集 = 范围内的那些。**服务端要按它拦越界动作**(见 proposal_validation),
    # 渲染出来的记号清单只是把同一件事告诉模型。
    writable = tuple(handle for handle, node_id in handles if uuid.UUID(node_id) in scope.in_scope)

    snapshot = await input_snapshot.capture(
        db,
        ctx,
        scope_root_id=scope.scope_id,
        focus_node_id=scope.focus_id,
        window=nodes,
        window_truncated=live_total > len(nodes),
    )
    time_view = await load_time_view(db, ctx, today=today, handles=handle_of)

    return TurnContext(
        current_date=today.isoformat(),
        weekday=_weekday_cn(today),
        timezone=ctx.timezone,
        workspace_title=ctx.workspace.title,
        workspace_intent=ctx.workspace.intent or "",
        known=to_known_conditions(brief),
        nodes=views,
        history=tuple(history),
        user_message=user_message,
        current_view=current_view,
        context_node_title=focus_node.title if focus_node else None,
        scope_root_handle=handle_of.get(scope.scope_id) if scope.scope_id else None,
        scope_root_title=scope_node.title if scope_node else None,
        focus_handle=handle_of.get(scope.focus_id) if scope.focus_id else None,
        writable_handles=writable,
        edges=edges,
        edges_hidden=hidden_edges,
        live_node_count=live_total,
        window_truncated=live_total > len(nodes),
        time=time_view,
        input_snapshot=snapshot,
        node_handles=handles,
    )
