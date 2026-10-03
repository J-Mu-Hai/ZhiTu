"""问题节点:AI 提问、用户回答、以及回答之后触发的一轮后续对话。

## 三条不能混在一起的边界

1. **问题不是计划写入。** 它建在 `agent_questions` 上,不进 `plan_nodes`,不参与排期、
   依赖、统计,也不出现在画布的计划树里。用户回答问题之后,模型这一轮提的 `actions`
   仍然走**原来那份** `proposal -> 用户确认 -> 事务写入` 的链路。回答本身**永远不直接
   改计划**。

2. **答案是"用户输入事实",不是"用户同意计划变更"。** 所以回答接口只做三件事:
   存答案、推状态、把答案当成一轮普通对话交给 `conversation_service`。

3. **答案先落库,再调模型。** 模型失败(超时、限流、没 key)时,答案与
   `investigating` 状态都已经提交,刷新后读得回。这正是"回答不能静默丢失"的落点。

## 幂等

`client_answer_id` 是回答的幂等键。双击或重试带同一个值时,第二次不会重跑后续处理,
而是回读第一次写好的答案原样返回。后续那一轮的模型调用也用
`client_message_id = "question-answer:{id}"` 再兜一层 —— 即使第一次在"答案已存、
状态已改、模型还没跑完"之间断掉,重试也不会产生第二条用户消息。
"""

from __future__ import annotations

import logging
import unicodedata
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agent.runtime.base import QuestionDraft
from backend.contracts.question import QuestionAnswer, QuestionView
from backend.db.base import utcnow
from backend.db.locking import lock_workspace
from backend.db.models import AgentQuestion, PlanNode
from backend.db.models.enums import (
    ACTIVE_QUESTION_STATUSES,
    QuestionPresentation,
    QuestionResponseMode,
    QuestionStatus,
    QuestionUserAction,
)
from backend.services.context import WorkspaceContext
from backend.services.errors import (
    InvalidQuestionAnswer,
    QuestionNotAnswerable,
    QuestionNotFound,
)

logger = logging.getLogger(__name__)

#: 一轮最多创建几个问题。与 `agent/runtime/response.py::MAX_QUESTIONS` 对齐;
#: 这里是**服务端对模型输出的最终限制**,不依赖提示词是否听话。
MAX_QUESTIONS_PER_TURN = 2

#: 用户补充输入的长度上限。与 `contracts/question.py::MAX_CUSTOM_INPUT_CHARS` 对齐。
MAX_CUSTOM_INPUT_CHARS = 2000

#: 战略阶段**不该问**的东西:精确投入时间、当前水平、具体截止日、日程/每日安排。
#: 这些是阶段/周/排期层信息,不是战略层的默认门槛。
#:
#: 实现是**确定性的关键词判断**,不猜语义:它只拦那些一看就是排期问卷的问法,
#: 宁可漏过也不误杀。误杀一个合理的战略问题(比如“你更看重哪个结果”)比漏掉一个
#: 时间问题更贵 —— 前者直接阻断战略对话,后者只是多问一句。
_STRATEGY_PHASE_FORBIDDEN: tuple[str, ...] = (
    "每周",
    "小时",
    "可投入",
    "投入时间",
    "每天",
    "每日",
    "几点",
    "日程",
    "排期",
    "排得开",
    "工时",
    "截止",
    "deadline",
    "当前水平",
    "什么水平",
    "水平如何",
    "基础如何",
    "掌握程度",
)

#: 阶段 10:战略阶段**只问会改变路线/阶段/成果物/风险策略的问题**。
#: 这些是“一问就掉进执行层”的琐碎题 —— 工具、资料、每天安排、代码细节、措辞偏好。
#: 同样是**确定性关键词**,宁可漏过也不误杀合法的战略取舍。
_STRATEGY_PHASE_TRIVIAL: tuple[str, ...] = (
    "用哪个工具",
    "哪个工具",
    "什么工具",
    "工具链",
    "用什么软件",
    "用哪个软件",
    "ide",
    "编辑器",
    "代码编辑器",
    "用哪本书",
    "哪本书",
    "什么教材",
    "哪个教程",
    "看什么资料",
    "资料清单",
    "每天学",
    "每天花",
    "每周几",
    "周几",
    "几点学",
    "作息",
    "变量名",
    "函数名",
    "类名",
    "代码风格",
    "代码细节",
    "怎么命名",
    "措辞",
    "叫法",
    "怎么称呼",
    "偏好哪种说法",
)


