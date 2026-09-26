"""当前用户的档案。

只有 `/me` —— 没有 `/users/{id}`,也没有用户列表。这个产品里没有任何界面需要看别人,
少一个路由就少一处"忘了加鉴权"的机会。

`GET /me/growth-spaces` 已删除:它和 `/api/workspaces` 是同一件事的两种说法,
而"一个状态多个视图"的前提是**只有一处真相**。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.dependencies.auth import AuthContext, get_auth_context
from backend.contracts.auth import UpdateProfileRequest, UserProfile
from backend.db.session import get_db
from backend.services import auth_service

router = APIRouter()

#: PATCH 允许改的字段。**白名单,不是黑名单** —— 黑名单的写法是"除了这些都能改",
#: 而 `users` 表里有 password_hash / token_version / is_active,漏掉任何一个都是事故。
#: 用白名单,新增列不会自动变成可改字段。
_EDITABLE_FIELDS = frozenset(
    {"display_name", "timezone", "school", "major", "year", "rank", "target_year", "target_goal", "bio"}
)


@router.get("/me", response_model=UserProfile, summary="当前用户档案")
async def read_current_user(ctx: AuthContext = Depends(get_auth_context)) -> UserProfile:
    return UserProfile.model_validate(ctx.account)


@router.patch("/me", response_model=UserProfile, summary="修改档案")
async def update_current_user(
    payload: UpdateProfileRequest,
    ctx: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
) -> UserProfile:
    """只改传了的字段。

    用 `model_fields_set` 而不是 `model_dump(exclude_none=True)` 区分"没传"和"传了 null":
    前者不动那个字段,后者把它清空。没有这个区分,PATCH 就没法表达"把简介删掉" ——
    那会变成用户怎么点都清不掉的一段文字。
    """
    changes = {
        field: getattr(payload, field)
        for field in payload.model_fields_set
        if field in _EDITABLE_FIELDS
    }
    if changes:
        await auth_service.update_profile(db, ctx.account, changes)
    return UserProfile.model_validate(ctx.account)
