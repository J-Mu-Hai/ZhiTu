"""规划智能体重构 V1 — P1:阶段一画布与节点讨论。

## 它是什么

一条**程序控制**的“先想清楚”最小体验,只做 P1 范围:

```
INITIAL_THINKING   初始界面只有根目标 + 大号“初步思考”输入区
        │  用户提交目标
        ▼
GOAL_REFRAME       画布生成 3 个一级分组 + 10 个固定分析容器(推理层)
        │          首轮给出整体判断 + **一个**全局关键问题
        │  用户点开/回答某个分析节点
        ▼
FACTOR_ANALYSIS / STRATEGY_DRAFT   只更新该节点,不重建地图
```

## 三条不可破坏的边界(P1)

1. **只写 reasoning 层。** 分组与分析节点都是 `ReasoningNode`,**绝不**创建或修改
   `plan_nodes`、时间线、周/日任务。P1 不生成路线、阶段或时间线。
2. **初始为占位判断,明确标注“待验证”。** 不把模型猜测写成用户事实;用户回答只
   追加为“已知事实”,节点判断仍保留待验证语气。
3. **不展示隐藏思维链。** 只保存可审阅的结论、假设、事实来源与下一步问题。

## 与老空间 / V0.1 的关系

`v1_stage is None` = 非 V1,本模块所有入口直接返回/不介入。老会话不迁移、不重写。
内容在 P1 由确定性模板产出(不依赖真实模型),但 UI、状态与数据边界已经支持这种交互。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agent.runtime.base import ReasoningResult
from backend.contracts.reasoning import AgentTurnResponse
from backend.db.models import Conversation, GoalReasoningSession, Message, ReasoningNode
from backend.db.models.enums import (
    ModelSource,
    ReasoningNodeStatus,
    ReasoningNodeType,
    ReasoningSessionPhase,
    ReasoningSessionStatus,
    ReasoningSource,
)
from backend.services import conversation_service, reasoning_service
from backend.services.context import WorkspaceContext

# =================================================================================
# V1 阶段一档位
# =================================================================================
#: 初始界面:只有根目标与干净画布,右侧是大号“初步思考”输入区。
V1_INITIAL_THINKING = "initial_thinking"
#: 已建立三组画布,正在澄清目标定义。
V1_GOAL_REFRAME = "goal_reframe"
#: 目标定义足以讨论后,进入影响因素分析。
V1_FACTOR_ANALYSIS = "factor_analysis"
#: 目标与因素稳定后,形成战略路径草案。
V1_STRATEGY_DRAFT = "strategy_draft"

#: 三个一级分组。第三组是**空的战略容器** —— P1 不在这里生成路线。
_GROUPS: tuple[tuple[str, str, str, str], ...] = (
    ("g1", "goal_reframe", "目标重构", "把一句愿望变成可判断的定义。"),
    ("g2", "problem_structure", "问题结构", "看清结果由什么决定、什么真正卡住你。"),
    ("g3", "strategy_path", "战略路径", "待形成战略路径。"),
)

#: 十个固定分析容器:(分组 key, 节点 key, 标题, 暂定判断, 为什么重要, 一个关键问题)。
_ANALYSIS: tuple[tuple[str, str, str, str, str, str], ...] = (
    (
        "goal_reframe",
        "current_state",
        "你现在在哪",
        "你现在的起点还不清楚:已有基础、可投入时间与可用资源都还没有确认。",
        "现状决定起点,也决定每个阶段该多长。",
        "在这件事上,你现在已经具备什么、还缺什么?",
    ),
    (
        "goal_reframe",
        "true_intent",
        "你真正想要什么",
        "表面目标背后可能有几种不同的真实诉求,它们会导向不同的学习深度。",
        "真实意图不同,后面的成果定义与阶段顺序会完全不同。",
        "你希望最后能拿出什么具体结果,证明它真正解决了你的问题?",
    ),
    (
        "goal_reframe",
        "value_assessment",
        "这件事值得做吗",
        "值不值得做,取决于它与你的长期方向的关系,而不是它本身热不热门。",
        "先判断值不值得投入,再谈怎么投入,能避免把时间花在伪目标上。",
        "如果这件事三年内不会带来直接回报,你还会做吗?为什么?",
    ),
    (
        "goal_reframe",
        "key_conflict",
        "真正卡你的是什么",
        "真正卡住你的可能不是知识量,而是目标不清、反馈太慢或时间不够。",
        "只处理最关键的一两个矛盾,比同时补十个短板更有效。",
        "如果要只解决一个障碍,哪一个解决了,整件事就会往前推进?",
    ),
    (
        "goal_reframe",
        "goal_definition",
        "最后到底要做到什么",
        "目标还没有可观察的成果定义与成功判据。",
        "没有成果定义,后面的阶段与时间线都无从判断。",
        "做到什么程度、拿出什么,你就认为这件事成了?",
    ),
    (
        "problem_structure",
        "hard_constraints",
        "硬约束",
        "截止窗口、可投入时间、设备与资格这类硬约束还没有确认。",
        "硬约束决定哪些路线根本不可行,必须先于偏好确认。",
        "有哪些是你不能改、只能接受的限制?",
    ),
    (
        "problem_structure",
        "controllable_factors",
        "可控变量",
        "可以通过行动改变的因素还没有筛出来。",
        "只讨论能改变的东西,才能把注意力放在真正有产出的动作上。",
        "在这件事上,哪些是你能直接行动改变的?",
    ),
    (
        "problem_structure",
        "key_levers",
        "关键杠杆",
        "最可能改变最终结果的一两个变量还没定。",
        "抓住关键杠杆,比均匀用力更快看到结果。",
        "哪个变量一旦改善,最终结果的提升最大?",
    ),
    (
        "problem_structure",
        "major_risks",
        "主要风险",
        "最可能让这件事失败的风险与早期信号尚未识别。",
        "提前识别风险与信号,才能设置检查点与备用路径。",
        "最可能让这件事失败的是什么?你能提前看到什么信号?",
    ),
    (
        "problem_structure",
        "external_conditions",
        "外部条件",
        "政策、竞争、导师、市场等外部因素还没有核对。",
        "外部条件不在你控制内,却常常决定路线的可行性。",
        "有哪些外部因素不在你控制内,却会明显影响结果?",
    ),
)

#: 分析节点初始的“已知事实”文案键。
_FACT_SOURCE = "（已知事实 · 来自你写下的目标）"


@dataclass(slots=True)
class _CanvasResult:
    """画布创建结果。"""

    created: bool
    node_count: int = 0
    group_count: int = 0
    analysis_count: int = 0
    keys: list[str] = field(default_factory=list)


def is_v1(session: GoalReasoningSession | None) -> bool:
    """这个会话是不是重构 V1。`v1_stage is None` = 非 V1。"""
    return session is not None and session.v1_stage is not None


def build_judgment(goal: str) -> str:
    """首轮整体判断(2–4 句,全是可审阅结论,不含隐藏思维链)。

    P1 用确定性模板;**明确不排课程、不生成时间线**。
    """
    subject = (goal or "这个目标").strip() or "这个目标"
    return (
        f"「{subject}」本身还不是结果,它可能服务于自动化、数据分析、AI 项目或求职"
        "这几类不同用途,而每种用途需要的最小成果并不一样。"
        "所以我先不排课程,也不生成时间线。"
        "在继续之前,我需要先确认一件会直接改变路线的事。"
    )


GLOBAL_QUESTION = "你希望最后能拿出什么具体结果,证明它真正解决了你的问题?"
#: 画布默认焦点(第一个目标重构节点)的说明。
FOCUS_REASON = "先确认起点与真实意图,后面的问题结构与战略路径才有依据。"


# =================================================================================
# 画布创建(只写 reasoning 层)
# =================================================================================
async def _ensure_canvas(
    db: AsyncSession, session: GoalReasoningSession, goal_text: str
) -> _CanvasResult:
    """建立三组层级与十个固定分析容器。**幂等** —— 已有 V1 节点就不重复建。"""
    existing = await _load_nodes(db, session.id)
    if any(node.v1_key for node in existing):
        analysis = [node for node in existing if node.v1_kind == "analysis"]
        return _CanvasResult(
            created=False,
            node_count=len(existing),
            group_count=len([n for n in existing if n.v1_kind in ("group", "strategy")]),
            analysis_count=len(analysis),
            keys=[str(node.v1_key) for node in existing if node.v1_key],
        )

    groups: dict[str, ReasoningNode] = {}
    for handle, key, title, summary in _GROUPS:
        node = ReasoningNode(
            session_id=session.id,
            parent_id=None,
            handle=handle,
            title=title,
            summary=summary,
            node_type=ReasoningNodeType.DIMENSION,
            status=ReasoningNodeStatus.UNEXPLORED,
            importance=5 if key == "goal_reframe" else 4,
            uncertainty=3,
            urgency=1,
            impact=5 if key == "goal_reframe" else 4,
            confidence=2,
            rationale="它决定后面所有判断的方向。" if key == "goal_reframe" else "它决定路线的可行性。",
            assumptions=[],
            evidence=[],
            source=ReasoningSource.AGENT,
            v1_kind="strategy" if key == "strategy_path" else "group",
            v1_key=key,
        )
        db.add(node)
        groups[key] = node
    await db.flush()

    counters: dict[str, int] = {}
    analysis_count = 0
    for group_key, node_key, title, judgment, rationale, question in _ANALYSIS:
        parent = groups[group_key]
        index = counters.get(group_key, 0) + 1
        counters[group_key] = index
        node = ReasoningNode(
            session_id=session.id,
            parent_id=parent.id,
            handle=f"{parent.handle}a{index}",
            title=title,
            #: P1 的暂定判断 —— **明确标注待验证**,不是用户事实。
            summary=f"（待验证）{judgment}",
            node_type=ReasoningNodeType.DIMENSION,
            status=ReasoningNodeStatus.UNEXPLORED,
            importance=4,
            uncertainty=4,
            urgency=1,
            impact=4,
            confidence=1,
            rationale=rationale,
            assumptions=[f"（AI 假设）{judgment}"],
            evidence=[f"{_FACT_SOURCE}{goal_text}"] if goal_text else [],
            source=ReasoningSource.AGENT,
            v1_kind="analysis",
            v1_key=node_key,
            v1_question=question,
        )
        db.add(node)
        analysis_count += 1
    await db.flush()

    total = len(_GROUPS) + analysis_count
    return _CanvasResult(
        created=True,
        node_count=total,
        group_count=len(_GROUPS),
        analysis_count=analysis_count,
        keys=[key for _, key, _, _ in _GROUPS]
        + [node_key for _, node_key, *_ in _ANALYSIS],
    )


async def _load_nodes(db: AsyncSession, session_id) -> list[ReasoningNode]:
    rows = await db.execute(
        select(ReasoningNode)
        .where(ReasoningNode.session_id == session_id)
        .order_by(ReasoningNode.created_at.asc(), ReasoningNode.handle.asc())
    )
    return list(rows.scalars())


async def _append_assistant(
    db, ctx: WorkspaceContext, *, reply: str, conversation: Conversation | None = None
) -> Message:
    conversation = conversation or await conversation_service.get_or_create_primary_conversation(
        db, ctx
    )
    result = ReasoningResult(
        reply=reply,
        source=ModelSource.DIRECT_LLM,
        request_id="v1-deterministic",
        prompt_version="v1-p1-template",
    )
    return await conversation_service.append_reply(
        db, ctx, conversation=conversation, result=result
    )


async def _response(
    db,
    ctx: WorkspaceContext,
    session: GoalReasoningSession,
    *,
    message=None,
    changed: bool = False,
    trace=None,
):
    if trace is not None:
        from backend.services import agent_trace_service

        agent_trace_service.mark_terminal(
            trace,
            degraded=False,
            degraded_reason=None,
            stopped_reason="ready_to_propose",
            code=None,
        )
    await db.commit()
    return await reasoning_service._response(
        db, ctx, session, message=message, changed=changed
    )


# =================================================================================
# 唯一自动推进入口(space_entered / retry)
# =================================================================================
async def advance(
    db: AsyncSession,
    ctx: WorkspaceContext,
    root,
    session: GoalReasoningSession,
    *,
    trace,
) -> AgentTurnResponse:
    """按当前 V1 档位推进**一步**。P1 的推进只建立初始状态,不生成计划。

    - `initial_thinking` 之前:标记会话进入初始思考,画布保持干净;
    - 其它档位:**返回当前状态**,不重建地图、不重复提问(供前端幂等重入)。
    """
    if session.v1_stage is None:
        session.v1_stage = V1_INITIAL_THINKING
    session.status = ReasoningSessionStatus.READY
    session.phase = ReasoningSessionPhase.INTAKE
    return await _response(db, ctx, session, changed=False, trace=trace)


# =================================================================================
# 对话入口:/messages 的第一条(初步思考提交)
# =================================================================================
async def answer_v1_in_conversation(
    db: AsyncSession,
    ctx: WorkspaceContext,
    session: GoalReasoningSession,
    *,
    content: str,
    client_message_id: str | None,
    context_node_id,
):
    """用户提交初步目标。**只写 reasoning 层**,然后给出判断 + 一个全局问题。"""
    conversation = await conversation_service.get_or_create_primary_conversation(db, ctx)
    user_message = await conversation_service.record_user_message(
        db,
        ctx,
        conversation=conversation,
        text=content,
        client_message_id=client_message_id,
        context_node_id=context_node_id,
    )
    root = await reasoning_service.root_plan_node(db, ctx)
    goal_text = (root.title if root is not None else "") or ""
    judgment = build_judgment(goal_text)
    await _ensure_canvas(db, session, goal_text)
    session.v1_judgment = judgment
    session.v1_question = GLOBAL_QUESTION
    if session.v1_stage == V1_INITIAL_THINKING:
        session.v1_stage = V1_GOAL_REFRAME
    session.phase = ReasoningSessionPhase.ROADMAP_DRAFT
    session.status = ReasoningSessionStatus.READY
    if root is not None:
        focus = await db.scalar(
            select(ReasoningNode).where(
                ReasoningNode.session_id == session.id,
                ReasoningNode.v1_key == "current_state",
            )
        )
        if focus is not None:
            session.focus_reasoning_node_id = focus.id
            session.focus_reason = FOCUS_REASON
    #: 只给判断 + 一个问题。**不向用户交代内部实现**(几个分组、几个容器、不写什么)。
    reply = f"{judgment}\n\n{GLOBAL_QUESTION}"
    message = await _append_assistant(db, ctx, reply=reply, conversation=conversation)
    await db.commit()
    return conversation_service.turn_outcome_for_reply(
        user_message=user_message,
        assistant_message=message,
        brief=None,
    )


# =================================================================================
# 节点局部讨论(显式 Agent turn,不经过模型)
# =================================================================================
async def _find_node(
    db: AsyncSession, session: GoalReasoningSession, handle: str
) -> ReasoningNode | None:
    return await db.scalar(
        select(ReasoningNode).where(
            ReasoningNode.session_id == session.id,
            ReasoningNode.handle == handle,
        )
    )


async def handle_node_turn(
    db: AsyncSession,
    ctx: WorkspaceContext,
    session: GoalReasoningSession,
    *,
    payload,
) -> AgentTurnResponse:
    """节点局部讨论:打开或回答。

    - `node_selected`:把焦点移到该节点(只改 reasoning 层指针);
    - `user_message`:把用户补充记为**已知事实**,只更新该节点;不重建地图,
      不生成任务/时间线/周计划。回答后清空该节点的“唯一待确认之事”。
    """
    from backend.services.errors import InvalidInput

    handle = (payload.reasoning_handle or "").strip()
    node = await _find_node(db, session, handle)
    if node is None:
        raise InvalidInput("没有找到这个分析节点。")

    session.focus_reasoning_node_id = node.id
    session.focus_reason = node.rationale or FOCUS_REASON
    if node.status is ReasoningNodeStatus.UNEXPLORED:
        node.status = ReasoningNodeStatus.EXPLORING

    if payload.trigger == "node_selected":
        await db.commit()
        return await reasoning_service._response(db, ctx, session, changed=True)

    text = (payload.message or "").strip()
    if not text:
        raise InvalidInput("讨论内容不能是空的。")

    # 只更新这一个节点:已知事实 + 用户原文 + 状态。**不触碰其它节点、不重建地图。**
    evidence = list(node.evidence or [])
    evidence.append(f"（用户补充）{text}")
    node.evidence = evidence
    node.user_description = (
        f"{node.user_description}\n{text}".strip() if node.user_description else text
    )
    node.status = ReasoningNodeStatus.RESOLVED
    node.v1_question = None
    node.version += 1

    conversation = await conversation_service.get_or_create_primary_conversation(db, ctx)
    reply = f"已记下你对「{node.title}」的补充,这条节点现在标记为已澄清。"
    message = await _append_assistant(db, ctx, reply=reply, conversation=conversation)
    return await _response(db, ctx, session, message=message, changed=True)


__all__ = [
    "GLOBAL_QUESTION",
    "V1_FACTOR_ANALYSIS",
    "V1_GOAL_REFRAME",
    "V1_INITIAL_THINKING",
    "V1_STRATEGY_DRAFT",
    "advance",
    "answer_v1_in_conversation",
    "build_judgment",
    "handle_node_turn",
    "is_v1",
]
