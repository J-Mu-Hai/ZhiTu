"""计划动作(plan-action)。契约见 shared/schemas/plan-action.schema.json。"""

from fastapi import APIRouter

router = APIRouter()


@router.get("/today")
async def list_today_actions() -> dict:
    """今日待执行的动作,供「今天」页使用。"""
    raise NotImplementedError


@router.post("")
async def create_action() -> dict:
    raise NotImplementedError


@router.patch("/{action_id}")
async def update_action(action_id: str) -> dict:
    """修改动作内容、时间或状态。"""
    raise NotImplementedError


@router.delete("/{action_id}")
async def delete_action(action_id: str) -> dict:
    raise NotImplementedError
