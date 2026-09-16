"""数据库连接与会话管理。

待补全:接入 Alembic 迁移、定义 Base 与通用 mixin,见 docs/05-DATA-MODEL.md。
"""

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from backend.core.config import settings

engine = create_async_engine(settings.database_url, echo=settings.app_env == "development")

SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖:提供一个请求级会话。"""
    async with SessionLocal() as session:
        yield session
