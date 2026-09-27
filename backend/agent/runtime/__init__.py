"""Reasoner 的构造。

选哪条路走由 `AGENT_REASONER` 决定,但**有一条兜底规则不受配置影响**:
没有可用的模型时,永远退回 `RuleFallbackReasoner`(它不编造计划),而不是
退回某个"看起来还行"的假实现。

## 三条路的优先级

```
AGENT_REASONER=rule         -> 规则兜底(不问模型)
AGENT_REASONER=script       -> **测试脚手架**:念一份写好的脚本(见 runtime/scripted.py)
AGENT_REASONER=direct       -> 直连 DeepSeek(绕开 SDK)
AGENT_REASONER=openjiuwen   -> openJiuwen;它用不了时**降级**,并留下一条日志
AGENT_REASONER=auto(默认)   -> 装了 openJiuwen 就用它,否则直连;没 key 则规则兜底
```

`script` 是这份清单里**唯一一个不是产品模式**的选项:它不连模型,念的是
`ZHITU_SCRIPTED_ACTIONS` 里那段 JSON。它的存在理由、以及为什么必须单独有一个
`ModelSource.SCRIPTED` 而不是借用 `direct_llm`,写在 `runtime/scripted.py` 的开头。
它排在 `llm_api_key` 那道判断**之前** —— 那段脚本不需要也没有密钥。剩下的四条路
一条都没变。

`auto` 之所以优先 openJiuwen:它是这个产品的既定技术路线,"没装就直连"是一条
**可用的退路**,而不是默认选择。

**降级必须留痕。** 指定了 `openjiuwen` 却走了直连时,日志里会有一条警告,而响应里的
`source` 是 `direct_llm` —— 界面上那个徽标因此会写"直连模型"。用户看到的和我们
实际做的必须一致:把直连说成"AI 规划 · openJiuwen"是撒谎,而这件事恰恰最容易在
一次"SDK 版本不兼容"里悄悄发生。
"""

from __future__ import annotations

import logging

from backend.agent.runtime.base import (
    BriefClaim,
    KnownConditions,
    PlanNodeView,
    Reasoner,
    ReasoningResult,
    TurnContext,
)
from backend.agent.runtime.direct_llm import DirectLLMReasoner
from backend.agent.runtime.openjiuwen_runtime import OpenJiuwenReasoner, available
from backend.agent.runtime.rule_fallback import RuleFallbackReasoner
from backend.agent.runtime.scripted import ScriptedReasoner
from backend.core.config import Settings

logger = logging.getLogger(__name__)

__all__ = [
    "BriefClaim",
    "DirectLLMReasoner",
    "KnownConditions",
    "OpenJiuwenReasoner",
    "PlanNodeView",
    "Reasoner",
    "ReasoningResult",
    "RuleFallbackReasoner",
    "ScriptedReasoner",
    "TurnContext",
    "build_reasoner",
]


def build_reasoner(settings: Settings) -> Reasoner:
    """按配置挑一个实现。"""
    choice = (settings.agent_reasoner or "auto").strip().lower()

    if choice == "rule":
        return RuleFallbackReasoner()

    if choice == "script":
        # **测试脚手架**(见 runtime/scripted.py)。配置不全时**直接抛错**,不是退回
        # 规则兜底:退回兜底的话,一个"忘了配脚本"的验收会以"模型什么都没提"的
        # 方式悄悄通过 —— 那是最贵的一种假绿。
        logger.warning(
            "AGENT_REASONER=script:正在念一份写死的脚本,没有模型参与。"
            "这**不是**产品模式,只应该出现在隔离栈的验收里。"
        )
        return ScriptedReasoner.from_env()

    if not settings.llm_api_key:
        # 没 key 时给规则兜底而不是让直连实现返回一个死胡同:规则兜底能问出
        # 真正缺的那几个条件(由 known.missing 确定性推出),用户说的话不白说,
        # 等模型恢复后接着排。它问的问题里不含任何计划内容。
        logger.info("未配置 LLM_API_KEY,使用规则兜底。")
        return RuleFallbackReasoner()

    if choice in ("auto", "openjiuwen"):
        if available():
            return OpenJiuwenReasoner(settings)
        if choice == "openjiuwen":
            logger.warning(
                "AGENT_REASONER=openjiuwen 但没找到 openjiuwen 包,本次改走直连;"
                "响应中的 source 会如实标注为 direct_llm。"
            )
        else:
            logger.info("没找到 openjiuwen 包,使用直连模型。")

    return DirectLLMReasoner(settings)
