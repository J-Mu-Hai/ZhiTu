"""测试全局配置。

三条纪律靠 autouse fixture 强制,而不是靠每个测试自觉 —— 会被忘记的约定等于不存在。

1. **测试绝不碰开发数据库。** 在导入任何 backend 模块之前就把 DATABASE_URL 指向临时
   文件。backend.db.session 在**导入时**就建好了 engine,晚一步就来不及了,所以这段
   代码必须待在本文件最顶部、任何 backend import 之前。
2. **测试绝不调用真实模型。** 清空 key 并把 reasoner 钉在本地规则上。
3. **测试绝不发起真实网络请求。** 拦在传输层,漏 mock 的测试会立刻报错,而不是在 CI
   上真的打一次付费 API。

4. **断言假模型确实被调用过。** 见下面的 `FakeReasoner.calls` —— 没有这一条,一个
   依赖注入漏了的测试会悄悄走真实分支,而它可能仍然是绿的。
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------------------
# 必须在 backend.* 被导入之前执行。
# --------------------------------------------------------------------------------------
_TEST_DB_DIR = Path(tempfile.mkdtemp(prefix="zhitu-tests-"))
_TEST_DB = _TEST_DB_DIR / "zhitu_test.db"

os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TEST_DB.as_posix()}"
os.environ["APP_ENV"] = "development"
os.environ["LLM_API_KEY"] = ""
os.environ["AGENT_REASONER"] = "rule"
os.environ["DB_ECHO"] = "0"
# 连不上库时不允许带病继续 —— 测试里要能看见启动失败。
os.environ["ALLOW_DEGRADED_DB"] = "0"

from datetime import datetime  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402
from sqlalchemy import func, select, text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

import backend.db.models  # noqa: E402,F401  导入即注册全部模型
from backend.core.config import settings  # noqa: E402
from backend.db.base import Base  # noqa: E402
from backend.db.session import SessionLocal, engine  # noqa: E402

assert "zhitu_test.db" in settings.resolved_database_url, (
    "测试数据库没有被正确指向临时文件,拒绝继续 —— 否则测试会写进开发库"
)
assert not settings.llm_api_key, "测试环境不允许出现模型 key"


# --------------------------------------------------------------------------------------
# 防呆:绝不真实出网
# --------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _block_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """让真实的 HTTP 出网调用直接失败。

    拦在**传输层**而不是 `AsyncClient.send`:后者是 ASGI 测试客户端也要走的路径,
    拦它会把 app_client 一起打死。ASGITransport / WSGITransport 不经过这两个类,
    因此不受影响。
    """

    async def _blocked_async(self, request):
        raise AssertionError(f"测试中禁止真实网络请求: {request.method} {request.url}")

    def _blocked_sync(self, request):
        raise AssertionError(f"测试中禁止真实网络请求: {request.method} {request.url}")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _blocked_async)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _blocked_sync)


@pytest.fixture(autouse=True)
def _no_model_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """即使用户的 .env 里有真 key,测试进程里也必须是空的。"""
    monkeypatch.setattr(settings, "llm_api_key", "")


@pytest.fixture(autouse=True)
def _reset_login_throttle() -> None:
    """登录限流器是进程级状态,必须逐个测试清空。

    没有这一条,一个故意打爆限流的测试会让后面所有测试莫名其妙地拿到 429 ——
    而且失败信息看起来完全无关("登录成功"断言失败),排查时间会很长。
    """
    from backend.core.throttle import LOGIN_THROTTLE

    LOGIN_THROTTLE.clear_all()
    yield
    LOGIN_THROTTLE.clear_all()


# --------------------------------------------------------------------------------------
# 数据库
# --------------------------------------------------------------------------------------


def _stamp_head(sync_connection) -> None:
    """把 `alembic_version` 写成代码里的 head。

    参数是 `run_sync` 传进来的同步连接,类型注解给不出来(Alembic 要的就是它)。

    `create_all` 建出来的库在结构上与迁移的产物一致(这一点由
    `test_migration_matches_models.py` 盯着),但它**没有** `alembic_version` 那一行。
    而启动期检查(`assert_schema_current`)会把"没有这张表"读成"这个库从来没跑过
    迁移"并拒绝启动 —— 于是每一个走 lifespan 的测试都会失败,而失败原因与它们
    要验的东西毫无关系。

    所以这里补上那一行,让测试库长得和**迁移之后**的真实库一样。用 Alembic 自己的
    `MigrationContext.stamp` 而不是手写 INSERT:建表语句由它决定,将来 Alembic 换了
    写法这里跟着走,不会各写一份。
    """
    from alembic.config import Config
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    config = Config(str(Path(__file__).resolve().parents[2] / "backend" / "alembic.ini"))
    script = ScriptDirectory.from_config(config)
    MigrationContext.configure(sync_connection).stamp(script, script.get_current_head())


async def _rebuild_schema() -> None:
    """把库清空重建,并标成"已迁移到 head"。

    SQLite 删表时**会把外键约束当真**:`plan_nodes.parent_id` 是自引用,表里一旦
    躺着一个带子节点的节点,`DROP TABLE plan_nodes` 就会因为"还有别的行指着它"
    而失败。建表时那条保命的 `PRAGMA foreign_keys=ON` 在删表时反而成了阻碍,
    所以这里临时关掉,建完再打开。

    这个开关是**连接级**的(db/session.py 里也是按连接设的),不会漏到别的连接上。
    """
    async with engine.connect() as conn:
        await conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        await conn.run_sync(Base.metadata.drop_all)
        # `alembic_version` **不在** `Base.metadata` 里(它是 Alembic 自己的表),
        # 所以 drop_all 不会碰它。不显式删掉的话,上一个测试写进去的版本号会活到
        # 下一个测试 —— 而有的测试**正是**要把这一行改坏(见 test_health.py 里
        # "版本对不上时拒绝启动"),于是下一个测试的 stamp 会去解析一个不存在的
        # 版本号并抛 `Can't locate revision`,整个套件从这里开始成片报错。
        await conn.exec_driver_sql("DROP TABLE IF EXISTS alembic_version")
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_stamp_head)
        # 先把 Alembic 留下的事务收掉,再打开外键。
        #
        # 顺序不能反:SQLite 的规则是"事务里设 `PRAGMA foreign_keys` 是**空操作**",
        # 而 Alembic 的 stamp 执行了 DDL,连接上因此挂着一个真实的、开着的 BEGIN。
        # 在它之前发那条 ON 会被静默忽略,连接带着 `foreign_keys=OFF` 回到池子里,
        # 紧接着的测试从池里拿到它 —— 于是**整个测试进程里所有外键约束都不生效**,
        # 而没有任何东西会报错,只是"外键真在生效"那条断言在别的地方失败。
        await conn.commit()
        await conn.exec_driver_sql("PRAGMA foreign_keys=ON")

        # 所以这里确认它真的生效了。上面那个失败模式的可怕之处在于它是**静默**的。
        if not await conn.scalar(text("PRAGMA foreign_keys")):
            raise RuntimeError(
                "PRAGMA foreign_keys=ON 没有生效 —— 这条连接上的外键约束是假的,"
                "而接下来的测试会以为它是真的。"
            )
        await conn.commit()


@pytest.fixture(autouse=True)
async def _clean_db() -> AsyncIterator[None]:
    """每个测试一个干净的库。

    比"外层包一个事务然后回滚"更彻底:被测代码自己 commit 的事务不会被漏掉,也不会
    因为"反正外层会回滚"而侥幸通过。19 张表在 SQLite 上重建是毫秒级,不值得为省这点
    时间牺牲隔离。

    teardown 里 dispose 连接池:每个测试跑在各自的 event loop 上,而 aiosqlite 的连接
    绑定在创建它的 loop 上,不 dispose 会让下一个测试拿到属于已关闭 loop 的连接。
    """
    await _rebuild_schema()
    yield
    await engine.dispose()


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    """请求级会话。绝不吞异常 —— 数据库写失败必须炸出来。"""
    async with SessionLocal() as session:
        yield session


async def snapshot(db: AsyncSession) -> dict[str, int]:
    """各表当前行数。

    "非法输入不产生部分写入"这条只需要一行断言:

        before = await snapshot(db)
        ...
        assert await snapshot(db) == before

    比逐个表去数更可靠:漏掉一张表就等于漏掉一条回归。
    """
    counts: dict[str, int] = {}
    for name, table in sorted(Base.metadata.tables.items()):
        total = await db.scalar(select(func.count()).select_from(table))
        counts[name] = int(total or 0)
    return counts


# --------------------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------------------


@pytest.fixture
async def app_client() -> AsyncIterator[httpx.AsyncClient]:
    """走 ASGI 的 HTTP 客户端,且**真正执行 lifespan**。

    注意不能写成模块级的 `TestClient(app)`:那样启动钩子根本不会跑,于是
    "连不上数据库就拒绝启动"这条永远不会被测到 —— 而它恰恰是阶段 1 要建立的核心
    保证之一。
    """
    from backend.api.main import app

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


# --------------------------------------------------------------------------------------
# 账户助手
# --------------------------------------------------------------------------------------


@dataclass
class Account:
    """一个注册好了的账号,连带它的令牌与一个空间。

    做成 dataclass 而不是一堆散落的变量,是为了让跨账号测试读起来就是它想表达的意思:
    `account_b` 拿 `account_a.workspace_id` 去访问,期望 404。
    """

    id: str
    email: str
    password: str
    token: str
    session_id: str
    workspace_id: str

    @property
    def headers(self) -> dict[str, str]:
        return auth_headers(self.token)


def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------------------
# 假模型
# --------------------------------------------------------------------------------------


@dataclass
class FakeReasoner:
    """一个完全可编程的假模型。

    `calls` 是它收到过的每一个 `TurnContext`。**这就是"上下文里到底有没有当前日期、
    有没有简报、有没有历史"的检查点** —— 断言拼出来的提示词字符串太难读也容易碎,
    断言传进来的结构体则直接指向那个事实。

    最后一个用例跑完还会断言 `calls` 非空,否则一个注入漏了的测试会悄悄走真实分支
    而它可能仍然是绿的。
    """

    reply: str = "这是一条来自假模型的回复。"
    claims: tuple = ()
    #: 这一轮要提的变更。写成 dict 而不是 `CreateNodeAction(...)`,是为了让测试里
    #: 那些**故意非法**的用例(未知 op、悬空 n7、`2026-13-45`)能原样写出来 ——
    #: 它们本来就该是"模型吐了一段不合法 JSON"的形状,而不是一个构造不出来的模型对象。
    actions: tuple[dict, ...] = ()
    degraded: bool = False
    degraded_reason: object | None = None
    retryable: bool = False
    source: object | None = None
    calls: list = field(default_factory=list)

    async def reason(self, turn):
        from backend.agent.runtime.base import ReasoningResult
        from backend.db.models.enums import ModelSource

        self.calls.append(turn)
        return ReasoningResult(
            reply=self.reply,
            source=self.source or ModelSource.DIRECT_LLM,
            degraded=self.degraded,
            degraded_reason=self.degraded_reason,
            retryable=self.retryable,
            brief_claims=tuple(self.claims),
            actions=tuple(self.actions),
            request_id="fake-request",
            prompt_version="fake-v1",
            model_name="fake-model",
            latency_ms=1,
        )


@dataclass
class ExplodingReasoner:
    """会抛异常的假模型。

    真实实现按契约**永远不会**抛(见 agent/runtime/base.py),但"契约要是被违反了
    会怎样"仍然必须测:用户那句话是不是还在库里。如果两次提交的顺序写反了,
    这个测试会红,而 `FakeReasoner` 那条路是绿的 —— 这正是它存在的理由。
    """

    calls: list = field(default_factory=list)

    async def reason(self, turn):
        self.calls.append(turn)
        raise RuntimeError("模型适配器崩了")


@pytest.fixture
def use_reasoner(app_client: httpx.AsyncClient):
    """把 get_reasoner 换成给定的假实现,测试结束后撤掉。

    这里是整个"不需要模型 key 也能跑全套测试"的关键落点:换掉依赖,就再没有
    任何代码路径会去碰网络。
    """
    from backend.api.dependencies.agent import get_reasoner
    from backend.api.main import app

    def _use(reasoner) -> object:
        app.dependency_overrides[get_reasoner] = lambda: reasoner
        return reasoner

    yield _use
    app.dependency_overrides.pop(get_reasoner, None)


@pytest.fixture
def use_clock(app_client: httpx.AsyncClient):
    """把 `get_now` 钉在给定的**绝对时刻**上,测试结束后撤掉。

    免打扰时段(22:00–08:00)会把一部分提醒压住,于是"读提醒"的结果取决于跑它的
    那一刻。拿真实时钟当输入的话,测的就不是规则,而是"我们碰巧在哪个钟点跑的"。
    这里换掉的是**钟**,不是规则 —— 该压的照样压(见
    `api/dependencies/clock.py` 与 `services/reminder_service.py` 的 `_STATE_KINDS`)。

    传进来的是带时区的时刻;用户的时区仍然从 `user.timezone` 取,两者是两件事。
    """
    from backend.api.dependencies.clock import get_now
    from backend.api.main import app

    def _use(moment: datetime) -> datetime:
        app.dependency_overrides[get_now] = lambda: moment
        return moment

    yield _use
    app.dependency_overrides.pop(get_now, None)


DEFAULT_PASSWORD = "correct-horse-battery"


@pytest.fixture
def make_account(app_client: httpx.AsyncClient):
    """注册一个账号并建一个空间,返回 Account。

    刻意走**真实 HTTP 接口**而不是直接写库:这样它同时也在验证注册、登录、
    建空间这三条路径本身是通的。测试里出现"只有测试能构造出来的状态"是最常见的
    自欺方式之一。
    """

    async def _make(
        email: str = "a@example.com",
        password: str = DEFAULT_PASSWORD,
        display_name: str = "小途",
        *,
        workspace_title: str = "Python 学习",
        create_workspace: bool = True,
        timezone: str = "Asia/Shanghai",
    ) -> Account:
        response = await app_client.post(
            "/api/auth/register",
            json={
                "email": email,
                "password": password,
                "displayName": display_name,
                # 时区在这里是**账号的属性**,不是测试的开关:免打扰窗口按用户的
                # 当地时间算,所以"同一个绝对时刻,两个时区的用户一个在免打扰里、
                # 一个不在"是产品行为,得能验。
                "timezone": timezone,
            },
        )
        assert response.status_code == 201, response.text
        body = response.json()
        token = body["token"]

        workspace_id = ""
        if create_workspace:
            created = await app_client.post(
                "/api/workspaces",
                json={"title": workspace_title, "intent": "三个月内完成一个项目"},
                headers=auth_headers(token),
            )
            assert created.status_code == 201, created.text
            workspace_id = created.json()["workspace"]["id"]

        sessions = await app_client.get("/api/auth/sessions", headers=auth_headers(token))
        assert sessions.status_code == 200, sessions.text

        return Account(
            id=body["user"]["id"],
            # 用服务端返回的规范形式,不是传进去的那个 —— 注册会归一化邮箱,
            # 而账号对象应该描述"服务端认为你是谁",不是"我发了什么"。
            email=body["user"]["email"],
            password=password,
            token=token,
            session_id=sessions.json()[0]["id"],
            workspace_id=workspace_id,
        )

    return _make
