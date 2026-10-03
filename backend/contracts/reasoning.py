"""目标推理地图的接口契约(阶段 7)。

## 为什么单独一份契约,而不是塞进 plan / question

推理地图**不是计划**,也**不是问题列表**:它是一棵持久的决策结构。把它混进
`PlanPayload` 会让前端把两类节点画在同一份数据上,而"这四个是推理维度、那两个才是
任务"只能靠命名约定分辨 —— 那是会漂移的边界。

`AgentTurnRequest` 是**显式的 Agent turn 入口**,不是"发一条空消息"。`trigger` 决定
服务端怎么处理这一轮,`idempotencyKey` 决定重放是否安全。借空用户消息触发自动探索
是一种必须避免的写法:它会把"系统自己发起"混进用户对话历史。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import Field

from backend.contracts.common import ApiModel
from backend.contracts.conversation import MessageView
from backend.contracts.proposal import ActionError
from backend.contracts.question import QuestionView

#: 显式 Agent turn 的触发来源。**闭集** —— 服务端按它选阶段与动作,前端不拼状态。
AgentTurnTrigger = Literal[
    "space_entered",
    "user_message",
    "node_selected",
    "question_answered",
    "strategy_confirmation",
    "regenerate_roadmap",
    "progress_update",
    "execution_planning",
    "retry",
]


class AgentTurnRequest(ApiModel):
    trigger: AgentTurnTrigger
    #: 用户当前选中的**业务**节点(可解析指代)。
    selected_node_id: uuid.UUID | None = None
    #: 用户在地图上点的**推理**节点(用会话内短记号,不是 UUID)。
    reasoning_handle: str | None = Field(default=None, max_length=16)
    #: `user_message` / 讨论动作携带的自由文本。
    message: str | None = Field(default=None, max_length=4000)
    #: 幂等键。同一把钥匙重放返回上一次结果,不重复跑模型、不重复建节点。
    idempotency_key: str = Field(min_length=1, max_length=64)
    #: 前端手上这份地图的版本。落后时服务端会以自己那份为准(不信任客户端状态)。
    context_version: int | None = None


class ReasoningNodeView(ApiModel):
    id: uuid.UUID
    handle: str
    parent_handle: str | None = None
    linked_plan_node_id: uuid.UUID | None = None
    title: str
    summary: str | None = None
    #: 用户原文。**与 summary 分区显示;Agent 不覆盖它。**
    user_description: str | None = None
    node_type: str
    status: str
    next_action: str
    importance: int
    uncertainty: int
    urgency: int
    impact: int
    confidence: int
    #: 可解释启发式现算,不落库(见 `reasoning_service.compute_priority`)。
    priority: float
    rationale: str | None = None
    assumptions: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    #: --- 阶段 8 的路线要素。只对 route / stage 节点有值。 ---
    #: 粗粒度时间带(例如“约 2 周”),**不是排期/截止日期**。
    timeframe: str | None = None
    deliverable: str | None = None
    pass_criteria: str | None = None
    #: 阶段 11:结构化时间架构。`dated` 用 start_date/end_date;`relative` 用 start_week/end_week。
    #: `dates_calibrated=False` 时只有相对周,不伪造日历日期。
    timeframe_kind: str | None = None
    start_week: int | None = None
    end_week: int | None = None
    start_date: date | None = None
    end_date: date | None = None
    source: str
    version: int
    updated_at: datetime
    #: --- 规划智能体重构 V1(P1) ---
    #: 画布角色:`group`(一级分组) / `analysis`(固定分析容器) / `strategy`(战略容器)。
    #: None = 非 V1 节点。
    v1_kind: str | None = None
    #: V1 固定标识(`current_state` / `true_intent` / …)。**用标识定位,不用标题。**
    v1_key: str | None = None
    #: 该分析节点当前**唯一待确认的一件事**。None = 不再需要追问。
    v1_question: str | None = None


class ReasoningLinkView(ApiModel):
    id: uuid.UUID
    source_handle: str
    target_handle: str
    link_type: str
    note: str | None = None


class PendingIntakeView(ApiModel):
    """阶段 12:对话式战略 intake 当前等回答的那一条。**不是问题实体。**

    它只来自会话上的最小状态 `(pending_intake_message_id, pending_intake_decision)`;
    问题原文同时已经写在那条助手消息正文里。前端据此在消息下渲染快捷回复。
    """

    #: 正在等回答的那条助手消息 id。刷新后前端靠它把提问挂在对应气泡下。
    message_id: uuid.UUID | None = None
    question: str
    decision_scope: str = ""
    why_this_matters: str = ""
    quick_replies: list[str] = Field(default_factory=list)


class V01TimelineItemView(ApiModel):
    """V0.1 时间轴上的一个投影项(阶段 / 里程碑 / 截止 / 成果)。

    它来自会话上的 `v01_timeline` JSON,是**唯一权威来源**;前端不解析自然语言
    描述去猜日期。`status=draft` 表示还没被用户确认。
    """

    id: str
    title: str
    #: phase / milestone / deadline / deliverable
    kind: str
    start_week: int | None = None
    end_week: int | None = None
    start_date: date | None = None
    end_date: date | None = None
    goal: str = ""
    deliverable: str = ""
    completion_criteria: str = ""
    #: draft / planned
    status: str = "draft"
    plan_node_id: uuid.UUID | None = None


class GoalReasoningView(ApiModel):
    """当前目标推理地图。**读接口与 agent turn 都返回这一份。**"""

    workspace_id: uuid.UUID
    session_id: uuid.UUID | None = None
    root_plan_node_id: uuid.UUID | None = None
    phase: str
    turn_action: str
    status: str
    map_version: int = 0
    focus_handle: str | None = None
    focus_reasoning_node_id: uuid.UUID | None = None
    #: 面向用户的一句话:为什么现在先处理它。
    focus_reason: str | None = None
    #: 阶段 11:intake 进度(已问几个关键问题 / 上限)。对话区用它显示极轻量进度。
    intake_questions_asked: int = 0
    intake_question_limit: int = 0
    #: 阶段 12:当前正在等回答的 intake 关键问题。None = 不在等回答。
    pending_intake: PendingIntakeView | None = None
    #: 规划智能体 V0.1 的工作流阶段。None = 非 V0.1(老 workspace)。
    workflow_stage: str | None = None
    #: V0.1:阶段一当前等回答的核心问题(2–4 个),放在会话面上、不建问题实体。
    discovery_questions: list[str] = Field(default_factory=list)
    #: V0.1:时间线投影。非 V0.1 一律为空。
    v01_timeline: list[V01TimelineItemView] = Field(default_factory=list)
    #: V0.1:待确认的时间线提案 id。None = 没有待确认草案。
    v01_timeline_proposal_id: uuid.UUID | None = None
    #: --- 规划智能体重构 V1(P1:阶段一画布与节点讨论) ---
    #: V1 阶段一档位:`initial_thinking` / `goal_reframe` / `factor_analysis` /
    #: `strategy_draft`。None = 非 V1(老空间 / V0.1)。
    v1_stage: str | None = None
    #: 首轮回答后的整体判断(可审阅结论,不含隐藏思维链)。
    v1_judgment: str | None = None
    #: 当前唯一需要回答的全局关键问题。None = 不等待全局回答。
    v1_question: str | None = None
    #: 当前焦点容器键(模型选出的最值得讨论的一项)。
    v1_focus_key: str | None = None
    #: 为什么这个焦点比其他未知项更能改变路线。
    v1_focus_reason: str | None = None
    #: 战略路径草案:`{main_line, parallel_line, defer_or_avoid, risk_control,
    #: tradeoff, confirmed}`。None = 还没形成。
    v1_strategy: dict | None = None
    #: V1 模型回合状态:`idle` / `running` / `failed`。给 UI 准确状态与重试入口。
    v1_status: str | None = None
    #: 上一次 V1 模型回合失败的可读原因。
    v1_error: str | None = None
    #: R1:当前/最近一轮 OpenJiuwen 工作回合的 turn id、deadline 与真实来源。
    #: `v1_turn_deadline_at` 非空 = 正在工作;到点仍未完成会转成可重试失败。
    v1_turn_id: str | None = None
    v1_turn_deadline_at: datetime | None = None
    v1_turn_source: str | None = None
    #: R1:V1 是否强制真实 OpenJiuwen。UI 据此显示“AI 规划 · OpenJiuwen”。
    v1_require_openjiuwen: bool = False
    #: P2.1:当前战略判断(优先展示的 AI 暂定理解,可被用户纠正)。
    v1_strategic_thesis: str | None = None
    #: P2.1:用户无法回答时 AI 给出的候选方向(最多 3 个)。
    v1_candidate_directions: list[dict] | None = None
    #: P2.1:用户选择的候选方向键。
    v1_selected_direction: str | None = None
    #: P2.2:当前**画布默认可见**的分析维度键(内部十维 ≠ 十个待回答问题)。
    v1_visible_analysis_keys: list[str] = Field(default_factory=list)
    #: P2.2:当前隐藏的分析维度数(通过“其余维度(N)”展开)。
    v1_hidden_analysis_count: int = 0
    #: P2.2:真正需要用户回答的问题数(0 或 1),**不**统计内部分析维度。
    v1_actual_pending_question_count: int = 0
    #: P2.2:十维 + 四战略的分析维度投影(key / title / visible / isFocus / requiresResponse)。
    v1_dimensions: list[dict] = Field(default_factory=list)
    #: P2.3:显式下一步动作(如 `continue_strategy`)。非终态阶段不允许“无下一步”。
    v1_next_action: str | None = None
    #: P2.3:是否仍可切换候选起点(只在 goal_reframe)。
    v1_can_reselect_direction: bool = False
    #: P5:该空间是否允许导出决策审计记录(`AGENT_AUDIT_EXPORT`)。前端据此显示/隐藏入口。
    v1_audit_export_enabled: bool = False
    #: 阶段 11:时间架构里的日期是否已校准。False = 只有相对周,不伪造日历日期。
    dates_calibrated: bool = False
    input_version: str | None = None
    strategy_proposal_id: uuid.UUID | None = None
    explored_at: datetime | None = None
    last_evaluated_at: datetime | None = None
    nodes: list[ReasoningNodeView] = Field(default_factory=list)
    links: list[ReasoningLinkView] = Field(default_factory=list)
    #: 最近一次自动探索失败的可读原因。**失败不留半成品地图。**
    error: str | None = None


class V01FeedbackRequest(ApiModel):
    """规划智能体 V0.1 的一条执行反馈。`outcome` 是闭集。"""

    node_id: uuid.UUID
    outcome: Literal["done", "partial", "missed", "delayed"]


class UpdateReasoningNodeRequest(ApiModel):
    """用户对地图节点的编辑。**只改标题与用户原文** —— 其余字段由 Agent 维护。

    标题一旦被用户改过就锁定(`title_locked`),之后 Agent 的任何一轮都不再覆盖它。
    """

    title: str | None = Field(default=None, min_length=1, max_length=200)
    user_description: str | None = Field(default=None, max_length=4000)
    #: 用户动作:暂缓 / 标记完成等。只改推理地图的节点状态,不碰业务计划。
    status: str | None = Field(default=None, max_length=16)


class AgentTurnResponse(ApiModel):
    """一次显式 Agent turn 的结果。**所有正式计划写入仍走 proposal + 用户确认。**"""

    reasoning: GoalReasoningView
    #: 这一轮新增/更新的助手消息。自动探索可以不产生聊天消息。
    message: MessageView | None = None
    #: 这一轮提出的主要问题(最多 3 个,响应里给最优先的一个)。
    question: QuestionView | None = None
    replayed: bool = False
    degraded: bool = False
    degraded_reason: str | None = None
    retryable: bool = False
    #: 这一轮是否真的改动了地图。前端据此决定要不要重画。
    changed: bool = False
    #: 战略确认提案没通过校验时的逐条原因。**必须显示出来。**
    proposal_errors: list[ActionError] = Field(default_factory=list)


__all__ = [
    "AgentTurnRequest",
    "AgentTurnResponse",
    "AgentTurnTrigger",
    "GoalReasoningView",
    "PendingIntakeView",
    "ReasoningLinkView",
    "ReasoningNodeView",
    "UpdateReasoningNodeRequest",
    "V01FeedbackRequest",
    "V01TimelineItemView",
]
