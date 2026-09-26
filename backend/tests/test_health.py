"""HTTP 探针。

必须走 `app_client` fixture 而不是模块级的 `TestClient(app)` —— 后者不会执行 ASGI
lifespan,于是"连不上数据库就拒绝启动"这条永远不会被测到,而它正是阶段 1 要建立的
核心保证之一。
"""

from __future__ import annotations


async def test_liveness_does_not_touch_the_database(app_client) -> None:
    """存活探针:进程活着就该 200,不查库。

    如果这里查库,那么数据库抖一下,编排系统就会把进程杀掉重启 —— 而重启并不解决
    数据库的问题,只会让故障扩大。
    """
    response = await app_client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["env"] == "development"


async def test_readiness_reports_database_and_reasoner_source(app_client) -> None:
    """就绪探针:数据库不可用时 503。"""
    response = await app_client.get("/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    assert body["database"]
    # 模型来源必须如实标注 —— 用户有权知道回复来自模型还是本地规则。
    assert body["reasoner"] == "rule"


# ---------------------------------------------------------------------------------
# 结构落后于代码时不许启动
# ---------------------------------------------------------------------------------
async def test_a_database_without_migrations_refuses_to_start(db) -> None:
    """**"连得上"和"能用"是两件事。**

    部署新代码时忘了 `alembic upgrade head`,库连得上、服务照常起来,然后在第一个
    碰到新列的请求上炸成一个跟真实原因毫无关系的 500;没有新列也能走的那些路径
    则照常返回,只是少了一栏,而没有任何东西会说不。

    这条断言的是"真的会拦",不是"有个函数叫这个名字":把版本表删掉,检查必须
    返回不一致。
    """
    from sqlalchemy import text

    from backend.db.health import check_schema_current

    ok, _ = await check_schema_current()
    assert ok, "测试库本该是'已迁移到 head'的样子 —— 见 conftest 的 _stamp_head"

    await db.execute(text("DROP TABLE alembic_version"))
    await db.commit()

    ok, reason = await check_schema_current()
    assert ok is False, "库没有迁移过,启动检查却放行了"
    assert reason and "alembic_version" in reason
    # 原因要说人话,并且指向那个能解决问题的命令。
    assert "alembic upgrade head" in reason


async def test_a_database_behind_the_head_refuses_to_start(db) -> None:
    """版本对不上时也要拦住,而且要说清楚两边各是什么。

    只说"迁移状态不对"的话,看到的人还是不知道该往哪边改。
    """
    from sqlalchemy import text

    from backend.db.health import alembic_head, check_schema_current

    await db.execute(text("UPDATE alembic_version SET version_num = 'deadbeef'"))
    await db.commit()

    ok, reason = await check_schema_current()
    assert ok is False
    assert reason is not None
    assert "deadbeef" in reason, "没说清楚库里现在是哪一版"
    assert alembic_head() in reason, "没说清楚代码要的是哪一版"
