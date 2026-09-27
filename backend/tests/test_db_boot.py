"""阶段 1 验收:数据库地基确实建起来了。

这些测试不是"跑一遍 create_all 看它不报错"——那种测试即使在架构完全错误时也会通过。
它们断言的是**引擎里真实存在的东西**(sqlite_master 与 PRAGMA),因为阶段的其余部分
全都建立在"约束真的在生效"这个前提上。
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models import User

EXPECTED_TABLES = {
    "auth_sessions",
    "availability_exceptions",
    "availability_rules",
    "conversations",
    "dependencies",
    "domain_events",
    "execution_records",
    "messages",
    # 画布上的关系与用户偏好。三张都是**加法**:`dependencies` 与 `plan_nodes` 一行
    # 都没改,排期那条链路不受影响。这张清单是"库里有哪些表"的唯一出处,新表忘了
    # 登记会让这条测试红 —— 那正是它该做的。
    # AI 做出的判断。**单独一张表,而且只增不改** —— 它既不是用户写的正文,也不是
    # 计划的一部分;把它塞进 `plan_nodes` 就等于让模型的一句猜测有了成为计划前提的
    # 路径(见 db/models/analysis.py)。
    "node_analyses",
    "node_positions",
    "node_relations",
    "plan_nodes",
    "plan_revisions",
    "planning_briefs",
    "proposal_decisions",
    "proposal_items",
    "proposals",
    # 站内提醒**只存用户的处置**(关掉/稍后),不存提醒内容本身 ——
    # 内容每次按事实重算,所以这里没有"提醒表",只有这一张状态表。
    "reminder_states",
    "schedule_applications",
    "scheduled_sessions",
    "scope_viewports",
    "user_capacity_profiles",
    "users",
    "workspaces",
}

# name -> WHERE 子句(归一化后比对)。同时传 postgresql_where 与 sqlite_where 的产物。
PARTIAL_UNIQUE_INDEXES = {
    # 每空间恰好一条活动主对话 —— 新空间是零行,不是一行空对话。
    "uq_conversations_primary": "kind = 'primary' and status = 'active'",
    # 双击确认不能重复写执行记录。
    "uq_execution_records_idempotency": "idempotency_key is not null",
    # 客户端重试同一条消息不能落两条。
    "uq_messages_client_message_id": "client_message_id is not null",
    # 每空间只有一个"已确认"的规划条件。
    "uq_planning_briefs_confirmed": "status = 'confirmed'",
    # 同一任务同一天同一序号只能有一场未取消的排期。
    "uq_scheduled_sessions_slot": "status not in ('canceled', 'moved')",
}


def _normalize(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip().lower()


async def _index_sql(db: AsyncSession, name: str) -> str | None:
    result = await db.execute(
        text("SELECT sql FROM sqlite_master WHERE type = 'index' AND name = :name"),
        {"name": name},
    )
    return result.scalar_one_or_none()


async def test_all_domain_tables_exist(db: AsyncSession) -> None:
    result = await db.execute(
        text("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
    )
    present = {row[0] for row in result}

    assert present - {"alembic_version"} == EXPECTED_TABLES, (
        f"缺少: {sorted(EXPECTED_TABLES - present)}; 多出: {sorted(present - EXPECTED_TABLES)}"
    )


async def test_foreign_keys_pragma_is_on(db: AsyncSession) -> None:
    """SQLite 默认**关闭**外键。

    不显式打开的后果不是报错,而是所有 ForeignKey / ON DELETE CASCADE 都静默失效 ——
    数据会不一致,而且没有任何迹象。这条必须由测试钉住。
    """
    assert await db.scalar(text("PRAGMA foreign_keys")) == 1


async def test_foreign_key_is_actually_enforced(db: AsyncSession) -> None:
    """pragma 打开还不够,要证明引擎真的会拒绝。"""
    now = datetime.now(UTC).replace(tzinfo=None)  # SQLite 侧 UtcDateTime 存 naive UTC
    with pytest.raises(IntegrityError):
        await db.execute(
            text(
                "INSERT INTO auth_sessions "
                "(id, user_id, token_hash, issued_at, expires_at, absolute_expires_at) "
                "VALUES (:id, :user_id, :token_hash, :now, :now, :now)"
            ),
            {
                "id": uuid.uuid4().hex,
                "user_id": uuid.uuid4().hex,  # 不存在的用户
                "token_hash": uuid.uuid4().hex,
                "now": now,
            },
        )
        await db.flush()


@pytest.mark.parametrize(("name", "where"), sorted(PARTIAL_UNIQUE_INDEXES.items()))
async def test_partial_unique_index_exists(db: AsyncSession, name: str, where: str) -> None:
    """部分唯一索引是本产品防"双击重复写入"的主要机制。

    红线:`postgresql_where=` 与 `sqlite_where=` **必须同时传**。只传前者时,
    PostgreSQL 上有约束、本地 SQLite 上静默没有 —— 于是"双击确认只建一次"这类保证
    在开发环境看起来正常,上线才失效。这条测试直接查 sqlite_master,专治这种偏离。
    """
    sql = await _index_sql(db, name)
    assert sql is not None, f"数据库里没有索引 {name}(模型声明了但迁移没建出来?)"
    assert _normalize(where) in _normalize(sql), f"{name} 的 WHERE 子句不对:\n{sql}"


async def test_email_is_normalized_by_the_orm(db: AsyncSession) -> None:
    """走 ORM 写入时邮箱被归一化成小写。"""
    db.add(User(email="Alice@Example.COM", password_hash="x", display_name="a"))
    await db.flush()

    assert await db.scalar(text("SELECT email FROM users")) == "alice@example.com"

    # 大小写不同但其实是同一个邮箱 —— 必须被拦住。
    db.add(User(email="alice@example.com", password_hash="y", display_name="b"))
    with pytest.raises(IntegrityError):
        await db.flush()


@pytest.mark.parametrize(
    "bypass_email",
    ["Alice@Example.COM", "alice@example.com "],
    ids=["mixed-case", "trailing-space"],
)
async def test_database_rejects_non_normalized_email_writes(
    db: AsyncSession, bypass_email: str
) -> None:
    """**绕过 ORM** 写入时,数据库也必须拦住。

    这条测试来自一次真实的漏判:当时只有 `UniqueConstraint` + ORM 侧归一化,
    我据此认为"大小写不敏感唯一"成立。实测发现,一条原生 SQL 写入可以塞进
    `Alice@Example.COM` —— 它与 `alice@example.com` 并不相等,唯一约束直接放行。
    也就是说那个保证只是应用层的君子协定,而不是数据库的事实。

    现在 `ck_users_email_is_canonical` 让任何写入者都无法制造这种逃逸。
    断言"被拒绝"而不是"报错文案",因为这里是行为,不是实现细节。
    """
    db.add(User(email="alice@example.com", password_hash="x", display_name="a"))
    await db.flush()

    with pytest.raises(IntegrityError):
        await db.execute(
            text(
                "INSERT INTO users "
                "(id, email, password_hash, display_name, timezone, token_version, is_active,"
                " created_at, updated_at) "
                "VALUES (:id, :email, 'h', 'b', 'Asia/Shanghai', 0, 1, :now, :now)"
            ),
            {"id": uuid.uuid4().hex, "email": bypass_email, "now": datetime.now(UTC).replace(tzinfo=None)},
        )
        await db.flush()


def test_production_refuses_to_fall_back_to_the_local_sqlite_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**线上的库必须是显式配的,不能靠"留空就落到本地那个文件"。**

    本地开发时 `DATABASE_URL` 留空是方便的:它落到仓库里的 `data/zhitu_dev.db`。
    但同一个默认值到了线上就变成一件很坏的事 —— 部署时忘了配连接串,服务照样起得来、
    照样能用,只是所有数据都写进了那台机器上的一个文件里。它看起来一切都好,直到
    那台机器被重建。

    所以非开发环境下这个回落是**直接抛错**,不是警告。这条断言的就是那个抛错。
    """
    from backend.core.config import Settings

    production = Settings(app_env="production", database_url="")
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        _ = production.resolved_database_url

    # 配了就用配的那个 —— 而且开发环境的回落依然有效,否则本条会把本地开发一起打死。
    configured = Settings(
        app_env="production", database_url="postgresql+asyncpg://u:p@db:5432/x"
    )
    assert configured.resolved_database_url == "postgresql+asyncpg://u:p@db:5432/x"
    assert Settings(app_env="development", database_url="").resolved_database_url.startswith(
        "sqlite+aiosqlite:///"
    )


def test_describe_target_hides_password(monkeypatch: pytest.MonkeyPatch) -> None:
    """日志里绝不允许出现数据库密码。"""
    import backend.db.health as health

    monkeypatch.setattr(
        health,
        "DATABASE_URL",
        "postgresql+asyncpg://zhitu:sup3r-s3cret@db.internal:5432/zhitu",
    )
    described = health.describe_target()

    assert "sup3r-s3cret" not in described
    assert described == "postgresql+asyncpg://zhitu:***@db.internal:5432/zhitu"
