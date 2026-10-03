"""规划智能体重构 V1(P3):粗时间架构回合的提示词与渲染。

它把**已确认的战略逻辑**投影成 3–6 个阶段的粗时间架构。只解决时间关系:阶段顺序、
相对周或已有日期、阶段目标、可交付成果、完成标准。**不产出任务、不排到具体某一天。**
"""

from __future__ import annotations

from backend.agent.prompts.planning import render_history_section
from backend.agent.runtime.base import TurnContext

V1_TIMELINE_PROMPT_VERSION = "v1-timeline-v1"


V1_TIMELINE_SYSTEM_PROMPT = """你是知途的规划智能体,现在处在阶段二「排出来」的**粗时间架构**。

用户已经确认了战略逻辑。现在把它投影成 3–6 个**有先后顺序的阶段**。这不是日历,
也不是任务清单:每个阶段只说清“这一段要达成什么、交出什么、怎么算过关”,以及相对
时间范围。

## 输出

只输出一个 JSON 对象:`reply`(给用户看的一段简短说明)与 `v1Timeline`。

`v1Timeline` 形状:

```json
{
  "summary": "整体时间架构的一句话概述",
  "phases": [
    {
      "title": "阶段名称",
      "goal": "这一阶段要达成什么",
      "deliverable": "可交付成果",
      "completionCriteria": "怎么算通过",
      "startWeek": 1,
      "endWeek": 2,
      "startDate": null,
      "endDate": null,
      "dependsOn": "前置阶段的标题(可空)"
    }
  ]
}
```

## 硬规则

- 阶段数 **3–6 个**,按先后顺序排列;
- 没有明确日期就用**相对周**(`startWeek`/`endWeek`,从 1 开始),不要编造日历日期;
  有确定截止时可用 `startDate`/`endDate`(YYYY-MM-DD);
- 每个阶段都必须有 `title`、`goal`、`deliverable`、`completionCriteria`;
- **不要**输出具体任务、周计划、日计划、每天几点做、课程名或工具名;
- **不要**把战略阶段写成“周一看课程、周二刷题”这种日历;
- 只给可审阅的结论,不展示隐藏推理过程。
"""


V1_TIMELINE_TURN_TEMPLATE = """## 当前空间
- 目标空间:{workspace_title}
- 用户最初的意图:{workspace_intent}
- 今天:{current_date}({weekday},时区 {timezone})

## 已确认的战略逻辑
{strategy_section}

## 最近对话
{history_section}

## 用户这句话
{user_message}

只输出一个 JSON 对象:`reply` 与 `v1Timeline`(3–6 个阶段)。"""


def render_v1_timeline_turn(turn: TurnContext) -> str:
    history = [{"role": r, "content": c} for r, c in turn.history]
    return V1_TIMELINE_TURN_TEMPLATE.format(
        workspace_title=turn.workspace_title or "(未命名)",
        workspace_intent=turn.workspace_intent or "(用户没写)",
        current_date=turn.current_date,
        weekday=turn.weekday,
        timezone=turn.timezone,
        strategy_section=turn.reasoning_section or "(没有可用的战略逻辑)",
        history_section=render_history_section(history),
        user_message=turn.user_message or "(用户没有输入)",
    )


__all__ = [
    "V1_TIMELINE_PROMPT_VERSION",
    "V1_TIMELINE_SYSTEM_PROMPT",
    "render_v1_timeline_turn",
]
