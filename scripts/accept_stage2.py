#!/usr/bin/env python
"""阶段 2 验收脚本 —— 一条命令跑完,只打印"通过 / 不通过"。

阶段 2 是**真实账户 + 接口权限 + 空间隔离**。这个脚本不用你手动点网页:它直接起一个
内存里的 HTTP 客户端,把真实接口按顺序打一遍,每一步断言的都是"这一条保证到底成不成立"。

**不碰开发数据库。** 全程在临时目录里建库,`data/zhitu_dev.db` 原封不动。这一点往后
越来越重要:那个文件里会装着你的真实计划。

用法(必须用项目环境跑):

    /c/Users/j/miniconda3/envs/zhitu/python.exe scripts/accept_stage2.py

PowerShell:

    C:\\Users\\j\\miniconda3\\envs\\zhitu\\python.exe scripts\\accept_stage2.py
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# 控制台代码页是 936(GBK),中文会变成乱码。先固定成 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# --------------------------------------------------------------------------------------
# **必须在 import backend 之前**把环境指向临时库。
# backend.db.session 在导入时就建好了 engine,晚一步就来不及了。
# --------------------------------------------------------------------------------------
_TMP = Path(tempfile.mkdtemp(prefix="zhitu-accept2-"))
_DB_PATH = _TMP / "accept.db"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB_PATH.as_posix()}"
os.environ["APP_ENV"] = "development"
os.environ["LLM_API_KEY"] = ""  # 验收过程不产生任何模型调用
os.environ["AGENT_REASONER"] = "rule"
os.environ["DB_ECHO"] = "0"

import httpx  # noqa: E402

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{'通过' if ok else '不通过'}] {name}" + (f" —— {detail}" if detail else ""))


def build_schema() -> None:
    """走 Alembic 而不是 create_all —— 验收要验的是线上那条路。"""
    from alembic import command
    from alembic.config import Config

    command.upgrade(Config(str(REPO_ROOT / "backend" / "alembic.ini")), "head")


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def exercise() -> None:
    from backend.api.main import app

    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://accept") as client,
    ):
        # ---- 1. 注册 ----
        print("1. 注册一个真实账号")
        registered = await client.post(
            "/api/auth/register",
            json={
                "email": "Accept@Example.COM",  # 故意用大写,顺便验证归一化
                "password": "accept-stage-2",
                "displayName": "验收账号",
            },
        )
        ok = registered.status_code == 201
        body = registered.json() if ok else {}
        token_a = body.get("token", "")
        record(
            "注册返回 201 并拿到令牌",
            ok and bool(token_a),
            f"HTTP {registered.status_code}",
        )
        if not ok or not token_a:
            print(f"\n注册就失败了,后面无法继续:{registered.text}")
            return
        record(
            "大写邮箱被归一化成小写",
            body["user"]["email"] == "accept@example.com",
            body["user"]["email"],
        )

        # ---- 2. 没有令牌就进不来 ----
        print("\n2. 未登录访问受保护接口")
        anonymous = await client.get("/api/workspaces")
        record(
            "不带 Authorization 头 -> 401",
            anonymous.status_code == 401
            and anonymous.json()["error"]["code"] == "UNAUTHENTICATED",
            f"HTTP {anonymous.status_code}",
        )

        # ---- 3. 新空间必须是空的 ----
        print("\n3. 新建空间里到底有什么")
        created = await client.post(
            "/api/workspaces",
            json={
                "title": "Python 学习",
                "intent": "三个月内完成一个项目",
                "goal": "三个月内做出一个能跑起来的小项目",
            },
            headers=auth(token_a),
        )
        counts = created.json()["workspace"]["counts"] if created.status_code == 201 else {}
        record(
            "新空间 = 1 个根目标 + 0 会话 + 0 提案 + 0 排期",
            created.status_code == 201
            and counts == {"nodes": 1, "conversations": 0, "proposals": 0, "scheduledSessions": 0},
            str(counts),
        )
        space_a = created.json()["workspace"]["id"] if created.status_code == 201 else ""

        # ---- 4. 另一个账号进不来 ----
        print("\n4. 另一个账号拿这个空间的 id 访问")
        second = await client.post(
            "/api/auth/register",
            json={"email": "b@example.com", "password": "accept-stage-2", "displayName": "B"},
        )
        token_b = second.json()["token"]

        mine = await client.get(f"/api/workspaces/{space_a}", headers=auth(token_a))
        theirs = await client.get(f"/api/workspaces/{space_a}", headers=auth(token_b))
        record(
            "本人 200、他人 404(而不是 403)",
            mine.status_code == 200 and theirs.status_code == 404,
            f"本人 {mine.status_code} / 他人 {theirs.status_code}",
        )

        # ---- 5. 猜不出账号存不存在 ----
        print("\n5. 密码错误 vs 账号不存在")
        wrong_password = await client.post(
            "/api/auth/login", json={"email": "accept@example.com", "password": "wrong"}
        )
        no_such_account = await client.post(
            "/api/auth/login", json={"email": "nobody@example.com", "password": "wrong"}
        )
        record(
            "两者返回同一个状态码与同一个 code",
            wrong_password.status_code == no_such_account.status_code
            and wrong_password.json()["error"]["code"] == no_such_account.json()["error"]["code"]
            == "INVALID_CREDENTIALS",
            f"{wrong_password.status_code} {wrong_password.json()['error']['code']}",
        )

        # ---- 6. 登出真的生效 ----
        print("\n6. 登出")
        logged_out = await client.post("/api/auth/logout", headers=auth(token_a))
        after_logout = await client.get("/api/users/me", headers=auth(token_a))
        record(
            "登出后旧令牌失效",
            logged_out.status_code == 204 and after_logout.status_code == 401,
            f"登出 {logged_out.status_code} / 再用 {after_logout.status_code}",
        )

        # ---- 7. 令牌轮换与重放 ----
        print("\n7. 令牌轮换与重放")
        fresh = await client.post(
            "/api/auth/login",
            json={"email": "accept@example.com", "password": "accept-stage-2"},
        )
        old_token = fresh.json()["token"]
        rotated = await client.post("/api/auth/refresh", json={"token": old_token})
        new_token = rotated.json().get("token", "")

        replayed = await client.post("/api/auth/refresh", json={"token": old_token})
        new_token_after_replay = await client.get("/api/users/me", headers=auth(new_token))
        record(
            "轮换后旧令牌被拒",
            rotated.status_code == 200
            and replayed.status_code == 401
            and replayed.json()["error"]["code"] == "TOKEN_REUSE_DETECTED",
            f"重放 HTTP {replayed.status_code}",
        )
        record(
            "重放旧令牌导致整族撤销(新令牌一并失效)",
            new_token_after_replay.status_code == 401,
            f"新令牌 HTTP {new_token_after_replay.status_code}",
        )

        # ---- 8. 库里不该有明文 ----
        print("\n8. 数据库里有没有明文凭据")
        await client.post(
            "/api/auth/login",
            json={"email": "accept@example.com", "password": "accept-stage-2"},
        )
        leaked: list[str] = []
        con = sqlite3.connect(_DB_PATH)
        try:
            blob = "\n".join(
                str(row[0])
                for row in con.execute(
                    "SELECT password_hash FROM users UNION ALL SELECT token_hash FROM auth_sessions"
                )
            )
            for secret in ("accept-stage-2", token_a, old_token, new_token):
                if secret and secret in blob:
                    leaked.append(secret[:12] + "…")
        finally:
            con.close()
        record(
            "库里只有 scrypt 哈希与令牌 sha256,没有明文",
            not leaked,
            f"发现明文: {leaked}" if leaked else "口令与令牌均未以明文出现",
        )


def run_subprocess(args: list[str]) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    return subprocess.run(
        [sys.executable, *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def tail(text: str, lines: int = 1) -> str:
    kept = [ln for ln in text.strip().splitlines() if ln.strip()][-lines:]
    return " / ".join(kept)


def main() -> int:
    print()
    print("阶段 2 验收:真实账户 / 接口权限 / 空间隔离")
    print(f"临时库: {_DB_PATH}")
    print()
    print("0. 用 Alembic 在临时库上建表")
    build_schema()
    tables = 0
    if _DB_PATH.exists():
        con = sqlite3.connect(_DB_PATH)
        tables = con.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
        con.close()
    record("迁移建库成功", tables == 19, f"{tables} 张表")

    asyncio.run(exercise())

    print("\n9. 自动测试与代码检查")
    pytest_proc = run_subprocess(["-m", "pytest", "backend/tests", "-q", "-p", "no:warnings"])
    record("pytest 全部通过", pytest_proc.returncode == 0, tail(pytest_proc.stdout))
    ruff_proc = run_subprocess(["-m", "ruff", "check", "backend"])
    record("ruff 无告警", ruff_proc.returncode == 0, tail(ruff_proc.stdout))

    failed = [name for name, ok, _ in results if not ok]
    print()
    print("=" * 68)
    if failed:
        print(f"结果:有 {len(failed)} 项没通过")
        for name in failed:
            print(f"  - {name}")
        print("=" * 68)
        print()
        print("把上面这段贴给我即可。")
        return 1
    print(f"结果:全部 {len(results)} 项通过。阶段 2 可以验收。")
    print("=" * 68)
    print()
    print("说明:阶段 2 的后端接口已经可用,但**网页还没有接上** ——")
    print("前端仍然走浏览器本地存储,所以你在网页上暂时看不到这些能力。")
    print("界面要等阶段 5。你的开发数据库 data/zhitu_dev.db 未被本次验收触碰。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
