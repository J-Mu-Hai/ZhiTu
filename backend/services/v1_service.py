"""规划智能体重构 V1 — P1:阶段一画布与节点讨论。

## 它是什么

一条**程序控制**的“先想清楚”最小体验,只做 P1 范围:

```
INITIAL_THINKING   初始界面只有根目标 + 右侧大号“初步思考”输入区
        │  用户提交目标
        ▼
GOAL_REFRAME       画布生成 3 个一级分组 + 10 个固定分析容器
        │          首轮给出整体判断 + **一个**全局关键问题
        │  用户点节点右上角箭头进入 / 点开讨论
        ▼
FACTOR_ANALYSIS / STRATEGY_DRAFT   使用既有节点系统继续
```

## 固定容器就是**真实节点**(PlanNode),不是问题节点

这些容器是**预先设定好的固定节点** —— 目标重构 / 问题结构 / 战略路径三组,以及每组
下面固定的分析容器。它们不是“问题节点”,也不需要用户逐项填空。所以这里把它们建成
**真实 `PlanNode`**(`purpose=information`,不排期、不计完成度),沿用既有画布:

- 根目标下挂三个分组;
- 分组下挂各自的分析容器;
- 每个节点右上角的箭头 = **直接进入**(既有 `node.space` 机制);
- 打开节点详情 / 在右侧对话里讨论 = 既有交互。

`purpose=information` 是关键:它是“信息主题”,不需要工时、完成勾选或截止日期,不进
排期、不计完成度,也不能作为硬排期依赖的端点 —— 正合“思考容器”。

## 与老空间 / V0.1 的关系

`v1_stage is None` = 非 V1,本模块所有入口直接返回/不介入。老会话不迁移、不重写。
内容在 P1 由确定性模板产出(不依赖真实模型)。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agent.runtime.base import ReasoningResult
from backend.contracts.reasoning import AgentTurnResponse
from backend.db.models import Conversation, GoalReasoningSession, Message, PlanNode
from backend.db.models.enums import (
    ModelSource,
    NodeOrigin,
    NodePurpose,
    NodeType,
    ReasoningSessionPhase,
    ReasoningSessionStatus,
)
from backend.services import conversation_service, node_service, reasoning_service
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

#: 三个一级分组:(标识, 标题, 说明)。第三组是**空的战略容器**。
_GROUPS: tuple[tuple[str, str, str], ...] = (
    ("goal_reframe", "目标重构", "把一句愿望变成可判断的定义。"),
    ("problem_structure", "问题结构", "看清结果由什么决定、什么真正卡住你。"),
    ("strategy_path", "战略路径", "待形成战略路径。"),
)

#: 十个固定分析容器:(分组标识, 节点标识, 标题, 暂定判断, 为什么重要, 一个关键问题)。
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


def is_v1(session: GoalReasoningSession | None) -> bool:
    """这个会话是不是重构 V1。`v1_stage is None` = 非 V1。"""
    return session is not None and session.v1_stage is not None


def build_judgment(goal: str) -> str:
    """首轮整体判断(2–4 句,全是可审阅结论,不含隐藏思维链)。"""
    subject = (goal or "这个目标").strip() or "这个目标"
    return (
        f"「{subject}」本身还不是结果,它可能服务于自动化、数据分析、AI 项目或求职"
        "这几类不同用途,而每种用途需要的最小成果并不一样。"
        "所以我先不排课程,也不生成时间线。"
        "在继续之前,我需要先确认一件会直接改变路线的事。"
    )


GLOBAL_QUESTION = "你希望最后能拿出什么具体结果,证明它真正解决了你的问题?"


def _container_description(judgment: str, rationale: str, question: str) -> str:
    """分析容器的正文:可审阅的暂定判断 + 为什么重要 + 唯一待确认的问题。"""
    return (
        f"（待验证）{judgment}\n\n"
        f"为什么影响整体战略：{rationale}\n\n"
        f"需要你确认：{question}"
    )


# =================================================================================
# 固定容器:建**真实 PlanNode**(purpose=information,不排期、不计完成度)
# =================================================================================
async def _create_containers(
    db: AsyncSession, ctx: WorkspaceContext, root: PlanNode
) -> int:
    """建立三组与十个固定分析容器。**幂等** —— 根下已有子节点就不重复建。

    每个容器都是 `purpose=information` 的真实节点:它有既有的进入 / 详情 / 讨论交互,
    但不进排期、不计完成度、不能作为硬依赖端点。
    """
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
        return 0

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
        )
        groups[key] = result.node

    created = 0
    for group_key, _node_key, title, judgment, rationale, question in _ANALYSIS:
        parent = groups[group_key]
        await node_service.create_node(
            db,
            ctx,
            parent_id=parent.id,
            title=title,
            node_type=NodeType.CAPABILITY.value,
            purpose=NodePurpose.INFORMATION.value,
            description=_container_description(judgment, rationale, question),
            origin=NodeOrigin.AI,
        )
        created += 1
    return created


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
    - 其它档位:**返回当前状态**,不重建、不重复提问(供前端幂等重入)。
    """
    if session.v1_stage is None:
        session.v1_stage = V1_INITIAL_THINKING
        session.phase = ReasoningSessionPhase.INTAKE
    session.status = ReasoningSessionStatus.READY
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
    """用户提交初步目标。建立三组固定容器,给出判断 + 一个全局问题。"""
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
    if root is not None:
        await _create_containers(db, ctx, root)
    session.v1_judgment = judgment
    session.v1_question = GLOBAL_QUESTION
    if session.v1_stage == V1_INITIAL_THINKING:
        session.v1_stage = V1_GOAL_REFRAME
    session.phase = ReasoningSessionPhase.ROADMAP_DRAFT
    session.status = ReasoningSessionStatus.READY
    #: 只给判断 + 一个问题。**不向用户交代内部实现**(几个分组、几个容器、不写什么)。
    reply = f"{judgment}\n\n{GLOBAL_QUESTION}"
    message = await _append_assistant(db, ctx, reply=reply, conversation=conversation)
    await db.commit()
    return conversation_service.turn_outcome_for_reply(
        user_message=user_message,
        assistant_message=message,
        brief=None,
    )


__all__ = [
    "GLOBAL_QUESTION",
    "V1_FACTOR_ANALYSIS",
    "V1_GOAL_REFRAME",
    "V1_INITIAL_THINKING",
    "V1_STRATEGY_DRAFT",
    "advance",
    "answer_v1_in_conversation",
    "build_judgment",
    "is_v1",
]
