"""战略复评触发器:**只提醒,不自动改战略**。

## 它做什么、不做什么

当出现"可能值得重审战略"的信号时,它创建(或建议创建)一个**问题节点**,
例如「这连续几周的偏差是否说明需要重审当前重点?」。用户怎么答、要不要改战略,
仍然走既有的 `问题 -> 后续对话 -> proposal -> 用户确认` 链路。

它**绝不**:

- 自动改写 strategy 节点;
- 把一次延期当成"需要重审战略";
- 把一次失败推断成"不自律"或"目标不适合";
- 在用户没回答、没确认的情况下调整战略优先级。

## 允许的触发类别(只有这三类)

1. 用户明确修改目标、截止日、长期可用时间、关键约束或资源;
2. 连续多个周/周期的执行失败,或实际耗时显著偏离计划;
3. 用户明确报告重大外部机会或限制变化。

第 1、2 类在这里做确定性判断(可断言、可审计)。第 3 类无法由数据库确定性地判定 ——
它由**模型通过正常的 `questions` 机制**提出,不在本模块。这一条如实写在文档里,
不假装它被服务端实现了。

## 触发依据必须能说出口

每条触发都带一句人话理由(例如「最近 21 天里有 3 场标记为没做完」),它会被写进
问题卡的 `whyNow`。不写理由的"AI 觉得你应该换方向"是这一期明确不要的东西。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agent.runtime.base import QuestionDraft, QuestionOptionDraft
from backend.db.models import AgentQuestion, ExecutionRecord, PlanNode, ScheduledSession
from backend.db.models.enums import (
    ExecutionResult,
    PlanningLevel,
    ScheduledSessionStatus,
)
from backend.services import question_service
from backend.services.context import WorkspaceContext
from backend.services.timeutil import today_in

logger = logging.getLogger(__name__)

#: 观察窗口。连续偏差按这段时间统计。
REVIEW_WINDOW_DAYS = 21
#: 窗口内至少这么多场"没完成/失败/只做了一部分"才认为够得上"连续多个周期"。
#: **一次延期绝不触发** —— 这条阈值就是那句话的实现。
CONSECUTIVE_SETBACKS = 3
#: 实际耗时超过计划这么多倍时,单独也算一条"显著偏离"。
ACTUAL_OVERRUN_RATIO = 1.5

#: 复评问题的固定文案。**判重靠它** —— 同一句还没被处理时不会重复创建。
REVIEW_QUESTION = "最近的变化是否说明需要重审当前的战略重点?"
REVIEW_WHY_PREFIX = "系统读到:"


@dataclass(frozen=True, slots=True)
class StrategyReviewTrigger:
    """一条可审计的复评信号。`reason` 是给用户看的事实依据。"""

    code: str
    reason: str


#: 长期字段 -> 说给用户听的那半句。用户动这些字段时,值得问一次"要不要重审战略"。
_LONG_TERM_FIELDS: dict[str, str] = {
    "goal": "你修改了目标",
    "deadline": "你修改了截止时间",
    "weekly_available_minutes": "你修改了长期可投入时间",
    "constraints": "你修改了关键约束或资源",
}


def long_term_change_reason(changed_fields: tuple[str, ...] | list[str]) -> str | None:
    """触发类别 1:用户明确改了长期条件。纯函数,便于测试。"""
    hits = [phrase for field, phrase in _LONG_TERM_FIELDS.items() if field in changed_fields]
    if not hits:
        return None
    return "、".join(hits) + "。这可能改变战略取舍,值得确认一次。"


async def detect_review_trigger(
    db: AsyncSession, ctx: WorkspaceContext
) -> StrategyReviewTrigger | None:
    """触发类别 2:连续多周期失败 / 实际耗时显著偏离。**只读。**"""
    today = today_in(ctx.timezone)
    earliest = today - timedelta(days=REVIEW_WINDOW_DAYS)
    live = select(PlanNode.id).where(
        PlanNode.workspace_id == ctx.id, PlanNode.deleted_at.is_(None)
    )
    rows = await db.execute(
        select(ExecutionRecord.result, ExecutionRecord.actual_minutes, ScheduledSession.planned_minutes)
        .join(ScheduledSession, ScheduledSession.id == ExecutionRecord.session_id)
        .where(
            ExecutionRecord.user_id == ctx.user.user_id,
            ExecutionRecord.workspace_id == ctx.id,
            ScheduledSession.scheduled_date >= earliest,
            ScheduledSession.status.not_in(
                (ScheduledSessionStatus.CANCELED, ScheduledSessionStatus.MOVED)
            ),
            ExecutionRecord.node_id.in_(live),
        )
    )

    setbacks = 0
    overruns = 0
    for result, actual_minutes, planned_minutes in rows.all():
        if result != ExecutionResult.COMPLETED:
            setbacks += 1
        if (
            actual_minutes is not None
            and planned_minutes
            and actual_minutes > planned_minutes * ACTUAL_OVERRUN_RATIO
        ):
            overruns += 1

    if setbacks >= CONSECUTIVE_SETBACKS:
        return StrategyReviewTrigger(
            code="CONSECUTIVE_SETBACKS",
            reason=(
                f"最近 {REVIEW_WINDOW_DAYS} 天里有 {setbacks} 场标记为没完成、"
                "只做了一部分或做了没成。这可能是局部安排的问题,也可能值得重审战略重点。"
            ),
        )
    if overruns >= CONSECUTIVE_SETBACKS:
        return StrategyReviewTrigger(
            code="ACTUAL_TIME_OVERRUN",
            reason=(
                f"最近 {REVIEW_WINDOW_DAYS} 天里有 {overruns} 场的实际用时明显超过计划。"
                "如果这不是偶然,可能说明当前战略取舍与真实投入不匹配。"
            ),
        )
    return None


async def has_strategy(db: AsyncSession, ctx: WorkspaceContext) -> bool:
    """这个空间里有没有一个活着的战略节点。没有战略时\"重审战略\"没有对象。"""
    found = await db.scalar(
        select(PlanNode.id)
        .where(
            PlanNode.workspace_id == ctx.id,
            PlanNode.deleted_at.is_(None),
            PlanNode.planning_level == PlanningLevel.STRATEGY,
        )
        .limit(1)
    )
    return found is not None


