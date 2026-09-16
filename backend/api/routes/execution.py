"""执行记录:完成、跳过、计时、体感。

这些数据是 analyzer 的主要输入,写入时不要丢失原始上下文。
"""

from fastapi import APIRouter

router = APIRouter()


@router.post("/actions/{action_id}/complete")
async def complete_action(action_id: str) -> dict:
    raise NotImplementedError


@router.post("/actions/{action_id}/skip")
async def skip_action(action_id: str) -> dict:
    """跳过时记录原因 —— 原因比结果更有分析价值。"""
    raise NotImplementedError


@router.post("/actions/{action_id}/sessions")
async def record_session(action_id: str) -> dict:
    """记录一次实际的执行时段(开始/结束/时长)。"""
    raise NotImplementedError


@router.get("/stats")
async def get_execution_stats() -> dict:
    """执行统计,用于成长状态的指标计算。"""
    raise NotImplementedError
