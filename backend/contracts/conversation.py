"""对话与规划简报的接口契约。

## 响应里为什么必须带 `source` / `degraded`

这是产品对用户的一句承诺能不能成立的问题。原来的实现在模型失败时静默回退到关键词
规则,回复看起来和真模型一模一样,用户没有任何办法分辨。所以这三个字段不是调试信息,
是**界面必须显示出来的东西**:徽标写"AI 规划"还是"本地规则 · 模型不可用",
取决于它们。

`degradedReason` 用闭集枚举(见 db/models/enums.py 的 DegradedReason),
前端可以按值分支显示不同文案,而不是去匹配一段会变的中文。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import Field

from backend.contracts.common import ApiModel
from backend.contracts.plan import BriefView
from backend.contracts.proposal import ActionError, ProposalView

#: 单条消息的长度上限。与 services/conversation_service.py 的常量对齐 ——
#: 两处不一致的话,契约层先放行、服务层再拒绝,用户看到的是一句含糊的 400。
MAX_MESSAGE_CHARS = 4000


class SendMessageRequest(ApiModel):
    content: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)

    #: 客户端生成的 id。重试时带上同一个值,服务端就不会重复落库。
    #: 不强制要求 —— 不带的代价只是"网络超时后重发可能产生两条",而不是出错。
    client_message_id: str | None = Field(default=None, max_length=64)

    #: 用户此刻在界面上看着哪个节点。用于"把这个阶段展开说说"这类指代消解。
    context_node_id: uuid.UUID | None = None
    #: 当前视图名(path / timeline / tasks / today)。同样是为了消解指代。
    current_view: str | None = Field(default=None, max_length=40)

    #: 用户此刻在**哪个层级**里(画布显示的那一层子空间的根节点)。
    #:
    #: 与 `context_node_id` 是两件事,别合成一个:那个是"我在聊哪个节点",
    #: 这个是"我在哪个范围里聊"。范围决定 AI 能改哪些节点 —— 范围外的默认只读,
    #: 越界的变更会被服务端拒绝(见 services/proposal_validation.py 的 OUT_OF_SCOPE)。
    #:
    #: 不传表示"整个空间",也就是不缩小范围。
    scope_root_id: uuid.UUID | None = None


class ResearchCitationView(ApiModel):
    """一条**服务端验证过**的公开来源。

    它不是模型写的。每个字段都来自真实 provider 返回或服务端对 URL 的解析
    (`sourceId` / `domain`),所以模型无法凭空编出一条看起来像真的引用。
    """

    #: 服务端从 URL 派生的稳定标识。同一来源在不同轮次里 id 相同。
    source_id: str
    title: str
    url: str
    domain: str
    #: 提供方返回的摘录。可能为空(它不是判断来源真伪的必要条件)。
    excerpt: str | None = None
    #: 发布日期。当前 provider 的 basic 深度不返回,故通常为 None。
    published_at: str | None = None
    #: 服务端抓到这条来源的时刻(ISO 字符串)。
    accessed_at: str
    provider: Literal["tavily"] = "tavily"


class ResearchView(ApiModel):
    """一次助手回复所依据的公开研究。

    ## 为什么单独一层,而不是几句文字

    引用必须可点、可核对。把它写成模型的一段转述,用户无法区分"它真的查到了"
    与"它编了个像来源的句子"。这里每一项都经过服务端工具执行,模型改不了。

    ## status 为什么是闭集

    它回答"这一轮公开研究到底发生了什么",界面据此决定显示引用列表还是如实的
    失败说明。`null` 只为兼容更早的历史行,不是一种"结果"。
    """

    status: (
        Literal["success", "cached", "unavailable", "blocked", "limited", "timeout", "failed"]
        | None
    ) = None
    #: 本轮是否**尝试过**公开研究(成功、缓存、限额、隐私拦截、失败都算)。
    #: 为假时下面必然什么都没有。
    consulted: bool = False
    #: 只有 `success` / `cached` 才非空。**不含搜索关键词。**
    citations: list[ResearchCitationView] = Field(default_factory=list)


class MessageView(ApiModel):
    id: uuid.UUID
    role: str
    content: str
    seq: int
    created_at: datetime
    context_node_id: uuid.UUID | None = None
    proposal_id: uuid.UUID | None = None

    #: 这条回复是谁生成的。历史消息也要带 —— 用户往上翻的时候,
    #: 应该能看出哪几句是模型不可用时留下的。
    model_source: str | None = None
    degraded: bool = False
    degraded_reason: str | None = None

    #: 这条回复依据的公开来源。None = 这一轮没查过公开研究。
    research: ResearchView | None = None


class SendMessageResponse(ApiModel):
    user_message: MessageView
    assistant_message: MessageView

    #: reply 与 assistant_message.content 是同一个值。重复给一份,是因为前端渲染
    #: 气泡用的是 messages 列表,而"这一轮说了什么"读 reply 更直接。
    reply: str

    source: str
    degraded: bool = False
    degraded_reason: str | None = None
    #: 这次失败重试有没有意义。界面据此决定重试按钮是显示还是灰掉 ——
    #: 让用户点一个注定失败的按钮,比不给他按钮更糟。
    retryable: bool = False

    prompt_version: str = ""
    model_name: str | None = None
    latency_ms: int | None = None

    #: 与 `assistant_message.research` 是同一份。重复给一份,是因为前端渲染当前
    #: 这一轮的来源时读它更直接 —— 与 `reply` 同样的理由。
    research: ResearchView | None = None

    brief: BriefView
    #: 这一轮真正被记下的字段。界面可以据此显示"已记下:每周 4 小时",
    #: 让用户知道系统确实听进去了 —— 否则他会怀疑自己白说了。
    changed_fields: list[str] = Field(default_factory=list)

    #: 这一轮模型提出的计划变更。**它还没有生效**,要等用户点确认。
    #:
    #: 为 None 表示这一轮模型没有提议任何变更(绝大多数轮次都是这样)。这与
    #: "提议了但没通过校验"是两回事,后者落在下面的 `proposal_errors` 里 ——
    #: 前端必须能分辨:前者什么都不用显示,后者要如实告诉用户
    #: "AI 这次提的变更我没能执行",而不是沉默。
    proposal: ProposalView | None = None
    proposal_errors: list[ActionError] = Field(default_factory=list)

    #: **模型回答期间用户改了输入。**
    #:
    #: 为真时这一段内容建立在一份已经过去的输入上:提案不会生成(那些动作逐条落在
    #: `proposal_errors` 里,码是 `INPUT_CHANGED`),分析记录也一出生就是过期的。
    #:
    #: 界面需要它来给出下一步 —— 只说"这次没有提案"的话,用户会以为模型什么都没想
    #: 出来,而真正该做的是点一下"根据最新内容重新分析"。
    input_changed: bool = False

    #: 这是一次重复提交,返回的是上次的结果,没有重新调用模型。
    replayed: bool = False


class ConversationView(ApiModel):
    """一个空间的对话。新空间没有对话时,`messages` 是空数组,不是 404。"""

    id: uuid.UUID | None = None
    messages: list[MessageView] = Field(default_factory=list)
    brief: BriefView = Field(default_factory=BriefView)

    #: 更早的消息没有随这次响应返回。**界面必须据此说明"上面还有"** ——
    #: 不说明的话,对话的第一条会是一句没头没尾的话,而用户会以为记录丢了。
    truncated: bool = False


__all__ = [
    "MAX_MESSAGE_CHARS",
    "BriefView",
    "ConversationView",
    "MessageView",
    "ResearchCitationView",
    "ResearchView",
    "SendMessageRequest",
    "SendMessageResponse",
]
