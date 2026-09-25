"""planner —— 把模糊目标拆解为计划节点与动作。

输入:用户的目标描述 + 成长空间当前状态(可选)。
输出:符合 shared/schemas/plan-node.schema.json 的计划节点树草案。

设计要点(待补全,见 docs/06-AGENT-DESIGN.md):
- 输出永远先作为 draft 呈现,经用户确认后才落库
- 需要考虑用户的历史中断模式,拆解颗粒度宁小勿大
- 提示词放 backend/agent/prompts/,不写在本模块
"""


class PlannerAgent:
    """占位实现。"""

    name = "planner"

    async def run(self, *, growth_space_id: str, user_input: str) -> dict:
        raise NotImplementedError