async def create_review_question(
    db: AsyncSession,
    ctx: WorkspaceContext,
    *,
    reason: str,
    source_node_id=None,
    source_message_id=None,
) -> AgentQuestion | None:
    """把一条触发信号变成一张待回答的问题卡。**不碰任何计划节点。**

    判重由 `question_service.create_from_drafts` 负责(同一句 pending 不再创建)。
    """
    draft = QuestionDraft(
        question=REVIEW_QUESTION,
        why_now=f"{REVIEW_WHY_PREFIX}{reason}",
        response_mode="mixed",
        options=(
            QuestionOptionDraft(id="review", label="是,一起重审战略"),
            QuestionOptionDraft(id="keep", label="不用,保持现在的方向"),
            QuestionOptionDraft(id="local", label="只想调整局部安排"),
        ),
        allow_custom_input=True,
    )
    created = await question_service.create_from_drafts(
        db,
        ctx,
        (draft,),
        source_message_id=source_message_id,
        source_node_id=source_node_id,
    )
    return created[0] if created else None


async def maybe_create_review_question(
    db: AsyncSession,
    ctx: WorkspaceContext,
    *,
    changed_fields: tuple[str, ...] | list[str] = (),
    source_node_id=None,
    source_message_id=None,
) -> AgentQuestion | None:
    """触发类别 1 与 2 的合并入口。

    只在**已经有战略**时才问"要不要重审战略" —— 没有战略时该做的是先提一个战略选择。
    """
    if not await has_strategy(db, ctx):
        return None
    reason = long_term_change_reason(changed_fields)
    trigger: StrategyReviewTrigger | None = None
    if reason is None:
        trigger = await detect_review_trigger(db, ctx)
        reason = trigger.reason if trigger is not None else None
    if reason is None:
        return None
    return await create_review_question(
        db,
        ctx,
        reason=reason,
        source_node_id=source_node_id,
        source_message_id=source_message_id,
    )


__all__ = [
    "ACTUAL_OVERRUN_RATIO",
    "CONSECUTIVE_SETBACKS",
    "REVIEW_QUESTION",
    "REVIEW_WINDOW_DAYS",
    "StrategyReviewTrigger",
    "create_review_question",
    "detect_review_trigger",
    "has_strategy",
    "long_term_change_reason",
    "maybe_create_review_question",
]