def strategy_phase_question_conflict(question: str) -> str | None:
    """战略阶段问了一个属于阶段/排期层或执行细节的问题时,返回原因;否则 `None`。

    纯函数,便于测试。服务端用它过滤模型输出的问题 —— **不依赖提示词自觉**。
    """
    text = question.strip().lower()
    for token in _STRATEGY_PHASE_FORBIDDEN:
        if token in text:
            return f"战略阶段不先问「{token}」这类阶段/排期信息"
    for token in _STRATEGY_PHASE_TRIVIAL:
        if token in text:
            return f"战略阶段不问「{token}」这类执行细节"
    return None


#: 阶段 10:一个“战略判断卡”必须带的字段。缺了就不写半成品。
_JUDGMENT_REQUIRED_FIELDS: tuple[str, ...] = (
    "analysis_summary",
    "recommendation",
    "decision_impact",
)


def strategy_judgment_missing(draft: QuestionDraft) -> str | None:
    """这个问题的战略判断是否完整。不完整时返回缺的那一项。

    纯函数,便于测试。要求:
    - `analysis_summary` / `recommendation` / `decision_impact` 都非空;
    - 有选项时,至少有一个选项被标为推荐(前端据此明确标“推荐”)。

    没有可信依据时**不要编造** —— 模型应该留空并说明缺少什么,由前端显示
    “当前还不足以给出推荐”。本函数只负责“缺了就不落库”,不做内容判断。
    """
    for field in _JUDGMENT_REQUIRED_FIELDS:
        if not str(getattr(draft, field, "") or "").strip():
            return field
    if draft.options and not any(option.recommended for option in draft.options):
        return "recommended_option"
    return None


@dataclass(frozen=True, slots=True)
class AnswerOutcome:
    """回答接口的结果。

    `turn` 是后续那一轮对话的原始 outcome(`conversation_service.TurnOutcome`),
    由路由层翻译成 `SendMessageResponse`。用 `object` 标注是为了不在这个模块里
    import 对话服务(那会绕成循环 import),真正消费它的是路由。
    """

    question: AgentQuestion
    turn: object | None
    replayed: bool


def _normalize(text: str) -> str:
    """问题的比较形状。只用来判重,不用来显示。

    `NFKC` 处理全角/半角(「排名３８」与「排名38」),`casefold` + 空白压平处理大小写与
    排版。与 `proposal_validation.normalize_title` 同一条纪律:判重看的不是字面相等。
    """
    folded = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(folded.split())


def _options_to_rows(draft: QuestionDraft) -> list[dict[str, object]]:
    return [
        {"id": option.id, "label": option.label, "recommended": bool(option.recommended)}
        for option in draft.options
    ]


def _event(action: QuestionUserAction, *, client_id: str | None = None, reason: str | None = None, answer: dict | None = None) -> dict:
    entry: dict = {"action": action.value, "at": utcnow().isoformat()}
    if client_id:
        entry["clientId"] = client_id
    if reason:
        entry["reason"] = reason
    if answer is not None:
        entry["answer"] = answer
    return entry


