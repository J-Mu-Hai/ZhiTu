"""数据库可达性检查。

启动时若连不上数据库就直接拒绝启动(除非显式设置 ALLOW_DEGRADED_DB=1)。
理由:本产品有"确认计划"这类写操作,一个连不上库却照常运行的服务会让用户
以为计划已经保存 —— 这比服务启动失败严重得多。

## 第二项检查:库里的结构是不是这份代码要的那一份

"连得上"和"能用"是两件事。部署新代码时忘了跑 `alembic upgrade head`,服务会
**正常启动**(连接是通的),然后在第一个碰到新列的请求上炸 —— 报出来的是一个
`UndefinedColumnError`,和一个跟真实原因毫无关系的 500。更糟的是没有新列也能活的
那些路径:它们照常返回数据,只是少了一栏,而没有任何东西会说不。

所以启动时也检查 `alembic_version` 是不是代码里的 head。它和"连不上库"同一个
纪律:宁可拒绝启动,不要带病运行。
"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError

from backend.core.config import settings
from backend.db.session import DATABASE_URL, SessionLocal

logger = logging.getLogger(__name__)

#: backend/db/health.py -> backend -> <repo root>
_REPO_ROOT = Path(__file__).resolve().parents[2]

#: Alembic 自己的版本表名。不是可配置项 —— 迁移脚本里也用这个名字。
_VERSION_TABLE = "alembic_version"


def describe_target() -> str:
    """给日志用的数据库标识。隐藏密码。"""
    if "@" not in DATABASE_URL:
        return DATABASE_URL
    scheme, _, rest = DATABASE_URL.partition("://")
    credentials, _, host_part = rest.rpartition("@")
    user = credentials.split(":", 1)[0]
    return f"{scheme}://{user}:***@{host_part}"


async def check_db_reachable() -> tuple[bool, str | None]:
    """探活。返回 (是否可达, 失败原因)。"""
    try:
        async with SessionLocal() as session:
            await session.execute(text("SELECT 1"))
    except (SQLAlchemyError, OSError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    return True, None


async def assert_db_reachable() -> None:
    """启动期校验。失败时要么抛错终止启动,要么在日志里高调告警。"""
    ok, reason = await check_db_reachable()
    if ok:
        logger.info("数据库连接正常: %s", describe_target())
        return

    message = (
        f"无法连接数据库 {describe_target()} —— {reason}\n"
        "如果这是本地开发,确认 DATABASE_URL 留空(使用仓库内 SQLite)"
        "或指向一个正在运行的 PostgreSQL。"
    )
    if settings.allow_degraded_db:
        logger.error(
            "%s\nALLOW_DEGRADED_DB=1 已设置,服务继续启动,但所有读写都会失败。"
            "此开关不得用于生产。",
            message,
        )
        return
    raise RuntimeError(message)


def alembic_head() -> str:
    """代码里那一版迁移的 head。

    从 `backend/alembic.ini` 读,与应用跑迁移时用的是同一份配置 —— 换了脚本目录
    或分了新分支,这里跟着变,不会各说各话。
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(str(_REPO_ROOT / "backend" / "alembic.ini"))
    return ScriptDirectory.from_config(config).get_current_head()


async def check_schema_current() -> tuple[bool, str | None]:
    """库里的迁移版本是不是代码的 head。返回 (是否一致, 不一致的原因)。"""
    try:
        head = alembic_head()
    # 迁移脚本目录本身有问题(删了文件、加了分支、路径配错)时,这里要**如实报出来**
    # 而不是让整个服务带着一个没人看得懂的 traceback 启动失败。
    except Exception as exc:
        return False, f"读不出迁移脚本的 head:{type(exc).__name__}: {exc}"

    try:
        async with SessionLocal() as session:
            connection = await session.connection()
            # 用 inspector 判断表在不在,而不是去 catch "表不存在"的异常:SQLite 和
            # PostgreSQL 抛的异常类型不同,而这里**必须**能把"从没迁移过"和
            # "连不上"分开说 —— 两者的处理方式完全不同。
            has_table = await connection.run_sync(
                lambda sync_connection: inspect(sync_connection).has_table(_VERSION_TABLE)
            )
            if not has_table:
                return False, (
                    f"数据库里没有 {_VERSION_TABLE} 表 —— 它从来没有跑过迁移。"
                    "先执行 `alembic upgrade head`(在仓库根目录,或 "
                    "`alembic -c backend/alembic.ini upgrade head`)。"
                )
            # 表名是模块里的常量,不是外部输入 —— 这里没有注入面。
            result = await session.execute(text(f"SELECT version_num FROM {_VERSION_TABLE}"))
            current = [row[0] for row in result.all()]
    except SQLAlchemyError as exc:
        return False, f"读迁移版本失败:{type(exc).__name__}: {exc}"

    if not current:
        return False, f"{_VERSION_TABLE} 表是空的 —— 迁移没有真正执行过。"
    if head in current:
        return True, None
    return False, (
        f"数据库的迁移版本是 {', '.join(current)},而这份代码要的是 {head}。"
        "先执行 `alembic upgrade head` 再启动服务。"
    )


async def assert_schema_current() -> None:
    """启动期校验:库里的结构必须是这份代码要的那一份。"""
    ok, reason = await check_schema_current()
    if ok:
        logger.info("数据库结构已是最新(迁移 head)")
        return

    message = f"{reason}\n数据库:{describe_target()}"
    if settings.allow_degraded_db:
        logger.error(
            "%s\nALLOW_DEGRADED_DB=1 已设置,服务继续启动。此开关不得用于生产。",
            message,
        )
        return
    raise RuntimeError(message)
