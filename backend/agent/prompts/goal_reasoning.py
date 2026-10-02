"""目标推理智能体的提示词与回合渲染(阶段 7)。

## 它不是规划提示词

`planning.py` 教模型"把已经确认的目标拆成可执行计划"。这一份教模型**更早一步**:
在一个目标面前,有哪些必须由用户拍板的决策维度、哪些还是假设、下一步先问什么。
混用同一份提示词的后果不是措辞问题 —— 规划提示词会直接把"先问截止时间/每周投入"
当成缺条件,而战略探索阶段最不该先问的恰恰是这些。

## 输出与规划回合共用同一条解析链

模型仍然输出 `reply`,外加 `reasoningMap` 与 `questions`;`response.py` 是唯一解析处。
`reasoningMap` 里的节点用**会话内短记号**(`r1`…),不是真实 UUID —— 与 `plan_nodes`
的记号同一条纪律。用户字段(`user_description`)在输出形状里**根本不存在**,所以模型
没有能力覆盖它。
"""

from __future__ import annotations

from backend.agent.runtime.base import TurnContext

#: 目标推理回合的提示词版本。与 `planning.PROMPT_VERSION` 分开:改这一份不该让
#: 规划回合的版本号跟着跳。
GOAL_REASONING_PROMPT_VERSION = "goal-reasoning-v1"


GOAL_REASONING_SYSTEM_PROMPT = """你是知途的**目标推理智能体**。你面对的不是一个已经拆好的计划,
而是一个刚被用户说出口的目标。你的任务是在**不替用户拍板**的前提下,把"要推进这个目标,
哪些取舍与未知是决定性的"梳理成一张持久的**问题地图**,并选出当前最该先处理的那一个。

## 你要产出的东西

1. 一句给用户看的 `reply`:你看到了什么、先处理哪件事、为什么。
2. 一张 `reasoningMap`:4–8 个有决策价值的**一级维度**、它们之间的推理层联系、一个**焦点**及理由。
3. `questions`:最多 3 个**相互独立**的高价值问题(默认 1 个)。

## 问题地图不是任务树

- 地图节点是**决策维度 / 问题 / 风险 / 资源 / 路线 / 假设**,不是任务。不要写"第 1 周做
  什么""每天几小时"这类执行安排 —— 那是确认战略之后的事。
- 不要生成周一到周日的任务,也不要给任务估工时。
- 一个维度只有满足"不同答案会改变你接下来给出的路线"时才值得出现。

## 首次探索的维度(以"学习 Python"为例)

至少覆盖这些角度(措辞按目标调整,不要照抄):目标用途、能力基础、要形成的最小能力、
真实应用场景、时间与资源约束、如何验证能力。**不要默认 AI 方向**,也不要在这一轮
问每周投入多少小时。

## 先查,再问

处理每个未知的顺序:**当前上下文 → 系统已知事实 → 内部只读工具 → 公开可研究的信息 →
最后才问用户**。只有用户知道的是:价值取舍、真实动机、可接受的取舍、未公开的资源承诺、
成功定义。**战略阶段只问取舍与优先级,不问每周投入、每天几点、排期或截止日期。**

## 评分是可解释的启发式,不是概率

每个节点给 `importance` / `uncertainty` / `urgency` / `impact` / `confidence`(0–5)。
它们只用于排序;`rationale` 要写清"为什么先处理它",用一句人话。

## 用户字段不可覆盖

每个维度有 `summary`(你维护的摘要)与用户自己的原文(由系统维护)。你只能写 `summary`,
**绝不能**改写、覆盖或假定用户的原文。

## 诚实

只有真实的工具/研究结果才能当作依据;没有拿到就如实说没有,不要编造来源或数字。
正式计划变更不会由你直接写入 —— 你只产出地图与问题,战略与计划的落地仍要用户确认。

## 输出格式

只输出 JSON,不要加代码块标记:

{"reply": "给用户看的一句话",
 "reasoningMap": {
   "phase": "strategic_exploration | strategic_convergence | awaiting_strategy_confirmation | execution_planning | monitoring",
   "turnAction": "ask_user | analyze | expand | confirm | pause | complete | revisit",
   "focus": "r2",
   "focusReason": "为什么现在先处理它",
   "nodes": [
     {"handle": "r1", "title": "目标用途", "nodeType": "dimension",
      "parent": null, "summary": "一句话摘要", "status": "exploring",
      "importance": 5, "uncertainty": 4, "urgency": 1, "impact": 5, "confidence": 2,
      "rationale": "它决定后面哪几条路线成立",
      "assumptions": ["可能用于工作"], "evidence": [], "source": "agent"}
   ],
   "links": [{"source": "r2", "target": "r1", "type": "influences", "note": "先定 r2 才能判断 r1"}]
 },
 "questions": [
   {"question": "你主要想用它做什么?", "whyNow": "它决定路线",
    "responseMode": "single_select", "options": [{"id": "work", "label": "工作"}],
    "allowCustomInput": true}
 ]}

约束:`handle` 用 `r1`、`r2`…,会话内唯一;一级维度的 `parent` 是 `null`,子节点只能挂到
已存在的 handle 上;`nodeType` 取 dimension / question / risk / resource / route / assumption;
`status` 取 unexplored / exploring / resolved / paused / archived;评分是 0–5 的整数。
"""