# ---------------------------------------------------------------------------------
# 创建(模型输出 -> 持久化)
# ---------------------------------------------------------------------------------
async def create_from_drafts(
    db: AsyncSession,
    ctx: WorkspaceContext,
    drafts: tuple[QuestionDraft, ...],
    *,
    source_message_id: uuid.UUID | None,
    source_node_id: uuid.UUID | None,
    reasoning_node_id: uuid.UUID | None = None,
    strategy_phase: bool = False,
    require_judgment: bool = False,
    max_questions: int | None = None,
    presentation: str = QuestionPresentation.CANVAS_QUESTION.value,
) -> list[AgentQuestion]:
    """把模型这一轮提的问题落库。**纯服务端校验在这里收口。**

    返回真正新建的那些。去重掉的与被截断的都不算 —— 调用方拿它做展示时不会看到
    一个"其实没有新建"的问题。

    ## 去重为什么按"同源 + 规范化问题"而不是全局

    §P0.3 要的是"同一个悬而未决的问题不要每次对话都冒出来"。判据是**问题本身**
    加上它的来源层级:同一层里问过的同一句话不再问;换了一层问同一句话,可能确实
    是不同语境下的两次确认。所以键是 `(source_node_id, normalize(question))`,
    只在 `pending` 状态里比 —— 用户答过之后,再问一次是新问题,不是重复。
    """
    if not drafts:
        return []

    # 先读这个空间里所有还没结束的问题(量很小,一次取全)。
    existing = await db.execute(
        select(AgentQuestion).where(
            AgentQuestion.workspace_id == ctx.id,
            AgentQuestion.status.in_(tuple(ACTIVE_QUESTION_STATUSES)),
        )
    )
    seen: set[tuple[uuid.UUID | None, str]] = set()
    for row in existing.scalars():
        seen.add((row.source_node_id, _normalize(row.question)))

    created: list[AgentQuestion] = []
    limit = max_questions if max_questions is not None else MAX_QUESTIONS_PER_TURN
    for draft in drafts:
        if len(created) >= limit:
            logger.info("一轮的问题超过 %d 个,多余的丢弃", limit)
            break
        # 再次做形状校验。`parse_questions` 已经做过一遍,但这里是**服务端对写入的
        # 最终把关** —— 脚本化 reasoner 与将来的任何新 reasoner 都必须过这一关。
        mode = draft.response_mode
        if mode not in {m.value for m in QuestionResponseMode}:
            continue
        if not draft.question.strip():
            continue
        if mode in {
            QuestionResponseMode.SINGLE_SELECT.value,
            QuestionResponseMode.MULTI_SELECT.value,
        } and not draft.options:
            continue
        # 战略阶段不先问排期条件（每周投入 / 当前水平 / 截止日 / 日程）。
        # **服务端拦，不靠提示词自觉** —— 与去重、形状校验同一条纪律。
        if strategy_phase:
            phase_conflict = strategy_phase_question_conflict(draft.question)
            if phase_conflict is not None:
                logger.info("战略阶段丢弃一个排期类问题(%s):%r", phase_conflict, draft.question[:40])
                continue
        # 阶段 10:**先有判断,再提问**。缺 analysis/recommendation/impact 的问题
        # 整条丢弃,不写半成品 —— 服务端硬闸,不靠提示词自觉。
        if require_judgment:
            missing = strategy_judgment_missing(draft)
            if missing is not None:
                logger.info("战略判断不完整(%s),丢弃该问题:%r", missing, draft.question[:40])
                continue

        key = (source_node_id, _normalize(draft.question))
        if key in seen:
            logger.info("同一个待回答的问题已经存在,跳过重复创建: %r", draft.question[:40])
            continue
        seen.add(key)

        question = AgentQuestion(
            workspace_id=ctx.id,
            source_node_id=source_node_id,
            source_message_id=source_message_id,
            # 有推理地图时,把问题挂到它的地图节点上 —— 回答后靠它定位要重评的节点。
            reasoning_node_id=reasoning_node_id,
            presentation=QuestionPresentation(presentation),
            question=draft.question.strip(),
            why_now=draft.why_now.strip(),
            analysis_summary=draft.analysis_summary.strip(),
            recommendation=draft.recommendation.strip(),
            decision_impact=draft.decision_impact.strip(),
            confidence_note=(
                draft.confidence_note.strip() if draft.confidence_note else None
            ),
            response_mode=QuestionResponseMode(mode),
            options=_options_to_rows(draft),
            allow_custom_input=bool(draft.allow_custom_input),
            status=QuestionStatus.PENDING,
            events=[],
        )
        db.add(question)
        created.append(question)

    if created:
        await db.flush()
    return created


