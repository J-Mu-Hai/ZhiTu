"""执行反馈,以及跨空间的「今天」。

## 反馈是**用户说的话**,不是系统推断出来的事实

`execution_records` 只追加,永不修改。用户点"我做完了"、写一句"今天加班只做了 20 分钟",
这些是他对自己那一晚的陈述。系统能做的只有如实记下来,并让排期把它当作既成事实。

**最关键的一条:没有记录 ≠ 没完成。** 用户完全可能做完了但没打开界面。所以这里的
每一个读接口在"这一场已经过去、却没有任何记录"时,给出的都是一句**提问**,而不是
一个结论。把未记录算成未完成,用户从第一天起就会被系统按一个他从没确认过的事实去
重排计划 —— 而且他纠正不了,因为界面上一片沉默。

## 写入必须**响亮**地成功或失败

这条路由的响应体里有 `saved`。数据库写不进去时返回 503 且 `saved: false`,绝不退回
内存 —— 用户据此决定要不要再录一次,而谎报成功会让那条记录永久消失。

## 记录一场不会顺手把节点标成完成

一个任务是"三个月内写完文献综述",它有 8 场安排。做完第 3 场不代表这个任务完成了。
节点状态是**用户对任务整体的判断**,不是场次的加法 —— 系统替他加总,等于替他宣布
一件他没说过的事。所以这里只改场次,节点状态一律不动(用户自己可以在节点上勾)。

## `scheduled_sessions.actual_minutes` 是**合计**

同一场分两次做完(先报 30 分钟没做完,再做 20 分钟报完成)时,场次行上记的是 50 ——
"这一场实际花了多久"这个问题的答案。每一次单独的反馈都在台账里,一行不少。

## 这一条路径不推 `plan_revisions`

它改的是"哪天做、做了多久",不是"要做什么"。理由与 `schedule_service.apply` 完全相同:
把执行反馈也记成一次计划版本,会让所有待确认的提案在下一次确认时被判成"计划已经变了",
而用户只是勾了一下完成。变化本身记在 `domain_events` 里。
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import exists, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.contracts.execution import (
    CheckInQuestion,
    ExecutionRecordView,
    RecordExecutionResponse,
    TodayItemView,
    TodayResponse,
    TodayWorkspaceView,
)
from backend.contracts.plan import SessionPayload
from backend.core.security import CurrentUser
from backend.db.base import utcnow
from backend.db.models import DomainEvent, ExecutionRecord, PlanNode, ScheduledSession, Workspace
from backend.db.models.enums import (
    ExecutionResult,
    ScheduledSessionStatus,
    WorkspaceStatus,
)
from backend.services import plan_service
from backend.services.context import WorkspaceContext
from backend.services.errors import IdempotencyKeyReused, InvalidInput, SessionNotFound
from backend.services.timeutil import today_in

logger = logging.getLogger(__name__)

#: 场次的状态 -> 用户报了这样一个结果之后它应该变成什么。
#:
#: `FAILED` **刻意不在表里**:试了但没成,说明这件事**还没做完**,而这一场仍然需要
#: 被重新安排。把它改成任何冻结状态都会让系统"少一件事要做",而用户其实还欠着它。
#: 所以 `failed` 只落台账、不动场次状态。
_RESULT_STATUS: dict[ExecutionResult, ScheduledSessionStatus] = {
    ExecutionResult.COMPLETED: ScheduledSessionStatus.DONE,
    # 做了一部分:这场还没结束,所以它不是"已完成"。改成 `in_progress` 而不是留在
    # `planned`,是因为这两种状态在用户眼里确实是两回事 —— 一个动过,一个没动过。
    ExecutionResult.PARTIAL: ScheduledSessionStatus.IN_PROGRESS,
    # 这场没有发生。跳过是一次真实的决定,冻结它,重排就不会再把它挪来挪去。
    ExecutionResult.SKIPPED: ScheduledSessionStatus.SKIPPED,
}

#: 墓碑。对它们的反馈无处可去:一场取消了的安排不存在"做没做"这个问题。
_TOMBSTONE = (ScheduledSessionStatus.CANCELED, ScheduledSessionStatus.MOVED)

#: 回溯多久之内的"没记录"值得问一句。两周:再久之前的事情用户也记不清了,
#: 而一串二十天前的问题会把真正要紧的那几条淹掉。
CHECK_IN_DAYS = 14

#: 一次最多问几句。
CHECK_IN_LIMIT = 8

#: 结果枚举的闭集文案。写在服务层而不是靠 `sa.Enum` 兜底:数据库那个报错的时机是
#: `commit()`,用户看到的是一句和"结果"毫无关系的约束异常。
_RESULT_LABELS = "、".join(member.value for member in ExecutionResult)


@dataclass(frozen=True, slots=True)
class RecordOutcome:
    response: RecordExecutionResponse
    replayed: bool


async def record_execution(
    db: AsyncSession,
    ctx: WorkspaceContext,
    session_id: uuid.UUID,
    *,
    result: str,
    idempotency_key: str,
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
    actual_minutes: int | None = None,
    completion_ratio: float | None = None,
    delay_reason: str | None = None,
    user_feedback: str | None = None,
) -> RecordOutcome:
    """记下用户对这一场的反馈。**只追加。**"""
    parsed_result = _parse_result(result)
    # 失败路径上有 `db.rollback()`,回滚会让会话里所有 ORM 对象过期,之后再读
    # `ctx.workspace.id` 会触发异步上下文里做不到的懒加载(MissingGreenlet),
    # 把一个本该是 409 的业务错误变成 500。所以入口处先把要用的标量取出来。
    user_id = ctx.user.user_id
    workspace_id = ctx.id

    request_hash = _hash(
        {
            "sessionId": str(session_id),
            "result": parsed_result.value,
            "startedAt": started_at,
            "endedAt": ended_at,
            "actualMinutes": actual_minutes,
            "completionRatio": completion_ratio,
            "delayReason": delay_reason,
            "userFeedback": user_feedback,
        }
    )

    # 快路径:这个幂等键已经用过。**先查台账再写** —— 用户的第二次点击不需要再走一遍
    # 业务写入,而先写再查的写法会把"重放"变成一次真实的重复录入。
    prior = await _find_record(db, user_id, idempotency_key)
    if prior is not None:
        return await _replay(db, prior, request_hash)

    session = await _load_session(db, ctx, session_id)
    if session.status in _TOMBSTONE:
        # 已取消 / 已搬走的场次是墓碑。对它说"我做完了"没有意义 —— 那件事要么发生
        # 在另一天(已搬走),要么用户已经不打算做了(已取消)。如实拒绝,并说清楚,
        # 而不是把一条反馈悄悄记在一个不存在的安排上。
        raise InvalidInput("这场安排已经取消或改期了,不能再对它记录执行结果。")

    record = ExecutionRecord(
        user_id=user_id,
        workspace_id=workspace_id,
        session_id=session.id,
        node_id=session.node_id,
        started_at=started_at,
        ended_at=ended_at,
        actual_minutes=actual_minutes,
        # 数据库列是 NUMERIC(4,3)。传 float 进去在某些驱动上会引入浮点尾数,
        # 而"0.30000000000000004 的完成度"会出现在所有下游的展示里。
        completion_ratio=None if completion_ratio is None else round(completion_ratio, 3),
        result=parsed_result,
        delay_reason=_clean(delay_reason),
        user_feedback=_clean(user_feedback),
        idempotency_key=idempotency_key,
        content_hash=request_hash,
        created_at=utcnow(),
    )
    db.add(record)

    try:
        # 先 flush 台账,**再**改场次:唯一约束要在这里就生效。否则两个并发请求会各自
        # 改一遍场次行,而"双击"最终变成两次实际写入。
        await db.flush()
        await _apply_to_session(db, session, parsed_result)
        db.add(
            DomainEvent(
                user_id=user_id,
                workspace_id=workspace_id,
                kind="execution_recorded",
                ref_type="scheduled_session",
                ref_id=session.id,
                payload={
                    "result": parsed_result.value,
                    "actualMinutes": actual_minutes,
                    "completionRatio": completion_ratio,
                    "hasDelayReason": bool(delay_reason),
                },
                created_at=utcnow(),
            )
        )
        await db.commit()
    except IntegrityError:
        # 台账的 (user_id, idempotency_key) 唯一键被撞了 —— 另一个请求用同一个键赢了。
        # 这不是错误,是"你双击的第二下"。回读赢家写好的那一行原样返回。
        await db.rollback()
        prior = await _find_record(db, user_id, idempotency_key)
        if prior is None:
            # 撞的是别的约束(比如外键)。那是真 bug,以原貌暴露,不伪装成幂等命中。
            raise
        return await _replay(db, prior, request_hash)

    return RecordOutcome(
        response=await _response_for(db, record, session, replayed=False), replayed=False
    )


async def _apply_to_session(
    db: AsyncSession, session: ScheduledSession, result: ExecutionResult
) -> None:
    """把这次反馈落到场次行上。

    ## 分钟数是**合计**,不是最后一次

    `actual_minutes` 取这个场次上所有记录的合计。用户分两次做完一场时,行上应该写
    50 而不是 20 —— 而台账里两次反馈都还在,想还原"第一次报了多少"随时查得到。
    合计是可导出的,反过来(只留最后一次)丢掉的是一去不返的事实。
    """
    status = _RESULT_STATUS.get(result)
    total = await db.scalar(
        select(func.sum(ExecutionRecord.actual_minutes)).where(
            ExecutionRecord.session_id == session.id
        )
    )

    values: dict[str, object] = {"actual_minutes": None if total is None else int(total)}
    if status is not None:
        values["status"] = status
        # `completed_at` 与状态**一起**改。留一个"已完成但没有完成时间"的行,复盘时
        # 就算不出"这件事实际做了多久" —— 而那正是复盘唯一有用的数字。
        values["completed_at"] = utcnow() if status is ScheduledSessionStatus.DONE else None

    await db.execute(
        update(ScheduledSession)
        .where(ScheduledSession.id == session.id, ScheduledSession.user_id == session.user_id)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    # 内存里那一份也要跟上:响应还要用它。CAS 用的 `synchronize_session=False`,
    # 不刷新的话返回给客户端的是改动前的旧状态。
    for field, value in values.items():
        setattr(session, field, value)


async def _response_for(
    db: AsyncSession,
    record: ExecutionRecord,
    session: ScheduledSession,
    *,
    replayed: bool,
) -> RecordExecutionResponse:
    return RecordExecutionResponse(
        saved=True,
        record=to_view(record),
        session=await session_payload(db, session),
        replayed=replayed,
        node_remaining_sessions=await _count_sessions(
            db, session.node_id, (ScheduledSessionStatus.PLANNED, ScheduledSessionStatus.IN_PROGRESS)
        ),
        node_completed_sessions=await _count_sessions(
            db, session.node_id, (ScheduledSessionStatus.DONE,)
        ),
    )


async def _replay(
    db: AsyncSession, record: ExecutionRecord, request_hash: str
) -> RecordOutcome:
    """返回上一次那一条记录。

    ## 与提案/排期不一样的地方:这里不需要存一份响应体

    那条路径上响应里带着"这次一共创建了几个节点"这类**当时算出来**的数字,事后重新
    计算是不可能的,所以必须把整份响应存进台账。执行反馈不存 —— 台账那一行本身就
    是所有字段的全部真相,回读它就是最初那一次的结果。

    唯一的例外是响应里的 `session`:它是一份**会变的投影**(用户后来又报了别的结果),
    所以重放时给的是"这场现在什么样",而不是"当时什么样"。这是有意的:一个重放的
    响应不该断言一件已经过去的事,而它必须与用户此刻在屏幕上看到的场次一致。

    内容哈希对不上,说明这个幂等键被用在了另一个请求上。那时如果照样返回上次的结果,
    用户会看到**另一次反馈**被"记录成功" —— 所以必须拦下来。
    """
    if record.content_hash != request_hash:
        raise IdempotencyKeyReused("这个幂等键已经用在另一次执行反馈上了,请换一个。")
    session = await db.get(ScheduledSession, record.session_id) if record.session_id else None
    if session is None:
        # 场次被删掉了,但记录还在(台账的 session_id 是 ON DELETE SET NULL,正常
        # 情况下不会走到这里)。响应照常给出记录本身,`session` 为空。
        logger.warning("执行记录 %s 指向的场次已经不在了。", record.id)
        return RecordOutcome(
            response=RecordExecutionResponse(
                saved=True, record=to_view(record), session=None, replayed=True
            ),
            replayed=True,
        )
    return RecordOutcome(
        response=await _response_for(db, record, session, replayed=True), replayed=True
    )


def to_view(record: ExecutionRecord) -> ExecutionRecordView:
    return ExecutionRecordView(
        id=record.id,
        session_id=record.session_id,
        node_id=record.node_id,
        workspace_id=record.workspace_id,
        result=record.result.value,
        actual_minutes=record.actual_minutes,
        completion_ratio=(
            None if record.completion_ratio is None else float(record.completion_ratio)
        ),
        started_at=record.started_at,
        ended_at=record.ended_at,
        delay_reason=record.delay_reason,
        user_feedback=record.user_feedback,
        created_at=record.created_at,
    )


async def list_records(
    db: AsyncSession, ctx: WorkspaceContext, session_id: uuid.UUID, *, limit: int = 50
) -> list[ExecutionRecord]:
    """一个场次的全部反馈,新的在前。**历史只读。**"""
    await _load_session(db, ctx, session_id)  # 归属校验,顺带确认它存在
    result = await db.execute(
        select(ExecutionRecord)
        .where(ExecutionRecord.session_id == session_id, ExecutionRecord.user_id == ctx.user.user_id)
        .order_by(ExecutionRecord.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars())


# ---------------------------------------------------------------------------------
# 今天
# ---------------------------------------------------------------------------------
async def load_today(db: AsyncSession, user: CurrentUser, timezone: str) -> TodayResponse:
    """这个账号**全部活动空间**今天要做的事。

    按人聚合而不是按空间:同一晚只能做一件事,而用户打开界面时问的是"我今天要做什么",
    不是"我这个空间今天要做什么"。逐空间看的话,两个空间各有一场 60 分钟,而他只有
    两个小时 —— 那个冲突在任何一个单独的空间里都看不出来。

    归档的空间不参与 —— 这也正是"归档"该有的效果:一个归档之后仍然每天早上出现在
    「今天」里的空间,等于没归档。
    """
    today = today_in(timezone)
    result = await db.execute(
        select(ScheduledSession, PlanNode, Workspace)
        .join(PlanNode, PlanNode.id == ScheduledSession.node_id)
        .join(Workspace, Workspace.id == ScheduledSession.workspace_id)
        .where(
            ScheduledSession.user_id == user.user_id,
            ScheduledSession.scheduled_date == today,
            ScheduledSession.status.not_in(_TOMBSTONE),
            PlanNode.deleted_at.is_(None),
            Workspace.status == WorkspaceStatus.ACTIVE,
        )
        .order_by(
            Workspace.title.asc(), ScheduledSession.seq.asc(), ScheduledSession.created_at.asc()
        )
    )
    rows = result.all()

    latest = await _latest_records(db, [session.id for session, _, _ in rows])
    by_workspace: dict[uuid.UUID, TodayWorkspaceView] = {}
    planned = actual = recorded = 0

    for session, node, workspace in rows:
        record = latest.get(session.id)
        item = TodayItemView(
            session_id=session.id,
            node_id=node.id,
            workspace_id=workspace.id,
            workspace_title=workspace.title,
            node_title=node.title,
            planned_minutes=session.planned_minutes,
            buffer_minutes=session.buffer_minutes,
            seq=session.seq,
            start_minute=session.start_minute,
            end_minute=session.end_minute,
            status=session.status.value,
            locked=session.locked,
            result=None if record is None else record.result.value,
            actual_minutes=None if record is None else record.actual_minutes,
            delay_reason=None if record is None else record.delay_reason,
            recorded=record is not None,
        )
        bucket = by_workspace.get(workspace.id)
        if bucket is None:
            bucket = TodayWorkspaceView(workspace_id=workspace.id, title=workspace.title)
            by_workspace[workspace.id] = bucket
        bucket.items.append(item)

        planned += session.planned_minutes
        if record is not None:
            recorded += 1
            # 只有真的报了分钟数才算进"今天投入了多久"。没记录的场次按 0 计会让这个
            # 数字在下午就变成一个假的低值 —— 用户会据此以为自己今天什么都没干。
            actual += record.actual_minutes or 0

    questions = await load_check_in_questions(db, user, timezone)
    return TodayResponse(
        today=today,
        timezone=timezone,
        workspaces=list(by_workspace.values()),
        planned_minutes=planned,
        actual_minutes=actual,
        item_count=sum(len(view.items) for view in by_workspace.values()),
        recorded_count=recorded,
        check_in_questions=questions,
        note=(
            "这里只有**排进日历的安排**。没有记录的那些不是「没做」——"
            "我们不知道你做了没有,所以下面会问一句。"
        ),
    )


async def load_check_in_questions(
    db: AsyncSession,
    user: CurrentUser,
    timezone: str,
    *,
    workspace_id: uuid.UUID | None = None,
) -> list[CheckInQuestion]:
    """过去这些天里"没有记录"的那些场次 —— 一句一句问回去。

    ## 为什么这是一句提问,而不是一条偏差

    "没有记录"里混着两种完全相反的事实:用户做完了忘了记,和用户确实没做。系统分不出
    来,而分不出来的东西就不该被当成事实参与计算。所以它在这里以**问题**的形式出现,
    用户回答之后它才变成一条记录,才可能进入偏差分析。

    ## 过滤条件里那两个"and"

    状态过滤(`planned` / `in_progress`)与"没有记录"两个条件都要有:
    - 只看状态的话,一个手工改成 `planned` 但已经报过"部分完成"的场次会被重复问;
    - 只看有没有记录的话,一场已经 `done` 但记录被外力清掉的场次会被问"做了吗"。

    归档空间、软删除节点上的场次同样不问 —— 用户已经不关心它们了。

    `workspace_id` 给出来时只看那一个空间。「今天」不带(它按人聚合),而复盘那份带 ——
    偏差是要变成一份**属于某个空间**的提案的,把别的空间的问题混进来,用户确认之后
    会发现另一个空间被改了。
    """
    today = today_in(timezone)
    earliest = today - timedelta(days=CHECK_IN_DAYS)
    conditions = [
        ScheduledSession.user_id == user.user_id,
        ScheduledSession.scheduled_date < today,
    ]
    if workspace_id is not None:
        conditions.append(ScheduledSession.workspace_id == workspace_id)
    rows = await db.execute(
        select(ScheduledSession, PlanNode, Workspace)
        .join(PlanNode, PlanNode.id == ScheduledSession.node_id)
        .join(Workspace, Workspace.id == ScheduledSession.workspace_id)
        .where(
            *conditions,
            ScheduledSession.scheduled_date >= earliest,
            ScheduledSession.status.in_(
                (ScheduledSessionStatus.PLANNED, ScheduledSessionStatus.IN_PROGRESS)
            ),
            PlanNode.deleted_at.is_(None),
            Workspace.status == WorkspaceStatus.ACTIVE,
            ~exists(
                select(ExecutionRecord.id).where(
                    ExecutionRecord.session_id == ScheduledSession.id
                )
            ),
        )
        # 最近的先问。昨天那场用户还记得,而三周前那场他多半只能猜 ——
        # 猜出来的答案作为"事实"记下来,比不问更糟。
        .order_by(ScheduledSession.scheduled_date.desc(), ScheduledSession.seq.asc())
        .limit(CHECK_IN_LIMIT)
    )
    return [
        CheckInQuestion(
            session_id=session.id,
            node_id=node.id,
            workspace_id=workspace.id,
            node_title=node.title,
            scheduled_date=session.scheduled_date,
            days_ago=(today - session.scheduled_date).days,
            planned_minutes=session.planned_minutes,
            question=(
                f"{_describe_day(session.scheduled_date, today)}那场 "
                f"{session.planned_minutes} 分钟的「{node.title}」还没有记录 —— 做了吗?"
            ),
        )
        for session, node, workspace in rows.all()
    ]


def _describe_day(day: date, today: date) -> str:
    """把日期说成一句人话。绝对日期与相对天数一起给 —— 前者可核对,后者能读快。"""
    delta = (today - day).days
    if delta == 1:
        return "昨天"
    if delta == 2:
        return "前天"
    weekday = "一二三四五六日"[day.weekday()]
    return f"{day.isoformat()}(周{weekday},{delta} 天前)"


async def _latest_records(
    db: AsyncSession, session_ids: list[uuid.UUID]
) -> dict[uuid.UUID, ExecutionRecord]:
    """每个场次**最近一次**反馈。

    "最近一次"由 `created_at` 决定。同一毫秒内写入两条时次序不确定,而那种情况下
    两条记录本来就来自同一个并发窗口 —— 取哪一条都不影响"用户报过什么"这件事,
    因为两条是同一个人在同一时刻说的。
    """
    if not session_ids:
        return {}
    rows = await db.execute(
        select(ExecutionRecord)
        .where(ExecutionRecord.session_id.in_(session_ids))
        .order_by(ExecutionRecord.created_at.desc())
    )
    latest: dict[uuid.UUID, ExecutionRecord] = {}
    for record in rows.scalars():
        if record.session_id is not None and record.session_id not in latest:
            latest[record.session_id] = record
    return latest


async def _count_sessions(
    db: AsyncSession, node_id: uuid.UUID, statuses: tuple[ScheduledSessionStatus, ...]
) -> int:
    total = await db.scalar(
        select(func.count(ScheduledSession.id)).where(
            ScheduledSession.node_id == node_id, ScheduledSession.status.in_(statuses)
        )
    )
    return int(total or 0)


async def session_payload(db: AsyncSession, session: ScheduledSession) -> SessionPayload:
    """一行场次 -> 线上形状。与 `/plan` 里那些场次用的是同一个转换函数。

    **共用一个转换函数**是有意的:同一个场次在两个接口里必须是同一个形状,否则
    界面上会出现"从「今天」点进去看到的和从时间线看到的不一样"这种没人能解释的差异。
    """
    title = await db.scalar(select(PlanNode.title).where(PlanNode.id == session.node_id))
    return SessionPayload.model_validate(plan_service.session_to_dict(session, title or ""))


async def _load_session(
    db: AsyncSession, ctx: WorkspaceContext, session_id: uuid.UUID
) -> ScheduledSession:
    """取一场**属于这个空间**的安排。

    归属写进 WHERE:`workspace_id` 与 `user_id` 都不匹配就查不到,查不到就是
    `SessionNotFound`。取出来再比一次归属的话,"忘记比"会成为一种可能的代码路径。
    """
    session = await db.scalar(
        select(ScheduledSession).where(
            ScheduledSession.id == session_id,
            ScheduledSession.workspace_id == ctx.id,
            ScheduledSession.user_id == ctx.user.user_id,
        )
    )
    if session is None:
        raise SessionNotFound("没有找到这场安排。")
    return session


async def _find_record(
    db: AsyncSession, user_id: uuid.UUID, idempotency_key: str
) -> ExecutionRecord | None:
    return await db.scalar(
        select(ExecutionRecord).where(
            ExecutionRecord.user_id == user_id,
            ExecutionRecord.idempotency_key == idempotency_key,
        )
    )


def _parse_result(value: str) -> ExecutionResult:
    try:
        return ExecutionResult(value)
    except ValueError:
        raise InvalidInput(f"「{value}」不是一个执行结果。可选:{_RESULT_LABELS}。") from None


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None


def _hash(value: object) -> str:
    """规范化 JSON 的 blake2b。键排序,保证"同样的内容"永远得到同一个哈希。"""
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.blake2b(encoded.encode("utf-8"), digest_size=32).hexdigest()


__all__ = [
    "CHECK_IN_DAYS",
    "CHECK_IN_LIMIT",
    "RecordOutcome",
    "list_records",
    "load_check_in_questions",
    "load_today",
    "record_execution",
    "session_payload",
    "to_view",
]
