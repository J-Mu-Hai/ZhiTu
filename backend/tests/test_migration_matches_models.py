"""防止 `create_all` 与 Alembic 两条路分叉。

测试用 `create_all` 建表(毫秒级),开发与生产用 `alembic upgrade head` 建表(可迁移)。
两条路一旦不一致,整个测试套件就会在一个"测试里存在、线上不存在"的模式上通过 ——
这正是阶段 1 真实发生过的事:模型里声明了 `uq_users_email_lower`,迁移里没有,于是
邮箱唯一性在开发库里完全没有生效,而 `alembic check` 照样报"无漂移"。

**为什么不用 `inspect()` / 只跑 `alembic check`:** SQLAlchemy 的反射在某些情况下是
看不见东西的(表达式索引就是典型:2.0.52 的 SQLite 方言无法反射它,autogenerate 会
把它静默丢掉,`alembic check` 也不会报)。所以这里直接对拍 **DDL 本身** ——
`sqlite_master` 是引擎的真实产物,没有反射盲区。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

import backend.db.models  # noqa: F401  导入即注册全部模型
from backend.core.config import settings
from backend.db.base import Base

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "backend" / "alembic.ini"


_CONSTRAINT = "constraint "


def _split_top_level(body: str) -> list[str]:
    """按**顶层**逗号切分表定义体,括号内的逗号不动(CHECK / NUMERIC(3, 2) 里都有)。"""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for char in body:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    parts.append("".join(current).strip())
    return [part for part in parts if part]


def _normalize(kind: str, sql: str) -> str:
    """归一化到"语义相同"的形态 —— 抹平两处书写顺序差异。

    1. **表内约束的书写顺序。** `create_all` 先写 UNIQUE 再写 CHECK,而 Alembic 生成的
       迁移按约束名排序输出。语义相同,SQLite 也不关心顺序,按顺序比对只会得到一堆
       无意义的失败,反而淹掉真正的问题(少一个索引)。

    2. **列的先后顺序。** 这条是被一次真实的失败逼出来的:给 `auth_sessions` 加一列时,
       迁移走的是 `ALTER TABLE ADD COLUMN`,SQLite 只能把新列**追加到末尾**,而
       `create_all` 把它放在模型里声明的那个位置。两者列完全相同,只有次序不同。

       更关键的是,这不是一次性偏差 —— 只要模型以后还加列,顺序就永远对不上。也就是说
       保留列序等于**宣布今后任何增量迁移都会让这条测试失败**,那样它抓不住真问题,
       只会逼人一次次放宽它。列序在 SQL 里没有语义(没有任何代码依赖位置,查询一律
       写列名),所以按名字排序后再比对。

    约束与列的内容、数量都不动:少写一条、类型写错、NOT NULL 漏掉、DEFAULT 不一致,
    都会改变拼接结果,照样报错。**这正是这条测试要守的东西。**
    """
    text = " ".join(sql.split())
    if kind != "table":
        return text

    head, _, rest = text.partition("(")
    # 表名可能带双引号:`create_all` 写裸名,而 Alembic 的 batch 迁移会重建表
    # (`CREATE TABLE "x" ... ` -> rename),SQLite 把带引号的写法原样存进 sqlite_master。
    # 引号只影响 DDL 文本,不影响标识 —— 这里的 key 本来就来自身份列 `sqlite_master.name`。
    head = head.replace('"', "")
    body, _, tail = rest.rpartition(")")
    parts = _split_top_level(body)
    constraints = sorted(p for p in parts if p.lower().startswith(_CONSTRAINT))
    columns = sorted(p for p in parts if not p.lower().startswith(_CONSTRAINT))
    return f"{head.strip()} ( {', '.join(columns + constraints)} ) {tail.strip()}".strip()


def _ddl(db_path: Path) -> dict[str, str]:
    """引擎里真实存在的每个对象 -> 归一化后的 DDL。

    排除 `sqlite_%`(自增索引等引擎内部产物)与 `alembic_version`(只有迁移那条路
    才会有,本来就不是模型的一部分)。
    """
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' AND name != 'alembic_version'"
        ).fetchall()
    finally:
        con.close()
    return {f"{kind}:{name}": _normalize(kind, sql or "") for kind, name, sql in rows}


_BASE_TABLE = (
    'CREATE TABLE t ( id CHAR(32) NOT NULL, name VARCHAR(64) NOT NULL, '
    'note VARCHAR(64), token_version INTEGER DEFAULT 0 NOT NULL, '
    'CONSTRAINT pk_t PRIMARY KEY (id), '
    'CONSTRAINT uq_t_name UNIQUE (name), '
    "CONSTRAINT ck_t_note CHECK (note <> '') )"
)


def test_normalize_ignores_only_writing_order() -> None:
    """`_normalize` 刚刚被放宽过(它现在也排序列),这条测试钉住它没有因此失效。

    放宽顺序与"看不出差异"只有一步之遥:如果排完序又把内容也丢掉,DDL 对拍就会对
    任何两套模式都报"一致"。所以这里从**两个方向**各钉一次 ——
    该忽略的差异必须忽略,该抓的差异必须抓。

    下面每一条"必须抓到"的用例都对应一类真实事故:漏列、类型写错、NOT NULL 漏掉、
    默认值不一致、漏约束。它们正是 DDL 对拍存在的理由。
    """
    base = _normalize("table", _BASE_TABLE)

    # 只换了书写顺序 —— 必须视为相同,否则每次增量迁移都会误报。
    reordered = (
        'CREATE TABLE t ( token_version INTEGER DEFAULT 0 NOT NULL, note VARCHAR(64), '
        'name VARCHAR(64) NOT NULL, id CHAR(32) NOT NULL, '
        "CONSTRAINT ck_t_note CHECK (note <> ''), "
        'CONSTRAINT uq_t_name UNIQUE (name), '
        'CONSTRAINT pk_t PRIMARY KEY (id) )'
    )
    assert _normalize("table", reordered) == base

    # 表名带引号 —— 同样只是写法差异。
    assert _normalize("table", _BASE_TABLE.replace("CREATE TABLE t", 'CREATE TABLE "t"')) == base

    # ---- 以下每一条都必须被抓到 ----
    mutations = {
        "column removed": _BASE_TABLE.replace("note VARCHAR(64), ", ""),
        "type changed": _BASE_TABLE.replace("name VARCHAR(64) NOT NULL", "name VARCHAR(32) NOT NULL"),
        "nullability changed": _BASE_TABLE.replace("note VARCHAR(64)", "note VARCHAR(64) NOT NULL"),
        "default changed": _BASE_TABLE.replace("DEFAULT 0", "DEFAULT 1"),
        "constraint removed": _BASE_TABLE.replace("CONSTRAINT uq_t_name UNIQUE (name), ", ""),
        "extra column": _BASE_TABLE.replace("note VARCHAR(64)", "note VARCHAR(64), extra INTEGER"),
    }
    for label, mutated in mutations.items():
        assert _normalize("table", mutated) != base, f"normalizer stopped catching: {label}"


def _build_from_models(db_path: Path) -> None:
    engine = sa.create_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()


def _build_from_migrations(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """让 Alembic 走到临时库上。env.py 读的就是这个 setting。"""
    monkeypatch.setattr(
        settings, "database_url", f"sqlite+aiosqlite:///{db_path.as_posix()}"
    )
    monkeypatch.setattr(settings, "app_env", "development")
    command.upgrade(Config(str(ALEMBIC_INI)), "head")


def test_alembic_and_create_all_build_identical_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from_models = tmp_path / "from_models.db"
    from_migrations = tmp_path / "from_migrations.db"

    _build_from_models(from_models)
    _build_from_migrations(from_migrations, monkeypatch)

    models_ddl = _ddl(from_models)
    migrations_ddl = _ddl(from_migrations)

    missing = sorted(set(models_ddl) - set(migrations_ddl))
    extra = sorted(set(migrations_ddl) - set(models_ddl))
    assert not missing, f"objects in models but not built by migrations: {missing}"
    assert not extra, f"objects in migrations but not in models: {extra}"

    differing = sorted(k for k in models_ddl if models_ddl[k] != migrations_ddl[k])
    # 诊断信息刻意用 ASCII:这台机器控制台是 GBK,中文在这里会被打成乱码,
    # 而报错信息打出来看不懂就等于没有。
    assert not differing, "DDL differs between the two paths:\n\n" + "\n\n".join(
        f"--- {key} ---\n  create_all : {models_ddl[key]}\n  migrations : {migrations_ddl[key]}"
        for key in differing
    )


def test_alembic_check_reports_no_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """模型改动如果没有配套迁移,这条要失败。

    与上面的 DDL 对拍互补:对拍抓"迁移建出来的东西和模型不一样",这条抓
    "模型改了但根本没人生成迁移"。
    """
    from alembic.util.exc import AutogenerateDiffsDetected

    db_path = tmp_path / "check.db"
    config = Config(str(ALEMBIC_INI))
    _build_from_migrations(db_path, monkeypatch)

    try:
        command.check(config)
    except AutogenerateDiffsDetected as exc:
        pytest.fail(f"models and migrations have drifted; generate a new migration:\n{exc}")
