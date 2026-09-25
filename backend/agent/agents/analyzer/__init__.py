"""analyzer —— 复盘执行数据,识别模式并给出调整建议。

输入:某成长空间一段时间内的执行记录与日记。
输出:符合 shared/schemas/growth-state.schema.json 的成长状态快照。

设计要点(待补全,见 docs/06-AGENT-DESIGN.md):
- 结论必须能追溯到具体的执行记录,不要凭空推断
- signals 用于驱动前端的提醒,阈值需要可配置
- 通常在异步任务里跑,不阻塞用户请求
"""


class AnalyzerAgent:
    """占位实现。"""

    name = "analyzer"

    async def run(self, *, growth_space_id: str, window_days: int = 7) -> dict:
        raise NotImplementedError
