"""`scripts/deploy/update.sh` 的参数与 dry-run 行为。

这不是在测一台真的服务器 —— systemd / venv / .env 的检查在 dry-run 下会被跳过。
这里钉的是**这个脚本自己的约定**:必须显式给完整 commit SHA、必须给新鲜备份、
工作树必须干净、dry-run 不产生任何副作用。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "deploy" / "update.sh"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="这些测试需要一个 bash(脚本本身是 bash)")


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [BASH, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )


def test_the_script_parses() -> None:
    result = subprocess.run([BASH, "-n", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_help_documents_ref_and_backup() -> None:
    result = _run("--help")
    assert result.returncode == 0
    assert "--ref" in result.stdout
    assert "--backup" in result.stdout


def test_ref_is_required() -> None:
    result = _run("--backup", "/tmp/whatever.dump")
    assert result.returncode != 0
    assert "--ref" in result.stderr


def test_ref_must_be_a_full_sha() -> None:
    result = _run("--ref", "abc123", "--backup", "/tmp/whatever.dump")
    assert result.returncode != 0
    assert "40" in result.stderr


def test_backup_is_required() -> None:
    result = _run("--ref", "a" * 40)
    assert result.returncode != 0
    assert "--backup" in result.stderr


def test_unknown_argument_is_rejected() -> None:
    result = _run("--ref", "a" * 40, "--backup", "/tmp/x.dump", "--nope")
    assert result.returncode != 0
    assert "未知参数" in result.stderr


# ---------------------------------------------------------------------------------
# dry-run:在临时 git 仓库上跑,验证它打印步骤且不产生副作用
# ---------------------------------------------------------------------------------
def _init_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    (repo / "a.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True)
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return repo, sha


def _fresh_backup(tmp_path: Path) -> Path:
    backup = tmp_path / "prod.dump"
    backup.write_bytes(b"PGDMP fake backup")
    return backup


def test_dry_run_prints_steps_and_does_not_touch_anything(tmp_path: Path) -> None:
    repo, sha = _init_repo(tmp_path)
    backup = _fresh_backup(tmp_path)

    result = _run(
        "--dry-run",
        "--repo-dir",
        str(repo),
        "--ref",
        sha,
        "--backup",
        str(backup),
    )
    assert result.returncode == 0, result.stderr
    assert "dry-run" in result.stdout
    assert sha in result.stdout
    assert "systemctl stop" in result.stdout
    assert "alembic" in result.stdout

    # **没有副作用**:HEAD 还指着分支(没有 checkout --detach)。
    head = (repo / ".git" / "HEAD").read_text(encoding="utf-8").strip()
    assert head.startswith("ref:"), "dry-run 不应该切换 HEAD"


def test_dry_run_refuses_a_dirty_worktree(tmp_path: Path) -> None:
    repo, sha = _init_repo(tmp_path)
    backup = _fresh_backup(tmp_path)
    (repo / "a.txt").write_text("changed", encoding="utf-8")

    result = _run("--dry-run", "--repo-dir", str(repo), "--ref", sha, "--backup", str(backup))
    assert result.returncode != 0
    assert "干净" in result.stderr


def test_dry_run_refuses_an_unfetched_commit(tmp_path: Path) -> None:
    repo, _sha = _init_repo(tmp_path)
    backup = _fresh_backup(tmp_path)

    result = _run(
        "--dry-run", "--repo-dir", str(repo), "--ref", "f" * 40, "--backup", str(backup)
    )
    assert result.returncode != 0
    assert "fetch" in result.stderr


def test_dry_run_refuses_a_stale_backup(tmp_path: Path) -> None:
    repo, sha = _init_repo(tmp_path)
    backup = _fresh_backup(tmp_path)
    old = time.time() - 90000  # 25 小时前
    os.utime(backup, (old, old))

    result = _run("--dry-run", "--repo-dir", str(repo), "--ref", sha, "--backup", str(backup))
    assert result.returncode != 0
    assert "24" in result.stderr or "小时" in result.stderr