# ---------------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------------
async def list_questions(
    db: AsyncSession, ctx: WorkspaceContext, *, include_decided: bool = False
) -> list[AgentQuestion]:
    """这个空间的问题,新的在前。

    默认只返回"还没结束"的那些(pending / answered / investigating)。`include_decided`
    为真时把 resolved / archived 也带上,用于审计与历史。
    """
    query = select(AgentQuestion).where(AgentQuestion.workspace_id == ctx.id)
    if not include_decided:
        query = query.where(AgentQuestion.status.in_(tuple(ACTIVE_QUESTION_STATUSES)))
    result = await db.execute(query.order_by(AgentQuestion.created_at.desc()))
    return list(result.scalars())


async def load_question(
    db: AsyncSession, ctx: WorkspaceContext, question_id: uuid.UUID
) -> AgentQuestion:
    """按 id 取问题,**同时**校验它属于当前空间。

    归属条件写在 WHERE 里,与所有别的资源同一条纪律:不存在与不属于当前用户返回
    同一个 `QuestionNotFound`。
    """
    result = await db.execute(
        select(AgentQuestion).where(
            AgentQuestion.id == question_id, AgentQuestion.workspace_id == ctx.id
        )
    )
    question = result.scalar_one_or_none()
    if question is None:
        raise QuestionNotFound("没有找到这个问题。")
    return question


# ---------------------------------------------------------------------------------
# 回答 / 跳过 / 稍后
# ---------------------------------------------------------------------------------
async def answer_question(
    db: AsyncSession,
    ctx: WorkspaceContext,
    reasoner,
    question_id: uuid.UUID,
    *,
    selected_option_ids: list[str],
    custom_input: str | None,
    client_answer_id: str,
) -> AnswerOutcome:
    """回答一个问题:存答案 -> 标记处理中 -> 跑一轮后续对话。

    **先提交答案,再调模型**(见模块 docstring 第 3 条)。模型失败时答案与
    `investigating` 状态都还在;后续处理成功时状态变 `resolved`。
    """
    # 守卫写让并发的两次回答排成队。没有它,两次请求可能都读到 pending,
    # 于是同一份答案跑出两轮后续对话。
    await lock_workspace(db, ctx.id)
    question = await load_question(db, ctx, question_id)

    if question.status is not QuestionStatus.PENDING:
        # 双击/重试:同一个幂等键就当"上次那一下",原样返回。
        if question.answer_client_id == client_answer_id and question.answer is not None:
            return AnswerOutcome(question=question, turn=None, replayed=True)
        raise QuestionNotAnswerable(
            "这个问题已经回答过或已经跳过了。",
            status=question.status.value,
        )

    normalized = _validate_answer(question, selected_option_ids, custom_input)
    question.answer = normalized
    question.answer_client_id = client_answer_id
    question.answered_at = utcnow()
    # 进入"处理中"。前端据此显示处理中,而不是让用户以为答案丢了。
    question.status = QuestionStatus.INVESTIGATING
    question.events = [
        *_as_list(question.events),
        _event(QuestionUserAction.ANSWERED, client_id=client_answer_id, answer=normalized),
    ]
    # **第一次提交:答案已经安全。** 从这里往下发生什么都不会让它消失。
    await db.commit()

    # 后续处理:把答案当成一轮普通对话。它可能产出提案,但**提案仍需用户确认**。
    from backend.services import conversation_service  # 延迟 import,避免循环

    source_node_id = await _live_source_node_id(db, ctx, question.source_node_id)
    turn = await conversation_service.submit_turn(
        db,
        ctx,
        reasoner,
        content=_answer_content(question, normalized),
        client_message_id=f"question-answer:{question.id}",
        context_node_id=source_node_id,
        trigger="question_answered",
    )

    # 成功则 resolved;降级/失败则停在 investigating,等用户刷新或重试。
    if not turn.result.degraded:
        question.status = QuestionStatus.RESOLVED
    await db.commit()
    return AnswerOutcome(question=question, turn=turn, replayed=False)


