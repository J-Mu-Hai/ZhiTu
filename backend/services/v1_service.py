"""规划智能体重构 V1 — P2:AI 战略判断、因素筛选与战略路径。

## 结构:分组是画布节点,子项是**画布问题节点**

```
根目标 (画布节点)
├─ 目标重构        ← 画布节点(PlanNode,固定框架,可进入)
│   └─ 进入后有若干**紫色画布问题节点**围绕它
├─ 问题结构        ← 画布节点
│   └─ 进入后有若干紫色画布问题节点
└─ 战略路径        ← 画布节点(先空,信息足够后长出问题)
```

- 三个分组是 `plan_nodes`(`purpose=information`),**正常在主画布看不到子项**;
  进入某个分组(它的子空间)才看到归属它的 `agent_questions`。
- 这些紫色问题节点是 `AgentQuestion`(`presentation=canvas_question`),不是计划节点:
  不参与排期 / 依赖 / 统计。
- 需要在**对话框**回答的问题走橙色(会话 intake),不出现在画布上。

## 程序控制 + 模型判断

模型只负责判断:**整体判断、已存在问题的结论/事实/假设、一个焦点、一个问题、战略取舍**。
它只能更新已存在的问题键,不能新建任意节点;每轮最多 3 个更新;战略只在四个受限键上加
节点。服务端决定接受什么、写什么。

## 失败不落半成品

模型不可用 / 输出不合格分别记为准确状态,一个问句内容都不改。`v1_stage is None` = 非 V1,
本模块所有入口直接返回/不介入。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from backend.agent.runtime.base import (
    HISTORY_TURNS,
    KnownConditions,
    PlanNodeView,
    ReasoningResult,
    TurnContext,
)
from backend.contracts.reasoning import AgentTurnResponse
from backend.core.config import settings
from backend.db.base import utcnow
from backend.db.models import AgentQuestion, Conversation, GoalReasoningSession, Message, PlanNode
from backend.db.models.enums import (
    DegradedReason,
    MessageRole,
    ModelSource,
    NodeOrigin,
    NodePurpose,
    NodeStatus,
    NodeType,
    QuestionPresentation,
    QuestionResponseMode,
    QuestionStatus,
    ReasoningSessionPhase,
    ReasoningSessionStatus,
    RevisionTrigger,
)
from backend.services import (
    audit_service,
    conversation_service,
    node_service,
    proposal_service,
    reasoning_service,
    turn_context,
    v01_service,
)
from backend.services.context import WorkspaceContext
from backend.services.timeutil import today_in

logger = logging.getLogger(__name__)

# =================================================================================
# V1 阶段一档位
# =================================================================================
V1_INITIAL_THINKING = "initial_thinking"
V1_GOAL_REFRAME = "goal_reframe"
V1_FACTOR_ANALYSIS = "factor_analysis"
V1_STRATEGY_DRAFT = "strategy_draft"
#: P2.2:目标定义经用户确认后,进入“问题结构”分析层(五个因素维度)。
V1_PROBLEM_STRUCTURE = "problem_structure"
#: 深度对话:已给出战略理解,等用户确认或指出哪一句不对(确认后才进入正式战略草案)。
V1_STRATEGY_ALIGNMENT = "strategy_alignment"
#: 用户已确认战略逻辑;先进入**时间架构共创**,不直接生成时间线。
V1_STRATEGY_CONFIRMED = "strategy_confirmed_for_timeline"
#: 时间架构共创:已给出时间假设(至多一个问题),等用户对齐节奏。
V1_TIMELINE_ALIGNMENT = "timeline_alignment"
#: P3:已生成 3–6 个阶段的粗时间架构草案,等用户确认。
V1_COARSE_TIMELINE_REVIEW = "coarse_timeline_review"
#: P4:粗时间架构已确认,进入周/日计划与执行。
V1_WEEKLY_EXECUTION = "weekly_execution"
#: P4:按执行偏差重规划未来。
V1_REPLANNING = "replanning"

V1_STATUS_IDLE = "idle"
V1_STATUS_RUNNING = "running"
V1_STATUS_FAILED = "failed"
#: R2:已产出待确认产物(战略 / 粗时间线),停止等待用户确认。**不是 idle。**
V1_STATUS_AWAITING_CONFIRMATION = "awaiting_user_confirmation"
#: R2:真的在等用户回答一个问题。
V1_STATUS_AWAITING_ANSWER = "awaiting_user_answer"

#: 真实模型来源。只有这两个值能通过 V1 的强制运行时校验:
#: - `openjiuwen`:真实经过 OpenJiuwen Workflow 的调用;
#: - `test`:显式注入的测试 fixture(响应与审计都会如实标明)。
V1_SOURCE_OPENJIUWEN = "openjiuwen"
V1_SOURCE_TEST = "test"

#: 三个一级分组:(键, 标题, 说明)。
_GROUPS: tuple[tuple[str, str, str], ...] = (
    ("goal_reframe", "目标重构", "把一句愿望变成可判断的定义。"),
    ("problem_structure", "问题结构", "看清结果由什么决定、什么真正卡住你。"),
    ("strategy_path", "战略路径", "待形成战略路径。"),
)

#: 十个固定画布问题节点:(分组键, 键, 问题, 为什么问)。
_ANALYSIS: tuple[tuple[str, str, str, str], ...] = (
    ("goal_reframe", "current_state", "你现在在哪?", "已有基础、可投入时间与可用资源决定起点,也决定每个阶段多长。"),
    ("goal_reframe", "true_intent", "你希望最后能拿出什么具体结果,证明它真正解决了你的问题?", "真实意图不同,成果定义与阶段顺序会完全不同。"),
    ("goal_reframe", "value_assessment", "这件事值得做吗?如果三年内没有直接回报,你还会做吗?", "先判断值不值得投入,再谈怎么投入,避免把时间花在伪目标上。"),
    ("goal_reframe", "key_conflict", "真正卡你的是什么?只解决一个障碍,哪一个解决了整件事就会推进?", "只处理最关键的一两个矛盾,比同时补十个短板更有效。"),
    ("goal_reframe", "goal_definition", "最后到底要做到什么?做到什么程度、拿出什么,你就认为这件事成了?", "没有可观察的成果定义,后面的阶段与时间线都无从判断。"),
    ("problem_structure", "hard_constraints", "有哪些是你不能改、只能接受的限制?", "硬约束决定哪些路线根本不可行,必须先于偏好确认。"),
    ("problem_structure", "controllable_factors", "在这件事上,哪些是你能直接行动改变的?", "只讨论能改变的东西,才能把注意力放在真正有产出的动作上。"),
    ("problem_structure", "key_levers", "哪个变量一旦改善,最终结果的提升最大?", "抓住关键杠杆,比均匀用力更快看到结果。"),
    ("problem_structure", "major_risks", "最可能让这件事失败的是什么?你能提前看到什么信号?", "提前识别风险与信号,才能设置检查点与备用路径。"),
    ("problem_structure", "external_conditions", "有哪些外部因素不在你控制内,却会明显影响结果?", "外部条件不在你控制内,却常常决定路线的可行性。"),
)

#: 战略路径的四个受限问题:(键, 标题, 问题)。
_STRATEGY_KEYS: tuple[tuple[str, str, str], ...] = (
    ("main_line", "主线", "主线:最优先投入什么?"),
    ("parallel_line", "并行线", "并行线:哪些可以同时做,但不该挤占主线?"),
    ("defer_or_avoid", "暂缓或放弃", "暂缓/放弃:当前不值得做什么?"),
    ("risk_control", "风险控制", "风险控制:在哪里设置检查点或备用路径?"),
)

MAX_V1_UPDATES = 3
MAX_V1_DIMENSIONS = 3
#: P2.3:problem_structure 中“继续形成战略路径”的显式下一步动作。
NEXT_CONTINUE_STRATEGY = "continue_strategy"
#: R2:每个非终态都能回答“现在等谁、下一步是什么”。
#: 取值是**编排器算出的事实**,不是前端猜的;前端仍可按阶段渲染具体按钮。
NEXT_SELECT_DIRECTION = "select_direction"
NEXT_CONFIRM_GOAL = "confirm_goal"
NEXT_CONFIRM_STRATEGY = "confirm_strategy"
NEXT_CONFIRM_TIMELINE = "confirm_timeline"
NEXT_CONFIRM_REPLAN = "confirm_replan"
#: 深度对话:先确认“战略理解”,再进入正式战略草案。
NEXT_CONFIRM_UNDERSTANDING = "confirm_strategy_understanding"
#: 时间架构共创:对齐节奏后才生成粗时间线。
NEXT_CONFIRM_TIMELINE_ALIGNMENT = "confirm_timeline_alignment"
#: 阶段一全局关键问题的总预算。超过后服务端不再接受新问题。
MAX_V1_QUESTIONS = 3

#: 用户输入的语义分类(P2.1)。**只有前三类能写节点事实。**
INPUT_STRATEGIC_FACT = "strategic_fact"
INPUT_USER_PREFERENCE = "user_preference"
INPUT_USER_CORRECTION = "user_correction"
INPUT_CONVERSATION_FEEDBACK = "conversation_feedback"
INPUT_AMBIGUOUS = "ambiguous_or_irrelevant"

#: 元对话 / 情绪 / 对 AI 的反馈 —— **不是战略事实**。
_META_MARKERS = (
    "走神",
    "人工整",
    "机器人",
    "你在问",
    "问点",
    "换个问题",
    "换一个",
    "听不懂",
    "答非所问",
    "你是不是",
    "傻",
    "垃圾",
    "无聊",
    "滚",
    "烦死",
)
_LOW_INFO_MARKERS = (
    "不知道",
    "不清楚",
    "没想好",
    "不确定",
    "暂时没有",
    "说不上",
    "没有想法",
    "都行",
    "随便",
    "你来定",
    "无所谓",
    "没概念",
)
_CORRECTION_MARKERS = ("不是", "其实", "我说的不是", "更正", "你误解")
_PREFERENCE_MARKERS = ("我想", "我希望", "我更喜欢", "我倾向", "我比较", "我更愿意", "优先")


def classify_user_message(content: str) -> str:
    """把一条用户消息分类为 P2.1 的五类之一。**确定性规则**,不依赖模型。

    元对话 / 情绪 / 对 AI 的反馈(“你走神了”“人工整你”)归为 `conversation_feedback`,
    不得进入 `knownFacts`;“不知道”这类归为 `ambiguous_or_irrelevant`。
    """
    text = (content or "").strip()
    lowered = text.lower()
    if not text:
        return INPUT_AMBIGUOUS
    if len(text) <= 24 and any(marker in text for marker in _LOW_INFO_MARKERS):
        return INPUT_AMBIGUOUS
    if any(marker in lowered for marker in _META_MARKERS):
        return INPUT_CONVERSATION_FEEDBACK
    if any(marker in text for marker in _CORRECTION_MARKERS):
        return INPUT_USER_CORRECTION
    if any(marker in text for marker in _PREFERENCE_MARKERS):
        return INPUT_USER_PREFERENCE
    if len(text) < 4:
        return INPUT_AMBIGUOUS
    return INPUT_STRATEGIC_FACT


def _counts_as_low_info(classification: str) -> bool:
    return classification in (INPUT_AMBIGUOUS, INPUT_CONVERSATION_FEEDBACK)

#: 内部十维 + 战略四子的**展示标题**。画布只显示标题 + 一句判断。
_DIMENSION_TITLES: dict[str, str] = {
    **{key: title for _group, key, title, _q in _ANALYSIS},
    **{key: title for key, title, _q in _STRATEGY_KEYS},
}
#: **内部十维**的键(不含战略子项)—— 可见性/隐藏数只针对这十个。
ANALYSIS_DIMENSION_KEYS = frozenset(key for _group, key, _title, _q in _ANALYSIS)
#: goal_reframe 阶段默认可见的三个**核心分析维度**。
_CORE_GOAL_KEYS: tuple[str, ...] = ("true_intent", "key_conflict", "goal_definition")
#: 进入 problem_structure 后默认可见的五个因素维度。
_PROBLEM_KEYS: tuple[str, ...] = (
    "hard_constraints",
    "controllable_factors",
    "key_levers",
    "major_risks",
    "external_conditions",
)


#: 模型可以指涉的全部键(10 个固定问题 + 4 个战略问题)。**分组键不可被模型更新。**
ALLOWED_V1_KEYS = frozenset(
    {key for _group, key, _q, _why in _ANALYSIS} | {key for key, _t, _q in _STRATEGY_KEYS}
)
_STRATEGY_KEY_SET = frozenset(key for key, _t, _q in _STRATEGY_KEYS)
_STRATEGY_GROUP_KEY = "strategy_path"
_GROUP_KEY_SET = frozenset(key for key, _t, _d in _GROUPS)

_STRATEGY_FIELD = {
    "main_line": "mainLine",
    "parallel_line": "parallelLine",
    "defer_or_avoid": "deferOrAvoid",
    "risk_control": "riskControl",
}


# =================================================================================
# R1:OpenJiuwen 强制运行时 + 工作回合生命周期
# =================================================================================
def reasoner_source_kind(reasoner) -> str:
    """当前 reasoner 的**真实**来源种类。不靠环境变量猜。

    - 显式声明了 `v1_source_kind` 的实现(测试 fixture / 适配器)以声明为准;
    - 真实适配器按类名与模块识别,避免鸭子类型误判成 openjiuwen;
    - 其余一律 `unknown`,由强制校验拒绝。
    """
    if reasoner is None:
        return "unknown"
    declared = getattr(reasoner, "v1_source_kind", None)
    if isinstance(declared, str) and declared:
        return declared
    cls = type(reasoner)
    module = getattr(cls, "__module__", "")
    if cls.__name__ == "OpenJiuwenReasoner" or module.endswith("openjiuwen_runtime"):
        return V1_SOURCE_OPENJIUWEN
    return "unknown"


def source_allowed(kind: str) -> bool:
    """V1 是否接受这个来源。开关关闭时不限制(保留旧行为)。"""
    if not settings.v1_require_openjiuwen:
        return True
    return kind in (V1_SOURCE_OPENJIUWEN, V1_SOURCE_TEST)


def _start_turn(
    session: GoalReasoningSession,
    *,
    stage: str | None,
    trigger: str,
    source: str,
    idempotency_key: str | None = None,
) -> None:
    """把一个工作回合持久化到会话上并盖 deadline。**调用方负责 commit。**"""
    now = utcnow()
    session.v1_turn_id = uuid.uuid4().hex[:32]
    session.v1_turn_stage = stage
    session.v1_turn_trigger = trigger
    session.v1_turn_started_at = now
    session.v1_turn_deadline_at = now + timedelta(
        seconds=max(1, int(settings.v1_agent_turn_timeout_seconds))
    )
    session.v1_turn_attempt = int(session.v1_turn_attempt or 0) + 1
    session.v1_turn_source = source
    session.v1_turn_idempotency_key = idempotency_key
    session.v1_status = V1_STATUS_RUNNING
    session.v1_error = None


def _finish_turn(session: GoalReasoningSession, *, status: str) -> None:
    """结束一个回合:清掉 deadline,落最终状态。**调用方负责 commit。**"""
    session.v1_status = status
    session.v1_turn_deadline_at = None


async def _reason_with_timeout(reasoner, turn: TurnContext) -> ReasoningResult:
    """在 V1 的超时窗口内调用 reasoner。到点转可重试超时,不无限等待。"""
    timeout = max(1, int(settings.v1_agent_turn_timeout_seconds))
    try:
        return await asyncio.wait_for(reasoner.reason(turn), timeout=timeout)
    except TimeoutError:
        return ReasoningResult(
            reply=f"这次响应超过 {timeout} 秒还没回来,可以重试。",
            source=ModelSource.UNAVAILABLE,
            degraded=True,
            degraded_reason=DegradedReason.MODEL_TIMEOUT,
            retryable=True,
            request_id="v1-timeout",
            prompt_version="v1",
        )


async def _audit_turn_failure(
    db,
    ctx,
    session: GoalReasoningSession,
    result: ReasoningResult,
    *,
    trigger: str,
    stage_before: str | None,
) -> None:
    """把一次失败的模型回合落到审计。超时用**专用事件**,不混进普通不可用。"""
    is_timeout = result.degraded_reason == DegradedReason.MODEL_TIMEOUT
    await _audit(
        db,
        ctx,
        session,
        "v1_step_timed_out" if is_timeout else "model_unavailable",
        trigger=trigger,
        stage_before=stage_before,
        stage_after=session.v1_stage,
        source=result.source.value if result.source else None,
        summary=session.v1_error,
        validation_status="failed",
        error_code=(
            "V1_STEP_TIMEOUT" if is_timeout else str(result.degraded_reason or "MODEL_UNAVAILABLE")
        ),
    )


async def _refuse_unavailable_source(
    db: AsyncSession,
    ctx: WorkspaceContext,
    session: GoalReasoningSession,
    *,
    source: str,
    trigger: str,
) -> ReasoningResult:
    """来源不是真实 OpenJiuwen:准确失败 + 可重试,绝不当成真实规划成功。"""
    session.v1_status = V1_STATUS_FAILED
    _finish_turn(session, status=V1_STATUS_FAILED)
    session.v1_error = (
        "OpenJiuwen 未就绪,未开始规划。"
        f"本轮模型来源是 {source or 'unknown'},不满足 V1 的真实 OpenJiuwen 要求,"
        "已停止,可修复后重试。"
    )
    await _audit(
        db,
        ctx,
        session,
        "v1_openjiuwen_required",
        trigger=trigger,
        source=source or None,
        summary=session.v1_error,
        validation_status="failed",
        error_code="V1_OPENJIUWEN_REQUIRED",
    )
    await db.commit()
    return ReasoningResult(
        reply=session.v1_error,
        source=ModelSource.UNAVAILABLE,
        degraded=True,
        degraded_reason=DegradedReason.MODEL_UNAVAILABLE,
        retryable=True,
        request_id="v1-source",
        prompt_version="v1",
    )


def is_v1(session: GoalReasoningSession | None) -> bool:
    """这个会话是不是重构 V1。`v1_stage is None` = 非 V1。"""
    return session is not None and session.v1_stage is not None


def dimension_title(key: str | None) -> str | None:
    """分析维度键 -> 展示标题。"""
    return _DIMENSION_TITLES.get(key or "")


def visible_dimension_keys(session: GoalReasoningSession) -> set[str]:
    """当前**画布默认可见**的分析维度键。内部复杂,外部简单。

    R2 收口:主画布**只**投影三个核心分析维度 `true_intent` / `key_conflict` /
    `goal_definition`,以及当前焦点(若不同)。其余内部分析维度保留在数据层与右侧
    详情,**不**默认渲染为 canvas_question —— 不再让用户先点分组、再看十个待答。

    战略子项一旦形成就显示(在 `list_questions` 里单独放行)。
    """
    keys: set[str] = set(_CORE_GOAL_KEYS)
    # 焦点只接受**真实的分析维度键**:模型偶尔会返回分组键(如 `strategy_path`),
    # 那不是画布上的分析节点,不能因此被当成“可见维度”。
    if session.v1_focus_key in ANALYSIS_DIMENSION_KEYS:
        keys.add(session.v1_focus_key)
    return keys


def dimension_projection(
    session: GoalReasoningSession, questions: list[AgentQuestion] | None = None
) -> list[dict]:
    """**V1 画布的单一分析投影**。

    每项包含:key / title / judgment / status / visible / isFocus /
    hasPendingQuestion / discussionSummary / internal / questionId,以及详情所需的
    knownFacts / assumptions / importanceReason。

    `visible` 由服务端的 `visible_dimension_keys` 决定(根画布只显示三个核心维度 + 焦点);
    `internal` 单独标识“内部维度”,**不**再把隐藏维度伪造成 `archived` 状态。
    """
    visible = visible_dimension_keys(session)
    by_key = {q.v1_key: q for q in (questions or []) if q.v1_key}
    focus = session.v1_focus_key
    pending_focus = focus if (session.v1_question or "").strip() else None
    projection: list[dict] = []
    for key in (k for _group, k, _title, _q in _ANALYSIS):
        question = by_key.get(key)
        analysis = (question.v1_analysis or {}) if question is not None else {}
        discussion_count = int(analysis.get("discussionCount", 0) or 0)
        projection.append(
            {
                "key": key,
                "title": _DIMENSION_TITLES[key],
                "judgment": str(analysis.get("judgment") or ""),
                "status": question.status.value if question is not None else "pending",
                "visible": key in visible,
                "isFocus": key == focus,
                "hasPendingQuestion": key == pending_focus,
                "discussionSummary": (
                    str(analysis.get("summary") or "")
                    or (f"已讨论 {discussion_count} 次" if discussion_count else "")
                ),
                "internal": key not in _CORE_GOAL_KEYS,
                "questionId": str(question.id) if question is not None else None,
                "knownFacts": [str(x) for x in (analysis.get("knownFacts") or [])],
                "assumptions": [str(x) for x in (analysis.get("assumptions") or [])],
                "importanceReason": str(analysis.get("importanceReason") or ""),
                #: 分析维度**从不**是“待回答问题”;真正的问题只来自对话区那一个。
                "requiresResponse": False,
            }
        )
    return projection


def actual_pending_question_count(session: GoalReasoningSession) -> int:
    """真正需要用户回答的问题数:只数会话上那个全局关键问题(0 或 1)。"""
    return 1 if (session.v1_question or "").strip() else 0


def compute_next_action(session: GoalReasoningSession) -> str | None:
    """编排器算出的显式下一步。**禁止无解释 idle 的唯一事实来源。**

    返回 None 只意味着“有一个已经解释得清的待处理项”(一个待答问题或已进入执行),
    不存在“idle 且无事可做且没理由”这种状态。
    """
    if (session.v1_question or "").strip():
        # 有一个待答关键问题,它本身就是解释。
        return None
    if session.v1_stage == V1_COARSE_TIMELINE_REVIEW:
        return NEXT_CONFIRM_TIMELINE
    if session.v1_stage == V1_TIMELINE_ALIGNMENT:
        return NEXT_CONFIRM_TIMELINE_ALIGNMENT
    if session.v1_stage == V1_REPLANNING:
        return NEXT_CONFIRM_REPLAN
    if session.v1_stage == V1_STRATEGY_ALIGNMENT:
        return NEXT_CONFIRM_UNDERSTANDING
    if session.v1_strategy and not (session.v1_strategy or {}).get("confirmed"):
        return NEXT_CONFIRM_STRATEGY
    if session.v1_stage == V1_GOAL_REFRAME:
        if session.v1_candidate_directions:
            return NEXT_SELECT_DIRECTION
        if session.v1_strategic_thesis:
            return NEXT_CONFIRM_GOAL
    if session.v1_stage in (V1_PROBLEM_STRUCTURE, V1_FACTOR_ANALYSIS) and not session.v1_strategy:
        return NEXT_CONTINUE_STRATEGY
    # 执行阶段已经有计划,不再强塞一个“下一步”;但仍不是无解释 idle。
    return None


def has_explained_state(session: GoalReasoningSession) -> bool:
    """当前状态是否可解释:有进度、有等待对象,或已有明确下一步。

    规格 4.1 禁止的是“不透明 idle”。这里把那条不变量变成可断言的函数。
    """
    if session.v1_status in (V1_STATUS_RUNNING, V1_STATUS_FAILED):
        return True
    if (session.v1_question or "").strip():
        return True
    if session.v1_next_action:
        return True
    if session.v1_strategy or session.v01_timeline or session.timeline_proposal_id:
        return True
    # 新建空间尚未跑完第一轮;编排器会立刻推进,不算不透明 idle。
    # 执行阶段已经有计划,同样不算。
    return session.v1_stage in (None, V1_INITIAL_THINKING, V1_WEEKLY_EXECUTION)


def _stable_interaction_id(kind: str, prompt: str, focus_key: str | None) -> str:
    digest = hashlib.blake2b(
        f"{kind}|{focus_key or ''}|{prompt}".encode(), digest_size=6
    ).hexdigest()
    return f"ci-{kind}-{digest}"


def current_interaction(session: GoalReasoningSession) -> dict | None:
    """**当前唯一待处理动作**。由既有会话状态派生,不改状态机。

    每个 V1 阶段同时最多一个 active interaction;它不是一条普通聊天消息,而是
    “现在需要用户回答/确认的那件事”。`presentation=focus_modal` 只给高价值动作。
    """
    if not is_v1(session):
        return None
    kind = ""
    title = ""
    prompt = ""
    context = session.v1_strategic_thesis or ""
    why_now = ""
    options: list[dict] = []
    recommended: str | None = None
    focus_key = session.v1_focus_key

    if (session.v1_question or "").strip():
        kind, title = "strategic_question", "需要你回答一个关键问题"
        prompt = session.v1_question or ""
        why_now = session.v1_decision_context or session.v1_focus_reason or ""
    elif session.v1_stage == V1_GOAL_REFRAME and session.v1_candidate_directions:
        kind, title = "candidate_selection", "请选择一个起点"
        prompt = "选一个候选方向,或直接否定我。"
        why_now = session.v1_decision_context or session.v1_focus_reason or ""
        for item in session.v1_candidate_directions:
            if isinstance(item, dict):
                options.append(
                    {
                        "key": str(item.get("key") or ""),
                        "title": str(item.get("title") or ""),
                        "reason": str(item.get("reason") or ""),
                        "impact": str(item.get("impact") or ""),
                    }
                )
        if options:
            recommended = options[0]["key"]
    elif session.v1_strategy and not (session.v1_strategy or {}).get("confirmed"):
        kind, title = "strategy_review", "确认战略理解与战略"
        prompt = "确认这份战略理解,或指出哪一句不对。"
        why_now = "战略决定后面所有阶段与时间线,先确认再往下。"
    elif session.v1_stage == V1_TIMELINE_ALIGNMENT:
        alignment = session.v1_timeline_alignment or {}
        kind, title = "timeline_alignment", "对齐时间节奏"
        prompt = str(alignment.get("question") or "认可默认节奏,或提出调整。")
        context = str(alignment.get("summary") or context)
        why_now = "先对齐节奏,才生成粗时间架构。"
        options = [
            {"key": str(index), "title": str(item), "reason": "", "impact": ""}
            for index, item in enumerate(alignment.get("options") or [])
        ]
    elif session.v1_stage == V1_COARSE_TIMELINE_REVIEW:
        kind, title = "timeline_review", "确认粗时间架构"
        prompt = "确认这份粗时间架构,或调整时间范围与验收标准。"
        why_now = "确认前不写正式计划;确认后进入月/周/日细化。"
    elif session.v1_stage == V1_REPLANNING:
        kind, title = "weekly_review", "确认未来重规划"
        prompt = "确认这份只调整未来的重规划。"
        why_now = "执行偏差需要调整未来计划,已完成历史不变。"
    else:
        return None

    return {
        "id": _stable_interaction_id(kind, prompt, focus_key),
        "nonce": session.v1_turn_id or "",
        "kind": kind,
        "priority": "high",
        "title": title,
        "context": context,
        "whyNow": why_now,
        "prompt": prompt,
        "options": options,
        "recommendedOption": recommended,
        "focusKey": focus_key,
        "status": "active",
        # **回答渠道**:自由叙述型问题在对话里回答;流程决策在画布节点里确认。
        # 这是稳定字段,前端据此分流,**不靠文案猜**。新增字段不改状态机。
        "answerChannel": "conversation" if kind == "strategic_question" else "canvas_node",
        # 高价值动作才居中专注;其余留 Dock(当前派生出的都是高价值动作)。
        "presentation": "focus_modal",
    }


async def record_interaction_event(
    db: AsyncSession,
    ctx: WorkspaceContext,
    session: GoalReasoningSession,
    *,
    interaction_id: str,
    event: str,
) -> None:
    """居中专注模式的打开 / 收起。**只记交互编排,不记草稿或思维链。**"""
    if event not in ("opened", "dismissed"):
        return
    await _audit(
        db,
        ctx,
        session,
        "v1_interaction_focus_opened" if event == "opened" else "v1_interaction_focus_dismissed",
        trigger="focus_modal",
        summary="打开居中专注对话。" if event == "opened" else "收起居中专注对话。",
        payload={"interactionId": interaction_id},
    )
    await db.commit()


async def _audit(db, ctx: WorkspaceContext, session, event_type: str, **kwargs):
    """写一条 V1 审计事件。**只 flush,随调用方的领域事务一起提交。**"""
    return await audit_service.record(db, ctx, session, event_type=event_type, **kwargs)


async def _guard_reject(db, ctx, session, *, reason: str, code: str = "GUARD_REJECTED") -> None:
    """守卫拒绝:记一条**失败**事件并提交,然后把拒绝交给调用方抛出。"""
    await _audit(
        db,
        ctx,
        session,
        "guard_rejected",
        summary=reason,
        validation_status="failed",
        error_code=code,
    )
    await db.commit()


# =================================================================================
# 读
# =================================================================================
async def _v1_groups(db: AsyncSession, ctx: WorkspaceContext) -> list[PlanNode]:
    result = await db.execute(
        select(PlanNode)
        .where(
            PlanNode.workspace_id == ctx.id,
            PlanNode.v1_key.in_(tuple(_GROUP_KEY_SET)),
            PlanNode.deleted_at.is_(None),
        )
        .order_by(PlanNode.order_index.asc(), PlanNode.created_at.asc())
    )
    return list(result.scalars())


async def _v1_questions(db: AsyncSession, ctx: WorkspaceContext) -> list[AgentQuestion]:
    result = await db.execute(
        select(AgentQuestion)
        .where(AgentQuestion.workspace_id == ctx.id, AgentQuestion.v1_key.is_not(None))
        .order_by(AgentQuestion.created_at.asc())
    )
    return list(result.scalars())


# =================================================================================
# 建:分组(PlanNode)+ 固定问题节点(AgentQuestion)
# =================================================================================
async def _create_question(
    db: AsyncSession,
    ctx: WorkspaceContext,
    *,
    group: PlanNode,
    key: str,
    question: str,
    why_now: str,
) -> AgentQuestion:
    row = AgentQuestion(
        workspace_id=ctx.id,
        source_node_id=group.id,
        source_message_id=None,
        presentation=QuestionPresentation.CANVAS_QUESTION,
        question=question,
        why_now=why_now,
        response_mode=QuestionResponseMode.FREE_TEXT,
        options=[],
        allow_custom_input=True,
        status=QuestionStatus.PENDING,
        events=[],
        v1_key=key,
    )
    db.add(row)
    await db.flush()
    return row


async def _create_containers(
    db: AsyncSession, ctx: WorkspaceContext, root: PlanNode
) -> None:
    """建立三组画布节点与十个固定紫色问题节点。**幂等**。"""
    existing = await db.scalar(
        select(PlanNode.id)
        .where(
            PlanNode.workspace_id == ctx.id,
            PlanNode.parent_id == root.id,
            PlanNode.deleted_at.is_(None),
        )
        .limit(1)
    )
    if existing is not None:
        return

    groups: dict[str, PlanNode] = {}
    for key, title, summary in _GROUPS:
        result = await node_service.create_node(
            db,
            ctx,
            parent_id=root.id,
            title=title,
            node_type=NodeType.CAPABILITY.value,
            purpose=NodePurpose.INFORMATION.value,
            description=summary,
            origin=NodeOrigin.AI,
            v1_key=key,
        )
        groups[key] = result.node

    for group_key, key, question, why_now in _ANALYSIS:
        await _create_question(
            db, ctx, group=groups[group_key], key=key, question=question, why_now=why_now
        )


async def _ensure_strategy_questions(
    db: AsyncSession, ctx: WorkspaceContext, values: dict[str, str]
) -> None:
    """战略成形时,在“战略路径”分组下建立受限问题节点(每个键最多一次)。"""
    groups = {group.v1_key: group for group in await _v1_groups(db, ctx)}
    group = groups.get(_STRATEGY_GROUP_KEY)
    if group is None:
        return
    existing = {q.v1_key for q in await _v1_questions(db, ctx)}
    for key, title, question in _STRATEGY_KEYS:
        if key not in values or key in existing:
            continue
        row = await _create_question(
            db, ctx, group=group, key=key, question=question, why_now=title
        )
        row.analysis_summary = values[key]
        row.v1_analysis = {"judgment": values[key], "status": "discussing"}


# =================================================================================
# 回合上下文
# =================================================================================
def _render_canvas(groups: list[PlanNode], questions: list[AgentQuestion]) -> str:
    by_group: dict[str | None, list[AgentQuestion]] = {}
    for question in questions:
        by_group.setdefault(str(question.source_node_id) if question.source_node_id else None, []).append(question)
    lines: list[str] = []
    title_of = {group.v1_key: group.title for group in groups}
    for key, title, _summary in _GROUPS:
        if key not in title_of:
            continue
        group = next(group for group in groups if group.v1_key == key)
        lines.append(f"[{key}] {title}")
        for question in by_group.get(str(group.id), []):
            lines.append(f"  - [{question.v1_key}] {question.question}")
            analysis = question.v1_analysis or {}
            if analysis.get("judgment"):
                lines.append(f"      判断:{analysis['judgment']}")
            if analysis.get("knownFacts"):
                lines.append("      已知事实:" + ";".join(str(x) for x in analysis["knownFacts"]))
            if analysis.get("assumptions"):
                lines.append("      AI 假设(未验证):" + ";".join(str(x) for x in analysis["assumptions"]))
            if question.answer:
                answer = question.answer.get("customInput") if isinstance(question.answer, dict) else None
                if answer:
                    lines.append(f"      用户已回答:{answer}")
            lines.append(
                f"      状态:{analysis.get('status', 'unexplored')} / "
                f"不确定性:{analysis.get('uncertainty', 'medium')} / 提问状态:{question.status.value}"
            )
    strategy_keys = " / ".join(key for key, _t, _q in _STRATEGY_KEYS)
    if _STRATEGY_GROUP_KEY in title_of:
        lines.append(f"战略路径可用子项键:{strategy_keys}(信息足够时才用)")
    return "\n".join(lines) if lines else "(还没有固定问题)"


async def _build_turn_context(
    db: AsyncSession,
    ctx: WorkspaceContext,
    session: GoalReasoningSession,
    root: PlanNode,
    *,
    user_message: str,
    exclude_message_id=None,
) -> TurnContext:
    questions = await _v1_questions(db, ctx)
    page = await conversation_service.list_messages(db, ctx, limit=HISTORY_TURNS + 1)
    history = tuple(
        (message.role.value, message.content)
        for message in page.messages
        if message.id != exclude_message_id
    )[-HISTORY_TURNS:]
    today = today_in(ctx.timezone)
    return TurnContext(
        current_date=today.isoformat(),
        weekday=today.strftime("%A"),
        timezone=ctx.timezone,
        workspace_title=ctx.workspace.title or "",
        workspace_intent=ctx.workspace.intent or "",
        known=KnownConditions(goal=root.title or None),
        nodes=tuple(
            PlanNodeView(
                handle=question.v1_key or "",
                title=question.question[:80],
                node_type="question",
                status=question.status.value,
                depth=1,
                purpose="information",
            )
            for question in questions
            if question.v1_key
        ),
        history=history,
        user_message=user_message,
        purpose="v1_strategy",
        reasoning_section=_render_canvas(await _v1_groups(db, ctx), questions),
        node_handles=tuple(
            (question.v1_key, str(question.id)) for question in questions if question.v1_key
        ),
    )


# =================================================================================
# 应用模型判断
# =================================================================================
def _apply_updates(
    questions: list[AgentQuestion],
    draft,
    *,
    classification: str,
    is_local_discussion: bool,
    discussion_key: str | None,
    raw_message: str,
) -> tuple[list[AgentQuestion], dict[str, dict], dict[str, str]]:
    """把形状合法的判断落到**已存在的分析节点**上。

    P2.1:
    - `knownFacts` 只接受战略事实/偏好/纠正;元对话/含混内容一律不进事实;
    - `discussionCount` 只统计**该节点的真正局部讨论/回答**,全局对话不虚增;
    - **只有确有新判断/新事实/新假设时才写**;内容未变则不动,也不产生审计事件。
    """
    by_key = {question.v1_key: question for question in questions if question.v1_key}
    changed: list[AgentQuestion] = []
    analyses: dict[str, dict] = {}
    strategy_values: dict[str, str] = {}

    # node_updates 优先;keyDimensions 只在未被 nodeUpdates 覆盖时补充判断。
    normalized: list[tuple] = [
        (
            update.node_key,
            update.judgment,
            update.importance_reason,
            update.known_facts,
            update.assumptions,
            update.evidence,
            update.uncertainty,
            update.status,
            update.impacted_node_keys,
        )
        for update in draft.node_updates[:MAX_V1_UPDATES]
    ]
    covered = {item[0] for item in normalized}
    for dimension in draft.key_dimensions[:MAX_V1_DIMENSIONS]:
        if dimension.key in covered or not dimension.judgment:
            continue
        normalized.append(
            (dimension.key, dimension.judgment, dimension.why_it_matters, (), (), (), "medium", "discussing", ())
        )

    for key, judgment, importance, facts, assumptions, evidence, uncertainty, status, impacts_raw in normalized:
        if key not in ALLOWED_V1_KEYS:
            continue
        if key in _STRATEGY_KEY_SET:
            if judgment:
                strategy_values[key] = judgment
            continue
        question = by_key.get(key)
        if question is None:
            continue
        existing = dict(question.v1_analysis or {})
        new_facts = _sanitized_facts(facts, classification=classification, raw_message=raw_message)
        analysis = {
            "judgment": judgment or existing.get("judgment", ""),
            "knownFacts": new_facts or list(existing.get("knownFacts", [])),
            "assumptions": list(assumptions) or list(existing.get("assumptions", [])),
            "evidence": list(evidence) or list(existing.get("evidence", [])),
            "importanceReason": importance or existing.get("importanceReason", ""),
            "uncertainty": uncertainty,
            "status": status,
            "impactedNodeKeys": [item for item in impacts_raw if item in by_key],
            "discussionCount": int(existing.get("discussionCount", 0))
            + (1 if is_local_discussion and key == discussion_key else 0),
        }
        if _analysis_unchanged(existing, analysis):
            continue
        question.v1_analysis = analysis
        # 卡片/问题节点上的一句话:分析摘要与依据。
        if analysis["judgment"]:
            question.analysis_summary = analysis["judgment"]
        if analysis["importanceReason"]:
            question.decision_impact = analysis["importanceReason"]
        if analysis["assumptions"]:
            question.confidence_note = "AI 假设:" + ";".join(str(x) for x in analysis["assumptions"])
        analyses[key] = analysis
        changed.append(question)
    return changed, analyses, strategy_values


def _analysis_unchanged(existing: dict, analysis: dict) -> bool:
    """判断是否与库里那一份实质相同 —— 相同就不写、不发事件。"""
    keys = (
        "judgment",
        "knownFacts",
        "assumptions",
        "evidence",
        "importanceReason",
        "uncertainty",
        "status",
        "impactedNodeKeys",
        "discussionCount",
    )
    for key in keys:
        before = existing.get(key)
        after = analysis.get(key)
        if isinstance(before, list) or isinstance(after, list):
            if list(before or []) != list(after or []):
                return False
        elif before != after:
            return False
    return True


def _sanitized_facts(facts, *, classification: str, raw_message: str) -> list[str]:
    """只有战略事实/偏好/纠正能写事实;元对话与含混内容一律丢弃。"""
    if classification not in (
        INPUT_STRATEGIC_FACT,
        INPUT_USER_PREFERENCE,
        INPUT_USER_CORRECTION,
    ):
        return []
    clean: list[str] = []
    for item in facts or ():
        text = str(item).strip()
        if not text:
            continue
        if any(marker in text for marker in _META_MARKERS):
            continue
        clean.append(text)
    return clean


def _strategy_ready(analyses: dict[str, dict]) -> bool:
    """服务端的战略成形条件。**模型说了不算,这里再判一次。**"""

    def has(key: str) -> bool:
        return bool((analyses.get(key) or {}).get("judgment"))

    return bool(
        has("goal_definition")
        and has("key_conflict")
        and (has("key_levers") or has("hard_constraints"))
        and has("major_risks")
    )


def _strategy_understanding_payload(strategy: dict, analyses: dict) -> dict:
    """从战略四条结构 + 目标/矛盾分析,组织“我据此形成的战略理解”。"""
    return {
        "goal": str((analyses.get("goal_definition") or {}).get("judgment") or ""),
        "keyConflict": str((analyses.get("key_conflict") or {}).get("judgment") or ""),
        "mainLine": strategy.get("mainLine", ""),
        "parallelLine": strategy.get("parallelLine", ""),
        "deferOrAvoid": strategy.get("deferOrAvoid", ""),
        "riskControl": strategy.get("riskControl", ""),
        "tradeoff": strategy.get("tradeoff", ""),
        "confirmed": False,
    }


async def _build_strategy_synthesis_context(
    db: AsyncSession, ctx: WorkspaceContext, session: GoalReasoningSession
) -> TurnContext:
    """战略合成回合的上下文:已确认的目标定义 + 既有分析画布。"""
    questions = await _v1_questions(db, ctx)
    groups = await _v1_groups(db, ctx)
    page = await conversation_service.list_messages(db, ctx, limit=HISTORY_TURNS)
    history = tuple((m.role.value, m.content) for m in page.messages)[-HISTORY_TURNS:]
    today = today_in(ctx.timezone)
    goal = next((q for q in questions if q.v1_key == "goal_definition"), None)
    lines = [
        "已确认/待确认的目标定义:",
        "- "
        + (
            ((goal.v1_analysis or {}).get("judgment") if goal else None)
            or "(尚未形成)"
        ),
        "",
        "既有分析画布:",
        _render_canvas(groups, questions),
    ]
    return TurnContext(
        current_date=today.isoformat(),
        weekday=today.strftime("%A"),
        timezone=ctx.timezone,
        workspace_title=ctx.workspace.title or "",
        workspace_intent=ctx.workspace.intent or "",
        known=KnownConditions(),
        history=history,
        user_message="把已确认的目标定义与既有分析合成为四条战略结构。",
        purpose="v1_strategy_synthesis",
        reasoning_section="\n".join(lines),
    )


async def synthesize_strategy(
    db: AsyncSession,
    ctx: WorkspaceContext,
    session: GoalReasoningSession,
    reasoner,
    *,
    trigger: str = "strategy_synthesis",
) -> ReasoningResult:
    """**窄契约战略合成回合**:只要四条结构 + 取舍,四条齐全才接受。

    - 来源不是真实 OpenJiuwen 时准确失败(同 R1);
    - 缺任一结构 -> `MODEL_OUTPUT_INVALID` 可重试,不落半成品;
    - 成功 -> 写 `v1_strategy`(未确认)、建战略子节点、进入 `strategy_draft`。
    """
    root = await reasoning_service.root_plan_node(db, ctx)
    if root is None:
        from backend.services.errors import InvalidInput

        raise InvalidInput("这个空间还没有根目标。")
    await _create_containers(db, ctx, root)

    source = reasoner_source_kind(reasoner)
    if not source_allowed(source):
        return await _refuse_unavailable_source(
            db, ctx, session, source=source, trigger=trigger
        )

    turn = await _build_strategy_synthesis_context(db, ctx, session)
    stage_before = session.v1_stage
    _start_turn(session, stage=stage_before, trigger=trigger, source=source)
    await db.commit()

    result = await _reason_with_timeout(reasoner, turn)
    draft = result.v1_strategy
    if result.degraded:
        session.v1_status = V1_STATUS_FAILED
        _finish_turn(session, status=V1_STATUS_FAILED)
        session.v1_error = result.reply or "模型暂时不可用。"
        await _audit_turn_failure(
            db, ctx, session, result, trigger=trigger, stage_before=stage_before
        )
        await db.commit()
        return result
    if draft is None:
        session.v1_status = V1_STATUS_FAILED
        _finish_turn(session, status=V1_STATUS_FAILED)
        session.v1_error = "模型这次没有按契约给出四条战略结构,可以重试。"
        await _audit(
            db,
            ctx,
            session,
            "model_output_invalid",
            trigger=trigger,
            stage_before=stage_before,
            stage_after=session.v1_stage,
            source=result.source.value if result.source else None,
            summary=session.v1_error,
            validation_status="failed",
            error_code="MODEL_OUTPUT_INVALID",
        )
        await db.commit()
        return ReasoningResult(
            reply=session.v1_error,
            source=result.source,
            degraded=True,
            degraded_reason=DegradedReason.MODEL_OUTPUT_INVALID,
            retryable=True,
            request_id=result.request_id,
            prompt_version=result.prompt_version,
        )

    values = {
        "main_line": draft.main_line,
        "parallel_line": draft.parallel_line,
        "defer_or_avoid": draft.defer_or_avoid,
        "risk_control": draft.risk_control,
    }
    await _ensure_strategy_questions(db, ctx, values)
    merged = dict(session.v1_strategy or {})
    for key, value in values.items():
        merged[_STRATEGY_FIELD.get(key, key)] = value
    merged["tradeoff"] = draft.tradeoff or merged.get("tradeoff", "")
    merged["confirmed"] = False
    session.v1_strategy = merged
    if session.v1_stage in (V1_GOAL_REFRAME, V1_FACTOR_ANALYSIS, V1_PROBLEM_STRUCTURE):
        session.v1_stage = V1_STRATEGY_DRAFT
    # 深度对话:先把“战略理解”作为可审阅对象存下来(目标 / 关键矛盾 / 主线 / 暂缓 /
    # 风险 / 取舍),前端先展示它,用户确认理解后才进入正式战略确认。
    questions = await _v1_questions(db, ctx)
    by_key = {q.v1_key: (q.v1_analysis or {}) for q in questions if q.v1_key}
    session.v1_strategy_understanding = _strategy_understanding_payload(merged, by_key)
    session.v1_question = None
    session.v1_next_action = None
    session.phase = ReasoningSessionPhase.ROADMAP_DRAFT
    session.status = ReasoningSessionStatus.READY
    _finish_turn(session, status=V1_STATUS_IDLE)
    session.v1_error = None
    await _audit(
        db,
        ctx,
        session,
        "strategy_draft_generated",
        trigger=trigger,
        stage_before=stage_before,
        stage_after=session.v1_stage,
        source=result.source.value if result.source else None,
        summary="窄契约战略合成回合产出四条结构。",
        payload={"strategy": session.v1_strategy},
    )
    await _audit(
        db,
        ctx,
        session,
        "strategy_understanding_presented",
        trigger=trigger,
        stage_after=session.v1_stage,
        summary="先给出“我据此形成的战略理解”,等用户确认或指出哪一句不对。",
        payload={"understanding": session.v1_strategy_understanding},
    )
    await _audit(
        db,
        ctx,
        session,
        "strategy_review_ready",
        trigger=trigger,
        stage_after=session.v1_stage,
        summary="战略草案已就绪,等用户确认或调整。",
    )
    await db.commit()
    return result


async def _maybe_synthesize_strategy(
    db: AsyncSession,
    ctx: WorkspaceContext,
    session: GoalReasoningSession,
    reasoner,
    *,
    trigger: str,
) -> ReasoningResult | None:
    """通用回合没能合出战略时的**窄契约兜底回合**。

    真实模型(DeepSeek-chat 实测)会在 `problem_structure` 反复给白话、`nodeUpdates`
    为空、`strategyReady` 甚至为 false。所以这里**不依赖**模型声明,也不依赖四个分析
    维度是否齐全 —— 只要通用回合还没合出战略、没有待答问题,就让窄契约回合用现有
    画布 + 历史合一次。四条齐全才接受,否则准确失败。
    """
    if session.v1_strategy:
        return None
    if session.v1_stage not in (
        V1_GOAL_REFRAME,
        V1_FACTOR_ANALYSIS,
        V1_PROBLEM_STRUCTURE,
    ):
        return None
    # 还在等用户回答关键问题时,不抢跑合成 —— 那一问就是战略的输入。
    if (session.v1_question or "").strip():
        return None
    # 上一轮已经准确失败(来源/输出)时不再叠一次模型调用;重试由用户发起。
    if session.v1_status == V1_STATUS_FAILED:
        return None
    result = await synthesize_strategy(db, ctx, session, reasoner, trigger=trigger)
    await _append_assistant(
        db,
        ctx,
        reply=result.reply,
        conversation=await conversation_service.get_or_create_primary_conversation(db, ctx),
        result=result,
    )
    await db.commit()
    return result


async def _sync_question_states(
    db: AsyncSession, ctx: WorkspaceContext, session: GoalReasoningSession
) -> None:
    """把画布分析节点的状态统一到**分析事实**上,消除 `AgentQuestion.status`
    与 `v1Analysis.status` 的双真相:

    - 已写入判断 -> `resolved` / `investigating`;
    - 真正等待用户回答的那个焦点 -> `pending`;
    - 尚未判断 -> `pending`(由 visibility/internal 投影隐藏,不再伪造成 `archived`)。

    **隐藏不用状态表达** —— 那是 `dimension_projection.visible/internal` 的职责。
    分组节点状态由子分析节点汇总,不再永远 `pending`。
    """
    questions = await _v1_questions(db, ctx)
    visible = visible_dimension_keys(session)
    pending_key = session.v1_focus_key if (session.v1_question or "").strip() else None
    for question in questions:
        if not question.v1_key or question.v1_key in _STRATEGY_KEY_SET:
            continue
        analysis = question.v1_analysis or {}
        if analysis.get("judgment"):
            question.status = (
                QuestionStatus.RESOLVED
                if analysis.get("status") == "resolved"
                else QuestionStatus.INVESTIGATING
            )
        elif question.v1_key in visible:
            # 默认可见但尚未写入判断:是 AI 正在形成的维度,不是待用户回答的问卷。
            question.status = (
                QuestionStatus.PENDING
                if question.v1_key == pending_key
                else QuestionStatus.INVESTIGATING
            )
        else:
            # 内部维度:状态照实为 pending,由 visibility/internal 投影隐藏(不用 archived)。
            question.status = QuestionStatus.PENDING

    groups = await _v1_groups(db, ctx)
    by_group: dict = {}
    for question in questions:
        by_group.setdefault(question.source_node_id, []).append(question)
    for group in groups:
        children = by_group.get(group.id, [])
        if not children:
            continue
        if all(child.status is QuestionStatus.RESOLVED for child in children):
            group.status = NodeStatus.COMPLETED
        elif any(
            child.status in (QuestionStatus.RESOLVED, QuestionStatus.INVESTIGATING)
            for child in children
        ):
            group.status = NodeStatus.DOING
        else:
            group.status = NodeStatus.PENDING


# =================================================================================
# 回合执行
# =================================================================================
async def _run_assessment(
    db: AsyncSession,
    ctx: WorkspaceContext,
    session: GoalReasoningSession,
    *,
    user_message: str,
    reasoner,
    classification: str,
    is_local_discussion: bool = False,
    force_no_question: bool = False,
    trigger: str = "user_message",
    exclude_message_id=None,
) -> ReasoningResult:
    root = await reasoning_service.root_plan_node(db, ctx)
    if root is None:
        from backend.services.errors import InvalidInput

        raise InvalidInput("这个空间还没有根目标。")
    # 任何 V1 回合都保证固定画布存在(幂等)——首轮自动判断也要建立分组。
    await _create_containers(db, ctx, root)
    stage_before = session.v1_stage
    _existing = await _v1_questions(db, ctx)
    before_status = {
        q.v1_key: (q.v1_analysis or {}).get("status") for q in _existing if q.v1_key
    }
    turn = await _build_turn_context(
        db, ctx, session, root, user_message=user_message, exclude_message_id=exclude_message_id
    )
    source = reasoner_source_kind(reasoner)
    if not source_allowed(source):
        return await _refuse_unavailable_source(
            db, ctx, session, source=source, trigger=trigger
        )
    _start_turn(session, stage=stage_before, trigger=trigger, source=source)
    await db.commit()

    result = await _reason_with_timeout(reasoner, turn)
    assessment = result.v1_assessment

    if result.degraded:
        session.v1_status = V1_STATUS_FAILED
        _finish_turn(session, status=V1_STATUS_FAILED)
        session.v1_error = result.reply or "模型暂时不可用。"
        await _audit_turn_failure(
            db, ctx, session, result, trigger=trigger, stage_before=stage_before
        )
        await db.commit()
        return result
    if assessment is None or not (
        assessment.global_assessment
        or assessment.strategic_thesis
        or assessment.node_updates
        or assessment.key_dimensions
    ):
        session.v1_status = V1_STATUS_FAILED
        _finish_turn(session, status=V1_STATUS_FAILED)
        session.v1_error = "模型这次的回答没能解析成战略判断。"
        await _audit(
            db,
            ctx,
            session,
            "model_output_invalid",
            trigger="user_message",
            source=result.source.value if result.source else None,
            summary=session.v1_error,
            validation_status="failed",
            error_code="MODEL_OUTPUT_INVALID",
        )
        await db.commit()
        return ReasoningResult(
            reply="模型这次的回答没能解析成战略判断,可以再试一次。",
            source=result.source,
            degraded=True,
            degraded_reason=DegradedReason.MODEL_OUTPUT_INVALID,
            retryable=True,
            request_id=result.request_id,
            prompt_version=result.prompt_version,
            model_name=result.model_name,
            latency_ms=result.latency_ms,
        )

    questions = await _v1_questions(db, ctx)
    changed, analyses, strategy_values = _apply_updates(
        questions,
        assessment,
        classification=classification,
        is_local_discussion=is_local_discussion,
        discussion_key=assessment.focus_key,
        raw_message=user_message,
    )
    persisted = {q.v1_key: (q.v1_analysis or {}) for q in questions if q.v1_key}

    from_problem_structure = session.v1_stage == V1_PROBLEM_STRUCTURE
    #: 本回合是否形成了战略草案(决定 problem_structure 是否还需要兜底 CTA)。
    synthesized = False
    #: 本回合是否首次呈现了“战略理解”。
    understanding_presented = False
    if strategy_values and _strategy_ready(persisted):
        await _ensure_strategy_questions(db, ctx, strategy_values)
        merged = dict(session.v1_strategy or {})
        for key, value in strategy_values.items():
            merged[_STRATEGY_FIELD.get(key, key)] = value
        merged["tradeoff"] = assessment.strategy_tradeoff or merged.get("tradeoff", "")
        merged["confirmed"] = False
        session.v1_strategy = merged
        if session.v1_stage in (V1_GOAL_REFRAME, V1_FACTOR_ANALYSIS, V1_PROBLEM_STRUCTURE):
            session.v1_stage = V1_STRATEGY_DRAFT
        if not session.v1_strategy_understanding:
            session.v1_strategy_understanding = _strategy_understanding_payload(merged, persisted)
            understanding_presented = True
        synthesized = True

    thesis = assessment.strategic_thesis or assessment.global_assessment
    if thesis:
        session.v1_strategic_thesis = thesis
        session.v1_judgment = thesis

    # ---- P2.1 服务端追问守卫:默认不问;同焦点最多 1 次;总预算 3;低信息强制给候选 ----
    budget_used = int(session.v1_question_budget_used or 0)
    last_focus = session.v1_last_focus_key
    low_streak = int(session.v1_low_info_streak or 0)
    #: 兼容旧字段:模型/测试可能只给 `question`。
    proposed_question = (assessment.critical_question or assessment.question or "").strip()
    same_focus_repeat = bool(
        proposed_question and assessment.focus_key and assessment.focus_key == last_focus
    )
    budget_exhausted = budget_used >= MAX_V1_QUESTIONS
    force_options = low_streak >= 2
    question_accepted = bool(proposed_question) and not (
        same_focus_repeat or budget_exhausted or force_options or force_no_question
    )

    # ---- R2:选项问卷 -> 有解释的战略对话 ----
    # 候选方向是“选择题”;只允许在**真正的有限战略分叉**上、且**不连续重复**时出现,
    # 且必须同时带 decisionContext / provisionalRecommendation / optionImpact。
    direction_locked = bool(session.v1_selected_direction)
    offered = () if direction_locked else assessment.candidate_directions
    options_streak = int(session.v1_options_streak or 0)

    if force_options:
        # 用户连续两轮答不上来:不再问,强制给候选方向或退回暂定综合。
        response_mode = "offer_options" if len(offered) >= 2 else "provisional_synthesis"
    else:
        response_mode = assessment.response_mode

    offer_allowed = bool(
        response_mode == "offer_options"
        and len(offered) >= 2
        and options_streak < 1
        and not direction_locked
        and not question_accepted
    )
    if response_mode == "offer_options" and not offer_allowed:
        # 不是真正的分叉 / 连续重复 / 用户已选过起点:退回“暂定综合”,
        # 给出判断与推荐,不再抛选择题。
        response_mode = "provisional_synthesis"

    if offer_allowed:
        session.v1_candidate_directions = [
            {
                "key": d.key,
                "title": d.title,
                "reason": d.reason,
                "path": d.path,
                "impact": d.impact,
            }
            for d in offered
        ]
        session.v1_decision_context = assessment.decision_context or None
        session.v1_provisional_recommendation = (
            assessment.provisional_recommendation or None
        )
        session.v1_options_streak = options_streak + 1
    else:
        session.v1_decision_context = None
        session.v1_provisional_recommendation = None
        session.v1_options_streak = 0
        if not direction_locked and assessment.candidate_directions:
            # 被守卫拒绝的一组裸选项不留在页面上继续当问卷。
            session.v1_candidate_directions = None

    if assessment.focus_key:
        session.v1_focus_key = assessment.focus_key
        session.v1_focus_reason = assessment.focus_reason or None
    # 深度对话:问题之前必须先给“对用户已说内容的具体理解 / 会改变什么 / 一个例子”。
    if assessment.user_understanding:
        session.v1_user_understanding = assessment.user_understanding
    if assessment.question_example:
        session.v1_question_example = assessment.question_example
    if question_accepted:
        session.v1_question = proposed_question
        session.v1_last_focus_key = assessment.focus_key
        session.v1_question_budget_used = budget_used + 1
        if assessment.decision_context:
            session.v1_decision_context = assessment.decision_context
    else:
        session.v1_question = None
    if session.v1_stage == V1_INITIAL_THINKING:
        session.v1_stage = V1_GOAL_REFRAME
    # P2.3:非终态阶段不允许“idle + 无问题 + 无 CTA + 无战略”。
    # `v1_next_action` 是**兜底 CTA**(如 continue_strategy);一般的“下一步是什么”
    # 由 `compute_next_action` 在读视图时现算(见 `v1_workflow_next`)。
    if session.v1_stage == V1_PROBLEM_STRUCTURE:
        session.v1_next_action = (
            None if (synthesized or question_accepted) else NEXT_CONTINUE_STRATEGY
        )
    else:
        session.v1_next_action = None
    session.phase = ReasoningSessionPhase.ROADMAP_DRAFT
    session.status = ReasoningSessionStatus.READY
    _finish_turn(session, status=V1_STATUS_IDLE)
    session.v1_error = None

    # ---- 审计:战略判断 / 关键问题 / 候选方向 / 节点更新 / 战略草案 ----
    await _audit(
        db,
        ctx,
        session,
        "strategic_thesis_generated",
        trigger=trigger,
        stage_before=stage_before,
        stage_after=session.v1_stage,
        focus_key=session.v1_focus_key,
        focus_reason=session.v1_focus_reason,
        source=result.source.value if result.source else None,
        summary=thesis,
        payload={
            "strategicThesis": thesis,
            "keyDimensions": [
                {
                    "key": dimension.key,
                    "judgment": dimension.judgment,
                    "whyItMatters": dimension.why_it_matters,
                }
                for dimension in assessment.key_dimensions
            ],
            "responseMode": response_mode,
        },
    )
    await _audit(
        db,
        ctx,
        session,
        "global_assessment_generated",
        trigger=trigger,
        stage_before=stage_before,
        stage_after=session.v1_stage,
        focus_key=session.v1_focus_key,
        focus_reason=session.v1_focus_reason,
        source=result.source.value if result.source else None,
        summary=thesis,
        payload={"globalAssessment": assessment.global_assessment or thesis},
    )
    if question_accepted:
        await _audit(
            db,
            ctx,
            session,
            "global_question_asked",
            trigger=trigger,
            stage_before=stage_before,
            stage_after=session.v1_stage,
            focus_key=assessment.focus_key,
            focus_reason=assessment.focus_reason or None,
            summary=proposed_question,
            payload={
                "question": proposed_question,
                "focus": assessment.focus_key,
                "reason": assessment.focus_reason,
                "responseMode": response_mode,
            },
        )
    if offer_allowed:
        await _audit(
            db,
            ctx,
            session,
            "candidate_directions_offered",
            stage_before=stage_before,
            stage_after=session.v1_stage,
            focus_key=session.v1_focus_key,
            summary="给出候选方向,由用户选择、修正或否定。",
            payload={
                "decisionContext": session.v1_decision_context,
                "provisionalRecommendation": session.v1_provisional_recommendation,
                "optionImpact": [
                    {"key": d.key, "impact": d.impact} for d in offered
                ],
                "candidateDirections": session.v1_candidate_directions,
            },
        )
    if response_mode == "provisional_synthesis":
        await _audit(
            db,
            ctx,
            session,
            "provisional_synthesis_created",
            summary=thesis,
            payload={"strategicThesis": thesis},
        )
    node_updates = [
        {
            "key": question.v1_key,
            "beforeStatus": before_status.get(question.v1_key),
            "afterStatus": analyses[question.v1_key]["status"],
            "summary": analyses[question.v1_key]["judgment"],
            "facts": analyses[question.v1_key]["knownFacts"],
            "assumptions": analyses[question.v1_key]["assumptions"],
            "evidence": analyses[question.v1_key]["evidence"],
        }
        for question in changed
    ]
    if node_updates:
        await _audit(
            db,
            ctx,
            session,
            "node_analysis_updated",
            trigger="question_answered" if is_local_discussion else "user_message",
            focus_key=session.v1_focus_key,
            summary=f"更新了 {len(node_updates)} 个分析节点。",
            payload={"nodeUpdates": node_updates},
        )
    if understanding_presented and session.v1_strategy_understanding:
        await _audit(
            db,
            ctx,
            session,
            "strategy_understanding_presented",
            trigger=trigger,
            stage_before=stage_before,
            stage_after=session.v1_stage,
            summary="先给出“我据此形成的战略理解”,等用户确认或指出哪一句不对。",
            payload={"understanding": session.v1_strategy_understanding},
        )
    if strategy_values and session.v1_strategy:
        await _audit(
            db,
            ctx,
            session,
            "strategy_draft_generated",
            stage_before=stage_before,
            stage_after=session.v1_stage,
            focus_key=session.v1_focus_key,
            summary="形成战略路径草案。",
            payload={"strategy": session.v1_strategy},
        )
        if from_problem_structure:
            await _audit(
                db,
                ctx,
                session,
                "problem_structure_synthesized",
                stage_before=V1_PROBLEM_STRUCTURE,
                stage_after=session.v1_stage,
                summary="problem_structure 中主动完成第一版战略路径。",
                payload={"strategy": session.v1_strategy},
            )
        await _audit(
            db,
            ctx,
            session,
            "strategy_review_ready",
            stage_after=session.v1_stage,
            summary="战略草案已就绪,等用户确认或调整。",
        )
    # R2:把画布节点状态统一到分析事实(消除双真相 + 分组不再永远 pending)。
    await _sync_question_states(db, ctx, session)
    await db.commit()
    return result


async def answer_v1_in_conversation(
    db: AsyncSession,
    ctx: WorkspaceContext,
    reasoner,
    session: GoalReasoningSession,
    *,
    content: str,
    client_message_id: str | None,
    context_node_id,
    trigger: str = "user_message",
):
    """V1 空间里的用户消息(含紫色问题节点的回答):交给模型做战略判断。"""
    conversation = await conversation_service.get_or_create_primary_conversation(db, ctx)
    user_message = await conversation_service.record_user_message(
        db,
        ctx,
        conversation=conversation,
        text=content,
        client_message_id=client_message_id,
        context_node_id=context_node_id,
    )
    existing = await conversation_service.find_reply_after(
        db, conversation.id, user_message.seq
    )
    if existing is not None and existing.role is MessageRole.ASSISTANT:
        return conversation_service.turn_outcome_for_reply(
            user_message=user_message, assistant_message=existing, brief=None
        )

    root = await reasoning_service.root_plan_node(db, ctx)
    if root is not None:
        await _create_containers(db, ctx, root)

    # ---- P2.1 输入分类:元对话 / 情绪 / 对 AI 的反馈不是战略事实 ----
    classification = classify_user_message(content)
    is_local_discussion = trigger == "question_answered" or context_node_id is not None
    await _audit(
        db,
        ctx,
        session,
        "user_message_received",
        trigger=trigger,
        stage_before=session.v1_stage,
        focus_key=session.v1_focus_key,
        summary=content,
        payload={"classification": classification, "isLocalDiscussion": is_local_discussion},
    )
    if classification == INPUT_CONVERSATION_FEEDBACK:
        await _audit(
            db,
            ctx,
            session,
            "conversation_feedback_received",
            trigger=trigger,
            summary=content,
            payload={"classification": classification},
        )
    # 连续低信息 / 元对话回答计数:>=2 时服务端强制给候选方向或暂定综合。
    if _counts_as_low_info(classification):
        session.v1_low_info_streak = int(session.v1_low_info_streak or 0) + 1
    else:
        session.v1_low_info_streak = 0

    # ---- 审计:首轮提交 / 紫色问题回答 ----
    if session.v1_stage == V1_INITIAL_THINKING:
        await _audit(
            db,
            ctx,
            session,
            "initial_thinking_submitted",
            trigger=trigger,
            stage_before=V1_INITIAL_THINKING,
            summary=content,
        )
    elif is_local_discussion:
        await _audit(
            db,
            ctx,
            session,
            "canvas_question_answered",
            trigger=trigger,
            focus_key=session.v1_focus_key,
            summary=content,
        )

    result = await _run_assessment(
        db,
        ctx,
        session,
        user_message=content,
        reasoner=reasoner,
        classification=classification,
        is_local_discussion=is_local_discussion,
        trigger=trigger,
        exclude_message_id=user_message.id,
    )
    message = await _append_assistant(
        db, ctx, reply=result.reply, conversation=conversation, result=result
    )
    await db.commit()
    return conversation_service.turn_outcome_for_reply(
        user_message=user_message, assistant_message=message, brief=None
    )


async def _response(
    db,
    ctx: WorkspaceContext,
    session: GoalReasoningSession,
    *,
    message=None,
    changed: bool = False,
    trace=None,
) -> AgentTurnResponse:
    """先提交写入、再拼视图 —— **提交必须在拼视图之前**,否则视图看到的是旧状态。"""
    if trace is not None:
        from backend.services import agent_trace_service

        agent_trace_service.mark_terminal(
            trace, degraded=False, degraded_reason=None, stopped_reason="ready_to_propose", code=None
        )
    await db.commit()
    return await reasoning_service._response(db, ctx, session, message=message, changed=changed)


async def _append_assistant(
    db,
    ctx: WorkspaceContext,
    *,
    reply: str,
    conversation: Conversation | None = None,
    result: ReasoningResult | None = None,
) -> Message:
    conversation = conversation or await conversation_service.get_or_create_primary_conversation(
        db, ctx
    )
    reason = result or ReasoningResult(
        reply=reply,
        source=ModelSource.DIRECT_LLM,
        request_id="v1",
        prompt_version="v1-strategy",
    )
    return await conversation_service.append_reply(
        db, ctx, conversation=conversation, result=reason
    )


# =================================================================================
# P3:粗时间架构(战略确认后才生成)
# =================================================================================
def _phase_category(index: int, total: int, title: str) -> str:
    """给阶段一个**语义类别标记**(不靠颜色区分):定位 / 基础闭环 / 深入建设 / 产出 / 缓冲。"""
    text = title or ""
    if any(marker in text for marker in ("缓冲", "收尾", "复盘")):
        return "缓冲"
    if total <= 1:
        return "产出"
    if index <= 1:
        return "定位"
    if index >= total:
        return "产出"
    middle = total - 2
    rank = index - 1
    return "基础闭环" if rank <= (middle + 1) // 2 else "深入建设"


def _timeline_payload(phases, *, include_dates: bool = True) -> list[dict]:
    """阶段草案 -> 前端时间轴的**唯一权威投影**(与 V0.1 同形)。

    `include_dates=False`(用户没有明确日期时)会把模型“估算出来的日期”清空,
    只保留 `startWeek`/`endWeek` 作为规划真值 —— 前端用展示锚点自行推算预测日历,
    但**不**把估算日期写进后端真值。
    """
    payload: list[dict] = []
    total = len(phases)
    for index, phase in enumerate(phases, start=1):
        category = _phase_category(index, total, phase.title)
        payload.append(
            {
                "id": f"phase-{index}",
                "index": index,
                "title": phase.title,
                "kind": "phase",
                "category": category,
                "startWeek": phase.start_week,
                "endWeek": phase.end_week,
                "startDate": phase.start_date if include_dates else None,
                "endDate": phase.end_date if include_dates else None,
                "goal": phase.goal,
                "deliverable": phase.deliverable,
                "completionCriteria": phase.completion_criteria,
                "dependsOn": phase.depends_on,
                "whyHere": f"第 {index}/{total} 阶段({category}):承接上一阶段成果并解锁下一阶段。",
                "status": "draft",
                "planNodeId": None,
            }
        )
    return payload


def _timeline_gaps(draft, *, require_weeks: bool) -> list[str]:
    """粗时间架构的**严格契约缺口**。空列表 = 合格。

    每个阶段必须有:title / 时间范围(startWeek+endWeek,或 startDate+endDate)/
    goal / deliverable / completionCriteria。用户未给截止日期时必须用相对周。
    """
    gaps: list[str] = []
    if draft is None:
        return ["缺少可解析的 v1Timeline"]
    phases = list(draft.phases)
    if not (3 <= len(phases) <= 6):
        gaps.append(f"阶段数量必须是 3–6 个(现在是 {len(phases)})")
    for index, phase in enumerate(phases, start=1):
        label = f"阶段 {index}"
        if not (phase.title or "").strip():
            gaps.append(f"{label} 缺 title")
        if not (phase.goal or "").strip():
            gaps.append(f"{label} 缺 goal")
        if not (phase.deliverable or "").strip():
            gaps.append(f"{label} 缺 deliverable")
        if not (phase.completion_criteria or "").strip():
            gaps.append(f"{label} 缺 completionCriteria")
        has_weeks = phase.start_week is not None and phase.end_week is not None
        has_dates = bool(phase.start_date and phase.end_date)
        if require_weeks and not has_weeks:
            gaps.append(f"{label} 缺相对周 startWeek/endWeek")
        elif not require_weeks and not (has_weeks or has_dates):
            gaps.append(f"{label} 缺时间范围(startWeek/endWeek 或 startDate/endDate)")
    return gaps


async def _build_timeline_turn_context(
    db: AsyncSession, ctx: WorkspaceContext, session: GoalReasoningSession
) -> TurnContext:
    page = await conversation_service.list_messages(db, ctx, limit=HISTORY_TURNS)
    history = tuple((m.role.value, m.content) for m in page.messages)[-HISTORY_TURNS:]
    today = today_in(ctx.timezone)
    strategy = session.v1_strategy or {}
    lines = ["已确认的战略逻辑:"]
    for field, label in (
        ("mainLine", "主线"),
        ("parallelLine", "并行线"),
        ("deferOrAvoid", "暂缓/放弃"),
        ("riskControl", "风险控制"),
    ):
        if strategy.get(field):
            lines.append(f"- {label}:{strategy[field]}")
    if strategy.get("tradeoff"):
        lines.append(f"- 取舍:{strategy['tradeoff']}")
    alignment = session.v1_timeline_alignment or {}
    if alignment:
        lines.append("")
        lines.append("时间架构共创结论(按此节奏排):")
        if alignment.get("cadence"):
            lines.append(f"- 默认节奏:{alignment['cadence']}")
        if alignment.get("totalSpan"):
            lines.append(f"- 总周期:{alignment['totalSpan']}")
        if alignment.get("phaseCount"):
            lines.append(f"- 预计阶段数:{alignment['phaseCount']}")
        if alignment.get("answer"):
            lines.append(f"- 用户对齐回答:{alignment['answer']}")
        lines.append("- 用户未给明确日期时只用相对周,不要伪造日历日期。")
    return TurnContext(
        current_date=today.isoformat(),
        weekday=today.strftime("%A"),
        timezone=ctx.timezone,
        workspace_title=ctx.workspace.title or "",
        workspace_intent=ctx.workspace.intent or "",
        known=KnownConditions(),
        history=history,
        user_message="把已确认的战略逻辑投影成粗时间架构。",
        purpose="v1_timeline",
        reasoning_section="\n".join(lines),
    )


async def _build_timeline_repair_context(
    db: AsyncSession,
    ctx: WorkspaceContext,
    session: GoalReasoningSession,
    *,
    partial,
    gaps: list[str],
    require_weeks: bool,
) -> TurnContext:
    """窄契约 repair 回合:只补全缺失的时间架构字段。"""
    page = await conversation_service.list_messages(db, ctx, limit=HISTORY_TURNS)
    history = tuple((m.role.value, m.content) for m in page.messages)[-HISTORY_TURNS:]
    today = today_in(ctx.timezone)
    partial_json = "(模型没有给出可解析的阶段)"
    if partial is not None:
        partial_json = json.dumps(
            [
                {
                    "title": p.title,
                    "goal": p.goal,
                    "deliverable": p.deliverable,
                    "completionCriteria": p.completion_criteria,
                    "startWeek": p.start_week,
                    "endWeek": p.end_week,
                    "startDate": p.start_date,
                    "endDate": p.end_date,
                }
                for p in partial.phases
            ],
            ensure_ascii=False,
        )
    lines = [
        "上一版粗时间架构存在以下缺口,请**只补全这些字段**,不要重写已有内容:",
        *[f"- {gap}" for gap in gaps],
        "",
        "时间范围要求:" + ("必须用相对周 startWeek/endWeek(用户没有截止日期)。" if require_weeks else "用 startDate/endDate 或 startWeek/endWeek。"),
        "",
        "上一版内容(JSON):",
        partial_json,
    ]
    return TurnContext(
        current_date=today.isoformat(),
        weekday=today.strftime("%A"),
        timezone=ctx.timezone,
        workspace_title=ctx.workspace.title or "",
        workspace_intent=ctx.workspace.intent or "",
        known=KnownConditions(),
        history=history,
        user_message="补全粗时间架构缺失的字段。",
        purpose="v1_timeline_repair",
        reasoning_section="\n".join(lines),
    )


async def _repair_coarse_timeline(
    db: AsyncSession,
    ctx: WorkspaceContext,
    session: GoalReasoningSession,
    reasoner,
    *,
    partial,
    gaps: list[str],
    require_weeks: bool,
) -> tuple[object | None, ReasoningResult]:
    """走一次窄契约 repair 回合。仍不合格返回 (None, 最后一次结果)。"""
    turn = await _build_timeline_repair_context(
        db, ctx, session, partial=partial, gaps=gaps, require_weeks=require_weeks
    )
    _start_turn(
        session, stage=session.v1_stage, trigger="timeline_repair", source=reasoner_source_kind(reasoner)
    )
    await db.commit()
    result = await _reason_with_timeout(reasoner, turn)
    repaired = result.v1_timeline
    if result.degraded or repaired is None or _timeline_gaps(repaired, require_weeks=require_weeks):
        return None, result
    return repaired, result


async def _build_timeline_alignment_context(
    db: AsyncSession, ctx: WorkspaceContext, session: GoalReasoningSession
) -> TurnContext:
    """时间架构共创回合的上下文:已确认战略 + 用户说过的条件。"""
    page = await conversation_service.list_messages(db, ctx, limit=HISTORY_TURNS)
    history = tuple((m.role.value, m.content) for m in page.messages)[-HISTORY_TURNS:]
    today = today_in(ctx.timezone)
    strategy = session.v1_strategy or {}
    lines = ["已确认的战略逻辑:"]
    for field, label in (
        ("mainLine", "主线"),
        ("parallelLine", "并行线"),
        ("deferOrAvoid", "暂缓/放弃"),
        ("riskControl", "风险控制"),
    ):
        if strategy.get(field):
            lines.append(f"- {label}:{strategy[field]}")
    if strategy.get("tradeoff"):
        lines.append(f"- 取舍:{strategy['tradeoff']}")
    understanding = session.v1_strategy_understanding or {}
    if understanding.get("goal"):
        lines.append(f"- 已确认目标:{understanding['goal']}")
    questions = await _v1_questions(db, ctx)
    facts: list[str] = []
    for question in questions:
        analysis = question.v1_analysis or {}
        facts.extend(str(item) for item in (analysis.get("knownFacts") or []))
    if facts:
        lines.append("用户已说过的条件(只能作为 user_fact):" + ";".join(facts[:8]))
    return TurnContext(
        current_date=today.isoformat(),
        weekday=today.strftime("%A"),
        timezone=ctx.timezone,
        workspace_title=ctx.workspace.title or "",
        workspace_intent=ctx.workspace.intent or "",
        known=KnownConditions(),
        history=history,
        user_message="先给出你的时间架构假设,至多问一个真正影响时间架构的战略级问题。",
        purpose="v1_timeline_alignment",
        reasoning_section="\n".join(lines),
    )


async def generate_timeline_alignment(
    db: AsyncSession,
    ctx: WorkspaceContext,
    session: GoalReasoningSession,
    reasoner,
    *,
    trigger: str = "strategy_confirmed",
) -> ReasoningResult:
    """**时间架构共创回合**:先讲清时间假设,至多问一个战略级问题。

    它**不生成**时间线、不建 proposal;只有用户对齐节奏后(`confirm_timeline_alignment`)
    才调用粗时间架构生成。
    """
    root = await reasoning_service.root_plan_node(db, ctx)
    if root is None:
        from backend.services.errors import InvalidInput

        raise InvalidInput("这个空间还没有根目标。")
    source = reasoner_source_kind(reasoner)
    if not source_allowed(source):
        return await _refuse_unavailable_source(db, ctx, session, source=source, trigger=trigger)

    turn = await _build_timeline_alignment_context(db, ctx, session)
    stage_before = session.v1_stage
    _start_turn(session, stage=stage_before, trigger=trigger, source=source)
    await db.commit()

    result = await _reason_with_timeout(reasoner, turn)
    draft = result.v1_timeline_alignment
    if result.degraded:
        session.v1_status = V1_STATUS_FAILED
        _finish_turn(session, status=V1_STATUS_FAILED)
        session.v1_error = result.reply or "模型暂时不可用。"
        await _audit_turn_failure(db, ctx, session, result, trigger=trigger, stage_before=stage_before)
        await db.commit()
        return result
    if draft is None:
        session.v1_status = V1_STATUS_FAILED
        _finish_turn(session, status=V1_STATUS_FAILED)
        session.v1_error = "模型这次没有给出时间架构假设,可以重试。"
        await _audit(
            db, ctx, session, "model_output_invalid", trigger=trigger,
            stage_before=stage_before, stage_after=session.v1_stage,
            source=result.source.value if result.source else None, summary=session.v1_error,
            validation_status="failed", error_code="MODEL_OUTPUT_INVALID",
        )
        await db.commit()
        return ReasoningResult(
            reply=session.v1_error, source=result.source, degraded=True,
            degraded_reason=DegradedReason.MODEL_OUTPUT_INVALID, retryable=True,
            request_id=result.request_id, prompt_version=result.prompt_version,
        )

    payload = {
        "summary": draft.summary,
        "totalSpan": draft.total_span,
        "cadence": draft.cadence,
        "phaseCount": draft.phase_count,
        "biggestRisk": draft.biggest_risk,
        "assumptions": [
            {"text": item.text, "source": item.source} for item in draft.assumptions
        ],
        "question": draft.question,
        "options": list(draft.options),
        "answer": "",
        "confirmed": False,
    }
    session.v1_timeline_alignment = payload
    session.v1_stage = V1_TIMELINE_ALIGNMENT
    session.v1_question = None
    session.v1_next_action = None
    session.phase = ReasoningSessionPhase.ROADMAP_DRAFT
    session.status = ReasoningSessionStatus.READY
    _finish_turn(session, status=V1_STATUS_AWAITING_CONFIRMATION)
    session.v1_error = None
    await _audit(
        db, ctx, session, "timeline_assumptions_presented",
        trigger=trigger, stage_before=stage_before, stage_after=session.v1_stage,
        source=result.source.value if result.source else None,
        summary="给出时间架构假设(总周期 / 节奏 / 阶段数 / 风险)。",
        payload=payload,
    )
    if draft.question:
        await _audit(
            db, ctx, session, "timeline_alignment_question_asked",
            trigger=trigger, stage_after=session.v1_stage,
            summary=draft.question,
            payload={"question": draft.question, "options": list(draft.options)},
        )
    await db.commit()
    return result


async def confirm_timeline_alignment(
    db: AsyncSession,
    ctx: WorkspaceContext,
    session: GoalReasoningSession,
    reasoner,
    *,
    answer: str = "",
    accepted: bool = False,
) -> AgentTurnResponse:
    """用户对齐时间节奏:记录回答/默认接受,然后才生成粗时间架构。"""
    from backend.services.errors import InvalidInput

    if session.v1_stage != V1_TIMELINE_ALIGNMENT:
        await _guard_reject(db, ctx, session, reason="当前不在时间架构共创阶段。")
        raise InvalidInput("当前不在时间架构共创阶段。")
    payload = dict(session.v1_timeline_alignment or {})
    payload["answer"] = (answer or "").strip()
    payload["confirmed"] = True
    session.v1_timeline_alignment = payload
    if payload["answer"]:
        await _audit(
            db, ctx, session, "timeline_alignment_answered",
            trigger="timeline_alignment", stage_after=session.v1_stage,
            summary=payload["answer"], payload={"answer": payload["answer"]},
        )
    else:
        await _audit(
            db, ctx, session, "timeline_alignment_accepted",
            trigger="timeline_alignment", stage_after=session.v1_stage,
            summary="用户认可默认时间节奏。",
            payload={"accepted": True, **({"acceptedDefault": True} if accepted else {})},
        )
    await db.commit()
    await generate_coarse_timeline(db, ctx, session, reasoner)
    return await reasoning_service._response(db, ctx, session, changed=True)


async def _root_handle(db: AsyncSession, ctx: WorkspaceContext, root: PlanNode):
    conversation = await conversation_service.find_primary_conversation(db, ctx)
    turn = await turn_context.build_turn_context(
        db,
        ctx,
        conversation_id=conversation.id if conversation else uuid.uuid4(),
        user_message="生成时间架构提案",
        context_node_id=root.id,
        scope_root_id=root.id,
    )
    handle = next((h for h, node_id in turn.node_handles if node_id == str(root.id)), None)
    if handle is None:
        from backend.services.errors import InvalidInput

        raise InvalidInput("找不到根目标的记号。")
    return handle, turn.node_handles


async def generate_coarse_timeline(
    db: AsyncSession,
    ctx: WorkspaceContext,
    session: GoalReasoningSession,
    reasoner,
    *,
    trace=None,
) -> ReasoningResult:
    """把已确认战略投影为 3–6 个阶段,落成**待确认提案**。不写正式计划。"""
    from backend.services.errors import InvalidInput

    if session.v1_stage not in (V1_STRATEGY_CONFIRMED, V1_TIMELINE_ALIGNMENT):
        await _guard_reject(db, ctx, session, reason="当前不在“可生成粗时间架构”的状态。")
        raise InvalidInput("当前不在“可生成粗时间架构”的状态。")
    root = await reasoning_service.root_plan_node(db, ctx)
    if root is None:
        raise InvalidInput("这个空间还没有根目标。")
    turn = await _build_timeline_turn_context(db, ctx, session)
    source = reasoner_source_kind(reasoner)
    if not source_allowed(source):
        return await _refuse_unavailable_source(
            db, ctx, session, source=source, trigger="strategy_confirmation"
        )
    _start_turn(session, stage=V1_STRATEGY_CONFIRMED, trigger="strategy_confirmation", source=source)
    await db.commit()

    result = await _reason_with_timeout(reasoner, turn)
    draft = result.v1_timeline
    if result.degraded:
        session.v1_status = V1_STATUS_FAILED
        _finish_turn(session, status=V1_STATUS_FAILED)
        session.v1_error = result.reply or "模型暂时不可用。"
        await _audit_turn_failure(
            db,
            ctx,
            session,
            result,
            trigger="strategy_confirmation",
            stage_before=V1_STRATEGY_CONFIRMED,
        )
        await db.commit()
        return result
    # R2:严格时间架构 —— 每阶段必须有 title / 时间范围 / goal / deliverable /
    # completionCriteria。用户未给截止日期时必须用相对周。缺字段先自动补全一次。
    require_weeks = root.deadline is None
    gaps = _timeline_gaps(draft, require_weeks=require_weeks)
    if gaps:
        await _audit(
            db,
            ctx,
            session,
            "timeline_repair_requested",
            trigger="strategy_confirmation",
            stage_before=V1_STRATEGY_CONFIRMED,
            summary="粗时间架构字段不全,自动补全一次。",
            payload={"missing": gaps, "requireWeeks": require_weeks},
        )
        await db.commit()
        repaired, repair_result = await _repair_coarse_timeline(
            db,
            ctx,
            session,
            reasoner,
            partial=draft,
            gaps=gaps,
            require_weeks=require_weeks,
        )
        if repaired is None:
            session.v1_status = V1_STATUS_FAILED
            _finish_turn(session, status=V1_STATUS_FAILED)
            session.v1_error = "粗时间架构缺少时间范围或验收标准,可重试。"
            await _audit(
                db,
                ctx,
                session,
                "model_output_invalid",
                trigger="timeline_repair",
                stage_before=V1_STRATEGY_CONFIRMED,
                stage_after=session.v1_stage,
                source=(repair_result.source.value if repair_result.source else None),
                summary=session.v1_error,
                validation_status="failed",
                error_code="MODEL_OUTPUT_INVALID",
                payload={"missing": gaps},
            )
            await db.commit()
            return ReasoningResult(
                reply=session.v1_error,
                source=repair_result.source,
                degraded=True,
                degraded_reason=DegradedReason.MODEL_OUTPUT_INVALID,
                retryable=True,
                request_id=repair_result.request_id,
                prompt_version=repair_result.prompt_version,
            )
        draft = repaired

    session.v01_timeline = _timeline_payload(draft.phases, include_dates=not require_weeks)
    root_handle, handles = await _root_handle(db, ctx, root)
    actions: list[dict] = []
    for index, phase in enumerate(draft.phases, start=1):
        if phase.start_week is not None and phase.end_week is not None:
            range_text = f"相对范围:第 {phase.start_week}–{phase.end_week} 周"
        else:
            range_text = f"时间范围:{phase.start_date} ~ {phase.end_date}"
        description = (
            f"{range_text}\n"
            f"目标:{phase.goal}\n"
            f"成果:{phase.deliverable}"
        )
        actions.append(
            {
                "op": "create_node",
                "localId": f"n{9200 + index}",
                "parentRef": root_handle,
                "title": phase.title,
                "nodeType": "stage",
                "purpose": "planning",
                "description": description,
                "acceptanceCriteria": phase.completion_criteria or None,
            }
        )
    conversation = await conversation_service.get_or_create_primary_conversation(db, ctx)
    outcome = await proposal_service.build_from_actions(
        db,
        ctx,
        conversation_id=conversation.id,
        actions=tuple(actions),
        handles=handles,
        reasoning="由已确认战略投影出的粗时间架构草案。",
        assistant_message=None,
        trigger_type=RevisionTrigger.INITIAL_PLAN,
    )
    if outcome.proposal is None:
        session.v1_status = V1_STATUS_FAILED
        _finish_turn(session, status=V1_STATUS_FAILED)
        session.v1_error = ";".join(error.message for error in outcome.errors) or "时间架构没有通过校验。"
        await _audit(
            db,
            ctx,
            session,
            "proposal_validation_failed",
            trigger="strategy_confirmation",
            summary=session.v1_error,
            validation_status="failed",
            error_code="PROPOSAL_VALIDATION_FAILED",
        )
        await db.commit()
        return ReasoningResult(
            reply=session.v1_error,
            source=result.source,
            degraded=True,
            degraded_reason=DegradedReason.MODEL_OUTPUT_INVALID,
            retryable=True,
            request_id=result.request_id,
            prompt_version=result.prompt_version,
        )

    session.timeline_proposal_id = outcome.proposal.id
    session.v1_stage = V1_COARSE_TIMELINE_REVIEW
    # R2:已产出待确认产物 -> awaiting_user_confirmation,不是 idle。
    _finish_turn(session, status=V1_STATUS_AWAITING_CONFIRMATION)
    session.v1_error = None
    await _audit(
        db,
        ctx,
        session,
        "coarse_timeline_draft_generated",
        stage_before=V1_STRATEGY_CONFIRMED,
        stage_after=V1_COARSE_TIMELINE_REVIEW,
        summary="生成 3–6 个阶段的粗时间架构草案。",
        payload={
            "requireWeeks": require_weeks,
            "phases": [
                {
                    "title": phase.title,
                    "goal": phase.goal,
                    "deliverable": phase.deliverable,
                    "completionCriteria": phase.completion_criteria,
                    "startWeek": phase.start_week,
                    "endWeek": phase.end_week,
                    "startDate": phase.start_date,
                    "endDate": phase.end_date,
                }
                for phase in draft.phases
            ],
        },
    )
    await _audit(
        db,
        ctx,
        session,
        "timeline_proposal_created",
        summary="粗时间架构提案已生成,待用户确认。",
        payload={
            "proposal": {
                "id": str(outcome.proposal.id),
                "kind": "timeline",
                "status": "pending_confirmation",
            }
        },
    )
    await db.commit()
    if trace is not None:
        from backend.services import agent_trace_service

        agent_trace_service.mark_terminal(
            trace, degraded=False, degraded_reason=None, stopped_reason="ready_to_propose", code=None
        )
    return result


async def _proposal_kind(db: AsyncSession, proposal_id) -> str | None:
    """从提案条目标题判断它是周计划还是日计划(用于审计事件)。"""
    from backend.db.models import ProposalItem

    items = list(
        await db.scalars(
            select(ProposalItem).where(ProposalItem.proposal_id == proposal_id)
        )
    )
    titles = " ".join(str((item.payload or {}).get("title") or "") for item in items)
    if "本周计划" in titles or "下周预览" in titles:
        return "weekly"
    if "日计划" in titles:
        return "daily"
    return None


async def on_proposal_confirmed(
    db: AsyncSession, ctx: WorkspaceContext, proposal_id
) -> None:
    """提案确认后由路由调用:V1 的时间架构 / 重规划在这里推进状态。"""
    session = await reasoning_service.get_session(db, ctx)
    if session is None or not is_v1(session):
        return
    timeline = list(session.v01_timeline or [])
    if session.v1_stage == V1_COARSE_TIMELINE_REVIEW and session.timeline_proposal_id == proposal_id:
        root = await reasoning_service.root_plan_node(db, ctx)
        if root is not None:
            stages = await db.execute(
                select(PlanNode).where(
                    PlanNode.workspace_id == ctx.id,
                    PlanNode.parent_id == root.id,
                    PlanNode.node_type == NodeType.STAGE,
                    PlanNode.deleted_at.is_(None),
                )
            )
            by_title = {node.title: node.id for node in stages.scalars()}
            for item in timeline:
                if isinstance(item, dict):
                    item["status"] = "planned"
                    linked = by_title.get(str(item.get("title") or ""))
                    item["planNodeId"] = str(linked) if linked else None
        session.v01_timeline = timeline
        session.v1_stage = V1_WEEKLY_EXECUTION
        await _audit(
            db,
            ctx,
            session,
            "timeline_confirmed",
            stage_before=V1_COARSE_TIMELINE_REVIEW,
            stage_after=V1_WEEKLY_EXECUTION,
            summary="时间线已确认,写入正式阶段。",
            payload={"proposal": {"id": str(proposal_id), "kind": "timeline", "status": "applied"}},
        )
        await db.commit()
        # 时间线确认后**自动进入本周计划**(确定性,不需要模型;仍落成待确认提案)。
        try:
            await generate_weekly_plan(db, ctx, session)
        except Exception:
            session.v1_status = V1_STATUS_FAILED
            session.v1_error = "本周计划没有自动生成,可以点「生成本周计划」重试。"
            await db.commit()
    elif session.v1_stage == V1_REPLANNING:
        for item in timeline:
            if isinstance(item, dict):
                item["status"] = "planned"
        session.v01_timeline = timeline
        session.v1_stage = V1_WEEKLY_EXECUTION
        await _audit(
            db,
            ctx,
            session,
            "replan_confirmed",
            stage_before=V1_REPLANNING,
            stage_after=V1_WEEKLY_EXECUTION,
            summary="未来重规划已确认,已完成历史不变。",
            payload={"proposal": {"id": str(proposal_id), "kind": "replan", "status": "applied"}},
        )
        await db.commit()
    else:
        kind = await _proposal_kind(db, proposal_id)
        if kind in ("weekly", "daily"):
            await _audit(
                db,
                ctx,
                session,
                "weekly_plan_confirmed" if kind == "weekly" else "daily_plan_confirmed",
                summary="本周计划已确认。" if kind == "weekly" else "日计划已确认。",
                payload={"proposal": {"id": str(proposal_id), "kind": kind, "status": "applied"}},
            )
            await db.commit()


# =================================================================================
# P4:周/日计划、执行反馈与自动回顾重规划
# =================================================================================
async def generate_weekly_plan(
    db: AsyncSession,
    ctx: WorkspaceContext,
    session: GoalReasoningSession,
    *,
    trace=None,
    include_monthly: bool = True,
) -> AgentTurnResponse:
    """从已确认时间线派生**月度里程碑 + 本周计划 + 下周预览**。

    复用 V0.1 的版本化提案(旧未完成版本归档,已完成历史不动)。`include_monthly`
    默认 True —— V1 的“最近可执行窗口”从月度里程碑开始;显式重新生成本周计划时
    可以关掉(里程碑已经存在)。
    """
    from backend.services.errors import InvalidInput

    if session.v1_stage != V1_WEEKLY_EXECUTION:
        await _guard_reject(db, ctx, session, reason="当前不在周计划阶段。")
        raise InvalidInput("当前不在周计划阶段。")
    root = await reasoning_service.root_plan_node(db, ctx)
    if root is None:
        raise InvalidInput("这个空间还没有根目标。")
    response = await v01_service.generate_weekly_plan(
        db, ctx, root, session, trace=trace, include_monthly=include_monthly
    )
    if await v01_service._has_open_proposal(db, ctx):
        await _audit(
            db,
            ctx,
            session,
            "weekly_plan_proposal_created",
            summary="生成月度里程碑、本周计划与下周预览(待确认)。",
            payload={"proposal": {"kind": "weekly"}},
        )
        await db.commit()
    return response


async def _v1_capacity_window(db, ctx, turn) -> tuple[bool, list[str]]:
    """读这个人的可用容量:返回 (是否已配置容量, 本周内可用日期 ISO 列表)。

    **只读**。没有配置容量档案 / 没有可用时段时返回 `(False, [])` —— 调用方据此把
    日工作块标为“待校准”,而**不是**伪造一个具体日期。
    """
    today = today_in(ctx.timezone)
    handles = {uuid.UUID(node_id): handle for handle, node_id in turn.node_handles}
    try:
        view = await turn_context.load_time_view(db, ctx, today=today, handles=handles)
    except Exception:  # pragma: no cover - 读不到容量信息时按“待校准”处理
        logger.warning("读取容量信息失败,日计划将标为待校准。", exc_info=True)
        return False, []
    if not view.capacity_configured or not view.windows or view.weekly_budget_minutes <= 0:
        return False, []
    weekdays = {window.weekday for window in view.windows}
    dates = [
        (today + timedelta(days=offset)).isoformat()
        for offset in range(7)
        if (today + timedelta(days=offset)).weekday() in weekdays
    ]
    return True, dates


async def generate_daily_plan(
    db: AsyncSession, ctx: WorkspaceContext, session: GoalReasoningSession, *, trace=None
) -> AgentTurnResponse:
    """把本周计划拆成**少量工作日工作块**(不是均摊七天),落成待确认提案。

    只有用户配置了可用容量/时段时,才把工作块落到具体日期;否则明确标为“待校准”。
    """
    from backend.services.errors import InvalidInput

    if session.v1_stage != V1_WEEKLY_EXECUTION:
        await _guard_reject(db, ctx, session, reason="当前不在周计划阶段。")
        raise InvalidInput("当前不在周计划阶段。")
    root = await reasoning_service.root_plan_node(db, ctx)
    if root is None:
        raise InvalidInput("这个空间还没有根目标。")
    phases = await v01_service._phase_nodes(db, ctx, root)
    weeks = await v01_service._week_nodes(db, phases)
    current = [
        week
        for week in weeks
        if week.title.startswith("本周计划")
        and week.status in (NodeStatus.PENDING, NodeStatus.DOING)
    ]
    if not current:
        raise InvalidInput("还没有可排的本周计划。")
    week = current[0]
    tasks = await db.execute(
        select(PlanNode)
        .where(PlanNode.parent_id == week.id, PlanNode.deleted_at.is_(None))
        .order_by(PlanNode.order_index.asc())
    )
    task_rows = list(tasks.scalars())
    if not task_rows:
        raise InvalidInput("本周计划里还没有任务。")

    conversation = await conversation_service.get_or_create_primary_conversation(db, ctx)
    turn = await turn_context.build_turn_context(
        db,
        ctx,
        conversation_id=conversation.id,
        user_message="生成日计划",
        context_node_id=root.id,
        scope_root_id=root.id,
    )
    week_handle = next((h for h, node_id in turn.node_handles if node_id == str(week.id)), None)
    if week_handle is None:
        raise InvalidInput("找不到本周计划的记号。")

    # R3:可用容量接入时才排到具体日期;否则明确“待校准”。
    capacity_ok, available_dates = await _v1_capacity_window(db, ctx, turn)
    session.dates_calibrated = capacity_ok

    # 少量工作日:最多 3 天,不均匀摊到七天。
    days = ("第 1 个工作日", "第 2 个工作日", "第 3 个工作日")
    actions: list[dict] = []
    counter = 0
    for day_index, day in enumerate(days):
        day_ref = f"n{9300 + day_index}"
        if capacity_ok and available_dates:
            planned_date = available_dates[min(day_index, len(available_dates) - 1)]
            day_description = f"计划日期:{planned_date}(按已配置可用时段校准)。"
        else:
            day_description = "待校准:尚未配置每周可用时段,这不是已排入的具体日期。"
        actions.append(
            {
                "op": "create_node",
                "localId": day_ref,
                "parentRef": week_handle,
                "title": f"日计划 · {day}",
                "nodeType": "stage",
                "purpose": "planning",
                "description": day_description,
            }
        )
        blocks = [task_rows[i] for i in range(day_index, len(task_rows), len(days))][:2]
        for block in blocks:
            counter += 1
            actions.append(
                {
                    "op": "create_node",
                    "localId": f"n{9300 + 100 + counter}",
                    "parentRef": day_ref,
                    "title": f"{block.title} · 工作块",
                    "nodeType": "task",
                    "purpose": "planning",
                    "description": f"来源:本周计划「{week.title}」。",
                }
            )
    outcome = await proposal_service.build_from_actions(
        db,
        ctx,
        conversation_id=conversation.id,
        actions=tuple(actions),
        handles=turn.node_handles,
        reasoning="由本周计划拆出的少量工作日工作块。",
        assistant_message=None,
        trigger_type=RevisionTrigger.INITIAL_PLAN,
    )
    if outcome.proposal is None:
        session.v1_status = V1_STATUS_FAILED
        session.v1_error = "日计划没有通过校验。"
        await _audit(
            db,
            ctx,
            session,
            "proposal_validation_failed",
            summary="日计划没有通过校验。",
            validation_status="failed",
            error_code="PROPOSAL_VALIDATION_FAILED",
        )
        return await _response(db, ctx, session, changed=False)
    await _audit(
        db,
        ctx,
        session,
        "daily_plan_proposal_created",
        summary="把本周计划拆成少量工作日工作块(待确认)。",
        payload={
            "proposal": {
                "id": str(outcome.proposal.id),
                "kind": "daily",
                "status": "pending_confirmation",
            },
            "datesCalibrated": capacity_ok,
        },
    )
    reply = (
        "我把本周计划拆成了几天的工作块,并按你已配置的可用时段校到了具体日期。确认后写入。"
        "(不是均摊七天。)"
        if capacity_ok
        else "我把本周计划拆成了几天的工作块。你还没配置每周可用时段,这些工作块先标为**待校准**,"
        "配置后我再排到具体日期。确认后写入。(不是均摊七天。)"
    )
    message = await _append_assistant(db, ctx, reply=reply)
    return await _response(db, ctx, session, message=message, changed=True)


async def record_feedback(
    db: AsyncSession,
    ctx: WorkspaceContext,
    session: GoalReasoningSession,
    *,
    node_id,
    outcome: str,
    trace=None,
) -> AgentTurnResponse:
    """记录一条任务反馈;完成率 < 60% 时把 V1 推进到重规划阶段。"""
    from backend.services.errors import InvalidInput

    node = await node_service.load_node(db, ctx, node_id)
    mapping = {
        "done": NodeStatus.COMPLETED,
        "partial": NodeStatus.DOING,
        "missed": NodeStatus.PENDING,
        "delayed": NodeStatus.PENDING,
    }
    if outcome not in mapping:
        raise InvalidInput("不认识的反馈结果。")
    node.status = mapping[outcome]
    if outcome in {"partial", "delayed"}:
        note = "部分完成" if outcome == "partial" else "延期"
        node.description = (node.description or "") + f"\n【反馈】{note}"
    node.content_version += 1
    await db.commit()

    root = await reasoning_service.root_plan_node(db, ctx)
    done, total = await v01_service.weekly_completion(db, ctx, root) if root else (0, 0)
    rate = (done / total) if total else 0.0
    if total and rate < 0.6:
        session.v1_stage = V1_REPLANNING
        reply = f"本周完成率 {done}/{total}(低于 60%)。我先不催你,而是把剩余时间线往后再排一版,你看过再确认。"
    else:
        reply = f"记下了。本周进度 {done}/{total}。"
    await _audit(
        db,
        ctx,
        session,
        "execution_feedback_recorded",
        summary=f"任务反馈:{outcome};本周完成 {done}/{total}。",
        payload={"nodeId": str(node_id), "outcome": outcome, "done": done, "total": total},
    )
    message = await _append_assistant(db, ctx, reply=reply)
    # R4:完成率过低时进入 `replanning`;真正的未来重规划草案由编排器在
    # **下一次进入空间 / 主动周回顾**时自动生成 —— 不在这里生成,是因为后续的
    # 任务反馈还会写库,过早生成的提案会被版本校验(`STALE_BASE_REVISION`)作废。
    return await _response(db, ctx, session, message=message, changed=True)


async def generate_replan(
    db: AsyncSession, ctx: WorkspaceContext, session: GoalReasoningSession, *, trace=None
) -> AgentTurnResponse:
    """重规划:只为**未来**阶段生成调整提案,已完成的历史一律不动。"""
    from backend.services.errors import InvalidInput

    root = await reasoning_service.root_plan_node(db, ctx)
    if root is None:
        raise InvalidInput("这个空间还没有根目标。")
    phases = await v01_service._phase_nodes(db, ctx, root)
    if not phases:
        raise InvalidInput("还没有已确认的时间线阶段。")
    conversation = await conversation_service.get_or_create_primary_conversation(db, ctx)
    turn = await turn_context.build_turn_context(
        db,
        ctx,
        conversation_id=conversation.id,
        user_message="重规划未来时间线",
        context_node_id=root.id,
        scope_root_id=root.id,
    )
    stage_before_replan = session.v1_stage
    phase_handles = {node_id: h for h, node_id in turn.node_handles}
    completed_ids = {str(phase.id) for phase in phases if phase.status is NodeStatus.COMPLETED}
    timeline = list(session.v01_timeline or [])
    for item in timeline:
        if not isinstance(item, dict) or item.get("planNodeId") in completed_ids:
            continue
        if isinstance(item.get("startWeek"), int):
            item["startWeek"] += 1
        if isinstance(item.get("endWeek"), int):
            item["endWeek"] += 1
        item["status"] = "draft"
    session.v01_timeline = timeline
    flag_modified(session, "v01_timeline")

    actions: list[dict] = []
    for phase in phases:
        if phase.status is NodeStatus.COMPLETED:
            continue
        handle = phase_handles.get(str(phase.id))
        if handle is None:
            continue
        actions.append(
            {
                "op": "update_node",
                "target_ref": handle,
                "description": (phase.description or "")
                + "\n【重规划】按当前完成情况,后续阶段整体后移一档。",
            }
        )
    # R4:未来阶段下**未完成**的周计划归档为可恢复历史;已完成的一律不动。
    future_phase_ids = {
        str(phase.id) for phase in phases if phase.status is not NodeStatus.COMPLETED
    }
    for week in await v01_service._week_nodes(db, phases):
        if str(week.parent_id) not in future_phase_ids:
            continue
        if week.status not in (NodeStatus.PENDING, NodeStatus.DOING):
            continue
        handle = phase_handles.get(str(week.id))
        if handle is None:
            continue
        actions.append(
            {
                "op": "update_node",
                "target_ref": handle,
                "status": "archived",
                "description": (week.description or "")
                + "\n【历史版本 · 已被重规划替代】",
            }
        )
    if not actions:
        message = await _append_assistant(
            db, ctx, reply="未来阶段都已经完成,暂时不需要重规划。", conversation=conversation
        )
        session.v1_stage = V1_WEEKLY_EXECUTION
        return await _response(db, ctx, session, message=message, changed=False)
    outcome = await proposal_service.build_from_actions(
        db,
        ctx,
        conversation_id=conversation.id,
        actions=tuple(actions),
        handles=turn.node_handles,
        reasoning="根据最近执行情况,对未完成阶段提出的未来调整。",
        assistant_message=None,
        trigger_type=RevisionTrigger.EXECUTION_DEVIATION,
    )
    if outcome.proposal is None:
        session.v1_status = V1_STATUS_FAILED
        session.v1_error = "重规划提案没有通过校验。"
        await _audit(
            db,
            ctx,
            session,
            "proposal_validation_failed",
            trigger="replan",
            summary=session.v1_error,
            validation_status="failed",
            error_code="PROPOSAL_VALIDATION_FAILED",
        )
        return await _response(db, ctx, session, changed=False)
    session.v1_stage = V1_REPLANNING
    await _audit(
        db,
        ctx,
        session,
        "replan_proposal_created",
        stage_before=stage_before_replan,
        stage_after=V1_REPLANNING,
        summary="生成只调整未来阶段的重规划草案,待确认。",
        payload={"proposal": {"id": str(outcome.proposal.id), "kind": "replan", "status": "pending_confirmation"}},
    )
    message = await _append_assistant(
        db,
        ctx,
        reply="我按最近的完成情况提了一版**只调整未来阶段**的重规划,已完成的阶段原样保留。确认后生效。",
        conversation=conversation,
    )
    return await _response(db, ctx, session, message=message, changed=True)


async def weekend_review(
    db: AsyncSession, ctx: WorkspaceContext, session: GoalReasoningSession, *, trace=None
) -> AgentTurnResponse:
    """自动/主动发起的周末回顾入口:汇总完成度,并准备一份未来重规划草案。"""
    from backend.services.errors import InvalidInput

    if session.v1_stage not in (V1_WEEKLY_EXECUTION, V1_REPLANNING):
        await _guard_reject(db, ctx, session, reason="当前不在周执行阶段。")
        raise InvalidInput("当前不在周执行阶段。")
    root = await reasoning_service.root_plan_node(db, ctx)
    done, total = await v01_service.weekly_completion(db, ctx, root) if root else (0, 0)
    # 标记“这一周已经回顾过”:同一个周末重进空间不再重复发起。
    session.v1_last_review_week = _iso_week(today_in(ctx.timezone))
    await _audit(
        db,
        ctx,
        session,
        "weekly_review_started",
        summary=f"发起周末回顾:本周完成 {done}/{total}。",
        payload={"done": done, "total": total, "week": session.v1_last_review_week},
    )
    await _append_assistant(
        db,
        ctx,
        reply=f"这一周完成 {done}/{total}。我按完成情况准备一份**只调整未来**的重规划,你看过再确认。",
    )
    await db.commit()
    return await generate_replan(db, ctx, session, trace=trace)


async def select_candidate_direction(
    db: AsyncSession,
    ctx: WorkspaceContext,
    session: GoalReasoningSession,
    key: str,
    reasoner=None,
) -> AgentTurnResponse:
    """用户选择一个候选方向:幂等记录 + **立即发起一次 V1 推理回合**更新判断。

    - 同一方向重复选择**幂等**:不再审计、不再调模型;
    - 选中后强制**不再提问**,并更新目标定义;
    - 不进入时间线、不生成任务。
    """
    from backend.services.errors import InvalidInput

    # 幂等优先:同一方向重复点击(选定后阶段可能已推进)不再写审计、不再跑模型。
    if session.v1_selected_direction == key:
        return await reasoning_service._response(db, ctx, session, changed=False)
    # P2.3:候选方向只属于 goal_reframe。目标定义确认后必须走“重新选择起点”。
    if session.v1_stage != V1_GOAL_REFRAME:
        await _guard_reject(db, ctx, session, reason="目标定义已确认,请先“重新选择起点”。")
        raise InvalidInput("目标定义已确认;如要改方向,请先选择“重新选择起点”。")
    directions = [d for d in (session.v1_candidate_directions or []) if isinstance(d, dict)]
    valid = {str(d.get("key")) for d in directions}
    if key not in valid:
        await _guard_reject(db, ctx, session, reason="没有这个候选方向。")
        raise InvalidInput("没有这个候选方向。")

    direction = next(d for d in directions if str(d.get("key")) == key)
    session.v1_selected_direction = key
    await _audit(
        db,
        ctx,
        session,
        "candidate_direction_selected",
        focus_key=session.v1_focus_key,
        focus_reason=session.v1_focus_reason,
        summary=f"用户选择了候选方向:{key}。",
        payload={
            "focusKey": session.v1_focus_key,
            "focusReason": session.v1_focus_reason,
            "selectedDirection": key,
        },
    )
    await db.commit()
    if reasoner is None:
        return await reasoning_service._response(db, ctx, session, changed=True)

    message = (
        f"用户选择了候选方向「{direction.get('title') or key}」。"
        f"理由:{direction.get('reason') or ''}。路径:{direction.get('path') or ''}。"
        "请据此更新战略判断,并更新目标定义;不要再问新问题。"
    )
    result = await _run_assessment(
        db,
        ctx,
        session,
        user_message=message,
        reasoner=reasoner,
        classification=INPUT_USER_PREFERENCE,
        force_no_question=True,
        trigger="direction_selected",
    )
    conversation = await conversation_service.get_or_create_primary_conversation(db, ctx)
    await _append_assistant(db, ctx, reply=result.reply, conversation=conversation, result=result)
    await db.commit()
    # R2:用户已选定起点 -> **自动综合**,不再抛下一道选择题。
    await _maybe_synthesize_strategy(db, ctx, session, reasoner, trigger="direction_selected")
    return await reasoning_service._response(db, ctx, session, changed=True)


async def confirm_goal_definition(
    db: AsyncSession,
    ctx: WorkspaceContext,
    session: GoalReasoningSession,
    reasoner=None,
) -> AgentTurnResponse:
    """用户确认目标定义:进入 problem_structure,并**自动发起一次战略合成回合**。

    P2.3:确认后绝不是“idle 无下一步”。自动合成要么形成战略草案(等确认),
    要么留下显式 CTA `continue_strategy`。
    """
    from backend.services.errors import InvalidInput

    if session.v1_stage != V1_GOAL_REFRAME:
        await _guard_reject(db, ctx, session, reason="当前不在目标重构阶段。")
        raise InvalidInput("当前不在目标重构阶段。")
    session.v1_stage = V1_PROBLEM_STRUCTURE
    #: 先给一个明确 CTA;合成成功后会被清掉。
    session.v1_next_action = NEXT_CONTINUE_STRATEGY
    await _audit(
        db,
        ctx,
        session,
        "goal_definition_confirmed",
        stage_before=V1_GOAL_REFRAME,
        stage_after=V1_PROBLEM_STRUCTURE,
        summary="用户确认了目标定义,进入问题结构。",
    )
    await _audit(
        db,
        ctx,
        session,
        "problem_structure_entered",
        stage_before=V1_GOAL_REFRAME,
        stage_after=V1_PROBLEM_STRUCTURE,
        summary="进入问题结构,自动发起战略路径合成。",
    )
    await db.commit()
    if reasoner is None:
        return await reasoning_service._response(db, ctx, session, changed=True)

    result = await _run_assessment(
        db,
        ctx,
        session,
        user_message=(
            "目标定义已确认。请基于已有目标定义、已采用的起点与已知事实,"
            "主动识别可控变量、主要风险与关键杠杆,形成第一版战略路径;"
            "若确有缺口,最多问一个会改变路线的关键问题。"
        ),
        reasoner=reasoner,
        classification=INPUT_USER_PREFERENCE,
        trigger="problem_structure_entered",
    )
    await _append_assistant(
        db,
        ctx,
        reply=result.reply,
        conversation=await conversation_service.get_or_create_primary_conversation(db, ctx),
        result=result,
    )
    await db.commit()
    return await reasoning_service._response(db, ctx, session, changed=True)


async def continue_strategy(
    db: AsyncSession,
    ctx: WorkspaceContext,
    session: GoalReasoningSession,
    reasoner=None,
) -> AgentTurnResponse:
    """`responseMode=none` 的兜底 CTA:受控地再跑一次战略合成回合。"""
    from backend.services.errors import InvalidInput

    if session.v1_stage not in (V1_PROBLEM_STRUCTURE, V1_FACTOR_ANALYSIS):
        await _guard_reject(db, ctx, session, reason="当前不在问题结构阶段。")
        raise InvalidInput("当前不在问题结构阶段。")
    await _audit(
        db,
        ctx,
        session,
        "strategy_continue_triggered",
        stage_before=session.v1_stage,
        summary="用户触发继续形成战略路径。",
    )
    await db.commit()
    if reasoner is None:
        return await reasoning_service._response(db, ctx, session, changed=True)
    result = await _run_assessment(
        db,
        ctx,
        session,
        user_message=(
            "请基于已有分析继续形成第一版战略路径;若确有缺口,最多问一个关键问题。"
        ),
        reasoner=reasoner,
        classification=INPUT_USER_PREFERENCE,
        trigger="strategy_continue",
    )
    await _append_assistant(
        db,
        ctx,
        reply=result.reply,
        conversation=await conversation_service.get_or_create_primary_conversation(db, ctx),
        result=result,
    )
    await db.commit()
    # 通用回合没能合出战略 -> 窄契约回合兜底(四条齐全才接受)。
    await _maybe_synthesize_strategy(db, ctx, session, reasoner, trigger="strategy_continue")
    return await reasoning_service._response(db, ctx, session, changed=True)


async def reopen_direction_selection(
    db: AsyncSession, ctx: WorkspaceContext, session: GoalReasoningSession
) -> AgentTurnResponse:
    """用户明确要求“重新选择起点”:回到 goal_reframe,重新开放候选方向。"""
    from backend.services.errors import InvalidInput

    if not session.v1_candidate_directions:
        await _guard_reject(db, ctx, session, reason="还没有可重新选择的候选方向。")
        raise InvalidInput("还没有可重新选择的候选方向。")
    stage_before = session.v1_stage
    session.v1_stage = V1_GOAL_REFRAME
    session.v1_selected_direction = None
    session.v1_next_action = None
    await _audit(
        db,
        ctx,
        session,
        "direction_reselection_started",
        stage_before=stage_before,
        stage_after=V1_GOAL_REFRAME,
        summary="用户选择重新选择起点,回到目标重构。",
    )
    await db.commit()
    return await reasoning_service._response(db, ctx, session, changed=True)


# =================================================================================
# 自动推进与战略确认
# =================================================================================
#: 运行中超过这个秒数没有推进,判定为超时:给出明确失败与重试,不无限转圈。
#: **保留为默认值**;真实窗口取 `V1_AGENT_TURN_TIMEOUT_SECONDS`。
V1_STALE_SECONDS = 45

def _iso_week(day) -> str:
    """`YYYY-Www` 形式的自然周标记(用于“同一个周末只回顾一次”)。"""
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


#: V1 事件闭集(见规格 4.1)。所有入口都必须能归到其中一个。
V1_EVENTS = frozenset(
    {
        "space_entered",
        "user_message",
        "canvas_question_answered",
        "candidate_direction_selected",
        "goal_definition_confirmed",
        "strategy_understanding_confirmed",
        "strategy_confirmed",
        "timeline_alignment_confirmed",
        "timeline_proposal_confirmed",
        "execution_feedback",
        "weekly_review_due",
        "retry",
        "recovery_after_restart",
        # R3:细化入口同样是编排事件,由编排器统一决定阶段。
        "weekly_refinement_requested",
        "daily_refinement_requested",
        "direction_reselection_requested",
    }
)


def _turn_timeout_seconds() -> int:
    return max(1, int(settings.v1_agent_turn_timeout_seconds or V1_STALE_SECONDS))


def _step_is_stale(session: GoalReasoningSession) -> bool:
    """运行中的回合是否越过 deadline。没有 deadline 时退回 updated_at + 超时窗口。"""
    if session.v1_turn_deadline_at is not None:
        deadline = session.v1_turn_deadline_at
        if deadline.tzinfo is None:
            from datetime import UTC

            deadline = deadline.replace(tzinfo=UTC)
        return utcnow() >= deadline
    reference = session.updated_at
    if reference is None:
        return False
    return (utcnow() - reference).total_seconds() > _turn_timeout_seconds()


def _blocked_reason(session: GoalReasoningSession) -> str:
    """当前为何停下 —— 让审计能直接回答“下一步等谁”。"""
    if (session.v1_question or "").strip():
        return "等待用户回答关键问题"
    if session.v1_stage == V1_GOAL_REFRAME and session.v1_candidate_directions:
        return "等待用户选择候选方向"
    if session.v1_stage == V1_COARSE_TIMELINE_REVIEW:
        return "等待用户确认时间线"
    if session.v1_stage == V1_TIMELINE_ALIGNMENT:
        return "等待用户对齐时间节奏"
    if session.v1_strategy and not (session.v1_strategy or {}).get("confirmed"):
        return "等待用户确认战略理解与战略"
    if session.v1_stage == V1_STRATEGY_CONFIRMED:
        return "等待生成粗时间架构"
    if session.v1_stage == V1_WEEKLY_EXECUTION:
        return "已进入周执行"
    return "等待用户输入"


async def advance_v1_workflow(
    db: AsyncSession,
    ctx: WorkspaceContext,
    session: GoalReasoningSession,
    reasoner=None,
    *,
    trigger: str = "space_entered",
    event: str | None = None,
    payload: dict | None = None,
    trace=None,
) -> AgentTurnResponse:
    """V1 工作流的**唯一编排入口**。

    每次进入空间 / 切阶段都调它:能自行推进的就推进(首轮整体判断、
    problem_structure 自动合成、确认战略后自动时间线),只在真正需要用户时停下,
    并把“为什么停 / 下一步等谁”写进审计。非终态阶段不会停在无解释的 idle。

    `event` 是规格 4.1 的事件闭集之一(默认取 `trigger`)。所有入口——
    进入空间、用户确认战略、选择方向、执行反馈、周末回顾——都从这里分发,
    endpoint 不自己决定下一阶段。
    """
    entry_event = (event or trigger or "space_entered").strip().lower()
    if entry_event not in V1_EVENTS:
        # 未知触发不猜语义:按“进入空间”处理,并留下可读原因。
        entry_event = "space_entered"
    body = payload or {}

    # 1) 超时恢复:运行中卡死 -> 明确失败 + 重试,不无限转圈。
    if session.v1_status == V1_STATUS_RUNNING and _step_is_stale(session):
        turn_id = session.v1_turn_id
        turn_stage = session.v1_turn_stage
        source = session.v1_turn_source
        session.v1_status = V1_STATUS_FAILED
        _finish_turn(session, status=V1_STATUS_FAILED)
        session.v1_error = "上一次处理超时,可以重试。"
        await _audit(
            db,
            ctx,
            session,
            "v1_step_timed_out",
            trigger=entry_event,
            source=source,
            stage_after=turn_stage,
            validation_status="failed",
            error_code="V1_STEP_TIMEOUT",
            summary=session.v1_error,
            payload={"turnId": turn_id, "stage": turn_stage},
        )
        await db.commit()
    retrying = entry_event == "retry"
    # 用户显式确认/选择/反馈类事件必须能继续:它们自带守卫(会在不合法时拒绝),
    # 不能被“上一次失败”提前挡掉。只有自动推进类入口才在失败态上直接返回。
    explicit_events = {
        "candidate_direction_selected",
        "goal_definition_confirmed",
        "strategy_understanding_confirmed",
        "strategy_confirmed",
        "timeline_alignment_confirmed",
        "execution_feedback",
        "weekly_review_due",
        "weekly_refinement_requested",
        "daily_refinement_requested",
        "direction_reselection_requested",
    }
    explicit = entry_event in explicit_events
    if session.v1_status == V1_STATUS_RUNNING:
        # 真正在跑:第二请求只读状态,不重复出模型调用。
        return await _response(db, ctx, session, changed=False, trace=trace)
    if session.v1_status == V1_STATUS_FAILED and not retrying and not explicit:
        return await _response(db, ctx, session, changed=False, trace=trace)
    if retrying:
        # 重试复用同阶段/同幂等语义:清掉失败态,继续走下面的自动推进。
        session.v1_status = V1_STATUS_IDLE
        session.v1_error = None

    if session.v1_stage is None:
        session.v1_stage = V1_INITIAL_THINKING
        session.phase = ReasoningSessionPhase.INTAKE
        await _audit(
            db,
            ctx,
            session,
            "space_entered",
            trigger=entry_event,
            stage_after=session.v1_stage,
            summary="进入空间,建立初步思考状态。",
        )
        await db.commit()

    # 2) 事件分发:所有入口都归到这里,由编排器决定下一阶段。
    if entry_event == "candidate_direction_selected":
        return await select_candidate_direction(
            db, ctx, session, str(body.get("key") or ""), reasoner
        )
    if entry_event == "goal_definition_confirmed":
        return await confirm_goal_definition(db, ctx, session, reasoner)
    if entry_event == "strategy_understanding_confirmed":
        return await confirm_strategy_understanding(db, ctx, session)
    if entry_event == "strategy_confirmed":
        return await confirm_strategy(db, ctx, session, reasoner)
    if entry_event == "timeline_alignment_confirmed":
        return await confirm_timeline_alignment(
            db,
            ctx,
            session,
            reasoner,
            answer=str(body.get("answer") or ""),
            accepted=bool(body.get("accepted")),
        )
    if entry_event == "execution_feedback":
        return await record_feedback(
            db,
            ctx,
            session,
            node_id=body.get("node_id"),
            outcome=str(body.get("outcome") or ""),
            trace=trace,
        )
    if entry_event == "weekly_review_due":
        return await weekend_review(db, ctx, session, trace=trace)
    if entry_event == "weekly_refinement_requested":
        return await generate_weekly_plan(db, ctx, session, trace=trace)
    if entry_event == "daily_refinement_requested":
        return await generate_daily_plan(db, ctx, session, trace=trace)
    if entry_event == "direction_reselection_requested":
        return await reopen_direction_selection(db, ctx, session)

    # 2.5)R4:周末首次进入空间**自动发起周回顾**,同一个周末只触发一次。
    today = today_in(ctx.timezone)
    if (
        entry_event in ("space_entered", "recovery_after_restart")
        and session.v1_stage == V1_WEEKLY_EXECUTION
        and today.weekday() >= 5
        and session.v1_last_review_week != _iso_week(today)
        and not await v01_service._has_open_proposal(db, ctx)
    ):
        return await weekend_review(db, ctx, session, trace=trace)

    # 3) 没有模型运行时就只能停在“等输入”,不假装在思考。
    if reasoner is None:
        await _audit(
            db,
            ctx,
            session,
            "v1_workflow_blocked",
            trigger=entry_event,
            stage_after=session.v1_stage,
            summary="没有可用的模型运行时,停在等待输入。",
            payload={"stage": session.v1_stage, "nextAction": session.v1_next_action},
        )
        return await _response(db, ctx, session, changed=False, trace=trace)

    stage = session.v1_stage
    advanced = False
    if stage == V1_INITIAL_THINKING:
        # **进入空间就实际启动首轮整体判断**,而不是干等用户先输入。
        root = await reasoning_service.root_plan_node(db, ctx)
        goal = (root.title if root else "") or ctx.workspace.title or "这个目标"
        intent = (ctx.workspace.intent or "").strip()
        message = (
            f"用户刚创建目标空间「{goal}」"
            + (f",并写下意图:{intent}" if intent else "")
            + "。请先给出整体判断:它可能服务于什么、真正的歧义在哪;"
            "最多只问一个会改变路线的关键问题(或给候选方向)。"
        )
        await _run_assessment(
            db,
            ctx,
            session,
            user_message=message,
            reasoner=reasoner,
            classification=INPUT_STRATEGIC_FACT,
            trigger=entry_event,
        )
        advanced = True
    elif stage == V1_GOAL_REFRAME and not session.v1_strategic_thesis:
        root = await reasoning_service.root_plan_node(db, ctx)
        goal = (root.title if root else "") or "这个目标"
        await _run_assessment(
            db,
            ctx,
            session,
            user_message=f"请基于「{goal}」给出整体判断。",
            reasoner=reasoner,
            classification=INPUT_STRATEGIC_FACT,
            trigger=entry_event,
        )
        advanced = True
    elif (
        stage in (V1_PROBLEM_STRUCTURE, V1_FACTOR_ANALYSIS)
        and not session.v1_strategy
        and session.v1_next_action == NEXT_CONTINUE_STRATEGY
    ):
        # 先跑通用判断(模型若直接给了战略结构就在这里成形);仍没有战略时,
        # `continue_strategy` 内部会退到窄契约合成回合。
        await _audit(
            db,
            ctx,
            session,
            "v1_workflow_blocked",
            trigger=entry_event,
            stage_after=stage,
            summary="problem_structure 没有战略草案,自动合成。",
            payload={"stage": stage, "nextAction": session.v1_next_action},
        )
        await db.commit()
        return await continue_strategy(db, ctx, session, reasoner)
    elif stage == V1_STRATEGY_CONFIRMED and not session.v01_timeline:
        # 深度对话:不直接生成时间线,先做时间架构共创。
        await generate_timeline_alignment(db, ctx, session, reasoner, trigger=entry_event)
        advanced = True
    elif (
        stage == V1_TIMELINE_ALIGNMENT
        and not session.v01_timeline
        and not (session.v1_timeline_alignment or {}).get("summary")
    ):
        # 已进入共创但假设还没呈现(例如进程重启):补一次呈现,仍不直接生成时间线。
        await generate_timeline_alignment(db, ctx, session, reasoner, trigger=entry_event)
        advanced = True
    elif stage == V1_REPLANNING and not await v01_service._has_open_proposal(db, ctx):
        # R4:进入重规划阶段后**自动**准备未来重规划提案(确定性,不需要再点一次)。
        await generate_replan(db, ctx, session, trace=trace)
        advanced = True

    # 模型回合失败 / 来源不合规时不算“推进”。
    if session.v1_status == V1_STATUS_FAILED:
        advanced = False

    if advanced:
        await _audit(
            db,
            ctx,
            session,
            "v1_workflow_advanced",
            trigger=entry_event,
            stage_after=session.v1_stage,
            focus_key=session.v1_focus_key,
            summary=f"自动推进到 {session.v1_stage}。",
            payload={
                "stage": session.v1_stage,
                "nextAction": session.v1_next_action,
                "workflowNext": compute_next_action(session),
                "pendingQuestion": bool(session.v1_question),
                "candidates": len(session.v1_candidate_directions or []),
                "hasStrategy": bool(session.v1_strategy),
            },
        )
    else:
        await _audit(
            db,
            ctx,
            session,
            "v1_workflow_blocked",
            trigger=entry_event,
            stage_after=session.v1_stage,
            focus_key=session.v1_focus_key,
            summary=_blocked_reason(session),
            payload={
                "stage": session.v1_stage,
                "nextAction": session.v1_next_action,
                "workflowNext": compute_next_action(session),
                "explained": has_explained_state(session),
                "pendingQuestion": bool(session.v1_question),
                "candidates": len(session.v1_candidate_directions or []),
                "hasStrategy": bool(session.v1_strategy),
            },
        )
    return await _response(db, ctx, session, changed=advanced, trace=trace)


async def advance(
    db: AsyncSession,
    ctx: WorkspaceContext,
    root,
    session: GoalReasoningSession,
    *,
    trace,
) -> AgentTurnResponse:
    """**已弃用(R1)**:V1 的唯一编排入口是 `advance_v1_workflow`。

    这个签名只为兼容可能残留的旧调用方。它不再自己决定阶段,而是原样转发到
    编排器(不带模型运行时)。新代码不应直接调用它。
    """
    logger.warning("v1_service.advance 已弃用,请改用 advance_v1_workflow。")
    return await advance_v1_workflow(
        db, ctx, session, reasoner=None, trigger="space_entered", trace=trace
    )


async def confirm_strategy_understanding(
    db: AsyncSession,
    ctx: WorkspaceContext,
    session: GoalReasoningSession,
) -> AgentTurnResponse:
    """用户确认“我据此形成的战略理解”。确认后才进入正式战略草案确认。"""
    from backend.services.errors import InvalidInput

    understanding = dict(session.v1_strategy_understanding or {})
    if not understanding or not session.v1_strategy:
        await _guard_reject(db, ctx, session, reason="现在还没有可确认的战略理解。")
        raise InvalidInput("现在还没有可确认的战略理解。")
    understanding["confirmed"] = True
    session.v1_strategy_understanding = understanding
    await _audit(
        db,
        ctx,
        session,
        "strategy_understanding_confirmed",
        stage_after=session.v1_stage,
        summary="用户确认了战略理解,可以进入正式战略确认。",
        payload={"understanding": understanding},
    )
    await db.commit()
    return await reasoning_service._response(db, ctx, session, changed=True)


async def confirm_strategy(
    db: AsyncSession,
    ctx: WorkspaceContext,
    session: GoalReasoningSession,
    reasoner=None,
) -> AgentTurnResponse:
    """用户确认战略逻辑:进入**时间架构共创**;有 reasoner 时立即给出时间假设。

    **不生成时间线、不写正式计划** —— 必须先经过 `timeline_alignment` 对齐节奏。
    """
    from backend.services.errors import InvalidInput

    strategy = dict(session.v1_strategy or {})
    if not strategy:
        await _guard_reject(db, ctx, session, reason="现在还没有可确认的战略路径。")
        raise InvalidInput("现在还没有可确认的战略路径。")
    # 深度对话:确认战略前先把“战略理解”标为已确认(若前端未单独调过确认)。
    understanding = dict(session.v1_strategy_understanding or {})
    if understanding and not understanding.get("confirmed"):
        understanding["confirmed"] = True
        session.v1_strategy_understanding = understanding
        await _audit(
            db,
            ctx,
            session,
            "strategy_understanding_confirmed",
            stage_after=session.v1_stage,
            summary="用户确认了战略理解(随战略确认一并记录)。",
            payload={"understanding": understanding},
        )
    strategy["confirmed"] = True
    session.v1_strategy = strategy
    stage_before = session.v1_stage
    session.v1_stage = V1_STRATEGY_CONFIRMED
    await _audit(
        db,
        ctx,
        session,
        "strategy_confirmed",
        stage_before=stage_before,
        stage_after=V1_STRATEGY_CONFIRMED,
        summary="用户确认了战略逻辑,进入时间架构共创。",
        payload={"strategy": strategy},
    )
    await db.commit()
    if reasoner is not None:
        # 深度对话:不直接生成时间线,先做时间架构共创。
        await generate_timeline_alignment(db, ctx, session, reasoner)
    return await reasoning_service._response(db, ctx, session, changed=True)


__all__ = [
    "ALLOWED_V1_KEYS",
    "ANALYSIS_DIMENSION_KEYS",
    "NEXT_CONFIRM_GOAL",
    "NEXT_CONFIRM_REPLAN",
    "NEXT_CONFIRM_STRATEGY",
    "NEXT_CONFIRM_TIMELINE",
    "NEXT_CONFIRM_TIMELINE_ALIGNMENT",
    "NEXT_CONFIRM_UNDERSTANDING",
    "NEXT_CONTINUE_STRATEGY",
    "NEXT_SELECT_DIRECTION",
    "V1_COARSE_TIMELINE_REVIEW",
    "V1_FACTOR_ANALYSIS",
    "V1_GOAL_REFRAME",
    "V1_INITIAL_THINKING",
    "V1_REPLANNING",
    "V1_STATUS_FAILED",
    "V1_STATUS_IDLE",
    "V1_STATUS_RUNNING",
    "V1_STRATEGY_ALIGNMENT",
    "V1_STRATEGY_CONFIRMED",
    "V1_STRATEGY_DRAFT",
    "V1_TIMELINE_ALIGNMENT",
    "V1_WEEKLY_EXECUTION",
    "actual_pending_question_count",
    "advance",
    "advance_v1_workflow",
    "answer_v1_in_conversation",
    "classify_user_message",
    "compute_next_action",
    "confirm_goal_definition",
    "confirm_strategy",
    "confirm_strategy_understanding",
    "confirm_timeline_alignment",
    "continue_strategy",
    "current_interaction",
    "dimension_projection",
    "dimension_title",
    "generate_coarse_timeline",
    "generate_daily_plan",
    "generate_replan",
    "generate_timeline_alignment",
    "generate_weekly_plan",
    "has_explained_state",
    "is_v1",
    "on_proposal_confirmed",
    "record_feedback",
    "reopen_direction_selection",
    "select_candidate_direction",
    "synthesize_strategy",
    "visible_dimension_keys",
    "weekend_review",
]
