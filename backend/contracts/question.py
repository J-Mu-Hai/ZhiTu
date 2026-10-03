"""问题节点的接口契约。

## 为什么 `AnswerQuestionResponse` 里带着一整个 `SendMessageResponse`

用户答完一个问题,后端会**把它当成一轮对话**跑:合成一条用户消息、调模型、把模型
这一轮提的变更按老规矩落成待确认提案。前端拿到的应该是和发消息**同一种**结果 ——
否则它就得为"回答问题"这条路径写第二套读取逻辑(消息、简报、提案、降级、输入变化),
而那套逻辑一定会在某个不起眼的字段上漏掉一处。

所以 `turn` 就是 `SendMessageResponse`。`replayed=true` 时 `turn` 为 `None` ——
那是重复提交,后续处理没有也不应该再跑一遍。

## 为什么 `answer` 用 id 列表而不是标签

见 `agent/runtime/base.py::QuestionOptionDraft`。这里补一条:**选项是加速器,不是限制**;
`allow_custom_input` 允许用户在选项之外补一句话,所以答案同时带 `selectedOptionIds`
与 `customInput`。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import Field

from backend.contracts.common import ApiModel
from backend.contracts.conversation import SendMessageResponse

#: 一轮最多几个问题。与 agent/runtime/response.py 的 MAX_QUESTIONS 对齐。
MAX_QUESTIONS_PER_TURN = 2
#: 一个问题最多几个选项。阶段 10 收到 3 —— 选项是加速器,不是问卷。
MAX_QUESTION_OPTIONS = 3
#: 自由输入 / 补充说明的长度上限。
MAX_CUSTOM_INPUT_CHARS = 2000


class QuestionOption(ApiModel):
    id: str = Field(min_length=1, max_length=40)
    label: str = Field(min_length=1, max_length=120)
    #: AI 的推荐项。前端据此明确标“推荐”。
    recommended: bool = False


class QuestionAnswer(ApiModel):
    """用户对一个问题给出的答案。

    `selected_option_ids` 里每个 id 都必须是这个问题的 `options` 里出现过的 ——
    服务端校验,不靠前端。`custom_input` 只有在 `allow_custom_input` 为真时才收。
    """

    selected_option_ids: list[str] = Field(default_factory=list)
    custom_input: str | None = Field(default=None, max_length=MAX_CUSTOM_INPUT_CHARS)


class QuestionView(ApiModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    #: 问题从哪个节点聊出来的。节点被归档后这里仍是原 id(问题可审计),但前端
    #: 在画布上找不到对应节点时不应再把它渲染成一个可跳转的引用。
    source_node_id: uuid.UUID | None = None
    source_message_id: uuid.UUID | None = None
    #: 这个问题在目标推理地图上对应哪个节点。可空 —— 不是每个问题都来自地图。
    #: 回答之后服务端靠它定位要重评的推理节点。
    reasoning_node_id: uuid.UUID | None = None
    question: str
    why_now: str
    #: 阶段 10:提问前的战略判断。**可审阅的结论,不是隐藏思维链。**
    #: 旧行 / 没有可信依据时为空字符串 —— 前端会显示“当前还不足以给出推荐”。
    analysis_summary: str = ""
    recommendation: str = ""
    decision_impact: str = ""
    #: 可选:哪些是假设、还需确认。
    confidence_note: str | None = None
    response_mode: str
    options: list[QuestionOption] = Field(default_factory=list)
    allow_custom_input: bool = False
    status: str
    answer: QuestionAnswer | None = None
    created_at: datetime
    updated_at: datetime
    answered_at: datetime | None = None


class QuestionListResponse(ApiModel):
    """这个空间的问题。**默认只返回还没结束的那些**(pending / answered / investigating)。

    `include_decided=true` 时把 resolved / archived 也带上(审计与历史)。
    """

    questions: list[QuestionView] = Field(default_factory=list)
    #: 被截断了吗?当前实现一次取全,这个字段为 `false` 时读的是全部。
    #: 留着是因为"只列了最近 N 条"和"一共就这些"必须是两件事(同 `MessagePage`)。
    truncated: bool = False


class AnswerQuestionRequest(ApiModel):
    """回答一个问题。

    `client_answer_id` 是幂等键:双击或网络重试带同一个值时,后端**不会**再跑一遍
    后续处理,而是原样返回上次的结果。它必填,理由与提案确认的幂等键相同 ——
    让服务端在缺失时补一个随机值,等于把这个保证悄悄关掉。
    """

    selected_option_ids: list[str] = Field(default_factory=list)
    custom_input: str | None = Field(default=None, max_length=MAX_CUSTOM_INPUT_CHARS)
    client_answer_id: str = Field(min_length=8, max_length=64)


class AnswerQuestionResponse(ApiModel):
    question: QuestionView
    #: 这一轮后续对话的结果(消息、提案、降级…)。重复提交时为 `None`。
    turn: SendMessageResponse | None = None
    replayed: bool = False


class QuestionActionRequest(ApiModel):
    """跳过 / 稍后回答的请求体。

    `client_action_id` 与回答的幂等键同义。`reason` 可选,只用于审计。
    """

    client_action_id: str = Field(min_length=8, max_length=64)
    reason: str | None = Field(default=None, max_length=500)


class QuestionActionResponse(ApiModel):
    question: QuestionView
    replayed: bool = False


__all__ = [
    "MAX_CUSTOM_INPUT_CHARS",
    "MAX_QUESTIONS_PER_TURN",
    "MAX_QUESTION_OPTIONS",
    "AnswerQuestionRequest",
    "AnswerQuestionResponse",
    "QuestionActionRequest",
    "QuestionActionResponse",
    "QuestionAnswer",
    "QuestionListResponse",
    "QuestionOption",
    "QuestionView",
]
