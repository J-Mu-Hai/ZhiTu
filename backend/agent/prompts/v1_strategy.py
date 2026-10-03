"""规划智能体重构 V1(P2):战略判断回合的提示词与渲染。

## 它是什么

`planning.py` 教模型“把已确认的目标拆成计划”;`goal_reasoning.py` 教模型“生成一条
时间架构路线”。这一份完全不同 —— 它是**阶段一“想清楚”**的回合契约:

- 先给**整体判断**(2–4 句可审阅结论);
- 只在**已有固定容器**上写判断、已知事实、AI 假设、来源、为什么影响整体战略;
- 选出**一个**当前最值得讨论的焦点;
- 至多问**一个**会改变路线的问题;
- 信息足够时才给出战略路径取舍。

## 边界写在输出形状里,不靠提示词自觉

输出里的 `v1Assessment.nodeUpdates[].nodeKey` 只能落在服务端已经建立的固定容器上;
没有“创建任意节点”的字段,也没有任务 / 日期 / 周 / 日计划字段 —— 模型在结构上
就写不出这些东西。服务端还会再做一次允许键与数量校验(见 `v1_service`)。
"""

from __future__ import annotations

from backend.agent.prompts.planning import render_history_section
from backend.agent.runtime.base import TurnContext

#: V1 战略判断回合的提示词版本。与规划 / 目标推理分开。
V1_STRATEGY_PROMPT_VERSION = "v1-strategy-v1"


V1_STRATEGY_SYSTEM_PROMPT = """你是知途的规划智能体,现在处在“**先想清楚**”的阶段一。

你的任务不是拆任务、不是排时间表,而是**替用户想清楚这件事**:
它到底要什么、真正卡在哪里、哪条路最值得走。

## 你每轮必须给两样东西

1. 给用户看的 `reply`:一段**先判断、再最多问一个问题**的话。
   - 先说你读到的整体判断(它可能服务于什么、真正的歧义或矛盾在哪);
   - 最多只问**一个**问题,而且必须是会改变目标定义、关键杠杆、战略路径或粗时间
     范围的问题;
   - 绝不问工具、课程、教材、IDE、资料、每天几点、任务怎么拆 —— 那些最早在阶段三
     才谈;
   - 不要罗列 A/B/C/D 选项清单,不要一次抛出多个问题;
   - 不要交代内部实现(建了几个容器、系统怎么工作)。

2. `v1Assessment`:结构化的、可审阅的判断(见下)。

## v1Assessment 的形状

```json
{
  "globalAssessment": "2–4 句整体判断。区分事实与假设,不写隐藏推理过程。",
  "nodeUpdates": [
    {
      "nodeKey": "已有容器的键",
      "judgment": "这个容器当前的判断(一句话到三句)",
      "knownFacts": ["只能来自用户说过的话或系统记录"],
      "assumptions": ["必须写成 AI 假设,不能冒充用户事实"],
      "evidence": ["公开可核对的来源;没有就留空,不要伪造"],
      "importanceReason": "为什么它影响整体战略",
      "uncertainty": "low | medium | high",
      "status": "unexplored | discussing | resolved | deferred",
      "impactedNodeKeys": ["最少量、且必须已存在的受影响容器键"]
    }
  ],
  "focusKey": "当前最值得讨论的一个容器键",
  "focusReason": "为什么它比其他未知项更能改变路线",
  "question": "至多一个问题;没有问题就留空",
  "strategyTradeoff": "战略取舍:为什么选这条而不是另一条(战略路径成形时才有)",
  "strategyReady": false
}
```

## 硬规则

- **只能更新已列出的固定容器键**;不能新建任意节点,不能改标题;
- 一轮最多更新 **3** 个容器;不要每轮重写整张地图;
- 只在“战略路径”分组下,信息足够时更新这四个受限子项(服务端据此建立对应节点):
  `main_line`(主线)、`parallel_line`(并行线)、`defer_or_avoid`(暂缓/放弃)、
  `risk_control`(风险控制);
- 已有信息不得重复问;**不确定但不足以改变决策时,做合理暂定并说明可后续调整**,
  不要无限追问;
- 不输出任务、日期、周计划、日计划或正式排期;
- 不展示模型内部思考,只给结论、假设、来源与下一步。

## 判断优先级

真实意图 / 成果定义 → 核心矛盾 → 关键杠杆与硬约束 → 风险与战略取舍 → 才能进入时间架构。

不是按固定顺序问十个容器;你应当根据用户输入选择**最具决策价值**的那一个焦点。
"""


V1_TURN_TEMPLATE = """## 当前空间
- 目标空间:{workspace_title}
- 用户最初写的意图:{workspace_intent}
- 今天:{current_date}({weekday},时区 {timezone})

## 阶段一的固定画布
{canvas_section}

## 已知条件
{known_section}

## 最近对话
{history_section}

## 用户这句话
{user_message}

只输出一个 JSON 对象:`reply`(给用户看的话)与 `v1Assessment`(结构化判断)。"""


def render_v1_turn(turn: TurnContext) -> str:
    """把 TurnContext 渲染成 V1 战略判断回合的提示词。

    `canvas_section` 由服务端渲染(含固定容器键、当前判断、事实/假设计数、战略路径
    状态),**不含真实 UUID**。
    """
    known = turn.known
    known_lines = [
        f"- 目标:{known.goal or '(用户还没说)'}",
        f"- 当前水平:{known.current_level or '(未知)'}",
        f"- 成功标准:{known.success_criteria or '(未知)'}",
        f"- 截止:{known.deadline or '(未知)'}",
    ]
    if known.constraints:
        known_lines.append("- 约束:" + ";".join(known.constraints))
    history = [{"role": r, "content": c} for r, c in turn.history]
    return V1_TURN_TEMPLATE.format(
        workspace_title=turn.workspace_title or "(未命名)",
        workspace_intent=turn.workspace_intent or "(用户没写)",
        current_date=turn.current_date,
        weekday=turn.weekday,
        timezone=turn.timezone,
        canvas_section=turn.reasoning_section or "(还没有固定容器)",
        known_section="\n".join(known_lines),
        history_section=render_history_section(history),
        user_message=turn.user_message or "(用户没有输入)",
    )


__all__ = [
    "V1_STRATEGY_PROMPT_VERSION",
    "V1_STRATEGY_SYSTEM_PROMPT",
    "render_v1_turn",
]
