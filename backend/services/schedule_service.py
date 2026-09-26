"""把纯排期算法接到真实的数据库上 —— 读数据、算、然后写回去。

## 三件事的形状被刻意分开

    ① 读      `load_schedule`   把库里的事实取出来,拼成一份 `ScheduleRequest`
    ② 算      `simulate`        纯函数,在 `backend/scheduler/` 里,不知道数据库存在
    ③ 写      `apply`           一个事务,幂等,带版本校验

## 输入为什么是"一个用户的全部空间",而不是"当前这个空间"

时间池是**按人**算的:两个空间争的是同一个晚上。逐空间排的话,两个空间会各自以为
自己能占满整周,而用户只有一个晚上 —— 而且这个错误在界面上完全看不出来,两边各自的
计划都"排得下",只有合起来看才超。所以 `load_schedule` 取的是这个账号下**所有活动
空间**的节点、依赖、场次与执行记录,池子只有一个。

后果是 `POST /schedule/apply` 的写入范围也不止一个空间。这是有意的,也是必须的:
只应用一半会让另一半的安排与实际占用对不上。接口响应里 `applied.workspaces` 把这件事
如实说出来,界面负责转述。

**归档的空间不参与排期,它的场次也不再占用时间预算。** 这正是"归档"该有的效果 ——
一个被归档之后仍然占着晚上的空间,等于没归档。

## "每周能投入多少"从哪来

`user_capacity_profiles` 是这件事的正典位置,但它**目前没有任何代码写它**(注册时刻意
不建,理由见 `auth_service`)。所以真正生效的数字来自用户自己说过的那一句,记在
`planning_briefs.weekly_available_minutes` 上 —— 那一列在用户亲口说出一个数字之前恒为
NULL,这正是 `brief_service` 存在的理由。三档优先级与多空间合并规则写在 `_capacity_profile`。

## `apply` 不写 `plan_revisions`,也不推版本号

排期是"哪天做",节点图是"要做什么"。后者有自己的版本线(`plan_revisions`),前者在
`scheduled_sessions` 和 `schedule_applications` 里。把应用排期也记成一次计划版本,副作用是
所有待确认的提案会在下一次确认时被判成"计划已经变了" —— 而用户只是点了一下"应用排期",
节点一个都没改。让一份提案因为一件与它无关的事失效,代价由用户承担。

`current_revision_version` 仍然进 `schedule_version` 的哈希(它是输入之一),所以"计划结构
变了 → 预览失效"这条照样成立;失效的方向是**多**报一次"请重新预览",不是漏报。
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.contracts.schedule import (
    DailyLoadView,
    PlannedSessionView,
    RecoveryOptionView,
    ScheduleAppliedView,
    ScheduleApplyResponse,
    ScheduleChurnView,
    ScheduleGapView,
    SchedulePreviewResponse,
)
from backend.db.base import utcnow
from backend.db.locking import lock_user
from backend.db.models import (
    AvailabilityException,
    AvailabilityRule,
    Dependency,
    DomainEvent,
    ExecutionRecord,
    PlanNode,
    ScheduleApplication,
    ScheduledSession,
    UserCapacityProfile,
    Workspace,
)
from backend.db.models.enums import ScheduledSessionStatus, WorkspaceStatus
from backend.scheduler import diff as scheduler_diff
from backend.scheduler.calendar import build_day_pools
from backend.scheduler.schedule import recovery_options, simulate
from backend.scheduler.types import (
    AvailabilityWindow,
    CapacityProfile,
    ChurnSummary,
    DayException,
    ExecutionFact,
    ExistingSession,
    ScheduleDependency,
    ScheduleNode,
    ScheduleRequest,
    ScheduleResult,
)
from backend.scheduler.types import (
    ExecutionResult as SchedulerExecutionResult,
)
from backend.scheduler.types import (
    NodeStatus as SchedulerNodeStatus,
)
from backend.scheduler.types import (
    NodeType as SchedulerNodeType,
)
from backend.scheduler.types import (
    Priority as SchedulerPriority,
)
from backend.scheduler.types import (
    SessionOrigin as SchedulerSessionOrigin,
)
from backend.scheduler.types import (
    SessionStatus as SchedulerSessionStatus,
)
from backend.services import brief_service
from backend.services.context import WorkspaceContext
from backend.services.errors import (
    ConcurrencyConflict,
    IdempotencyKeyReused,
    StaleSchedulePreview,
)
from backend.services.timeutil import today_in

logger = logging.getLogger(__name__)

#: 默认往后看多少天。12 周 —— 一份"三个月完成一个项目"的计划正好装得下,而不至于让
#: 没有截止日的任务排到明年。
DEFAULT_HORIZON_DAYS = 84

#: 视界的上限。再远就没有意义了:一年之后的安排,用户既判断不了也执行不到,而把它
#: 算出来只为了让可行性闸门说一句"排得下"。
MAX_HORIZON_DAYS = 365

#: 截止日在视界之内时,再多给这么多天。正好贴着截止日切会让最后几场无处可去,而
#: "往后顺延几天"这条出路也需要一点余地。给一周 —— 这不是精确算法,是一个够用的余量。
_DEADLINE_SLACK_DAYS = 8

#: 不能被"取消"的状态。除了已经冻结的三种,**`moved` 也要排除**:那一行是一条墓碑,
#: 指向搬到的那一场。把它改成 `canceled` 会让"这一场搬去了哪里"这个事实消失。
_NOT_CANCELABLE = (
    ScheduledSessionStatus.DONE,
    ScheduledSessionStatus.SKIPPED,
    ScheduledSessionStatus.CANCELED,
    ScheduledSessionStatus.MOVED,
)


@dataclass(frozen=True, slots=True)
class LoadedSchedule:
    """一次排期的**全部**输入,以及几样只在展示与写入时才用得到的东西。

    分成两块而不是只给一个 `ScheduleRequest`:后者是纯数据、无标识,而预览要说"这是
    哪个节点的标题"。把这些塞进 `ScheduleRequest` 会让排期算法拿到它不需要的信息,也会
    让 `schedule_version` 的哈希随着无关的东西抖动 —— 而版本号一抖,用户就会白看到一句
    "计划已经变了,请重新预览"。
    """

    request: ScheduleRequest
    workspace_ids: tuple[uuid.UUID, ...]
    node_titles: dict[uuid.UUID, str]
    session_workspaces: dict[uuid.UUID, uuid.UUID]


@dataclass(frozen=True, slots=True)
class ApplyOutcome:
    response: ScheduleApplyResponse
    replayed: bool


# ---------------------------------------------------------------------------------
# ① 读
# ---------------------------------------------------------------------------------
async def active_workspace_ids(db: AsyncSession, user_id: uuid.UUID) -> tuple[uuid.UUID, ...]:
    """这个账号下**活动**空间的 id。

    预览与应用必须看到同一批空间,所以这个查询只写一遍 —— 两处各写一份的话,一边加了
    过滤条件而另一边没加,表现会是"预览里有这个空间,应用之后它不见了"。
    """
    return tuple(
        sorted(
            await db.scalars(
                select(Workspace.id).where(
                    Workspace.owner_id == user_id,
                    Workspace.status == WorkspaceStatus.ACTIVE,
                )
            ),
            key=str,
        )
    )


async def load_schedule(db: AsyncSession, ctx: WorkspaceContext) -> LoadedSchedule:
    """把这个账号的全部排期输入取出来。**只读,一行都不写。**"""
    user_id = ctx.user.user_id
    today = today_in(ctx.timezone)
    workspace_ids = await active_workspace_ids(db, user_id)

    if not workspace_ids:
        # 一个活动空间都没有。空请求是有意义的:结果里没有任何场次,写入也就什么都不做。
        # 显式早退的另一个理由是"没有空间时会怎样"从此不需要读者自己去推。
        return LoadedSchedule(
            request=ScheduleRequest(
                today=today,
                horizon_days=DEFAULT_HORIZON_DAYS,
                profile=await _capacity_profile(db, user_id, ()),
                plan_revision=ctx.workspace.current_revision_version,
            ),
            workspace_ids=(),
            node_titles={},
            session_workspaces={},
        )

    nodes = list(
        await db.scalars(
            select(PlanNode).where(
                PlanNode.workspace_id.in_(workspace_ids),
                PlanNode.deleted_at.is_(None),
            )
        )
    )
    horizon_days = _horizon_days(today, nodes)
    horizon_last = today + timedelta(days=horizon_days - 1)
    live_ids = {node.id for node in nodes}
    parents = {node.parent_id for node in nodes if node.parent_id is not None}

    dependencies = [
        row
        for row in await db.scalars(
            select(Dependency).where(Dependency.workspace_id.in_(workspace_ids))
        )
        # 两端都还活着才算数。软删除不级联清理依赖行,不过滤的话排期会去追一条指向已
        # 删除节点的边,而 `simulate` 会按"引用了不存在的节点"直接抛错 —— 用户看到的
        # 是一个和"删除一个任务"毫无关系的 500。
        if row.predecessor_id in live_ids and row.successor_id in live_ids
    ]

    sessions = list(
        await db.scalars(
            select(ScheduledSession).where(ScheduledSession.workspace_id.in_(workspace_ids))
        )
    )
    executions = list(
        await db.scalars(
            select(ExecutionRecord).where(ExecutionRecord.workspace_id.in_(workspace_ids))
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
        for row in await db.scalars(
            select(AvailabilityRule)
            .where(AvailabilityRule.user_id == user_id)
            .order_by(AvailabilityRule.weekday.asc(), AvailabilityRule.start_minute.asc())
        )
    )
    exceptions = tuple(
        DayException(
            on_date=row.on_date,
            available_minutes=row.available_minutes,
            is_unavailable=row.is_unavailable,
        )
        for row in await db.scalars(
            select(AvailabilityException)
            .where(
                AvailabilityException.user_id == user_id,
                # 只看视界之内的例外。视界之外的那些影响不了结果,却会进
                # `schedule_version` 的哈希 —— 于是用户改一个明年三月的请假,屏幕上
                # 会冒出一句"计划已经变了,请重新预览"。
                AvailabilityException.on_date >= today,
                AvailabilityException.on_date <= horizon_last,
            )
            .order_by(AvailabilityException.on_date.asc())
        )
    )

    return LoadedSchedule(
        request=ScheduleRequest(
            today=today,
            horizon_days=horizon_days,
            profile=await _capacity_profile(db, user_id, workspace_ids),
            nodes=tuple(
                # `parents` 里的是"有孩子的那些节点的 id",所以查的是 node.id。
                _schedule_node(node, has_children=node.id in parents)
                for node in nodes
            ),
            dependencies=tuple(
                ScheduleDependency(
                    predecessor_id=row.predecessor_id,
                    successor_id=row.successor_id,
                    lag_days=row.lag_days,
                )
                for row in dependencies
            ),
            existing=tuple(_existing_session(row) for row in sessions),
            executions=tuple(
                ExecutionFact(
                    node_id=row.node_id,
                    session_id=row.session_id,
                    result=SchedulerExecutionResult(row.result.value),
                    actual_minutes=row.actual_minutes,
                    completion_ratio=row.completion_ratio,
                )
                for row in executions
            ),
            windows=windows,
            exceptions=exceptions,
            # 这次排期是从哪个空间发起的。它不参与计算,只进哈希 —— 别把它当成
            # "这次排期的范围",范围是 `workspace_ids`。
            plan_revision=ctx.workspace.current_revision_version,
        ),
        workspace_ids=workspace_ids,
        node_titles={node.id: node.title for node in nodes},
        session_workspaces={row.id: row.workspace_id for row in sessions},
    )


async def _capacity_profile(
    db: AsyncSession, user_id: uuid.UUID, workspace_ids: tuple[uuid.UUID, ...]
) -> CapacityProfile:
    """这个人的时间预算。三档,按优先级:

    1. `user_capacity_profiles` 里那一行 —— 正典位置(将来"设置"里改的就是它)。
    2. 用户自己在对话里说过的数字(`planning_briefs.weekly_available_minutes`)。
    3. 表里的默认值(每周 600 分钟)。

    第 2 档是**目前实际生效**的那一档,因为第 1 档现在没有任何代码会写它。

    回落到默认值时会记一条日志。用户看到一份"每周 8 小时"的计划而他从没说过 8 小时,
    日志是唯一能解释这件事的地方 —— 界面上看不出任何区别。
    """
    row = await db.scalar(
        select(UserCapacityProfile).where(UserCapacityProfile.user_id == user_id)
    )
    if row is not None:
        return CapacityProfile(
            weekly_total_minutes=row.weekly_total_minutes,
            safety_factor=row.safety_factor,
            daily_max_minutes=row.daily_max_minutes,
            default_buffer_minutes=row.default_buffer_minutes,
            min_session_minutes=row.min_session_minutes,
            max_session_minutes=row.max_session_minutes,
            week_start_weekday=row.week_start_weekday,
        )

    stated = await _stated_weekly_minutes(db, workspace_ids)
    if stated is None:
        logger.info("用户 %s 没有说过每周可投入多少,排期按默认预算走。", user_id)
        return CapacityProfile()
    return CapacityProfile(weekly_total_minutes=stated)


async def _stated_weekly_minutes(
    db: AsyncSession, workspace_ids: tuple[uuid.UUID, ...]
) -> int | None:
    """用户在对话里亲口说过的每周可投入时间。

    ## 多个空间各说了一个数字时,取**最大**的那个

    不是相加。用户回答"你每周能投入多少"时说的是**他自己的日历**,不是"给这个空间多少" ——
    把两个空间的答案加起来,会得到一个他从没说过、也超出他能做到的总量的预算。取最大的
    那个仍然是猜的,但猜的方向是保守的:排出来的计划不会超出他至少说过一次的那个数。

    这不是一个完美答案。它至少是一个**偏向少排**的答案 —— 而少排的代价是用户多问一句,
    多排的代价是他每天做不完。

    "哪一份简报是当前的"这个问题交给 `brief_service.load_brief` 回答,不在这里复制一份
    排序规则。复制出来的第二份一定会和第一份漂移,而漂移的表现是"上周说过的 4 小时被一个
    更旧的草稿盖掉了"。
    """
    values: list[int] = []
    for workspace_id in workspace_ids:
        brief = await brief_service.load_brief(db, workspace_id)
        minutes = brief.weekly_available_minutes if brief is not None else None
        if isinstance(minutes, int) and minutes > 0:
            values.append(minutes)
    return max(values) if values else None


def _horizon_days(today: date, nodes: list[PlanNode]) -> int:
    """往后看多远。

    至少要覆盖最远的那个截止日,否则那个任务会被判成"超出视界"而排不进去 —— 用户会
    看到一句和"我的截止日是三个月后"接不上的话。多给一周的余量(见 `_DEADLINE_SLACK_DAYS`),
    并且始终兜在 `[1, MAX_HORIZON_DAYS]` 里:一个手滑写进 `deadline` 的 2099 年会在没有
    上限时让算法去算两万七千天。
    """
    furthest = max((node.deadline for node in nodes if node.deadline is not None), default=None)
    horizon = DEFAULT_HORIZON_DAYS
    if furthest is not None:
        horizon = max(horizon, (furthest - today).days + _DEADLINE_SLACK_DAYS)
    return max(1, min(horizon, MAX_HORIZON_DAYS))


def _schedule_node(node: PlanNode, *, has_children: bool) -> ScheduleNode:
    """ORM 行 -> 排期算法看得懂的那个节点。

    枚举按 `value` 转而不是直接把成员传过去:两边的枚举是**各自声明**的(理由见
    `scheduler/types.py` 顶部 —— 从 `backend.db` 那边 import 会连带着建一个 sqlalchemy
    引擎)。这里是两个集合唯一的相遇点,所以转换要做成**会炸的**:某一侧加了新取值而
    另一侧没跟上时,应当在这里立刻 `ValueError`,而不是让一个非法取值一路进到算法里。

    `has_children` 由调用方从**全部**节点里算出来(见 `load_schedule`),不是在这里查:
    单个 ORM 行上查一次子节点会是每个节点一次查询,而且那个懒加载在 async 上下文里
    还会抛 `MissingGreenlet`。
    """
    return ScheduleNode(
        id=node.id,
        workspace_id=node.workspace_id,
        title=node.title,
        node_type=SchedulerNodeType(node.node_type.value),
        status=SchedulerNodeStatus(node.status.value),
        priority=SchedulerPriority(node.priority.value),
        estimate_minutes=node.estimate_minutes,
        deadline=node.deadline,
        order_index=node.order_index,
        depth=node.depth,
        has_children=has_children,
    )


def _existing_session(row: ScheduledSession) -> ExistingSession:
    return ExistingSession(
        id=row.id,
        workspace_id=row.workspace_id,
        node_id=row.node_id,
        scheduled_date=row.scheduled_date,
        planned_minutes=row.planned_minutes,
        buffer_minutes=row.buffer_minutes,
        start_minute=row.start_minute,
        end_minute=row.end_minute,
        seq=row.seq,
        status=SchedulerSessionStatus(row.status.value),
        locked=row.locked,
        lock_reason=row.lock_reason,
        origin=SchedulerSessionOrigin(row.origin.value),
    )


# ---------------------------------------------------------------------------------
# ② 算 + 预览
# ---------------------------------------------------------------------------------
async def preview(db: AsyncSession, ctx: WorkspaceContext) -> SchedulePreviewResponse:
    """算一份排期给用户看。**只读,一行都不写。**"""
    loaded = await load_schedule(db, ctx)
    return _preview_view(loaded, simulate(loaded.request))


def _preview_view(loaded: LoadedSchedule, result: ScheduleResult) -> SchedulePreviewResponse:
    request = loaded.request
    pools = build_day_pools(
        start=request.today,
        horizon_days=request.horizon_days,
        profile=request.profile,
        windows=request.windows,
        exceptions=request.exceptions,
    )

    return SchedulePreviewResponse(
        schedule_version=result.schedule_version,
        today=request.today,
        horizon_days=request.horizon_days,
        scope_workspace_ids=list(loaded.workspace_ids),
        # 这份计划是建立在"每周多少分钟"上的。用户问"为什么只排了这么点儿"时,这是唯一
        # 能回答它的数字 —— 少了它,那个问题只能靠猜。
        weekly_budget_minutes=pools.weekly_budget,
        sessions=[
            PlannedSessionView(
                session_id=session.session_id,
                node_id=session.node_id,
                workspace_id=session.workspace_id,
                node_title=loaded.node_titles.get(session.node_id, ""),
                scheduled_date=session.scheduled_date,
                planned_minutes=session.planned_minutes,
                buffer_minutes=session.buffer_minutes,
                seq=session.seq,
                start_minute=session.start_minute,
                end_minute=session.end_minute,
                origin=session.origin.value,
                locked=session.locked,
            )
            for session in result.sessions
        ],
        gaps=[
            ScheduleGapView(
                workspace_id=gap.workspace_id,
                node_id=gap.node_id,
                node_title=gap.node_title,
                unscheduled_minutes=gap.unscheduled_minutes,
                reason_code=gap.reason_code,
                binding_constraint=gap.binding_constraint,
                detail=dict(gap.detail),
            )
            for gap in result.gaps
        ],
        options=[
            RecoveryOptionView(
                kind=option.kind,
                label=option.label,
                description=option.description,
                resolves_gap=option.resolves_gap,
                remaining_unscheduled_minutes=option.remaining_unscheduled_minutes,
                params=dict(option.params),
            )
            # `recovery_options` 会为每条出路**重新跑一遍** `simulate`(它的
            # `resolves_gap` 是算出来的,不是断言的)。所以只在真有缺口时才调用它 ——
            # 没有缺口时它立刻返回空,不会白算三遍。
            for option in recovery_options(request, result)
        ],
        daily_load=[
            DailyLoadView(
                date=day,
                planned_minutes=sum(minutes for _, minutes in entries),
                # 池子来自 `build_day_pools`,与排期用的是同一个函数。用别的方式再算一遍
                # "这天本来有多少",两边迟早会不一致 —— 而那意味着界面在解释一个算法
                # 根本没用过的约束。
                capacity_minutes=pools.pool(day),
                by_workspace={str(workspace): minutes for workspace, minutes in entries},
            )
            for day, entries in result.daily_load
        ],
        churn=_churn_view(result.churn),
        total_planned_minutes=sum(session.planned_minutes for session in result.sessions),
        unscheduled_minutes=result.unscheduled_minutes,
        truncated=result.truncated,
    )


def _churn_view(churn: ChurnSummary) -> ScheduleChurnView:
    return ScheduleChurnView(
        moved=churn.moved,
        created=churn.created,
        canceled=churn.canceled,
        kept=churn.kept,
        description=churn.describe(),
    )


# ---------------------------------------------------------------------------------
# ③ 应用
# ---------------------------------------------------------------------------------
async def apply(
    db: AsyncSession,
    ctx: WorkspaceContext,
    *,
    schedule_version: str,
    idempotency_key: str,
) -> ApplyOutcome:
    """把一份预览过的排期写进库。**三道保护,与提案确认同源:**

    1. `idempotency_key` + 唯一约束 —— 双击只写一次,第二次返回上次那个响应。
    2. `schedule_version` 校验 —— 预览之后输入变过就 409,不覆盖用户没看过的安排。
    3. 一个事务 —— 任何一步失败整批回滚,不存在"排了一半"。
    """
    # 失败路径上有 `db.rollback()`,而回滚会让会话里所有 ORM 对象过期。之后再读
    # `ctx.workspace.id` 会触发一次懒加载 —— 在 async SQLAlchemy 里那会抛
    # `MissingGreenlet`,把一个本该是 409 的业务错误变成 500。这些值在入口处就是确定的,
    # 先取出来,失败分支就再没有理由去碰 ORM 对象。
    user_id = ctx.user.user_id
    workspace_id = ctx.id
    request_hash = _hash({"scheduleVersion": schedule_version, "workspaceId": str(workspace_id)})

    # 快路径:这个幂等键已经处理过。**先查台账再算** —— 重算一遍要读十几张表,而双击
    # 的第二下根本不需要重算。
    prior = await _find_application(db, user_id, idempotency_key)
    if prior is not None:
        return _replay(prior, request_hash)

    try:
        # 守卫写必须在**人**这一行上:池子是按人算的,只锁空间会让两个空间的排期同时
        # 通过,各自把自己排满(见 `db/locking.lock_user`)。
        await lock_user(db, user_id)

        loaded = await load_schedule(db, ctx)
        current = scheduler_diff.schedule_version(loaded.request)
        if current != schedule_version:
            await db.rollback()
            raise StaleSchedulePreview(
                "这份排期预览之后,计划或时间预算又有过改动。直接应用会写下一份你没看过的"
                "安排,请重新预览。",
                previewedVersion=schedule_version,
                currentVersion=current,
            )

        result = simulate(loaded.request)

        # 幂等台账。**INSERT 放在业务写入之前,同一个事务里** —— 这样"台账写了但场次
        # 没写"和"场次写了但台账没写"都不可能出现。
        application = ScheduleApplication(
            user_id=user_id,
            workspace_id=workspace_id,
            idempotency_key=idempotency_key,
            content_hash=request_hash,
            schedule_version=schedule_version,
            # 占位。真正要返回的响应体在写入完成后才构造得出来,下面在同一个事务里把它
            # 写上去。
            result={},
            created_at=utcnow(),
        )
        db.add(application)
        await db.flush()

        applied = await _write_sessions(db, user_id, loaded, result)
        await db.flush()

        response = ScheduleApplyResponse(
            schedule_version=schedule_version,
            applied=applied,
            churn=_applied_churn(applied),
            replayed=False,
        )
        application.result = response.model_dump(mode="json", by_alias=True)

        db.add(
            DomainEvent(
                user_id=user_id,
                workspace_id=workspace_id,
                kind="schedule_applied",
                ref_type="schedule_application",
                ref_id=application.id,
                payload={
                    "scheduleVersion": schedule_version,
                    "created": applied.created,
                    "updated": applied.updated,
                    "canceled": applied.canceled,
                    "workspaces": applied.workspaces,
                    "unscheduledMinutes": applied.unscheduled_minutes,
                },
                created_at=utcnow(),
            )
        )
        await db.commit()
        return ApplyOutcome(response=response, replayed=False)

    except IntegrityError:
        # 台账的唯一键被撞了 —— 另一个请求用同一个幂等键赢了。这不是错误,是"你双击的
        # 第二下"。回读赢家写好的响应原样返回。
        await db.rollback()
        prior = await _find_application(db, user_id, idempotency_key)
        if prior is None:
            # 撞的是别的唯一约束(比如 `uq_scheduled_sessions_slot` 这个"同一天同一序号"
            # 的槽位)。那是真 bug,以原貌暴露,不把它伪装成一次幂等命中。
            raise
        return _replay(prior, request_hash)


async def _write_sessions(
    db: AsyncSession,
    user_id: uuid.UUID,
    loaded: LoadedSchedule,
    result: ScheduleResult,
) -> ScheduleAppliedView:
    """把算法算出来的场次落库。

    ## 写入契约:没被提到的行一律不碰

    `ScheduleResult.sessions` 里**没有**冻结的场次(已完成 / 已跳过 / 已取消 / 已锁定 /
    日期已经过去的那些)。于是"`sessions` 里没有它"的意思是**别动它**,而不是"删掉它"。
    这里因此只做三种写入:`sessions` 里的逐条更新或插入,`cancelations` 里的改成取消。

    ## 先取消,再插入

    顺序不是随意的。取消会把一行从 `(node_id, date, seq)` 那个部分唯一索引的谓词里移
    出去,插入则要占一个槽位。先取消后插入,槽位的释放一定排在占用之前。反过来(而且
    假设两者会撞上同一个槽位)就会在 flush 时炸出一个唯一约束违反 —— 而那是用户点
    "应用"时看到的一次 500。
    """
    workspaces: set[uuid.UUID] = {session.workspace_id for session in result.sessions}

    canceled = 0
    if result.cancelations:
        workspaces |= {
            loaded.session_workspaces[session_id]
            for session_id in result.cancelations
            if session_id in loaded.session_workspaces
        }
        outcome = await db.execute(
            update(ScheduledSession)
            .where(
                ScheduledSession.id.in_(result.cancelations),
                ScheduledSession.user_id == user_id,
                # **数据的最后一道闸门**,不是重复校验:算法已经保证冻结的场次不会出现在
                # `cancelations` 里(有测试钉着)。但写入路径不该把"用户的历史不会被我改掉"
                # 这件事寄托在调用方身上 —— 真漏一个,这里挡住,而 `rowcount` 会把少改的
                # 那部分如实报出来,不会没人发现。
                ScheduledSession.status.not_in(_NOT_CANCELABLE),
            )
            .values(status=ScheduledSessionStatus.CANCELED)
            .execution_options(synchronize_session=False)
        )
        canceled = int(outcome.rowcount or 0)

    created = 0
    updated = 0
    for planned in result.sessions:
        if planned.session_id is None:
            db.add(
                ScheduledSession(
                    user_id=user_id,
                    workspace_id=planned.workspace_id,
                    node_id=planned.node_id,
                    scheduled_date=planned.scheduled_date,
                    start_minute=planned.start_minute,
                    end_minute=planned.end_minute,
                    planned_minutes=planned.planned_minutes,
                    buffer_minutes=planned.buffer_minutes,
                    seq=planned.seq,
                    status=ScheduledSessionStatus.PLANNED,
                    locked=planned.locked,
                    origin=SchedulerSessionOrigin(planned.origin.value),
                )
            )
            created += 1
            continue

        await db.execute(
            update(ScheduledSession)
            .where(
                ScheduledSession.id == planned.session_id,
                ScheduledSession.user_id == user_id,
            )
            .values(
                scheduled_date=planned.scheduled_date,
                start_minute=planned.start_minute,
                end_minute=planned.end_minute,
                planned_minutes=planned.planned_minutes,
                buffer_minutes=planned.buffer_minutes,
                seq=planned.seq,
            )
            # 刻意**不写** `status` / `locked` / `origin`:这三样是用户的东西(他自己勾的
            # 状态、他自己上的锁),排期只决定"哪天做、做多久"。
            .execution_options(synchronize_session=False)
        )
        updated += 1

    return ScheduleAppliedView(
        created=created,
        updated=updated,
        moved=result.churn.moved,
        canceled=canceled,
        kept=result.churn.kept,
        workspaces=len(workspaces),
        unscheduled_minutes=result.unscheduled_minutes,
    )


def _applied_churn(applied: ScheduleAppliedView) -> ScheduleChurnView:
    """响应里那份"这次改了什么"。

    数字取自 `applied`(数据库回报的 rowcount),**不取算法说的**。两者不一致时(比如
    某一行被状态闸门挡下来了)用户看到的是真正发生的事 —— 而同一份响应里
    `churn.canceled` 与 `applied.canceled` 给出两个不同的数字,比数字本身错更糟。
    """
    return _churn_view(
        ChurnSummary(
            moved=applied.moved,
            created=applied.created,
            canceled=applied.canceled,
            kept=applied.kept,
        )
    )


async def _find_application(
    db: AsyncSession, user_id: uuid.UUID, idempotency_key: str
) -> ScheduleApplication | None:
    return await db.scalar(
        select(ScheduleApplication).where(
            ScheduleApplication.user_id == user_id,
            ScheduleApplication.idempotency_key == idempotency_key,
        )
    )


def _replay(application: ScheduleApplication, request_hash: str) -> ApplyOutcome:
    """返回上次存下来的那份响应。

    内容哈希对不上,说明这个幂等键被用在了另一个请求上 —— 那时如果照样返回上次的结果,
    用户会看到**另一份排期**被"应用成功"。所以这里必须拦下来。
    """
    if application.content_hash != request_hash:
        raise IdempotencyKeyReused("这个幂等键已经用在另一次排期应用上了,请换一个。")
    stored = application.result if isinstance(application.result, dict) else {}
    if not stored:
        # 台账写了但响应体还没写上 —— 只可能发生在赢家还没提交时(此时唯一约束尚未生效,
        # 读到的是一条空壳)。让用户重试即可,不必假装成功。
        raise ConcurrencyConflict("这份排期正在应用中,请稍候刷新。")
    return ApplyOutcome(
        response=ScheduleApplyResponse.model_validate({**stored, "replayed": True}),
        replayed=True,
    )


def _hash(value: object) -> str:
    """规范化 JSON 的 blake2b。键排序,保证"同样的内容"永远得到同一个哈希。"""
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.blake2b(encoded.encode("utf-8"), digest_size=32).hexdigest()


__all__ = [
    "DEFAULT_HORIZON_DAYS",
    "MAX_HORIZON_DAYS",
    "ApplyOutcome",
    "LoadedSchedule",
    "active_workspace_ids",
    "apply",
    "load_schedule",
    "preview",
]
