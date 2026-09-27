"""复盘:从执行情况里读出偏差,并请模型提一份调整方案。

## 分成两层,因为它们的可靠性完全不同

```
① 偏差检测   纯查询,确定性,不需要模型        —— 永远可用
② 调整方案   把事实交给模型,换回一批变更      —— 模型不在时什么都不给
```

第 ① 层是这个模块的实体。用户打开复盘,他至少能看到"这周你有三场没记录、两场记成了
没做完、一个阶段的截止日过了" —— 这些是数据库里的事实,与模型在不在没有任何关系。

第 ② 层是加分项。它**不允许**在模型不可用时退化成一个规则生成的调整方案:
`rule_fallback` 那条纪律是"禁止编造计划内容",而这里最容易犯的错就是"没有模型,
那我们按规则挪几天吧" —— 用户会把那份调整当成系统的判断,而它其实什么依据都没有。
所以模型不可用时的正确表现是:给出事实,说明这次没能生成方案,允许重试。

## 没有偏差时不调模型

"你这周排的 5 场里 4 场都记录了,计划是跟得上你的"是一个完全正常的结论。为了这个
结论去调一次模型,既花钱,又会换回一个为了有话可说而硬凑出来的调整建议。

## 这份提案走的是和阶段 4 完全相同的路径

`proposal_service.build_from_actions` → 校验 → 落一条 `validated` 的提案 → 用户点确认
→ `confirm_proposal` 那一个事务。这里**没有第二条写入计划的路**,也没有第二种确认
方式。区别只有一处:`trigger_type` 记成 `execution_deviation`,于是它在版本历史里
看得见"这次调整是因为执行情况"。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agent.runtime.base import Reasoner
from backend.contracts.review import DeviationsResponse, DeviationView, ReplanResponse
from backend.db.models import ExecutionRecord, PlanNode, ScheduledSession
from backend.db.models.enums import (
    ExecutionResult,
    NodeStatus,
    RevisionTrigger,
    ScheduledSessionStatus,
)
from backend.services import conversation_service, execution_service, proposal_service
from backend.services.context import WorkspaceContext
from backend.services.timeutil import today_in
from backend.services.turn_context import build_turn_context

logger = logging.getLogger(__name__)

#: 偏差最多列这么多条。再多就变成一堵墙了,而用户能处理的调整是有限的 ——
#: 一次给二十条"你这里没做到",结果是全都无视。
MAX_DEVIATIONS = 20

#: 已经报过"没做"的场次,多久之内还算一条值得提醒的偏差。超过这个天数,那件事
#: 要么已经不重要了,要么早该由用户自己重新决定要不要做。
RECORDED_WINDOW_DAYS = 30

#: 截止日过了多久还算"刚过期"。三个月前就到期的任务不叫偏差,叫废案 ——
#: 把它翻出来只会让真正要紧的那几条被淹掉。
OVERDUE_WINDOW_DAYS = 60


@dataclass(frozen=True, slots=True)
class Deviation:
    """一条偏差事实。与 `DeviationView` 一一对应,单独存在是为了让服务层不依赖契约层。"""

    code: str
    detail: str
    workspace_id: uuid.UUID | None = None
    node_id: uuid.UUID | None = None
    node_title: str = ""
    session_id: uuid.UUID | None = None
    is_question: bool = False
    days_ago: int | None = None
    facts: dict | None = None

    def to_view(self) -> DeviationView:
        return DeviationView(
            code=self.code,
            workspace_id=self.workspace_id,
            node_id=self.node_id,
            node_title=self.node_title,
            session_id=self.session_id,
            detail=self.detail,
            is_question=self.is_question,
            days_ago=self.days_ago,
            facts=self.facts or {},
        )


async def detect(db: AsyncSession, ctx: WorkspaceContext) -> tuple[Deviation, ...]:
    """这个空间里所有**确定性**的偏差事实。**只读,不调模型。**"""
    today = today_in(ctx.timezone)
    deviations: list[Deviation] = []

    # ① 过去了、却没有记录的场次。**这是一句提问,不是一条偏差。**
    for question in await execution_service.load_check_in_questions(
        db, ctx.user, ctx.timezone, workspace_id=ctx.id
    ):
        deviations.append(
            Deviation(
                code="UNRECORDED_PAST_SESSION",
                workspace_id=question.workspace_id,
                node_id=question.node_id,
                node_title=question.node_title,
                session_id=question.session_id,
                detail=question.question,
                # 见 `DeviationView.is_question`:没有记录 ≠ 没完成。
                is_question=True,
                days_ago=question.days_ago,
                facts={
                    "scheduledDate": question.scheduled_date.isoformat(),
                    "plannedMinutes": question.planned_minutes,
                },
            )
        )

    deviations.extend(await _recorded_deviations(db, ctx, today))
    deviations.extend(await _overdue_nodes(db, ctx, today))
    return tuple(deviations[:MAX_DEVIATIONS])


async def _recorded_deviations(
    db: AsyncSession, ctx: WorkspaceContext, today: date
) -> list[Deviation]:
    """用户**说过**的那些结果 —— 跳过、做了一部分、做失败了。

    这一批是真正的偏差:它们是用户的陈述,不是系统的推断。过了这些天还没有被重新安排
    的,才值得提出来 —— 一场上周报"没做"、今天已经被重排到下周三的安排,没有什么
    可复盘的。
    """
    earliest = today - timedelta(days=RECORDED_WINDOW_DAYS)
    rows = await db.execute(
        select(ExecutionRecord, ScheduledSession, PlanNode)
        .join(ScheduledSession, ScheduledSession.id == ExecutionRecord.session_id)
        .join(PlanNode, PlanNode.id == ExecutionRecord.node_id)
        .where(
            ExecutionRecord.user_id == ctx.user.user_id,
            ExecutionRecord.workspace_id == ctx.id,
            ExecutionRecord.result != ExecutionResult.COMPLETED,
            # 只看最近这些天。用 `scheduled_date` 而不是 `created_at` 划界:用户
            # 补记一场上个月的安排,那条记录是"今天写的",但事情发生在上个月 ——
            # 该不该翻出来由**事情**发生在什么时候决定。
            ScheduledSession.scheduled_date >= earliest,
            # 已经重排过的那些(取消/搬走)不再是问题:用户已经处理过了。
            ScheduledSession.status.not_in(
                (ScheduledSessionStatus.CANCELED, ScheduledSessionStatus.MOVED)
            ),
            PlanNode.deleted_at.is_(None),
        )
        .order_by(ExecutionRecord.created_at.desc())
    )

    labels = {
        ExecutionResult.SKIPPED: "记成了「这场没做」",
        ExecutionResult.PARTIAL: "记成了「只做了一部分」",
        ExecutionResult.FAILED: "记成了「做了但没成」",
    }
    deviations: list[Deviation] = []
    seen: set[uuid.UUID] = set()
    for record, session, node in rows.all():
        if session.id in seen:
            continue
        seen.add(session.id)
        detail = (
            f"「{node.title}」{session.scheduled_date.isoformat()} 那场 {session.planned_minutes} 分钟"
            f",你{labels.get(record.result, '记了一个结果')}"
        )
        if record.delay_reason:
            detail += f",原因是:{record.delay_reason}"
        deviations.append(
            Deviation(
                code="RECORDED_SETBACK",
                workspace_id=ctx.id,
                node_id=node.id,
                node_title=node.title,
                session_id=session.id,
                detail=detail + "。",
                is_question=False,
                days_ago=(today - session.scheduled_date).days,
                facts={
                    "result": record.result.value,
                    "plannedMinutes": session.planned_minutes,
                    "actualMinutes": record.actual_minutes,
                    "delayReason": record.delay_reason,
                },
            )
        )
    return deviations


async def _overdue_nodes(db: AsyncSession, ctx: WorkspaceContext, today: date) -> list[Deviation]:
    """截止日已经过去、节点还没完成 —— 一条客观的偏差。"""
    earliest = today - timedelta(days=OVERDUE_WINDOW_DAYS)
    rows = await db.execute(
        select(PlanNode)
        .where(
            PlanNode.workspace_id == ctx.id,
            PlanNode.deleted_at.is_(None),
            PlanNode.deadline.is_not(None),
            PlanNode.deadline < today,
            PlanNode.deadline >= earliest,
            PlanNode.status.not_in((NodeStatus.COMPLETED, NodeStatus.ARCHIVED)),
        )
        .order_by(PlanNode.deadline.asc())
    )
    deviations: list[Deviation] = []
    for node in rows.scalars():
        days = (today - node.deadline).days
        deviations.append(
            Deviation(
                code="OVERDUE_NODE",
                workspace_id=ctx.id,
                node_id=node.id,
                node_title=node.title,
                detail=(
                    f"「{node.title}」的截止日是 {node.deadline.isoformat()},"
                    f"已经过去 {days} 天,还没有标记完成。"
                ),
                is_question=False,
                days_ago=days,
                facts={"deadline": node.deadline.isoformat()},
            )
        )
    return deviations


async def list_deviations(db: AsyncSession, ctx: WorkspaceContext) -> DeviationsResponse:
    """只看偏差事实。**不碰模型,永远可用。**

    这是复盘里唯一能承诺"一定有东西看"的部分 —— 它是纯查询,模型在不在、有没有 key、
    额度够不够,都与它无关。
    """
    deviations = await detect(db, ctx)
    questions = sum(1 for item in deviations if item.is_question)

    if not deviations:
        note = "没有发现需要调整的地方:没有过期的节点,最近的安排要么有记录、要么已经重排过了。"
    elif questions:
        note = (
            f"其中 {questions} 条是还没有记录的场次 —— 没有记录不等于没完成,"
            "这些要问你,不能替你算。"
        )
    else:
        note = "这些都是你自己记过结果的情况,或者已经过了截止日的事实。"

    return DeviationsResponse(
        deviations=[item.to_view() for item in deviations],
        analyzed_for=today_in(ctx.timezone),
        question_count=questions,
        note=note,
    )


async def propose_replan(
    db: AsyncSession, ctx: WorkspaceContext, reasoner: Reasoner
) -> ReplanResponse:
    """按执行情况重规划。**模型的方案要用户确认之后才会生效。**"""
    today = today_in(ctx.timezone)
    deviations = await detect(db, ctx)

    if not deviations:
        # 没有偏差就不请模型。这一步不是优化,是纪律:为了"有没有建议"去问一次模型,
        # 得到的会是一个为了有话可说而硬凑出来的调整。
        return ReplanResponse(
            deviations=[],
            consulted_model=False,
            analyzed_for=today,
            message=(
                "这个空间里没有发现需要调整的地方:没有过期的节点,"
                "最近的安排要么有记录、要么已经被重新排过了。"
            ),
        )

    questions = sum(1 for item in deviations if item.is_question)
    conversation = await conversation_service.get_or_create_primary_conversation(db, ctx)
    turn = await build_turn_context(
        db,
        ctx,
        conversation_id=conversation.id,
        user_message=_replan_request(deviations, questions),
        current_view="schedule",
    )
    result = await reasoner.reason(turn)

    # 助手那句回复与提案在**同一个事务**里提交 —— 分开提交的话,刷新页面正好赶上中间
    # 那一刻的请求,会看到一句说着"我给你调整了一下"、却找不到任何提案的回复。
    #
    # **降级时不落这条回复。** 对话是"用户和助手说过的话"的记录,而模型这次什么都没说
    # 成 —— 把一句系统拼的"模型暂时不可用"当成助手的发言写进对话里,用户下次翻聊天
    # 记录会看到 AI 在一个他根本没说话的时刻开口。这次没能生成方案,由响应里的
    # `message` 如实说明就够了,那不是对话的一部分。
    message = (
        None
        if result.degraded
        else await conversation_service.append_reply(
            db, ctx, conversation=conversation, result=result
        )
    )

    # **不复用 `apply_claims`。** 简报里的条件只能由用户自己说的话来更新,而这里
    # 模型看到的那段文字是系统替用户写的 —— 模型完全可能把它读成"用户说过每周 4 小时"
    # 并标成 `user_stated`,那会静默改掉真正驱动排期的那一列。
    outcome = await proposal_service.build_from_actions(
        db,
        ctx,
        conversation_id=conversation.id,
        actions=result.actions,
        handles=turn.node_handles,
        # 复盘是**工作区级**的动作(用户在「排期」里点的那一下),没有"我在哪一层"
        # 可言,所以上面那个 turn 不带焦点,可改集自然就是整个空间。这里仍然把
        # `turn.writable_handles` 传下去,而不是留空:范围这件事只有一处定义,
        # 免得以后有人给复盘加上子空间视角、却忘了这个参数还是 None。
        writable_handles=turn.writable_handles,
        reasoning=result.reply,
        assistant_message=message,
        trigger_type=RevisionTrigger.EXECUTION_DEVIATION,
    )
    await db.commit()

    return ReplanResponse(
        deviations=[item.to_view() for item in deviations],
        consulted_model=True,
        proposal=(
            None if outcome.proposal is None else await proposal_service.view_of(db, outcome.proposal)
        ),
        proposal_errors=list(outcome.errors),
        source=result.source.value,
        degraded=result.degraded,
        degraded_reason=(
            None if result.degraded_reason is None else str(result.degraded_reason)
        ),
        retryable=result.retryable,
        analyzed_for=today,
        message=_summarize(
            deviations,
            questions,
            outcome.proposal,
            result.degraded,
            # 模型这一轮**有没有说话**。降级时那句"没能给出方案"已经覆盖了,
            # 这里只区分"有模型、有回复、但没提变更"和"它什么都没说"。
            replied=bool((result.reply or "").strip()),
        ),
    )


def _replan_request(deviations: tuple[Deviation, ...], question_count: int) -> str:
    """把事实渲染成一段送给模型的文字。

    ## 为什么用"用户消息"这个通道

    模型的输入契约只有 `TurnContext` 这一种,而它已经带着当前日期、空间目标、已知
    条件和真实节点表 —— 为了复盘再开一条并行的输入通道,等于把"什么是模型能看到的"
    这份纪律复制成两份,而它们一定会漂移。

    所以这里把事实渲染成一条**明确标注了来源**的消息。标注是必须的:如果它看起来像
    用户说的话,模型会以为用户确认过这些事实,甚至从中"读出"用户没说过的条件。
    """
    lines = [
        "【系统发起的一次复盘,不是用户说的话】",
        "用户点了「按执行情况调整计划」。以下是系统从记录里读出来的事实:",
        "",
    ]
    lines += [f"{index}. {item.detail}" for index, item in enumerate(deviations, start=1)]
    lines += ["", "请只根据上面这些事实提出调整建议。"]
    if question_count:
        lines.append(
            f"其中前 {question_count} 条是**没有记录**的场次 —— 没有记录不等于没完成,"
            "不要把它们当成用户没做到,也不要把它们当成已完成。"
        )
    lines.append("不要新增用户没有提过的东西。如果这些事实不需要调整计划,就不要提任何变更。")
    return "\n".join(lines)


def _summarize(
    deviations: tuple[Deviation, ...],
    question_count: int,
    proposal,
    degraded: bool,
    replied: bool,
) -> str:
    """给用户的一句话。**降级时必须如实说"这次没给出方案"。**

    悄悄返回一个空提案,用户会把它读成"系统看过之后认为不需要调整" —— 而系统其实
    根本没看。

    ## "没提变更"和"不需要调整"不是同一句话

    这里原来在"有模型、没提案"时说的是"模型看过之后认为不需要改动计划"。实测下来
    它是**错的**:模型不提变更的常见原因是**它要先问几个条件**——一个还没说过截止
    时间和每周投入的空间,模型会回一句"先告诉我这三件事,我再动手改",一个 op 都
    不发。

    说成"不需要改动计划",用户就放心地关掉页面,而模型那三个问题正躺在对话里等
    回答。调整这件事于是既没做、也没人知道它没做。

    所以这一刻只如实说"它这次没有提出变更",并且**指向对话** —— 那句解释在那儿。
    两者都有,用户才可能知道下一步该干什么。
    """
    total = len(deviations)
    recorded = total - question_count
    parts = [f"读到 {total} 条值得看一眼的情况"]
    detail_bits = []
    if question_count:
        detail_bits.append(f"{question_count} 条是还没有记录的场次(要问你)")
    if recorded:
        detail_bits.append(f"{recorded} 条是你自己记过结果的")
    if detail_bits:
        parts.append("其中 " + "、".join(detail_bits))
    summary = "，".join(parts) + "。"

    if proposal is not None:
        return summary + "下面这份调整方案还没有生效,你确认之后才会写进计划。"
    if degraded:
        return summary + "但模型这次没能给出调整方案,上面这些事实仍然有效,可以稍后重试。"
    if replied:
        return (
            summary
            + "模型这次没有提出变更,它在「对话」里说了原因"
            "——常见的是还缺几个条件,答完它就能接着改。"
        )
    return summary + "模型这次没有提出任何变更。"


__all__ = [
    "MAX_DEVIATIONS",
    "Deviation",
    "detect",
    "list_deviations",
    "propose_replan",
]
