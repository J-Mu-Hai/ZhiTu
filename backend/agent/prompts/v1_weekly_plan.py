"""V1 阶段三：针对一个已确认阶段生成可执行周任务。"""

from __future__ import annotations

from backend.agent.runtime.base import TurnContext

PROMPT_VERSION = "v1-weekly-plan-2"

V1_WEEKLY_PLAN_SYSTEM_PROMPT = """你是知途的周计划细化助手。只把已确认的一个阶段拆成 3 到 6 项本周任务。
每项必须具体、可做、可验收，且必须给出 action、content、output、acceptance、estimateMinutes 五个字段。
estimateMinutes 必须是 10 到 480 的整数分钟。你必须逐字读取输入中的“目标、成果、验收”：
- content 必须写出本阶段的具体知识点、材料或操作对象；例如阶段涉及 Python 基础时，写“for/while 循环、if/elif/else 分支、break/continue”，不能只写“学习 Python”。
- output 必须是阶段成果的一个可检查子成果；acceptance 必须能判断该子成果是否完成。
- 每项任务至少包含一个来自阶段资料的具体名词或成果要求。禁止“学习、练习、制作、记录、复盘”这类只有动作没有对象的模板任务。
不要生成日期、日程、节点 ID 或其它计划层级。reply 用一两句话说明这周推进重点。
严格输出 JSON，且 v1WeeklyPlan.tasks 中每项五字段不得为空。"""


def render_v1_weekly_plan_turn(turn: TurnContext) -> str:
    return f"""# 阶段三：生成本周任务明细

工作空间目标：{turn.workspace_intent or turn.workspace_title or '未填写'}

待拆解阶段资料：
{turn.reasoning_section}

请只输出该阶段本周要做的 3–6 项任务。"""

