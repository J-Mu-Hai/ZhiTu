"""日记与复盘。"""

from fastapi import APIRouter

router = APIRouter()


@router.get("")
async def list_entries() -> dict:
    raise NotImplementedError


@router.post("")
async def create_entry() -> dict:
    raise NotImplementedError


@router.get("/{entry_id}")
async def read_entry(entry_id: str) -> dict:
    raise NotImplementedError


@router.patch("/{entry_id}")
async def update_entry(entry_id: str) -> dict:
    raise NotImplementedError
