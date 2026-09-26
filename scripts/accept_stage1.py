#!/usr/bin/env python
"""阶段 1 验收脚本 —— 一条命令跑完所有验收项,只打印"通过 / 不通过"。

为什么要有这个脚本:

验收不该要求你读命令行输出、也不该要求你理解 alembic 或 pytest。你只需要看最后那张
表,以及"全部通过"还是"有 X 项没通过"。

**不碰开发数据库。** 所有需要建库的检查都在临时目录里做,`data/zhitu_dev.db` 原封不动。
这一点很重要:往后的阶段里那个文件会装着你的真实数据,验收脚本绝不可以删它。

用法(必须用项目环境跑):

    /c/Users/j/miniconda3/envs/zhitu/python.exe scripts/accept_stage1.py

PowerShell:

    C:\\Users\\j\\miniconda3\\envs\\zhitu\\python.exe scripts\\accept_stage1.py
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# 这台机器的控制台代码页是 936(GBK),Python 默认就会用 GBK 编码 stdout,中文在
# UTF-8 终端里会变成乱码。把标准输出固定成 UTF-8,免得验收信息本身看不懂。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

EXPECTED_TABLES = 19  # 18 张领域表 + alembic_version

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{'通过' if ok else '不通过'}] {name}" + (f" —— {detail}" if detail else ""))


def run(args: list[str], *, db_url: str | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    # 挡住真实出网:验收过程不应该产生任何模型调用费用。
    env["LLM_API_KEY"] = ""
    if db_url:
        env["DATABASE_URL"] = db_url
    return subprocess.run(
        [sys.executable, *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def tail(text: str, lines: int = 4) -> str:
    kept = [ln for ln in text.strip().splitlines() if ln.strip()][-lines:]
    return " / ".join(kept)


def main() -> int:
    tmpdir = Path(tempfile.mkdtemp(prefix="zhitu-accept-"))
    db_path = tmpdir / "accept.db"
    db_url = f"sqlite+aiosqlite:///{db_path.as_posix()}"

    print()
    print("阶段 1 验收:数据库地基")
    print(f"临时库: {db_path}")
    print()

    # ---- 1. 从空库能否建起来 ----
    print("1. 从空库执行 alembic upgrade head")
    proc = run(["-m", "alembic", "-c", "backend/alembic.ini", "upgrade", "head"], db_url=db_url)
    tables = 0
    if db_path.exists():
        con = sqlite3.connect(db_path)
        tables = con.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
        con.close()
    record(
        "从空库建出全部表",
        proc.returncode == 0 and tables == EXPECTED_TABLES,
        f"{tables} 张表(期望 {EXPECTED_TABLES})",
    )

    # ---- 2. 模型与迁移是否分叉 ----
    print("\n2. 模型与迁移是否漂移")
    proc = run(["-m", "alembic", "-c", "backend/alembic.ini", "check"], db_url=db_url)
    record(
        "没有未生成的迁移",
        proc.returncode == 0 and "No new upgrade operations" in (proc.stdout + proc.stderr),
        "两条建库路径一致",
    )

    # ---- 3. 约束是否真的在生效(绕过 ORM 的原生写入)----
    print("\n3. 数据库层的约束是否真的拦得住")
    if db_path.exists():
        con = sqlite3.connect(db_path)
        now = "2024-01-01 00:00:00"
        insert = (
            "INSERT INTO users (id,email,password_hash,display_name,timezone,"
            "token_version,is_active,created_at,updated_at) VALUES (?,?,?,?,?,0,1,?,?)"
        )
        con.execute(insert, (uuid.uuid4().hex, "alice@example.com", "h", "a", "Asia/Shanghai", now, now))
        con.commit()

        bad_ones = ["Alice@Example.COM", "alice@example.com "]
        escaped = []
        for bad in bad_ones:
            try:
                con.execute(insert, (uuid.uuid4().hex, bad, "h", "b", "Asia/Shanghai", now, now))
                con.commit()
                escaped.append(bad)
            except sqlite3.IntegrityError:
                pass
        con.close()
        record(
            "大小写 / 首尾空白不同的邮箱无法冒充同一账号",
            not escaped,
            f"被接受: {escaped}" if escaped else "两种写法都被拒绝",
        )
    else:
        record("邮箱唯一性", False, "建库失败,无法检查")

    # ---- 4. 测试套件 ----
    print("\n4. 自动测试")
    proc = run(["-m", "pytest", "backend/tests", "-q", "-p", "no:warnings"])
    summary = tail(proc.stdout, 1)
    record("pytest 全部通过", proc.returncode == 0, summary)

    # ---- 5. 代码检查 ----
    print("\n5. 代码检查")
    proc = run(["-m", "ruff", "check", "backend"])
    record("ruff 无告警", proc.returncode == 0, tail(proc.stdout, 1))

    # ---- 6. 假接口确实已经消失 ----
    #
    # 这里**必须**查 app.openapi()["paths"],不能查 app.routes。
    # FastAPI 0.141 把 include_router 进来的路由包在 _IncludedRouter 对象里,不会展平进
    # app.routes —— 用 app.routes 判断时,它永远返回空集合,于是这条检查在假接口还在的
    # 时候也是"通过"。第一版就是这么写的,等于什么都没检查。
    print("\n6. 旧的假接口是否已经移除")
    proc = run(
        [
            "-c",
            "from backend.api.main import app;"
            "print(sorted(p for p in app.openapi()['paths'] if p.startswith('/api/agent')))",
        ]
    )
    leftover = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "?"
    record("内存假实现的路由(/api/agent/*)已移除", leftover == "[]", f"残留: {leftover}")

    # ---- 汇总 ----
    failed = [name for name, ok, _ in results if not ok]
    print()
    print("=" * 62)
    if failed:
        print(f"结果:有 {len(failed)} 项没通过")
        for name in failed:
            print(f"  - {name}")
        print("=" * 62)
        print()
        print("把上面这段贴给我即可。")
        return 1
    print(f"结果:全部 {len(results)} 项通过。阶段 1 可以验收。")
    print("=" * 62)
    print()
    print("说明:阶段 1 是数据库地基,**没有网页可看**。")
    print("界面要等阶段 5,端到端要等阶段 8。")
    print("你的开发数据库 data/zhitu_dev.db 未被本次验收触碰。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