async def skip_question(
    db: AsyncSession,
    ctx: WorkspaceContext,
    question_id: uuid.UUID,
    *,
    client_action_id: str,
    reason: str | None = None,
) -> tuple[AgentQuestion, bool]:
    """跳过一个问题 -> `archived`。返回 (问题, 是否重放)。"""
    return await _decide_question(
        db,
        ctx,
        question_id,
        client_action_id=client_action_id,
        reason=reason,
        action=QuestionUserAction.SKIPPED,
        next_status=QuestionStatus.ARCHIVED,
    )


async def defer_question(
    db: AsyncSession,
    ctx: WorkspaceContext,
    question_id: uuid.UUID,
    *,
    client_action_id: str,
    reason: str | None = None,
) -> tuple[AgentQuestion, bool]:
    """稍后回答 -> **保持 pending**,只记一条用户动作。返回 (问题, 是否重放)。"""
    return await _decide_question(
        db,
        ctx,
        question_id,
        client_action_id=client_action_id,
        reason=reason,
        action=QuestionUserAction.DEFERRED,
        next_status=QuestionStatus.PENDING,
    )


async def _decide_question(
    db: AsyncSession,
    ctx: WorkspaceContext,
    question_id: uuid.UUID,
    *,
    client_action_id: str,
    reason: str | None,
    action: QuestionUserAction,
    next_status: QuestionStatus,
) -> tuple[AgentQuestion, bool]:
    """跳过与稍后共用的写路径。**只改状态与审计,不碰计划。**"""
    await lock_workspace(db, ctx.id)
    question = await load_question(db, ctx, question_id)

    events = _as_list(question.events)
    if any(
        isinstance(entry, dict)
        and entry.get("clientId") == client_action_id
        and entry.get("action") == action.value
        for entry in events
    ):
        # 同一个动作重复提交:幂等。
        return question, True

    if question.status is not QuestionStatus.PENDING:
        raise QuestionNotAnswerable(
            "这个问题已经回答过或已经跳过了。", status=question.status.value
        )

    if action is QuestionUserAction.DEFERRED:
        # 稍后回答不归档:问题仍然 pending,只是把这件事往后放。
        question.events = [
            *events,
            _event(QuestionUserAction.DEFERRED, client_id=client_action_id, reason=reason),
        ]
    else:
        question.status = next_status
        question.events = [
            *events,
            _event(QuestionUserAction.SKIPPED, client_id=client_action_id, reason=reason),
        ]
    await db.commit()
    return question, False


# ---------------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------------
def _as_list(value: object) -> list:
    return list(value) if isinstance(value, list) else []


def to_view(question: AgentQuestion) -> QuestionView:
    """问题 -> 接口视图。与 `proposal_service.to_view` 同一层,前端不自己拼。"""
    answer = None
    if isinstance(question.answer, dict):
        answer = QuestionAnswer(
            selected_option_ids=[str(x) for x in _as_list(question.answer.get("selectedOptionIds"))],
            custom_input=(
                str(question.answer["customInput"])
                if question.answer.get("customInput") is not None
                else None
            ),
        )
    return QuestionView(
        id=question.id,
        workspace_id=question.workspace_id,
        source_node_id=question.source_node_id,
        source_message_id=question.source_message_id,
        reasoning_node_id=question.reasoning_node_id,
        question=question.question,
        presentation=question.presentation.value,
        why_now=question.why_now,
        analysis_summary=question.analysis_summary,
        recommendation=question.recommendation,
        decision_impact=question.decision_impact,
        confidence_note=question.confidence_note,
        response_mode=question.response_mode.value,
        options=[
            {
                "id": str(option.get("id")),
                "label": str(option.get("label")),
                "recommended": bool(option.get("recommended", False)),
            }
            for option in _as_list(question.options)
            if isinstance(option, dict)
        ],
        allow_custom_input=question.allow_custom_input,
        status=question.status.value,
        answer=answer,
        created_at=question.created_at,
        updated_at=question.updated_at,
        answered_at=question.answered_at,
    )