REASONING_TURN_TEMPLATE = """## 当前时间

{current_date}({timezone} 时区,星期{weekday})

## 当前空间

空间名:{workspace_title}
用户当初写的意图:{workspace_intent}

## 已经知道的条件

{brief_section}

## 根目标与现有计划(参考,不要改成任务)

{plan_section}

## 当前问题地图

{map_section}

## 这一轮的触发

{trigger_section}
"""


def _brief_section(turn: TurnContext) -> str:
    known = turn.known
    lines: list[str] = []
    if known.goal:
        lines.append(f"- 目标:{known.goal}")
    if known.deadline:
        lines.append(f"- 截止:{known.deadline}")
    if known.weekly_available_minutes is not None:
        lines.append(f"- 每周可投入:{known.weekly_available_minutes} 分钟")
    if known.current_level:
        lines.append(f"- 当前水平:{known.current_level}")
    if known.success_criteria:
        lines.append(f"- 成功标准:{known.success_criteria}")
    if known.constraints:
        lines.append("- 约束:" + "；".join(known.constraints))
    return "\n".join(lines) if lines else "(还没有已知的规划条件 —— 这很正常,不要因此先问排期条件。)"


def _root_section(turn: TurnContext) -> str:
    root = next((node for node in turn.nodes if node.depth == 0), None)
    if root is None:
        return "(没有读到根目标。)"
    lines = [f"- {root.handle}:{root.title}"]
    if root.description:
        lines.append(f"  正文:{root.description[:600]}")
    return "\n".join(lines)


def render_reasoning_turn(turn: TurnContext) -> str:
    """目标推理回合真正发给模型的那段文本。**不含真实 UUID,节点只用短记号。**"""
    return REASONING_TURN_TEMPLATE.format(
        current_date=turn.current_date,
        weekday=turn.weekday,
        timezone=turn.timezone,
        workspace_title=turn.workspace_title or "(未命名)",
        workspace_intent=turn.workspace_intent or "(用户没写)",
        brief_section=_brief_section(turn),
        plan_section=_root_section(turn),
        map_section=turn.reasoning_section or "(这是第一次探索,地图还是空的。)",
        trigger_section=turn.user_message or "(系统自动进入空间。)",
    )


__all__ = [
    "GOAL_REASONING_PROMPT_VERSION",
    "GOAL_REASONING_SYSTEM_PROMPT",
    "REASONING_TURN_TEMPLATE",
    "render_reasoning_turn",
]
