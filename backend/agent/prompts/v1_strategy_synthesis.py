"""规划智能体重构 V1:**战略合成回合**的窄契约提示词。

## 为什么单开一个回合

通用 `v1_strategy` 回合的自由度高(判断/维度/问题/候选方向/战略混在一起),真实模型
(DeepSeek-chat 实测)在“可以给战略”时会把白话写进 `keyDimensions`/`candidateDirections`,
而**始终不产出** `main_line`/`parallel_line`/`defer_or_avoid`/`risk_control` 四个结构字段,
导致战略永远合不出来、闭环停在 `problem_structure`。

这个回合把输出面收窄到**只有四条结构 + 一句取舍**:模型没有别的地方可写,结构化合规率
显著提高。服务端仍然只在四条齐全时才接受(见 `response.parse_v1_strategy`)。

它不是计划、不是阶段、不是任务;不提问、不给候选方向、不排期。
"""

from __future__ import annotations

from backend.agent.runtime.base import TurnContext

#: 战略合成回合的提示词版本。
V1_STRATEGY_SYNTHESIS_PROMPT_VERSION = "v1-strategy-synthesis-v1"


V1_STRATEGY_SYNTHESIS_SYSTEM_PROMPT = """你是知途规划智能体的**战略合成器**。

现在目标定义已经由用户确认,问题结构也分析过了。你的唯一任务是:把已有分析收敛成
**四条战略结构 + 一句取舍**。不要分析、不要提问、不要给候选方向、不要拆任务或排期。

## 只输出这四项(四项都必须非空)

- `mainLine`(主线):最优先投入什么。一句话,具体到能据此取舍。
- `parallelLine`(并行线):哪些可以同时做,但不该挤占主线。
- `deferOrAvoid`(暂缓/放弃):当前**不值得**做什么。宁可写“暂不系统学语法”。
- `riskControl`(风险控制):在哪里设置检查点或备用路径,出现什么信号就调整。
- `tradeoff`(取舍,可空):这版战略放弃了什么、换来了什么。

## 输出格式(严格)

只输出一个 JSON 对象,不要输出任何多余文字:

```json
{
  "reply": "给用户看的两三句话:这版战略是什么、为什么这样取舍。",
  "v1Strategy": {
    "mainLine": "……",
    "parallelLine": "……",
    "deferOrAvoid": "……",
    "riskControl": "……",
    "tradeoff": "……"
  }
}
```

## 硬规则

- 四个字段缺一不可;任一为空都视为失败,整轮作废;
- 字段名只能是 `mainLine` / `parallelLine` / `deferOrAvoid` / `riskControl` / `tradeoff`;
- 不输出 `nodeUpdates`、`keyDimensions`、问题、候选方向、任务、日期或周/日计划;
- 判断要基于给定的目标定义与既有分析,不要凭空新增条件;
- 不要复述输入,不要写“还缺哪些信息”。
"""


V1_STRATEGY_SYNTHESIS_TURN_TEMPLATE = """## 已确认的条件与既有分析
{reasoning_section}

## 用户最近说过的话
{history_section}

只输出一个 JSON 对象:`reply` 与 `v1Strategy`(四条结构 + 取舍)。"""


def render_v1_strategy_synthesis_turn(turn: TurnContext) -> str:
    """渲染战略合成回合。输入是 `reasoning_section`(目标定义 + 分析画布)。"""
    history = "\n".join(f"- {role}: {text}" for role, text in turn.history) or "(暂无)"
    return V1_STRATEGY_SYNTHESIS_TURN_TEMPLATE.format(
        reasoning_section=turn.reasoning_section or "(暂无)",
        history_section=history,
    )


__all__ = [
    "V1_STRATEGY_SYNTHESIS_PROMPT_VERSION",
    "V1_STRATEGY_SYNTHESIS_SYSTEM_PROMPT",
    "render_v1_strategy_synthesis_turn",
]
