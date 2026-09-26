"""Alembic 运行环境。

三个关键点:

1. **连接串来自 backend.core.config**,不来自 alembic.ini。这样迁移和应用走的永远
   是同一个数据库,不会出现"迁移改了 PostgreSQL、应用连的是 SQLite"。
2. **用异步引擎跑迁移**,与应用的驱动完全一致(aiosqlite / asyncpg)。
   这样不必为了迁移再引入 psycopg,也避免"迁移用 A 驱动、应用用 B 驱动"的行为差异。
3. **render_as_batch=True**。SQLite 几乎不支持 ALTER TABLE(改列、加约束都要重建表),
   不开启 batch 模式时任何字段变更都会直接失败。
"""

from __future__ import annotations

import asyncio
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

# 允许从任何 cwd 运行:把仓库根放进 sys.path,保证 `backend` 可导入。
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import backend.db.models  # noqa: E402,F401  导入即注册全部模型
from backend.core.config import settings  # noqa: E402
from backend.db.base import Base  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    """与应用使用完全相同的连接串与驱动。"""
    return settings.resolved_database_url


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def _ensure_sqlite_dir(url: str) -> None:
    if not url.startswith("sqlite"):
        return
    raw_path = url.split("///", 1)[-1]
    if raw_path and raw_path != ":memory:":
        Path(raw_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


def _configure(connection=None, *, url: str = "") -> None:
    context.configure(
        connection=connection,
        url=url or None,
        target_metadata=target_metadata,
        render_as_batch=_is_sqlite(url),
        compare_type=True,
        compare_server_default=True,
        # 迁移脚本里用 backend.db 的模型做类型引用时需要的搜索路径。
        include_schemas=False,
    )


def run_migrations_offline() -> None:
    url = _database_url()
    _configure(url=url)
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection) -> None:
    _configure(connection=connection, url=_database_url())
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    url = _database_url()
    _ensure_sqlite_dir(url)

    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = url

    connectable = async_engine_from_config(
        section, prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    try:
        async with connectable.connect() as connection:
            await connection.run_sync(_do_run_migrations)
    finally:
        await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(_run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
