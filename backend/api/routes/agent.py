"""Agent 对话入口。

本模块只负责传输:接收用户消息、调用 backend/agent/ 的编排层、把结果(含流式 token)
送回客户端。不要在这里写提示词或直接调用模型。
"""

from fastapi import APIRouter

router = APIRouter()


@router.post("/conversations")
async def create_conversation() -> dict:
    raise NotImplementedError


@router.get("/conversations/{conversation_id}")
async def get_conversation(conversation_id: str) -> dict:
    raise NotImplementedError


@router.post("/conversations/{conversation_id}/messages")
async def send_message(conversation_id: str) -> dict:
    """发送消息并返回 Agent 回复。TODO: 改为 SSE 流式返回。"""
    raise NotImplementedError


@router.get("/growth-state/{growth_space_id}")
async def get_growth_state(growth_space_id: str) -> dict:
    """返回成长状态快照。契约见 shared/schemas/growth-state.schema.json。"""
    raise NotImplementedError
