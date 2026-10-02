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
from datetime import datetime
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
    source: str
    version: int
    updated_at: datetime


class ReasoningLinkView(ApiModel):
    id: uuid.UUID
    source_handle: str
    target_handle: str
    link_type: str
    note: str | None = None


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
    input_version: str | None = None
    strategy_proposal_id: uuid.UUID | None = None
    explored_at: datetime | None = None
    last_evaluated_at: datetime | None = None
    nodes: list[ReasoningNodeView] = Field(default_factory=list)
    links: list[ReasoningLinkView] = Field(default_factory=list)
    #: 最近一次自动探索失败的可读原因。**失败不留半成品地图。**
    error: str | None = None


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
    "ReasoningLinkView",
    "ReasoningNodeView",
    "UpdateReasoningNodeRequest",
]
