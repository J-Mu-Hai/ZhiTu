"""用户与成长空间。"""

from fastapi import APIRouter

router = APIRouter()


@router.get("/me")
async def read_current_user() -> dict:
    """获取当前用户档案。TODO: 接入数据库。"""
    raise NotImplementedError


@router.get("/me/growth-spaces")
async def list_growth_spaces() -> dict:
    """列出当前用户的成长空间。参见 docs/03-GROWTH-SPACE.md。"""
    raise NotImplementedError
