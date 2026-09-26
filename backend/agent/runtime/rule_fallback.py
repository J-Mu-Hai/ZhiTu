"""模型不可用时的兜底。

## 这个文件最重要的一条规矩:**不许编造计划内容**

之前的 `backend/agent/agents/main.py` 是这个样子:httpx 抛异常或 JSON 解析失败以后,
静默落到一套中文关键词规则上,凭"考研""英语"这类词凭空造出几个节点,回复的语气和
真模型几乎一样。用户完全没有线索知道刚才那段是规则产物。这是整个仓库里最危险的一处
"假装"——它不是没实现,是**实现了而且看起来像真的**。

所以这里的能力被砍到只剩三件事:

1. **问一个该问的问题** —— 而"该问什么"是由 `KnownConditions.missing` 算出来的,
   不是关键词匹配。这是确定性推导,不是创作。
2. **明确说不行** —— 条件齐了但模型不可用时,如实说"现在生成不了",并给出重试。
3. **执行用户已经说清楚的动作** —— 例如"把 X 标记完成"。阶段 3 还没有这类动作,
   留到这里是为了说明边界在哪:只有**精确锚定到已有节点**的显式指令才允许产生写入,
   而"帮我排个学 Python 的计划"永远不在其列。

第 3 条现在没有实现,是因为它需要先有节点的编辑能力(阶段 5)。**宁可空着,也不
用一句"好的,我来帮你规划"冒充。**
"""

from __future__ import annotations

import time
import uuid

from backend.agent.runtime.base import ReasoningResult, TurnContext
from backend.db.models.enums import DegradedReason, ModelSource

#: 缺哪项时问什么。措辞是产品文案,写在数据里而不是散在 if 分支中,
#: 改文案时不会碰到判断逻辑。
_QUESTIONS: dict[str, str] = {
    "deadline": "这件事你希望什么时候完成?给我一个具体日期就行(比如 2026-12-31)。",
    "weekly_available_minutes": "你每周大概能拿出多少时间?按小时说就行,比如「每周 6 小时」。",
    "current_level": "你现在大概是什么水平了?比如「完全没接触过」「学过基础语法」。",
}

#: 一次最多问两件。问满三个问题就不是对话,是问卷。
_MAX_QUESTIONS = 2


class RuleFallbackReasoner:
    """不连网的兜底。**它产出的任何一句话都不含计划内容。**"""

    async def reason(self, turn: TurnContext) -> ReasoningResult:
        started = time.monotonic()
        missing = turn.known.missing

        if missing:
            reply = self._ask(missing[: _MAX_QUESTIONS])
            degraded_reason = DegradedReason.NO_API_KEY
        else:
            reply = (
                "条件已经够了,但现在连不上模型服务,生成不了计划。"
                "过一会儿点重试,你的话我都记着。"
            )
            degraded_reason = DegradedReason.MODEL_UNAVAILABLE

        return ReasoningResult(
            reply=reply,
            source=ModelSource.RULE_FALLBACK,
            degraded=True,
            degraded_reason=degraded_reason,
            retryable=True,
            # 空。**这一行是这个文件存在的全部意义。**
            brief_claims=(),
            actions=(),
            request_id=uuid.uuid4().hex,
            prompt_version="rule-fallback-v1",
            model_name=None,
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    def _ask(self, fields: tuple[str, ...]) -> str:
        lines = ["模型服务现在连不上,我先按手头有的信息问一句:"]
        lines.append("")
        for field in fields:
            question = _QUESTIONS.get(field)
            if question:
                lines.append(question)
        return "\n".join(lines)
