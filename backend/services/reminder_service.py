"""站内提醒:什么时候该跟用户说一句话。

## 提醒的**内容**是推导出来的,只有用户的处置被存下来

库里没有一张"提醒表"。每一条提醒都是这次请求里按事实算出来的(这个空间还是空的、
计划刚更新过、你已经四天没有记录、今天是周末、某个阶段刚完成)。存下来的只有
`reminder_states` —— 用户关掉了哪条、让哪条过一会儿再说。

这样做的原因很实际:如果提醒是一行数据,就必须有人**在正确的那一刻**把它写进去。
那意味着定时任务、一个"写提醒"的服务、以及"任务没跑时提醒就永远不出现"这种失败模式。
推导出来则不会:它的正确性只取决于查询时的库状态,任何时刻打开界面看到的都是当下的事实。

代价是每次请求要多跑几个查询。这几个查询都走索引,而 `GET /reminders` 是"打开界面"
级别的调用,不是热路径 —— 这个代价可以接受。

## 只由事件触发,不做"每天定时提醒"

"每天提醒你学习"这类时间驱动的东西在用户连续做了两周之后照样每天出现,然后就被无视了。
一个总是出现的提醒等于没有提醒。所以这里的每一条都对应一个真实发生过的事。

## 免打扰时段:推导出来的,但只压住"催促",不压住"有事在等你"

时段来自用户自己说过的可用时段(见 `_quiet_hours`,那里解释了为什么下界不从可用时段推)。

而**为什么只压住一部分**:免打扰在推送世界里是"别把我吵醒",但这里提醒只在用户打开
界面时才出现 —— 用户此刻醒着,这个前提已经成立。真正会伤人的是"深夜了还在催你今天没
记录",而"你新建的空间还是空的、有一份计划等你确认"这类陈述并不催人,它只是把已经
等着他的事情摆出来。所以被压住的是 `weekend` 与 `user_returned` 这两种催促,
`suppressed_count` 如实报出被压住的条数。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import exists, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from backend.contracts.reminder import (
    QuietHoursView,
    RemindersResponse,
    ReminderStateView,
    ReminderView,
)
from backend.core.security import CurrentUser
from backend.db.base import utcnow
from backend.db.models import (
    AvailabilityRule,
    ExecutionRecord,
    Message,
    PlanNode,
    PlanRevision,
    ReminderState,
    ScheduledSession,
    Workspace,
)
from backend.db.models.enums import (
    ExecutionResult,
    MessageRole,
    NodeStatus,
    NodeType,
    ScheduledSessionStatus,
    WorkspaceStatus,
)
from backend.services.timeutil import resolve_zone

logger = logging.getLogger(__name__)

#: 免打扰的默认时段。上界 22:00 是"该休息了",下界 08:00 是"还没起床"。
DEFAULT_QUIET_FROM = 22 * 60
DEFAULT_QUIET_TO = 8 * 60

#: 新建的空间多久之内还算"刚建、还空着"。三个月前建的空间一直提示"还是空的"
#: 不是提醒,是骚扰 —— 用户显然已经做过决定了(哪怕是决定不用它)。
EMPTY_WORKSPACE_DAYS = 14

#: 计划刚更新过,多久之内值得说一声。
PLAN_FRESH_DAYS = 3

#: 多久没有用户信号才算"回来了一趟"。
RETURN_GAP_DAYS = 3

#: 一周之内攒下几场"没做/只做了一部分"才值得提。
SKIP_THRESHOLD = 2

#: 阶段完成之后多久之内值得祝贺一句。
STAGE_DONE_DAYS = 7

#: 免打扰时段**不**压住的那些:它们陈述的是"有事情在等你",不是"你该去做事了"。
_STATE_KINDS = frozenset(
    {"workspace_empty", "plan_created", "stage_completed", "repeated_skips"}
)

#: 同屏最多显示几条。再多就不看了。
MAX_REMINDERS = 5


@dataclass(frozen=True, slots=True)
class Reminder:
    key: str
    kind: str
    title: str
    body: str
    workspace_id: uuid.UUID | None = None
    for_date: str = ""

    def to_view(self) -> ReminderView:
        return ReminderView(
            key=self.key,
            kind=self.kind,
            title=self.title,
            body=self.body,
            workspace_id=self.workspace_id,
            for_date=self.for_date,
        )


async def load(db: AsyncSession, user: CurrentUser, *, now: datetime) -> RemindersResponse:
    """这个用户此刻该看到的提醒。**只读,不创建任何行。**

    `now` 是"此刻"这个**绝对时刻**,由调用方给(路由走 `Depends(get_now)`,见
    `api/dependencies/clock.py`)。它**必填**,没有"不传就是现在"的默认值 —— 这个函数
    里有两处判据挂在钟点上(免打扰窗口、周末),一个可以省略的 `now` 会让测试里那一条
    悄悄退回真实时钟,而它照样是绿的。

    传进来的那一刻按**用户自己的时区**换算:免打扰窗口说的是用户那边的晚上,不是
    服务器的。`user.timezone` 是账号属性,和"此刻"是两件事,不从这里推。
    """
    local = now.astimezone(resolve_zone(user.timezone))
    today = local.date()
    now = local

    candidates = await _collect(db, user, today=today, now=now)
    states = await _load_states(db, user.user_id, [item.key for item in candidates])
    live = [
        item
        for item in candidates
        # 关掉的、以及"稍后"还没到点的,都不出现。
        if not _is_suppressed(states.get(item.key), now)
    ]

    quiet = await _quiet_hours(db, user, now)
    if quiet.active:
        pending = [item for item in live if item.kind in _STATE_KINDS]
        suppressed = len(live) - len(pending)
        live = pending
    else:
        suppressed = 0

    return RemindersResponse(
        reminders=[item.to_view() for item in live[:MAX_REMINDERS]],
        quiet_hours=quiet,
        suppressed_count=suppressed,
        note=_note(quiet, suppressed, len(live)),
    )


def _note(quiet: QuietHoursView, suppressed: int, shown: int) -> str:
    """如实说明"现在没提醒"是不是"系统认为一切正常"。

    这两件事在界面上长得一模一样,而它们的含义完全相反。所以当提醒因为免打扰被压住时,
    必须说出来。
    """
    if suppressed:
        return (
            f"现在是免打扰时段({quiet.description}),有 {suppressed} 条提醒先不打扰你,"
            "时段过去后会重新出现。"
        )
    if not shown:
        return "现在没有需要提醒你的事。"
    return ""


# ---------------------------------------------------------------------------------
# 候选提醒
# ---------------------------------------------------------------------------------
async def _collect(
    db: AsyncSession, user: CurrentUser, *, today: date, now
) -> list[Reminder]:
    """按事件取候选。顺序就是界面上的顺序:越靠前越"有事在等你"。"""
    items: list[Reminder] = []
    active = await _active_workspaces(db, user.user_id)

    for workspace in active:
        empty = await _empty_workspace(db, workspace, today=today, now=now)
        if empty is not None:
            items.append(empty)
            # 一个还空着的空间不会同时有"计划刚更新"—— 有更新就说明它不空了。
            continue
        fresh = await _fresh_plan(db, workspace, now=now)
        if fresh is not None:
            items.append(fresh)

    items.extend(await _stages_completed(db, active, now=now))
    items.extend(await _repeated_skips(db, user.user_id, active, today=today))
    items.extend(await _returned(db, user, today=today, now=now))

    weekend = await _weekend(db, user.user_id, today=today, now=now)
    if weekend is not None:
        items.append(weekend)
    return items


async def _active_workspaces(db: AsyncSession, user_id: uuid.UUID) -> list[Workspace]:
    result = await db.execute(
        select(Workspace)
        .where(Workspace.owner_id == user_id, Workspace.status == WorkspaceStatus.ACTIVE)
        .order_by(Workspace.created_at.asc())
    )
    return list(result.scalars())


async def _empty_workspace(db: AsyncSession, workspace: Workspace, *, today: date, now):
    """刚建好、还没长出计划来的空间。

    判据是"除了根目标什么都没有"**并且**"从来没排过任何一场" —— 只看节点数不够:
    一个被 AI 排过计划、后来又被清空的空间,节点数也可能是 1,而它不是"还没开始",
    是"被放弃过"。排过场的空间不再提示。

    "刚建好"用两个时间戳相减,**不经过日期**:`created_at` 是 UTC 的,把它转成
    日期再和用户本地的今天比,在东八区的晚上会差一天(见 `timeutil` 的说明)。
    """
    if now - workspace.created_at > timedelta(days=EMPTY_WORKSPACE_DAYS):
        return None

    node_count = await db.scalar(
        select(func.count())
        .select_from(PlanNode)
        .where(PlanNode.workspace_id == workspace.id, PlanNode.deleted_at.is_(None))
    )
    if int(node_count or 0) > 1:
        return None

    ever_scheduled = await db.scalar(
        select(exists().where(ScheduledSession.workspace_id == workspace.id))
    )
    if ever_scheduled:
        return None

    return Reminder(
        key=f"workspace_empty:{workspace.id}",
        kind="workspace_empty",
        title="这个空间还是空的",
        body=(
            f"「{workspace.title}」还没有计划。跟 AI 说说你想达成什么、"
            "大概什么时候要,它会把阶段和任务搭出来。"
        ),
        workspace_id=workspace.id,
        for_date=today.isoformat(),
    )


async def _fresh_plan(db: AsyncSession, workspace: Workspace, *, now):
    """计划刚更新过 —— 让用户知道版本变了。

    键里带着 `version`,所以每出一版新计划就是一条新提醒。用户关掉第 3 版之后,
    第 4 版仍然会出现 —— 那是对的:它们是两次不同的变更。
    """
    result = await db.execute(
        select(PlanRevision)
        .where(PlanRevision.workspace_id == workspace.id)
        .order_by(PlanRevision.version.desc())
        .limit(1)
    )
    revision = result.scalar_one_or_none()
    if revision is None:
        return None
    if now - revision.created_at > timedelta(days=PLAN_FRESH_DAYS):
        return None

    return Reminder(
        key=f"plan_created:{workspace.id}:{revision.version}",
        kind="plan_created",
        title="计划更新了",
        body=(
            f"「{workspace.title}」现在是第 {revision.version} 版计划"
            f"({_trigger_label(revision.trigger_type.value)})。"
            "路径、时间线、任务几个视图读的都是同一份计划,去哪看都一样。"
        ),
        workspace_id=workspace.id,
        for_date=now.date().isoformat(),
    )


def _trigger_label(trigger: str) -> str:
    return {
        "initial_plan": "初次排定",
        "user_edit": "你自己改的",
        "execution_deviation": "按执行情况调整的",
        "brief_change": "因为你更新了条件",
        "deadline_change": "因为截止日变了",
        "manual_replan": "重新排的",
    }.get(trigger, trigger)


async def _stages_completed(db: AsyncSession, active: list[Workspace], *, now) -> list[Reminder]:
    """刚完成的阶段。

    `CAPABILITY` 也算在内 —— 用户眼里的"阶段"是"一个能往下拆的层",而不是枚举里的
    某个具体值。只挑有子节点的,因为一个没有子节点的节点完成与否不构成里程碑。
    """
    if not active:
        return []
    cutoff = now - timedelta(days=STAGE_DONE_DAYS)
    # 子节点用**另一个别名**查。反过来引用外层的 `PlanNode` 会让 SQLAlchemy 试图把
    # 子查询关联到外层那一份同名表上,结果是子查询里一个 FROM 都不剩 —— 它要么报错,
    # 要么(更糟)在某个版本上安静地退化成恒真。别名让"外面那棵"和"里面那棵"是两件事。
    child = aliased(PlanNode)
    result = await db.execute(
        select(PlanNode, Workspace.title)
        .join(Workspace, Workspace.id == PlanNode.workspace_id)
        .where(
            PlanNode.workspace_id.in_([item.id for item in active]),
            PlanNode.deleted_at.is_(None),
            PlanNode.node_type.in_((NodeType.STAGE, NodeType.CAPABILITY, NodeType.GOAL)),
            PlanNode.status == NodeStatus.COMPLETED,
            PlanNode.completed_at.is_not(None),
            PlanNode.completed_at >= cutoff,
            exists(
                select(child.id).where(
                    child.parent_id == PlanNode.id, child.deleted_at.is_(None)
                )
            ),
        )
        .order_by(PlanNode.completed_at.desc())
        .limit(3)
    )
    items: list[Reminder] = []
    for node, workspace_title in result.all():
        items.append(
            Reminder(
                key=f"stage_completed:{node.id}",
                kind="stage_completed",
                title="一个阶段完成了",
                body=(
                    f"「{node.title}」在「{workspace_title}」里已经标记完成。"
                    "可以看看下一阶段要不要提前开始,或者把后面的排期调紧一点。"
                ),
                workspace_id=node.workspace_id,
                for_date=now.date().isoformat(),
            )
        )
    return items


async def _repeated_skips(
    db: AsyncSession, user_id: uuid.UUID, active: list[Workspace], *, today: date
) -> list[Reminder]:
    """同一周里反复"没做"或"只做了一部分"的空间。

    这是提示"计划可能排得太满"的信号。一条都没记录时**不触发** —— 没有记录不等于
    没做,见 `execution_service` 里同一条判断。
    """
    if not active:
        return []
    since = today - timedelta(days=7)
    rows = await db.execute(
        select(
            Workspace.id,
            Workspace.title,
            func.count(func.distinct(ExecutionRecord.session_id)).label("sessions"),
        )
        .join(ExecutionRecord, ExecutionRecord.workspace_id == Workspace.id)
        .where(
            ExecutionRecord.user_id == user_id,
            Workspace.id.in_([item.id for item in active]),
            ExecutionRecord.result.in_((ExecutionResult.SKIPPED, ExecutionResult.PARTIAL)),
            ExecutionRecord.created_at >= _days_ago_utc(7),
            ExecutionRecord.session_id.is_not(None),
        )
        .group_by(Workspace.id, Workspace.title)
        .having(func.count(func.distinct(ExecutionRecord.session_id)) >= SKIP_THRESHOLD)
    )
    year, week, _ = today.isocalendar()
    items: list[Reminder] = []
    for workspace_id, title, count in rows.all():
        items.append(
            Reminder(
                key=f"repeated_skips:{workspace_id}:{year}-W{week:02d}",
                kind="repeated_skips",
                title="这周的安排可能排多了",
                body=(
                    f"「{title}」这周有 {count} 场记成了没做或只做了一部分"
                    f"(从 {since.isoformat()} 起算)。"
                    "可以点「按执行情况调整计划」,让 AI 按实际进度重排一遍。"
                ),
                workspace_id=workspace_id,
                for_date=today.isoformat(),
            )
        )
    return items


async def _returned(db: AsyncSession, user: CurrentUser, *, today: date, now) -> list[Reminder]:
    """隔了几天没来。

    "最后一次信号"取用户说过的话与记过的执行里较晚的那个 —— 只看登录时间会把
    "登录了但什么都没做"算成有活动。**没有说过话的新用户不触发**:他还没有可
    "回来"的过去。
    """
    last_message = await db.scalar(
        select(func.max(Message.created_at)).where(
            Message.user_id == user.user_id, Message.role == MessageRole.USER
        )
    )
    last_record = await db.scalar(
        select(func.max(ExecutionRecord.created_at)).where(ExecutionRecord.user_id == user.user_id)
    )
    signals = [value for value in (last_message, last_record) if value is not None]
    if not signals:
        return []

    last = max(signals)
    gap = (now - last.astimezone(now.tzinfo)).days
    if gap < RETURN_GAP_DAYS:
        return []

    return [
        Reminder(
            key=f"user_returned:{today.isoformat()}",
            kind="user_returned",
            title=f"有 {gap} 天没有你的消息了",
            body=(
                "这段时间的安排还留在原处,没有自动往后挪。"
                "想接着走就记录一下最近的进度;计划跟不上了就让 AI 重排。"
            ),
            for_date=today.isoformat(),
        )
    ]


async def _weekend(db: AsyncSession, user_id: uuid.UUID, *, today: date, now):
    """周末,而且这一周确实还有没记录的事。"""
    if now.weekday() < 5:
        return None

    since = today - timedelta(days=6)
    unrecorded = await db.scalar(
        select(func.count())
        .select_from(ScheduledSession)
        .where(
            ScheduledSession.user_id == user_id,
            ScheduledSession.scheduled_date >= since,
            ScheduledSession.scheduled_date <= today,
            ScheduledSession.status.not_in(
                (ScheduledSessionStatus.CANCELED, ScheduledSessionStatus.MOVED)
            ),
            ~exists(
                select(ExecutionRecord.id).where(
                    ExecutionRecord.session_id == ScheduledSession.id
                )
            ),
        )
    )
    count = int(unrecorded or 0)
    if count <= 0:
        return None

    year, week, _ = today.isocalendar()
    return Reminder(
        key=f"weekend:{year}-W{week:02d}",
        kind="weekend",
        title="周末了",
        body=(
            f"这一周有 {count} 场安排没有记录。"
            "没做不等于失败,记录一下实际情况,下周的排期才不会继续按一个不成立的假设走。"
        ),
        for_date=today.isoformat(),
    )


# ---------------------------------------------------------------------------------
# 免打扰
# ---------------------------------------------------------------------------------
async def _quiet_hours(db: AsyncSession, user: CurrentUser, now) -> QuietHoursView:
    """从用户的可用时段推出免打扰时段。

    ## 上界从可用时段推,下界不从

    用户说过"我平时晚上 19:00–22:00 有空",那 22:00 之后本来就不该再被系统叫住 ——
    这是他自己给出的边界。取**所有可用时段里最晚的那个结束时刻**作为上界。

    **下界固定 08:00,不从可用时段推。** 可用时段说的是"我什么时候有空",它推不出
    "我什么时候起床":一个只在周末白天有空的用户,时段是 09:00–17:00,拿它的开始时刻
    当免打扰下界,等于凌晨 3 点把他叫醒。这不是能算出来的东西,所以不编。

    一个诚实的偏差:如果用户的可用时段全在白天,上界会落在下午(比如 17:00),于是
    免打扰从 17:00 开始 —— 对一个"白天才有空"的人来说,17:00 之后不打扰他本来就是对的。
    偏差的方向是"更少打扰",而不是"更晚打扰"。
    """
    latest_end = await db.scalar(
        select(func.max(AvailabilityRule.end_minute)).where(AvailabilityRule.user_id == user.user_id)
    )
    # 早于默认下界的边界不采用:它落在默认时段里面,提供不了任何默认值没覆盖的信息,
    # 却会把窗口切成"07:00–08:00 免打扰"这种没人能解释的形状。
    if latest_end is None or int(latest_end) <= DEFAULT_QUIET_TO:
        start, source = DEFAULT_QUIET_FROM, "default"
    else:
        start, source = int(latest_end), "availability"

    minute = now.hour * 60 + now.minute
    # 跨过午夜:22:00 -> 08:00 表示"22:00 之后,或者 08:00 之前"。
    active = minute >= start or minute < DEFAULT_QUIET_TO

    return QuietHoursView(
        active=active,
        from_minute=start,
        to_minute=DEFAULT_QUIET_TO,
        description=_describe_window(start, DEFAULT_QUIET_TO),
        source=source,
    )


def _describe_window(start: int, end: int) -> str:
    return f"{_hhmm(start)}–{_hhmm(end)}"


def _hhmm(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


# ---------------------------------------------------------------------------------
# 处置:关掉 / 稍后
# ---------------------------------------------------------------------------------
async def dismiss(db: AsyncSession, user: CurrentUser, key: str) -> ReminderStateView:
    """关掉一条提醒。重复点没有副作用 —— 结果一样。"""
    state = await _upsert_state(db, user.user_id, key, workspace_id=None)
    state.dismissed_at = utcnow()
    state.snoozed_until = None
    await db.commit()
    return ReminderStateView(key=key, dismissed=True, snoozed_until=None)


async def snooze(
    db: AsyncSession, user: CurrentUser, key: str, *, hours: int, now: datetime
) -> ReminderStateView:
    """让一条提醒过一会儿再说。

    **"稍后"是一个真实的承诺。** 到点之后它会重新出现,而不是被软化成"关掉" ——
    这也是为什么 `snoozed_until` 必须落库:只在内存里记一笔的话,刷新页面它就
    立刻回来了,用户会以为按钮坏了。

    `now` 与 `load` 的同一个来源(路由的 `Depends(get_now)`)。这里**不能**自己去
    取时间:`snoozed_until` 会被 `load` 拿去和"此刻"比大小,两边要是各取各的钟,
    "稍后 24 小时"就可能在一注入时钟的测试里凭空变成"已经过期"或者"永远不到期"。
    一个请求里的"此刻"只能有一个。
    """
    local = now.astimezone(resolve_zone(user.timezone))
    until = local + timedelta(hours=hours)
    state = await _upsert_state(db, user.user_id, key, workspace_id=None)
    # 稍后与被关掉是互斥的两种处置:已经关掉的提醒不会因为"稍后"又冒出来。
    state.snoozed_until = until
    await db.commit()
    return ReminderStateView(key=key, dismissed=False, snoozed_until=until)


async def _upsert_state(
    db: AsyncSession, user_id: uuid.UUID, key: str, *, workspace_id: uuid.UUID | None
) -> ReminderState:
    existing = await _find_state(db, user_id, key)
    if existing is not None:
        return existing

    state = ReminderState(user_id=user_id, reminder_key=key, workspace_id=workspace_id)
    db.add(state)
    try:
        await db.flush()
    except IntegrityError:
        # 同一个用户双击两次。唯一键挡住第二条,回读第一条即可 ——
        # 用户看到的和顺序执行完全一样。
        await db.rollback()
        existing = await _find_state(db, user_id, key)
        if existing is None:
            raise
        return existing
    return state


async def _find_state(db: AsyncSession, user_id: uuid.UUID, key: str) -> ReminderState | None:
    result = await db.execute(
        select(ReminderState).where(
            ReminderState.user_id == user_id, ReminderState.reminder_key == key
        )
    )
    return result.scalar_one_or_none()


async def _load_states(
    db: AsyncSession, user_id: uuid.UUID, keys: list[str]
) -> dict[str, ReminderState]:
    if not keys:
        return {}
    result = await db.execute(
        select(ReminderState).where(
            ReminderState.user_id == user_id, ReminderState.reminder_key.in_(keys)
        )
    )
    return {state.reminder_key: state for state in result.scalars()}


def _is_suppressed(state: ReminderState | None, now) -> bool:
    if state is None:
        return False
    if state.dismissed_at is not None:
        return True
    if state.snoozed_until is None:
        return False
    return state.snoozed_until.astimezone(now.tzinfo) > now


def _days_ago_utc(days: int) -> datetime:
    """UTC 侧的粗粒度下界,给 `created_at` 这类时间戳列用。

    这里不需要精确到当天的 00:00 —— 它只是一道减少扫描范围的粗筛,真正的判定
    ("算不算这一周")走的是按用户时区算好的日期列。多取一天再由上层过滤,比在这里
    假装能算准一个跨时区的时间点要诚实。
    """
    return utcnow() - timedelta(days=days + 1)


__all__ = [
    "DEFAULT_QUIET_FROM",
    "DEFAULT_QUIET_TO",
    "MAX_REMINDERS",
    "Reminder",
    "dismiss",
    "load",
    "snooze",
]