def _validate_answer(
    question: AgentQuestion, selected_option_ids: list[str], custom_input: str | None
) -> dict:
    """答案与问题的 `response_mode` / 选项必须相容。不合法就 400。

    **服务端说了算。** 前端可以只发它自己渲染出来的选项,但校验看的是库里的
    `question.options` —— 客户端伪造一个不存在的 option id 会被拒。
    """
    valid_ids = {
        str(option.get("id"))
        for option in _as_list(question.options)
        if isinstance(option, dict)
    }
    selected = [str(item) for item in selected_option_ids if str(item).strip()]
    unknown = [item for item in selected if item not in valid_ids]
    if unknown:
        raise InvalidQuestionAnswer(f"选项 {'、'.join(unknown)} 不属于这个问题。")
    # 去重但保持顺序。
    selected = list(dict.fromkeys(selected))

    custom = (custom_input or "").strip()
    if len(custom) > MAX_CUSTOM_INPUT_CHARS:
        raise InvalidQuestionAnswer(f"补充输入最多 {MAX_CUSTOM_INPUT_CHARS} 字。")
    custom = custom or None

    mode = question.response_mode
    if custom is not None and not question.allow_custom_input:
        raise InvalidQuestionAnswer("这个问题不接受自由输入。")
    if mode is QuestionResponseMode.SINGLE_SELECT and len(selected) > 1:
        raise InvalidQuestionAnswer("这是单选题,只能选一个选项。")
    if mode is QuestionResponseMode.FREE_TEXT:
        if selected:
            raise InvalidQuestionAnswer("这是自由输入题,不需要选选项。")
        if custom is None:
            raise InvalidQuestionAnswer("自由输入题需要写一段回答。")
    if not selected and custom is None:
        raise InvalidQuestionAnswer("答案不能是空的:至少选一个选项或写一段话。")
    if (
        mode in {QuestionResponseMode.SINGLE_SELECT, QuestionResponseMode.MULTI_SELECT}
        and not question.allow_custom_input
        and not selected
    ):
        raise InvalidQuestionAnswer("这道题需要从选项里选一个。")

    return {"selectedOptionIds": selected, "customInput": custom}


def _option_labels(question: AgentQuestion, selected: list[str]) -> list[str]:
    by_id = {
        str(option.get("id")): str(option.get("label"))
        for option in _as_list(question.options)
        if isinstance(option, dict)
    }
    return [by_id.get(item, item) for item in selected]


def _answer_content(question: AgentQuestion, answer: dict) -> str:
    """把答案合成一条**用户消息**,交给正常的对话链路。

    带上问题原文:模型的最近历史里有那条提问回复,但把问题与答案放在同一条里
    最稳 —— 用户可能隔了几轮才回答,那几次往返不该让模型猜"他说的是哪道题"。
    """
    selected = list(answer.get("selectedOptionIds") or [])
    labels = _option_labels(question, selected)
    custom = answer.get("customInput")
    parts: list[str] = []
    if labels:
        parts.append("、".join(labels))
    if custom:
        parts.append(str(custom))
    joined = ";".join(parts) if parts else "(没有内容)"
    return f"回答你问的「{question.question}」:{joined}"


async def _live_source_node_id(
    db: AsyncSession, ctx: WorkspaceContext, source_node_id: uuid.UUID | None
) -> uuid.UUID | None:
    """源节点还活着才把它作为这一轮的焦点。

    归档/删除之后问题仍可回答(审计上它属于那个节点),但**不能**把失效的 id 传进
    `submit_turn` —— 那会在校验那一关变成 404,把一次本该成功的回答变成失败。
    """
    if source_node_id is None:
        return None
    alive = await db.scalar(
        select(PlanNode.id).where(
            PlanNode.id == source_node_id,
            PlanNode.workspace_id == ctx.id,
            PlanNode.deleted_at.is_(None),
        )
    )
    return alive


__all__ = [
    "MAX_CUSTOM_INPUT_CHARS",
    "MAX_QUESTIONS_PER_TURN",
    "AnswerOutcome",
    "answer_question",
    "create_from_drafts",
    "defer_question",
    "list_questions",
    "load_question",
    "skip_question",
    "strategy_phase_question_conflict",
    "to_view",
]
