"""计划节点(plan-node)。契约见 shared/schemas/plan-node.schema.json。"""

from fastapi import APIRouter

router = APIRouter()


@router.get("/{growth_space_id}")
async def get_plan_tree(growth_space_id: str) -> dict:
    """读取某个成长空间的完整计划树。"""
    raise NotImplementedError


@router.post("/{growth_space_id}/nodes")
async def create_plan_node(growth_space_id: str) -> dict:
    """新建计划节点。通常由 planner agent 提议、用户确认后调用。"""
    raise NotImplementedError


@router.patch("/nodes/{node_id}")
async def update_plan_node(node_id: str) -> dict:
    """修改节点标题、意图、状态或排序。"""
    raise NotImplementedError
