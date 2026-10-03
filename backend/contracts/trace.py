"""Agent 运行轨迹的接口契约(阶段 9)。

## 为什么单独一份契约

轨迹**不是**推理状态本身,也不是对话消息。它是一份**只读诊断投影**:只讲
"服务端真实执行到哪一步、用了多久、工具结果如何、终态是什么"。把它塞进
`AgentTurnResponse` 会让每次对话都背着诊断字段,也会诱使前端把它当业务状态用。

## 红线

契约里**没有** prompt、模型原始输出、用户原文、密钥、Cookie、连接串或隐藏思维链
字段。`safe_summary` 的值只来自 `agent_trace_service.TERMINAL_SUMMARIES` 的闭集,
工具摘要只含工具名、状态、条数 —— 即使底层 ORM 里有更多字段,这里也没有出口。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import Field

from backend.contracts.common import ApiModel


class AgentTraceToolView(ApiModel):
    """一次只读工具调用的**脱敏**摘要。"""

    tool_name: str
    #: `ok` / `error` / `rejected`。
    status: str
    duration_ms: int | None = None
    #: 中文一句话,只含工具名、状态与条数/域名等必要计数。
    summary: str


class AgentTraceTurnView(ApiModel):
    """最近一次 Agent turn 的运行轨迹。**按时间倒序返回。**"""

    id: uuid.UUID
    #: 只给排障用的短 ID,不是可用来跨空间访问的钥匙。
    short_id: str
    trigger: str
    trigger_label: str
    #: `AgentTraceStep` 的字面量,或历史行的 `unavailable`。
    current_step: str
    step_label: str
    #: `running` / `completed` / `failed` / `timed_out` / `cancelled` / `unavailable`。
    status: str
    terminal: bool
    started_at: datetime | None = None
    finished_at: datetime | None = None
    #: 服务端时间算出的耗时(毫秒)。前端不自己算。
    duration_ms: int | None = None
    last_progress_at: datetime | None = None
    #: 只在 `waiting_model` 时有值:距上次心跳多少秒。
    waiting_seconds: int | None = None
    #: 等待是否已经超过阈值 —— 界面据此说"仍在等待模型响应"。
    waiting_too_long: bool = False
    attempt: int = 1
    terminal_code: str | None = None
    #: **脱敏的**可读终态原因。历史行可能为空。
    safe_summary: str | None = None
    tools: list[AgentTraceToolView] = Field(default_factory=list)
    #: 只对"失败且本轮没有写入业务更改"的 turn 为真。重试仍走既有幂等键/版本检查。
    retryable: bool = False


class AgentTraceView(ApiModel):
    """诊断抽屉一次拉取的完整内容。"""

    workspace_id: uuid.UUID
    workspace_short_id: str
    #: 读取接口可用性(配置开关 + 本地开发)。关闭时路由本身会拒绝,这里仍如实返回。
    enabled: bool
    generated_at: datetime
    limit: int
    turns: list[AgentTraceTurnView] = Field(default_factory=list)


__all__ = [
    "AgentTraceToolView",
    "AgentTraceTurnView",
    "AgentTraceView",
]
