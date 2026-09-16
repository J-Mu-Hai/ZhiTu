"""coach —— 日常对话、推进执行、给出反馈。

输入:当前成长空间、今日动作、近期执行记录、用户的即时消息。
输出:面向用户的回复,可能附带对计划动作的调整建议。

设计要点(待补全,见 docs/06-AGENT-DESIGN.md):
- 语气与节奏需要保持一致,不能每次对话人设漂移
- 调整建议同样要经用户确认,不静默改计划
- 需要读取长期记忆来保持连贯(backend/agent/memory/)
"""


class CoachAgent:
    """占位实现。"""

    name = "coach"

    async def run(self, *, conversation_id: str, user_input: str) -> dict:
        raise NotImplementedError
