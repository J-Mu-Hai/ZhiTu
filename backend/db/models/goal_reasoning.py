"""目标推理地图:会话、决策节点与推理层连线。

## 它为什么**不是** `plan_nodes`

`plan_nodes` 回答"要做什么、什么时候做"。目标推理回答的是更早的一步:"这个目标里
有哪些必须由用户拍板的取舍、哪些还是假设、下一步先问什么"。把这两件事塞进一张表,
排期、依赖、任务统计、完成度、画布计划树全都要为它加一个例外;漏一处,用户就会在
计划里看到一个"不是任务的东西"。

所以这里是**独立的三张表**。它们只被 `reasoning_service` 读写,`scheduler` /
`proposal_validation` / 任务统计的查询里根本不出现 `reasoning_*` —— 边界是结构性的,
不是靠注释提醒。

## 用户字段与 Agent 字段分开

`user_description` 是用户的原文,Agent **任何一轮都不许覆盖**;`summary` 是 Agent
维护的摘要。分成两列而不是"一个 description 加一个 author",是因为后者要靠代码
自觉判断"这行现在是谁的",而前者是数据库层面的隔离:Agent 的输出解析里根本没有
`user_description` 这个键。

## `handle` 为什么必须存在

推理地图是**跨轮次**的:下一轮模型要能说"把 r3 的状态改成 resolved"。真实 UUID
不能进提示词(与 `plan_nodes` 的记号同一条纪律),所以每个节点在一个会话内有一个
稳定短记号(`r1`、`r2`…),由服务端分配、落库。它同时是 `AgentQuestion` 回指
地图节点的键 —— 回答之后靠它定位要重评的节点,而不是靠文本猜。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db.base import Base, JsonDict, TimestampMixin, UtcDateTime, UuidPk, enum_type
from backend.db.models.enums import (
    PlanningWorkflowStage,
    ReasoningLinkType,
    ReasoningNodeStatus,
    ReasoningNodeType,
    ReasoningSessionPhase,
    ReasoningSessionStatus,
    ReasoningSource,
    ReasoningTurnAction,
)


class GoalReasoningSession(UuidPk, TimestampMixin, Base):
    """一个空间的目标推理会话。**一个空间恰好一条** —— 根目标只有一个。

    `input_version` 是幂等的核心:它把"根目标内容 + 规划简报 + 节点结构"压成一个
    字符串摘要。`space_entered` 只在 `input_version` 变化、未探索、或上次失败时才
    真的跑一轮模型;否则直接返回当前地图。**不靠时间猜、不靠前端传状态。**
    """

    __tablename__ = "goal_reasoning_sessions"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    root_plan_node_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("plan_nodes.id", ondelete="CASCADE"), nullable=False
    )
    phase: Mapped[ReasoningSessionPhase] = mapped_column(
        enum_type(ReasoningSessionPhase, "reasoning_session_phase"),
        default=ReasoningSessionPhase.STRATEGIC_EXPLORATION,
        nullable=False,
    )
    turn_action: Mapped[ReasoningTurnAction] = mapped_column(
        enum_type(ReasoningTurnAction, "reasoning_turn_action"),
        default=ReasoningTurnAction.ANALYZE,
        nullable=False,
    )
    status: Mapped[ReasoningSessionStatus] = mapped_column(
        enum_type(ReasoningSessionStatus, "reasoning_session_status"),
        default=ReasoningSessionStatus.IDLE,
        nullable=False,
    )
    #: 当前焦点节点 id。**刻意不设外键** —— 它与 `reasoning_nodes` 互相引用,加外键会
    #: 在 SQLite 的批处理迁移与建表顺序上制造循环;节点只会随会话级联删除,由服务层
    #: 保证它指向本会话内的行。
    focus_reasoning_node_id: Mapped[uuid.UUID | None] = mapped_column()
    #: 面向用户的一句话:为什么现在先处理它。服务端存下来,不每轮重算。
    focus_reason: Mapped[str | None] = mapped_column(Text)
    #: 地图版本。每次成功写入地图 +1;前端据此判断"要不要重画"。
    map_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: 上一次成功探索时的输入摘要。幂等比较只用它,不用时间。
    input_version: Mapped[str | None] = mapped_column(String(128))
    #: 最近一次成功回合的幂等键。同一把钥匙重放直接返回当前地图,不重复跑模型。
    last_idempotency_key: Mapped[str | None] = mapped_column(String(64))
    #: 上一次失败的原因(可读,给人看)。成功后清空。**失败绝不写半成品地图。**
    last_error: Mapped[str | None] = mapped_column(Text)
    explored_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    last_evaluated_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    #: 战略确认提案。**确认之后**才把关联写回 `linked_plan_node_id`。
    strategy_proposal_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("proposals.id", ondelete="SET NULL")
    )
    # ---- 阶段 11:战略澄清 intake 与时间架构 ----
    #: intake 已经问过几个关键问题。**最多 5 个**;到顶必须继续生成架构,不能无限追问。
    intake_questions_asked: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # ---- 阶段 12:对话式 intake 的待回答问题(不再落 agent_questions) ----
    #: 正在等用户回答的那条 intake 助手消息。非空 = “这个空间正在等 intake 回答”,
    #: 用户下一条消息就是它的答案。问题询问的实体不在 `agent_questions` 里。
    pending_intake_message_id: Mapped[uuid.UUID | None] = mapped_column()
    #: 待回答问题的结构化决策(question / decisionScope / whyThisMatters / quickReplies)。
    #: **不存选项实体、不存分析 JSON 卡片** —— 只是最小的一句话与 0–3 个快捷回复。
    pending_intake_decision: Mapped[dict | None] = mapped_column(JsonDict)
    #: 时间架构里的日期是否已校准。False = 只有相对周(第 1–2 周…),不伪造日历日期。
    dates_calibrated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # ---- 规划智能体 V0.1:程序控制的三阶段工作流 ----
    #: V0.1 工作流阶段。**None = 老 workspace / 非 V0.1**,行为与以前完全一样。
    workflow_stage: Mapped[PlanningWorkflowStage | None] = mapped_column(
        enum_type(PlanningWorkflowStage, "planning_workflow_stage")
    )
    #: 阶段一的发现状态:`{questions[], answer, followups, template, insight}`。
    #: 最小状态,不是问卷表;用户回答只存一次。
    discovery: Mapped[dict | None] = mapped_column(JsonDict)
    #: V0.1 时间线的**唯一权威投影**:结构化阶段草案/已确认项。
    #: `[{id,title,kind,startWeek,endWeek,startDate,endDate,goal,deliverable,
    #:   completionCriteria,status,planNodeId}]`。前端只读它,不从自然语言猜日期。
    v01_timeline: Mapped[list | None] = mapped_column(JsonDict)
    #: 时间线确认提案 id。复用现有 proposal → 确认 → 版本校验 → 事务写入。
    #: 刻意不设外键 —— 与 `focus_reasoning_node_id` 同一取舍:避免 batch 迁移与
    #: 建表顺序上的循环,由服务层保证它指向本空间的提案。
    timeline_proposal_id: Mapped[uuid.UUID | None] = mapped_column()
    # ---- 规划智能体重构 V1(P1:阶段一画布与节点讨论) ----
    #: V1 阶段一工作流档位:`initial_thinking` / `goal_reframe` / `factor_analysis` /
    #: `strategy_draft`。**None = 非 V1**(老空间 / V0.1),行为与加列之前完全一样。
    v1_stage: Mapped[str | None] = mapped_column(String(32))
    #: 首轮回答后给用户的**整体判断**(2–4 句,可审阅结论,不含隐藏思维链)。
    v1_judgment: Mapped[str | None] = mapped_column(Text)
    #: 当前唯一需要用户回答的**全局关键问题**。一轮最多一个;回答后清空。
    v1_question: Mapped[str | None] = mapped_column(Text)
    #: 当前焦点容器标识(`current_state` / …)。模型选出的“最值得讨论的一项”。
    v1_focus_key: Mapped[str | None] = mapped_column(String(48))
    #: 为什么这个焦点比其他未知项更能改变路线。给用户看的一句话。
    v1_focus_reason: Mapped[str | None] = mapped_column(Text)
    #: 战略路径草案:`{main_line, parallel_line, defer_or_avoid, risk_control,
    #: tradeoff, confirmed}`。只在四项战略子节点都成形后才写入。
    v1_strategy: Mapped[dict | None] = mapped_column(JsonDict)
    #: V1 模型回合状态:`idle` / `running` / `failed`。给 UI 准确状态与重试入口。
    v1_status: Mapped[str | None] = mapped_column(String(16))
    #: 上一次模型回合失败的可读原因(成功/未跑时为空)。
    v1_error: Mapped[str | None] = mapped_column(Text)
    # ---- 规划智能体重构 V1(P2.1):战略判断优先 + 有限追问守卫 ----
    #: 当前战略判断(优先展示的 AI 暂定理解,可被用户纠正)。
    v1_strategic_thesis: Mapped[str | None] = mapped_column(Text)
    #: 用户无法回答时 AI 给出的候选方向(最多 3 个,可被选择/修正/否定)。
    v1_candidate_directions: Mapped[list | None] = mapped_column(JsonDict)
    #: 用户选择的候选方向键。
    v1_selected_direction: Mapped[str | None] = mapped_column(String(48))
    #: 阶段一已问关键问题的次数(全局预算最多 3)。
    v1_question_budget_used: Mapped[int | None] = mapped_column(Integer)
    #: 上一次提问的焦点键(同一焦点连续提问最多 1 次)。
    v1_last_focus_key: Mapped[str | None] = mapped_column(String(48))
    #: 连续“低信息/不确定/元对话”回答的轮数;>=2 时必须给候选方向或暂定综合。
    v1_low_info_streak: Mapped[int | None] = mapped_column(Integer)

    nodes: Mapped[list[ReasoningNode]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )

    __table_args__ = (UniqueConstraint("workspace_id", name="uq_goal_reasoning_sessions_workspace_id"),)


class ReasoningNode(UuidPk, TimestampMixin, Base):
    """推理地图上的一个决策维度 / 问题 / 风险 / 资源 / 路线 / 假设。

    `priority` 不落库 —— 它是四个维度的可解释启发式(见
    `reasoning_service.compute_priority`),由读取时现算。落库会让"改了评分忘了改
    排序"成为可能,而排序恰恰是用户看到的那一个数。
    """

    __tablename__ = "reasoning_nodes"

    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("goal_reasoning_sessions.id", ondelete="CASCADE"), nullable=False
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("reasoning_nodes.id", ondelete="CASCADE")
    )
    #: 若这个推理节点已经落到业务计划(例如用户确认了某条战略路线),指向那个节点。
    linked_plan_node_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("plan_nodes.id", ondelete="SET NULL")
    )
    #: 会话内稳定短记号(`r1`…)—— 模型只能用记号,不能用真实 UUID。
    handle: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    #: 用户改过标题之后置真。**Agent 永远不再覆盖已锁定的标题。**
    title_locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: Agent 维护的摘要。**与用户原文分开**。
    summary: Mapped[str | None] = mapped_column(Text)
    #: 用户写的原文。**Agent 任何一轮都不许覆盖这一列。**
    user_description: Mapped[str | None] = mapped_column(Text)
    node_type: Mapped[ReasoningNodeType] = mapped_column(
        enum_type(ReasoningNodeType, "reasoning_node_type"),
        default=ReasoningNodeType.DIMENSION,
        nullable=False,
    )
    status: Mapped[ReasoningNodeStatus] = mapped_column(
        enum_type(ReasoningNodeStatus, "reasoning_node_status"),
        default=ReasoningNodeStatus.UNEXPLORED,
        nullable=False,
    )
    next_action: Mapped[ReasoningTurnAction] = mapped_column(
        enum_type(ReasoningTurnAction, "reasoning_node_next_action"),
        default=ReasoningTurnAction.ANALYZE,
        nullable=False,
    )
    importance: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    uncertainty: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    urgency: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    impact: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    confidence: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: 为什么这样评分/排序。给人看的一句话。
    rationale: Mapped[str | None] = mapped_column(Text)
    #: 尚未验证的假设与依据。JSON 列表,整体读写。
    assumptions: Mapped[list] = mapped_column(JsonDict, default=list, nullable=False)
    evidence: Mapped[list] = mapped_column(JsonDict, default=list, nullable=False)
    #: --- 阶段 8 的路线要素。**只对 route / stage 节点有意义**,其余节点为 NULL。 ---
    #: 粗粒度时间带,例如“约 2 周”“3–4 周”。**不是排期**,不是截止日期。
    timeframe: Mapped[str | None] = mapped_column(String(64))
    #: 这个阶段要交出的东西(可运行的练习 / 一份分析报告……)。
    deliverable: Mapped[str | None] = mapped_column(Text)
    #: 怎么算通过 —— 决定阶段能不能进入下一阶段的判据。
    pass_criteria: Mapped[str | None] = mapped_column(Text)
    # ---- 阶段 11:结构化时间架构(只对 route / stage 有意义) ----
    #: `dated`(有年月日/自然周)或 `relative`(第 N–M 周)。空 = 还没有结构化时间。
    timeframe_kind: Mapped[str | None] = mapped_column(String(16))
    #: 相对周轴(从 1 开始,含端点)。与 `timeframe_kind="relative"` 搭配。
    start_week: Mapped[int | None] = mapped_column(Integer)
    end_week: Mapped[int | None] = mapped_column(Integer)
    #: 明确日期。与 `timeframe_kind="dated"` 搭配。**不伪造**:没有日期就留空。
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    source: Mapped[ReasoningSource] = mapped_column(
        enum_type(ReasoningSource, "reasoning_source"),
        default=ReasoningSource.AGENT,
        nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    # ---- 规划智能体重构 V1:画布层级标记(只对 V1 节点有意义) ----
    #: V1 画布角色:`group`(一级分组) / `analysis`(固定分析容器) / `strategy`
    #: (空的战略容器)。None = 非 V1 节点。
    v1_kind: Mapped[str | None] = mapped_column(String(16))
    #: V1 固定标识:`current_state` / `true_intent` / …。模型与前端都用它定位,
    #: 不用会漂移的标题。None = 非 V1 节点。
    v1_key: Mapped[str | None] = mapped_column(String(48))
    #: 这个分析节点当前**唯一待确认的一件事**。回答后清空,不无限追问。
    v1_question: Mapped[str | None] = mapped_column(Text)

    session: Mapped[GoalReasoningSession] = relationship(back_populates="nodes")

    __table_args__ = (
        UniqueConstraint("session_id", "handle", name="uq_reasoning_nodes_session_id_handle"),
        Index("ix_reasoning_nodes_session_id_parent_id", "session_id", "parent_id"),
        Index("ix_reasoning_nodes_session_id_status", "session_id", "status"),
    )


class ReasoningNodeLink(UuidPk, TimestampMixin, Base):
    """推理层的边。**只表达 depends_on / influences,不复用业务关系表。**"""

    __tablename__ = "reasoning_node_links"

    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("goal_reasoning_sessions.id", ondelete="CASCADE"), nullable=False
    )
    source_reasoning_node_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("reasoning_nodes.id", ondelete="CASCADE"), nullable=False
    )
    target_reasoning_node_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("reasoning_nodes.id", ondelete="CASCADE"), nullable=False
    )
    link_type: Mapped[ReasoningLinkType] = mapped_column(
        enum_type(ReasoningLinkType, "reasoning_link_type"),
        default=ReasoningLinkType.INFLUENCES,
        nullable=False,
    )
    note: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint(
            "session_id",
            "source_reasoning_node_id",
            "target_reasoning_node_id",
            "link_type",
            name="uq_reasoning_node_links_session_id_source_target_type",
        ),
        Index("ix_reasoning_node_links_session_id", "session_id"),
    )


__all__ = ["GoalReasoningSession", "ReasoningNode", "ReasoningNodeLink"]
