"""引擎与会话工厂。实际用哪个数据库由 DATABASE_URL 决定(见 core/config.py)。

这是引擎唯一的定义处;backend/core/database.py 只是转发,以兼容老 import。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.core.config import settings

_SQLITE_PREFIX = "sqlite+aiosqlite:///"


def is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def _ensure_sqlite_parent_dir(url: str) -> None:
    """SQLite 不会自动创建目录,缺失时报的是 "unable to open database file",很难排查。"""
    if not url.startswith(_SQLITE_PREFIX):
        return
    raw_path = url[len(_SQLITE_PREFIX) :]
    if not raw_path or raw_path == ":memory:":
        return
    Path(raw_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


DATABASE_URL = settings.resolved_database_url
_ensure_sqlite_parent_dir(DATABASE_URL)

engine = create_async_engine(
    DATABASE_URL,
    echo=settings.db_echo,
    # 连接池健康检查对 PostgreSQL 有意义;SQLite 是本地文件,没必要。
    pool_pre_ping=not is_sqlite(DATABASE_URL),
)

SessionLocal = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


if is_sqlite(DATABASE_URL):

    @event.listens_for(engine.sync_engine, "connect")
    def _apply_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        # SQLite 默认**关闭**外键约束。不开的话所有 ForeignKey 与 ON DELETE CASCADE
        # 都只是装饰,数据会静默地不一致。
        cursor.execute("PRAGMA foreign_keys=ON")
        # 允许读写并发,避免 "database is locked"。
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖:请求级会话。

    这里绝不捕获异常。数据库不可用时必须向上抛,由路由层转成明确的 503 ——
    悄悄降级到内存会让用户以为已经保存成功,那是本产品最不能犯的错。
    """
    async with SessionLocal() as session:
        yield session
