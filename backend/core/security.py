"""鉴权与加密。

待补全:确定认证方式(会话 / JWT),再实现以下函数。
"""

from fastapi import Header, HTTPException, status


async def get_current_user_id(authorization: str | None = Header(default=None)) -> str:
    """FastAPI 依赖:从请求中解析出当前用户 ID。

    当前为占位实现,任何请求都会拿到固定的开发用户。
    """
    if not authorization:
        # TODO: 换成真实的 401 逻辑
        return "dev_user"
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="认证尚未实现",
    )
