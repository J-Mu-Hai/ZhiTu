"""规划智能体重构 V1:**粗时间架构 repair 窄契约**。

当粗时间架构缺字段(时间范围 / goal / deliverable / completionCriteria,或阶段数不对)时,
编排器不再直接失败,而是走一次这个窄契约回合:输入上一版内容 + 缺失清单,只补全字段。
仍不合格才进入 `failed_retryable`(见 `v1_service.generate_coarse_timeline`)。

输出与 `v1_timeline` 同形(`v1Timeline`),所以解析器复用 `parse_v1_timeline`。
"""

from __future__ import annotations

from backend.agent.runtime.base import TurnContext

V1_TIMELINE_REPAIR_PROMPT_VERSION = "v1-timeline-repair-v1"


V1_TIMELINE_REPAIR_SYSTEM_PROMPT = """你是知途规划智能体的**时间架构修补器**。

上一版粗时间架构缺少一些必需字段。你的唯一任务:**只补全缺失字段**,不要重写已有内容、
不要改变阶段顺序或主题。输出与正常粗时间架构完全相同的 JSON。

## 输出格式(严格)

只输出一个 JSON 对象:`reply` 与 `v1Timeline`:

```json
{
  "reply": "一句话说明补全了什么。",
  "v1Timeline": {
    "summary": "整体时间架构一句话",
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
        "dependsOn": ""
      }
    ]
  }
}
```

## 硬规则

- 每个阶段**必须**有 `title`、`goal`、`deliverable`、`completionCriteria`;
- **必须有时间范围**:没有截止日期时只能用相对周 `startWeek`/`endWeek`(从 1 开始,
  不得为 null);有确定日期时可用 `startDate`/`endDate`(YYYY-MM-DD);
- 阶段数仍是 **3–6 个**;只修不换主题;
- 字段名逐字照上面写(`title` / `deliverable` / `completionCriteria` / `startWeek` / `endWeek`),
  不要用 `name` / `output` / `note` / `start` / `end`;
- 不输出任务、周计划、日计划或正式排期。
"""


V1_TIMELINE_REPAIR_TURN_TEMPLATE = """## 待修补的粗时间架构
{repair_section}

只输出一个 JSON 对象:`reply` 与 `v1Timeline`(把缺失字段补齐的完整版本)。"""


def render_v1_timeline_repair_turn(turn: TurnContext) -> str:
    return V1_TIMELINE_REPAIR_TURN_TEMPLATE.format(
        repair_section=turn.reasoning_section or "(没有可修补的内容)",
    )


__all__ = [
    "V1_TIMELINE_REPAIR_PROMPT_VERSION",
    "V1_TIMELINE_REPAIR_SYSTEM_PROMPT",
    "render_v1_timeline_repair_turn",
]
