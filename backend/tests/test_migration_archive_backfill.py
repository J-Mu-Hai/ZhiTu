"""`c4f8a1d6e9b3` 的回填:把"时间戳即批次"翻译成显式的批次号。

## 为什么要单独一条

加列、删列是不看数据的,回填是这次迁移里**唯一会按已有数据算出一个值**的一步。它要
保证的是一一对应:同一个 `deleted_at` 的行拿到同一个新号,不同 `deleted_at` 的行绝不
拿到同一个号。这两条里任何一条反了都不会报错 —— 只会让恢复多带回或少带回东西,而
"多带回"恰好是这个功能最不该有的错(见 `PlanNode.archive_batch_id`)。

真实开发库上跑之前先在这里跑一遍:那个库现在有 17 行归档、13 个不同的归档时刻。

## 为什么用裸 SQL 而不是模型

历史状态要用历史的表。那一刻 `plan_nodes` 上**没有** `archive_batch_id`,用现在的模型
插入会去写一个当时不存在的列 —— 那样测的是模型,不是迁移。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from backend.core.config import settings

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "backend" / "alembic.ini"
#: 加批次号之前的那个版本。回填只对**这个版本时就已经归档**的行有意义。
BEFORE = "b7d41c9f2a68"

#: 两个不同的归档时刻。微秒**刻意非零**:全零的话,两个后端来回转换时"有没有小数位"
#: 会变成另一个变量,而这条测试要盯的是分组,不是时间格式。
FIRST = datetime(2026, 9, 20, 10, 0, 0, 1, tzinfo=UTC)
SECOND = datetime(2026, 9, 21, 11, 30, 0, 500000, tzinfo=UTC)


def _upgrade(db_path: Path, revision: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """让 Alembic 走到临时库上。env.py 读的就是这个 setting。"""
    monkeypatch.setattr(
        settings, "database_url", f"sqlite+aiosqlite:///{db_path.as_posix()}"
    )
    monkeypatch.setattr(settings, "app_env", "development")
    command.upgrade(Config(str(ALEMBIC_INI)), revision)


def _hex_id(name: str) -> str:
    """从名字造一个确定的 32 位十六进制 id —— 主键在 SQLite 上就是这个形状。"""
    return (name.encode().hex() * 32)[:32]


def _stamp(value: datetime) -> str:
    """SQLite 上 `UtcDateTime` 的存储形状。

    显式格式化,不把 `datetime` 对象直接交给 sqlite3:那个默认适配器从 Python 3.12 起
    是弃用的,而它同时也决定了写进去的字符串长什么样 —— 迁移那边按字符串分组,
    格式由这里定下来比由驱动的弃用默认值定下来要好。
    """
    return value.strftime("%Y-%m-%d %H:%M:%S.%f")


def _insert_rows(db_path: Path, rows: list[tuple[str, datetime | None]]) -> None:
    """按 `b7d41c9f2a68` 那一刻的表结构插入节点(只有必填列,其余留空)。"""
    connection = sqlite3.connect(db_path)
    try:
        connection.executemany(
            "INSERT INTO plan_nodes (id, workspace_id, title, node_type, status, priority,"
            " order_index, depth, origin, created_at, updated_at, deleted_at)"
            " VALUES (?, ?, ?, 'task', 'pending', 'medium', 0, 0, 'user', ?, ?, ?)",
            [
                (
                    _hex_id(name),
                    _hex_id("workspace"),
                    f"节点 {name}",
                    _stamp(FIRST),
                    _stamp(FIRST),
                    _stamp(deleted_at) if deleted_at is not None else None,
                )
                for name, deleted_at in rows
            ],
        )
        connection.commit()
    finally:
        connection.close()


def _batches(db_path: Path) -> dict[str, str | None]:
    connection = sqlite3.connect(db_path)
    try:
        return {
            row[0]: row[1]
            for row in connection.execute("SELECT id, archive_batch_id FROM plan_nodes")
        }
    finally:
        connection.close()


def test_backfill_gives_each_archive_its_own_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "backfill.db"
    _upgrade(db_path, BEFORE, monkeypatch)
    _insert_rows(
        db_path,
        [
            ("a1", FIRST),   # 同一次归档里的父子两项 —— 必须同一个号
            ("a2", FIRST),
            ("b1", SECOND),  # 另一次归档 —— 必须另一个号
            ("live", None),  # 活着的节点 —— 没有批次可言
        ],
    )

    _upgrade(db_path, "head", monkeypatch)

    batches = _batches(db_path)
    assert set(batches) == {_hex_id(name) for name in ("a1", "a2", "b1", "live")}
    assert batches[_hex_id("a1")] == batches[_hex_id("a2")], "同一次归档要同一个号"
    assert batches[_hex_id("a1")] != batches[_hex_id("b1")], "两次归档不能同号"
    assert batches[_hex_id("a1")] is not None, "回填不能留空 —— 空号会让恢复退回单行"
    assert batches[_hex_id("live")] is None, "活着的节点不该有批次号"


def test_downgrade_drops_the_column_and_keeps_the_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回滚只掉这一列,不动任何一行。

    凌晨三点会有人去按它的。它要保证的不是"回到旧行为"(那要靠旧代码),而是
    **数据还在**:掉一列比掉一整个迁移容易得多,而"降级之后计划空了"这种事故
    恰恰是这样来的。
    """
    db_path = tmp_path / "downgrade.db"
    _upgrade(db_path, "head", monkeypatch)
    _insert_rows(db_path, [("a1", FIRST), ("a2", FIRST)])

    monkeypatch.setattr(
        settings, "database_url", f"sqlite+aiosqlite:///{db_path.as_posix()}"
    )
    command.downgrade(Config(str(ALEMBIC_INI)), BEFORE)

    connection = sqlite3.connect(db_path)
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(plan_nodes)")}
        assert "archive_batch_id" not in columns
        counted = connection.execute("SELECT COUNT(*) FROM plan_nodes").fetchone()[0]
        assert counted == 2, "降级掉的是列,不是行"
    finally:
        connection.close()
