"""排期必须是一个**叶子包**。这条用 AST 扫描来强制。

## 为什么这件事值得一个测试

"排期是确定性的,所以能验证" 是这份算法唯一的立身之本。而它会因为三个很自然的小改动
悄悄失效,每一个看起来都无害:

1. `import sqlalchemy` —— 比如为了复用 `UtcDateTime`。于是排期不再能脱离数据库运行,
   而"给我一份输入"变成了"给我一个 session"。
2. `from backend.services.plan_service import load_all_nodes` —— 于是"算哪天做"和
   "谁能写数据库"纠缠在一起,而后者是分层里最该守住的一条线。
3. `date.today()` —— 于是 `schedule_version` 在跨过午夜时变一下,同一份输入两次
   运行得到不同的版本号,用户看到"计划已经变了,请重新预览"而他什么也没改。

三条都不是"有人故意违规",三条都是顺手。所以规则写在测试里,而不是写在文档里。

## 枚举漂移

`types.py` 里那套取值枚举是**重新声明的**(理由见该文件顶部)。重新声明就有漂移的
风险,而漂移的后果是安静的:数据库写进去 `doing`,排期认的是 `in_progress`,于是
一个正在进行的任务被当成"没开始"重排一遍。所以这里逐成员比对。
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCHEDULER_DIR = Path(__file__).resolve().parents[3] / "scheduler"
REPO_ROOT = Path(__file__).resolve().parents[4]

#: 一律不许出现的顶层包。分三类的理由不同:
#: - `sqlalchemy` / `httpx`:排期不该有任何 I/O 与持久化概念
#: - `backend.services` / `backend.agent` / `backend.api`:那是别的层,反向依赖会让
#:   "谁能写数据库"这条线失效
#: - `backend.db`:见 `types.py` 顶部 —— 连 `backend.db.models.enums` 也不行,
#:   因为导入子模块会先执行 `backend/db/__init__.py`,而它会建引擎
FORBIDDEN_PREFIXES = (
    "sqlalchemy",
    "httpx",
    "backend.services",
    "backend.agent",
    "backend.api",
    "backend.db",
    "backend.core",
    "backend.contracts",
)

#: 会读时钟的调用。排期的"今天"只能由调用方传进来。
CLOCK_CALLS = {"today", "now", "utcnow"}

#: 这些名字指向 datetime 模块(或它的别名)。只有"日期时间模块的时钟方法"才算读时钟 ——
#: 这一点必须写清楚,否则规则会误伤 `request.today` / `self.today`,而那**正是**正确
#: 写法(把"今天"当输入)。一条会把正确答案判错的规则,最后的结局是被人加一行
#: `# noqa` 关掉,于是它连同真正要挡的三条一起失效。
CLOCK_MODULES = {"date", "datetime", "time", "dt"}


def _root_name(node: ast.expr) -> str | None:
    """`datetime.date.today` → `datetime`;`request.today` → `request`;其余 → None。"""
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _scheduler_files() -> list[Path]:
    files = sorted(SCHEDULER_DIR.glob("*.py"))
    assert files, f"没有在 {SCHEDULER_DIR} 找到任何模块 —— 路径变了?"
    return files


def _imported_modules(tree: ast.AST) -> list[str]:
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            found.append(node.module)
    return found


@pytest.mark.parametrize("path", _scheduler_files(), ids=lambda path: path.name)
def test_no_forbidden_imports(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for module in _imported_modules(tree):
        for prefix in FORBIDDEN_PREFIXES:
            assert not (module == prefix or module.startswith(f"{prefix}.")), (
                f"{path.name} 导入了 {module}。排期是叶子包:它只依赖标准库。"
                f"需要领域枚举时用 `types.py` 里那一套(有测试比对两者不漂移)。"
            )


@pytest.mark.parametrize("path", _scheduler_files(), ids=lambda path: path.name)
def test_never_reads_the_clock(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        # `date.today()` / `datetime.now()` / `datetime.utcnow()` —— 一律不许。
        if (
            isinstance(node, ast.Attribute)
            and node.attr in CLOCK_CALLS
            and _root_name(node.value) in CLOCK_MODULES
        ):
            raise AssertionError(
                f"{path.name}:{node.lineno} 读了时钟({node.attr})。"
                f"排期的'今天'由 `ScheduleRequest.today` 传进来 —— "
                f"自己取的话,同一份输入跨过午夜会算出不同结果,"
                f"而 `schedule_version` 会因此无端失效。"
            )


def test_enum_values_match_the_domain_enums() -> None:
    """`types.py` 里重新声明的枚举,取值必须与 `db/models/enums.py` 逐成员一致。

    **这个测试是允许 import 两边的** —— 它不参与排期,它只负责发现漂移。
    """
    from backend.db.models import enums as domain
    from backend.scheduler import types as scheduler_types

    pairs = [
        ("NodeStatus", scheduler_types.NodeStatus, domain.NodeStatus),
        ("NodeType", scheduler_types.NodeType, domain.NodeType),
        ("Priority", scheduler_types.Priority, domain.Priority),
        ("SessionStatus", scheduler_types.SessionStatus, domain.ScheduledSessionStatus),
        ("SessionOrigin", scheduler_types.SessionOrigin, domain.ScheduledSessionOrigin),
        ("ExecutionResult", scheduler_types.ExecutionResult, domain.ExecutionResult),
    ]
    for name, ours, theirs in pairs:
        assert {member.value for member in ours} == {member.value for member in theirs}, (
            f"{name} 与 backend.db.models.enums 漂移了。"
            f"两边取值不同意味着写进库的状态排期认不出来,"
            f"而表现是任务被安静地重排了一遍。"
        )


def test_frozen_statuses_match() -> None:
    """冻结集合也要一致 —— 差一个成员,一类历史记录就会被重排覆盖。"""
    from backend.db.models.enums import FROZEN_SESSION_STATUSES
    from backend.scheduler.types import FROZEN_STATUSES

    assert {status.value for status in FROZEN_STATUSES} == {
        status.value for status in FROZEN_SESSION_STATUSES
    }


#: 在一个**全新的解释器**里导入排期,然后报告被拖进来的第三方顶层包。
#:
#: 为什么必须是子进程:同一个测试文件里有一条测试要 import `backend.db.models`(比对
#: 枚举漂移),而 pytest 全跑在一个进程里 —— 于是轮到这一条时,sqlalchemy 早就在
#: `sys.modules` 里了,它测的是"别的测试导入过什么",不是"排期拖进来了什么"。
#: 前者永远为真,于是这条测试恒红;或者更糟 —— 把它改成只看增量,它就恒绿。
_LEAK_PROBE = """
import json, sys
import backend.scheduler  # noqa: F401
watched = {"sqlalchemy", "httpx", "pydantic", "fastapi", "alembic", "openjiuwen"}
leaked = sorted({name.split(".")[0] for name in sys.modules} & watched)
print("LEAKED=" + json.dumps(leaked))
"""


def test_only_stdlib_is_actually_imported() -> None:
    """真刀真枪地在新解释器里导入一遍,确认没有把第三方模块拖进来。

    AST 扫描能挡住显式的 `import sqlalchemy`,但挡不住 `backend.scheduler.foo` 这种
    同包导入,也挡不住某条连锁的间接依赖 —— 而"排期可以脱离数据库、脱离网络单独运行"
    正是这份算法能对着纯输入被验证的前提。
    """
    result = subprocess.run(
        [sys.executable, "-c", _LEAK_PROBE],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    assert result.returncode == 0, (
        f"子进程里 import backend.scheduler 就失败了(不是拖了依赖,是根本导不进来):\n"
        f"{result.stdout}\n{result.stderr}"
    )
    marker = [line for line in result.stdout.splitlines() if line.startswith("LEAKED=")]
    assert marker, f"探针没有输出结果:\n{result.stdout}\n{result.stderr}"
    leaked = json.loads(marker[-1].removeprefix("LEAKED="))
    assert not leaked, f"导入 backend.scheduler 之后,新进程里出现了 {leaked}"
