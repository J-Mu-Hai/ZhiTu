"""画布上的关系与布局。

## 这一组在防什么

三种边在界面上是同一种东西,存储上却是两张表(`dependencies` 与 `node_relations`)。
那个分叉由 `plan_service.relation_of` 一处抹平,而这个文件证明抹平之后**行为**是对的:

- `related_to` **无向** —— 从 A 拖到 B 和从 B 拖到 A 是同一条边、同一行。
- `influences` 有向 —— 两个方向是两条边。
- `depends_on` 参与排期,所以它继续受环检测约束(409);另外两种允许成环。
- 布局**不是计划** —— 位置与视口不写 `plan_revisions`、不发 `domain_events`。

最后一条是这个文件里最重要的断言。"顺手记一个版本"是个看起来负责的动作,代价是
版本历史被拖动淹没 —— 而这件事没有任何东西会报错,只会在用户想复盘"哪一版把我的
排期改了"时才发现,那时他已经没法从两百个版本里翻出来了。

## 关于 `_counts`

"什么都没写"这一句只有在**真的读到此刻的库**时才成立。这个文件里每个否定断言都走
`_counts`,它的注释里写了为什么(以及为什么在这套驱动上其实还没轮到那个理由出场)。
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models import NodePosition, NodeRelation, PlanRevision
from backend.services import proposal_validation as codes
from backend.tests.conftest import FakeReasoner, snapshot


# ---------------------------------------------------------------------------------
# 助手
# ---------------------------------------------------------------------------------
async def _plan(client: httpx.AsyncClient, account, workspace_id: str | None = None) -> dict:
    response = await client.get(
        f"/api/workspaces/{workspace_id or account.workspace_id}/plan", headers=account.headers
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _root(
    client: httpx.AsyncClient, account, workspace_id: str | None = None
) -> str:
    plan = await _plan(client, account, workspace_id)
    return next(node["id"] for node in plan["nodes"] if node["parentId"] is None)


async def _create_nodes(
    client: httpx.AsyncClient, account, *titles: str, workspace_id: str | None = None
) -> dict[str, str]:
    """在根目标下面建几个节点,返回 标题 -> id。

    刻意走**接口**而不是直接写库:测试里出现"只有测试能构造出来的状态"是最常见的
    自欺方式之一(同 conftest.make_account 的理由)。
    """
    target = workspace_id or account.workspace_id
    parent = await _root(client, account, target)
    ids: dict[str, str] = {}
    for title in titles:
        response = await client.post(
            f"/api/workspaces/{target}/nodes",
            json={"parentId": parent, "title": title},
            headers=account.headers,
        )
        assert response.status_code == 201, response.text
        ids[title] = response.json()["node"]["id"]
    return ids


async def _new_workspace(client: httpx.AsyncClient, account, title: str) -> str:
    """同一个用户的**第二个**空间。用来把"空间隔离"和"账号隔离"分开测。"""
    response = await client.post(
        "/api/workspaces",
        json={"title": title, "intent": "另一个目标"},
        headers=account.headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["workspace"]["id"]


async def _connect(
    client: httpx.AsyncClient,
    account,
    source: str,
    target: str,
    relation_type: str,
    note: str | None = None,
) -> httpx.Response:
    body: dict[str, object] = {
        "sourceId": source,
        "targetId": target,
        "relationType": relation_type,
    }
    if note is not None:
        body["note"] = note
    return await client.post(
        f"/api/workspaces/{account.workspace_id}/relations",
        json=body,
        headers=account.headers,
    )


async def _patch_relation(
    client: httpx.AsyncClient, account, relation_id: str, values: dict
) -> httpx.Response:
    return await client.patch(
        f"/api/workspaces/{account.workspace_id}/relations/{relation_id}",
        json=values,
        headers=account.headers,
    )


async def _delete_relation(client: httpx.AsyncClient, account, relation_id: str) -> httpx.Response:
    return await client.delete(
        f"/api/workspaces/{account.workspace_id}/relations/{relation_id}",
        headers=account.headers,
    )


def _edges(plan: dict) -> list[dict]:
    return plan["relations"]


def _position(node_id: str, x: float, y: float) -> dict[str, object]:
    return {"nodeId": node_id, "x": x, "y": y}


def _viewport(scope_node_id: str, zoom: float, pan_x: float, pan_y: float) -> dict[str, object]:
    return {"scopeNodeId": scope_node_id, "zoom": zoom, "panX": pan_x, "panY": pan_y}


async def _put_layout(
    client: httpx.AsyncClient,
    account,
    *,
    positions: list[dict] | None = None,
    viewports: list[dict] | None = None,
) -> httpx.Response:
    return await client.put(
        f"/api/workspaces/{account.workspace_id}/layout",
        json={"positions": positions or [], "viewports": viewports or []},
        headers=account.headers,
    )


async def _read_layout(client: httpx.AsyncClient, account) -> dict:
    response = await client.get(
        f"/api/workspaces/{account.workspace_id}/layout", headers=account.headers
    )
    assert response.status_code == 200, response.text
    return response.json()


def _positions(body: dict) -> dict[str, tuple[float, float]]:
    return {item["nodeId"]: (item["x"], item["y"]) for item in body["positions"]}


async def _counts(db: AsyncSession) -> dict[str, int]:
    """各表行数。**先结束当前读事务,再数。**

    "什么都没写"这类断言有一个不响的失效方式:`before` 与 `after` 读的是**同一个快照**
    的话,`assert after == before` 无论被断言的那段代码写了多少行都成立 —— 那一刻它
    不再是断言,而是一句口号。

    实测(2026-09-26,提交 `6c47a62` 前后各一次):在当前这套 SQLite + aiosqlite 上,
    那个失效方式**没有发生** —— 同一个 `db` 会话里,HTTP 写完再读,新行立刻可见。
    原因是 pysqlite 默认只在 DML 之前隐式 BEGIN,纯 SELECT 跑在自动提交里,不钉快照。
    这个文件里所有"什么都没写"的断言因此是**能失败的**,不是形同虚设。

    那为什么还要显式 `rollback()`:上面那句结论依赖**驱动的实现细节**,不是这套测试
    自己保证的东西。换连接串、换隔离级别、换驱动,它就可能翻过来,而翻过来时不会有
    任何东西报错 —— 只会让一批否定断言悄悄变成恒真。多一行,把这个性质放在这里,
    而不是放在"pysqlite 碰巧这么做"上。
    """
    await db.rollback()
    return await snapshot(db)


# ---------------------------------------------------------------------------------
# 关系:三种边,两种存储
# ---------------------------------------------------------------------------------
async def test_a_related_to_edge_round_trips(app_client: httpx.AsyncClient, make_account) -> None:
    account = await make_account()
    ids = await _create_nodes(app_client, account, "读文档", "写笔记")

    created = await _connect(
        app_client, account, ids["读文档"], ids["写笔记"], "related_to", note="两条线一起推"
    )
    assert created.status_code == 201, created.text
    edge = created.json()

    assert edge["relationType"] == "related_to"
    assert edge["note"] == "两条线一起推"
    assert edge["origin"] == "user"
    # `dependencies` 才有 `lag_days`,另外两种没有 —— 接口上仍给这个字段,值为 null。
    assert edge["lagDays"] is None

    assert [item["id"] for item in _edges(await _plan(app_client, account))] == [edge["id"]]


async def test_related_to_is_undirected(app_client: httpx.AsyncClient, make_account) -> None:
    """同一对节点换个方向再连一次 -> 还是那一条边。

    少了 `_endpoints` 那个按 UUID 整数序的规范化,唯一约束只能拦住"同一方向连两次",
    同一对节点会并存两行 —— 画布上是两条完全重叠的虚线,而用户点哪一条都"有反应"。
    """
    account = await make_account()
    ids = await _create_nodes(app_client, account, "A", "B")

    forward = await _connect(app_client, account, ids["A"], ids["B"], "related_to")
    backward = await _connect(app_client, account, ids["B"], ids["A"], "related_to")

    assert forward.status_code == 201, forward.text
    assert backward.status_code == 201, backward.text
    assert backward.json()["id"] == forward.json()["id"]

    edges = _edges(await _plan(app_client, account))
    assert len(edges) == 1
    # 存的方向是规范化之后的那个 —— 按 UUID 的整数序,不是按字符串序。
    expected = tuple(sorted((ids["A"], ids["B"]), key=lambda value: uuid.UUID(value).int))
    assert (edges[0]["sourceId"], edges[0]["targetId"]) == expected


async def test_influences_keeps_its_direction(app_client: httpx.AsyncClient, make_account) -> None:
    """「影响」是有向的:两个方向是两条边,同一方向连两次是同一条。"""
    account = await make_account()
    ids = await _create_nodes(app_client, account, "英语", "数学")

    one = await _connect(app_client, account, ids["英语"], ids["数学"], "influences")
    two = await _connect(app_client, account, ids["数学"], ids["英语"], "influences")
    again = await _connect(app_client, account, ids["英语"], ids["数学"], "influences")

    assert one.status_code == 201 and two.status_code == 201 and again.status_code == 201
    assert one.json()["id"] != two.json()["id"]
    assert again.json()["id"] == one.json()["id"]
    assert len(_edges(await _plan(app_client, account))) == 2


@pytest.mark.parametrize("relation_type", ["related_to", "influences", "depends_on"])
async def test_a_node_cannot_relate_to_itself(
    app_client: httpx.AsyncClient, make_account, relation_type: str
) -> None:
    """自连一律 400 —— 包括 `depends_on`,它是三种类型里唯一会改变排期的那种。"""
    account = await make_account()
    ids = await _create_nodes(app_client, account, "A")

    response = await _connect(app_client, account, ids["A"], ids["A"], relation_type)

    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "INVALID_INPUT"


async def test_an_unknown_relation_type_lists_the_valid_ones(
    app_client: httpx.AsyncClient, make_account
) -> None:
    """类型写错了 -> 400,并且报错里要能看见**全部**合法取值。

    包括他其实该用的那个 `depends_on`:只列 `node_relations` 里那两种的话,
    一个想连前置关系的用户会以为"前置"不能连。
    """
    account = await make_account()
    ids = await _create_nodes(app_client, account, "A", "B")

    response = await _connect(app_client, account, ids["A"], ids["B"], "blocks")

    assert response.status_code == 400, response.text
    error = response.json()["error"]
    assert error["code"] == "INVALID_INPUT"
    for value in ("depends_on", "related_to", "influences"):
        assert value in error["message"], error["message"]


# ---------------------------------------------------------------------------------
# 环检测:只属于排期那条路
# ---------------------------------------------------------------------------------
async def test_a_dependency_cycle_is_refused_and_writes_nothing(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession
) -> None:
    """**验收场景 4。** A 是 C 的前置、C 是 B 的前置,再让 B 当前置 -> 409。

    拒绝时必须**一行都没写**。写了一半的环比直接接受还糟:那是一个排不出来的计划,
    而用户看到的是"操作失败"。
    """
    account = await make_account()
    ids = await _create_nodes(app_client, account, "A", "B", "C")

    first = await _connect(app_client, account, ids["A"], ids["C"], "depends_on")
    second = await _connect(app_client, account, ids["C"], ids["B"], "depends_on")
    assert first.status_code == 201 and second.status_code == 201

    before = await _counts(db)
    response = await _connect(app_client, account, ids["B"], ids["A"], "depends_on")
    after = await _counts(db)

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "DEPENDENCY_CYCLE"

    assert after == before, "被拒绝的依赖在计划里留下了痕迹"
    # 反向控制:这两条边真的看得见 —— 否则上面那句"什么都没写"可能只是因为读的是旧快照。
    assert len((await _plan(app_client, account))["dependencies"]) == 2


@pytest.mark.parametrize(
    ("relation_type", "expected_edges"),
    [
        ("related_to", 1),
        ("influences", 2),
    ],
)
async def test_the_other_two_may_form_a_cycle(
    app_client: httpx.AsyncClient, make_account, relation_type: str, expected_edges: int
) -> None:
    """互相影响、互相关联都是正常的 —— 环检测只属于排期。

    两条都不查环。条数不同是因为「相关」无向(两个方向落到同一行),而「影响」有向
    (两个方向是两条边)—— 但两者都不是错误。
    """
    account = await make_account()
    ids = await _create_nodes(app_client, account, "A", "B")

    first = await _connect(app_client, account, ids["A"], ids["B"], relation_type)
    second = await _connect(app_client, account, ids["B"], ids["A"], relation_type)

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert len(_edges(await _plan(app_client, account))) == expected_edges


# ---------------------------------------------------------------------------------
# `depends_on` 从关系接口进来,落在排期那张表上
# ---------------------------------------------------------------------------------
async def test_depends_on_goes_to_the_scheduling_table(
    app_client: httpx.AsyncClient, make_account
) -> None:
    """它必须**同时**出现在 `relations` 与 `dependencies` 里。

    前者是画布画的那份投影(界面不 join 两张表),后者是排期真正的输入。少任何一份
    都是一个具体的坏结果:少了后者,画布上有一条箭头而排期当它不存在。
    """
    account = await make_account()
    ids = await _create_nodes(app_client, account, "A", "B")

    created = await _connect(app_client, account, ids["A"], ids["B"], "depends_on")
    assert created.status_code == 201, created.text
    edge = created.json()

    assert edge["relationType"] == "depends_on"
    assert edge["lagDays"] == 0
    # `dependencies` 没有 origin 这一列 —— 这里给 null,而不是编一个 "user"
    # (同 plan_service.dependency_to_relation_dict 的注释)。
    assert edge["origin"] is None

    plan = await _plan(app_client, account)
    assert [(item["predecessorId"], item["successorId"]) for item in plan["dependencies"]] == [
        (ids["A"], ids["B"])
    ]
    assert [item["id"] for item in _edges(plan)] == [edge["id"]]

    again = await _connect(app_client, account, ids["A"], ids["B"], "depends_on")
    assert again.json()["id"] == edge["id"]
    assert len((await _plan(app_client, account))["dependencies"]) == 1


async def test_a_dependency_cannot_carry_a_note(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession
) -> None:
    """前置关系没有说明列 —— 拒绝,而不是把用户刚写的那句话丢掉。"""
    account = await make_account()
    ids = await _create_nodes(app_client, account, "A", "B")

    before = await _counts(db)
    response = await _connect(
        app_client, account, ids["A"], ids["B"], "depends_on", note="因为要先学完基础"
    )
    after = await _counts(db)

    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "INVALID_INPUT"
    assert after == before


async def test_a_dependency_can_be_removed_through_the_relations_route(
    app_client: httpx.AsyncClient, make_account
) -> None:
    """接口上三种边共用一个 id 空间,所以按 id 删的时候两张表都要能删掉。

    这条边在 `dependencies` 里 —— 走的是 `remove_relation` 里那个 `isinstance` 分支。
    分支写漏了的表现是"删不掉,但接口返回 204"。
    """
    account = await make_account()
    ids = await _create_nodes(app_client, account, "A", "B")
    edge = (await _connect(app_client, account, ids["A"], ids["B"], "depends_on")).json()

    removed = await _delete_relation(app_client, account, edge["id"])
    assert removed.status_code == 204, removed.text

    plan = await _plan(app_client, account)
    assert plan["dependencies"] == []
    assert _edges(plan) == []


# ---------------------------------------------------------------------------------
# 改与删
# ---------------------------------------------------------------------------------
async def test_a_relation_note_can_be_written_and_cleared(
    app_client: httpx.AsyncClient, make_account
) -> None:
    """**"没传"与"传了 null"必须是两件事** —— 同 `UpdateNodeRequest` 那条纪律。"""
    account = await make_account()
    ids = await _create_nodes(app_client, account, "A", "B")
    edge = (await _connect(app_client, account, ids["A"], ids["B"], "related_to")).json()

    written = await _patch_relation(app_client, account, edge["id"], {"note": "共用同一份数据"})
    assert written.status_code == 200, written.text
    assert written.json()["note"] == "共用同一份数据"

    untouched = await _patch_relation(app_client, account, edge["id"], {})
    assert untouched.status_code == 200, untouched.text
    assert untouched.json()["note"] == "共用同一份数据", "空请求把说明清掉了"

    cleared = await _patch_relation(app_client, account, edge["id"], {"note": None})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["note"] is None


async def test_a_relation_can_switch_between_the_two_canvas_types(
    app_client: httpx.AsyncClient, make_account
) -> None:
    """「相关」和「影响」之间可以换,而且**换类型不会把说明弄丢**。

    用户从 B 拖到 A 连的「相关」,存下来是规范化之后的顺序(按 UUID 整数序),因为
    那条边本来就没有方向。换成有向的「影响」时沿用那个顺序 —— 这里不存在"丢掉了
    用户的方向"这回事:他从没表达过方向。断言"存的那两个端点没变",是因为换类型
    顺带改了端点的含义,那是这一版最容易悄悄写错的一步。
    """
    account = await make_account()
    ids = await _create_nodes(app_client, account, "A", "B")
    edge = (
        await _connect(app_client, account, ids["B"], ids["A"], "related_to", note="写在这里的解释")
    ).json()

    switched = await _patch_relation(
        app_client, account, edge["id"], {"relationType": "influences"}
    )

    assert switched.status_code == 200, switched.text
    assert switched.json()["id"] == edge["id"]
    assert switched.json()["relationType"] == "influences"
    assert switched.json()["note"] == "写在这里的解释"
    assert (switched.json()["sourceId"], switched.json()["targetId"]) == (
        edge["sourceId"],
        edge["targetId"],
    )

    assert [item["relationType"] for item in _edges(await _plan(app_client, account))] == [
        "influences"
    ]


@pytest.mark.parametrize("relation_type", ["related_to", "influences", "depends_on"])
async def test_switching_a_relation_to_depends_on_is_refused(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession, relation_type: str
) -> None:
    """跨表搬家会丢掉用户写的说明 —— 明确拒绝,而不是静默丢。

    `depends_on` 那一条走的是另一半:前置关系**在画布上根本改不了**(它连说明都没有,
    能被改的只有 `lag_days`,那是排期参数)。两者返回同一个 code,因为对用户来说是
    同一件事:"这一版不支持这么改"。
    """
    account = await make_account()
    ids = await _create_nodes(app_client, account, "A", "B")
    edge = (
        await _connect(app_client, account, ids["A"], ids["B"], relation_type, note="写在这里的解释")
        if relation_type != "depends_on"
        else await _connect(app_client, account, ids["A"], ids["B"], relation_type)
    ).json()

    before = await _counts(db)
    response = await _patch_relation(app_client, account, edge["id"], {"relationType": "depends_on"})
    after = await _counts(db)

    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "RELATION_TYPE_CHANGE_UNSUPPORTED"
    assert after == before, "被拒绝的改动在计划里留下了痕迹"


async def test_a_type_change_onto_an_existing_edge_is_refused(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession
) -> None:
    """A 与 B 之间既有「相关」又有「影响」,把「影响」也改成「相关」-> 拒绝。

    拒在**写之前**:让唯一约束在 commit 时抛的话,用户看到的是一句和关系毫无关系的
    数据库异常。
    """
    account = await make_account()
    ids = await _create_nodes(app_client, account, "A", "B")
    await _connect(app_client, account, ids["A"], ids["B"], "related_to")
    influenced = (await _connect(app_client, account, ids["A"], ids["B"], "influences")).json()

    before = await _counts(db)
    response = await _patch_relation(
        app_client, account, influenced["id"], {"relationType": "related_to"}
    )
    after = await _counts(db)

    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "INVALID_INPUT"
    assert after == before
    assert {item["relationType"] for item in _edges(await _plan(app_client, account))} == {
        "related_to",
        "influences",
    }


async def test_deleting_a_relation_keeps_both_nodes(
    app_client: httpx.AsyncClient, make_account
) -> None:
    """**删边不删节点。** 边没了,两端都还在。"""
    account = await make_account()
    ids = await _create_nodes(app_client, account, "A", "B")
    edge = (await _connect(app_client, account, ids["A"], ids["B"], "related_to")).json()

    removed = await _delete_relation(app_client, account, edge["id"])
    assert removed.status_code == 204, removed.text

    plan = await _plan(app_client, account)
    assert _edges(plan) == []
    assert set(ids.values()) <= {node["id"] for node in plan["nodes"]}

    # 再删一次 -> 404。这个接口收的是 id,不是"一对节点",所以没有"它已经没了"
    # 这种达成目标的语义(对比 `DELETE /dependencies`,那一条返回 204)。
    again = await _delete_relation(app_client, account, edge["id"])
    assert again.status_code == 404, again.text
    assert again.json()["error"]["code"] == "RELATION_NOT_FOUND"


async def test_a_soft_deleted_node_takes_its_edges_off_the_canvas(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession
) -> None:
    """节点是**软删除**的,外键的 CASCADE 不会触发 —— "两端都活着"必须显式过滤。

    少了那个过滤,画布上会画出一条连向不存在节点的线,而它会一直画在那里,
    除非有人去查数据库。
    """
    account = await make_account()
    ids = await _create_nodes(app_client, account, "A", "B")
    await _connect(app_client, account, ids["A"], ids["B"], "related_to")
    assert len(_edges(await _plan(app_client, account))) == 1

    deleted = await app_client.delete(
        f"/api/workspaces/{account.workspace_id}/nodes/{ids['B']}", headers=account.headers
    )
    assert deleted.status_code == 200, deleted.text

    assert _edges(await _plan(app_client, account)) == []
    # 行还在库里 —— 过滤是投影做的,不是删除做的。否则这条测试证明不了那个过滤器存在。
    assert (await _counts(db))["node_relations"] == 1


async def test_a_relation_lands_in_the_revision_snapshot(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession
) -> None:
    """连一条边 = 一次真正的计划变更:版本号前进,快照里有它。

    快照是"V3 是什么样"的唯一答案。少了关系这一半不会有任何东西报错,只会在有人
    想对比两个版本时才发现 —— 而那时已经补不回来了。
    """
    account = await make_account()
    ids = await _create_nodes(app_client, account, "A", "B")
    before = (await _plan(app_client, account))["revisionVersion"]

    edge = (
        await _connect(app_client, account, ids["A"], ids["B"], "related_to", note="一起推")
    ).json()

    assert (await _plan(app_client, account))["revisionVersion"] > before

    revision = await db.scalar(
        select(PlanRevision)
        .where(PlanRevision.workspace_id == uuid.UUID(account.workspace_id))
        .order_by(PlanRevision.version.desc())
    )
    assert revision is not None
    assert [item["id"] for item in revision.snapshot["relations"]] == [edge["id"]]
    assert revision.snapshot["relations"][0]["note"] == "一起推"


# ---------------------------------------------------------------------------------
# 隔离:空间与账号
# ---------------------------------------------------------------------------------
async def test_a_relation_cannot_span_two_workspaces(
    app_client: httpx.AsyncClient, make_account
) -> None:
    """同一个用户的**两个空间**之间连不了边。

    跨账号那条用例走不到这里:它第一道门就被空间归属挡下了。这一条过得了那道门,
    被挡在"节点必须属于这个空间"上 —— 那是另一段代码。
    """
    account = await make_account()
    ids = await _create_nodes(app_client, account, "A")
    other = await _new_workspace(app_client, account, "另一个空间")
    stranger = await _root(app_client, account, other)

    response = await _connect(app_client, account, ids["A"], stranger, "related_to")

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "NODE_NOT_FOUND"


async def test_a_relation_id_from_another_workspace_is_not_found(
    app_client: httpx.AsyncClient, make_account
) -> None:
    """边属于**空间**,不属于人。同一个用户另一个空间里的边,拿过来同样 404。"""
    account = await make_account()
    other = await _new_workspace(app_client, account, "另一个空间")
    theirs = await _create_nodes(app_client, account, "A", "B", workspace_id=other)
    edge = (
        await app_client.post(
            f"/api/workspaces/{other}/relations",
            json={
                "sourceId": theirs["A"],
                "targetId": theirs["B"],
                "relationType": "related_to",
            },
            headers=account.headers,
        )
    ).json()

    # 先证明这条边在它自己的空间里是通得过的 —— 否则下面的 404 可能只是因为 id 写错了。
    mine = await app_client.patch(
        f"/api/workspaces/{other}/relations/{edge['id']}",
        json={"note": "在这里是改得动的"},
        headers=account.headers,
    )
    assert mine.status_code == 200, mine.text

    response = await _patch_relation(app_client, account, edge["id"], {"note": "跨空间改一下"})
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "RELATION_NOT_FOUND"


@pytest.mark.parametrize("method", ["PATCH", "DELETE"])
async def test_relations_are_scoped_to_the_workspace(
    app_client: httpx.AsyncClient, make_account, method: str
) -> None:
    """B 拿 A 的空间 id + A 的边 id -> 404,而 A 自己改得动、删得掉。

    `test_authz_matrix.py` 里那张跨账号表覆盖不到它:那张表要求"A 用同一个路径必须
    成功",而这两条路径收的是 `{relation_id}`,拿一个假的 id 时 A 自己也是 404。
    所以这里先真的连出一条边 —— 反向断言才成立(少了它,B 的 404 完全可能是因为
    id 写错了,那样这条测试是绿的而归属校验一行都没被验证)。
    """
    account_a = await make_account(email="a@example.com")
    account_b = await make_account(email="b@example.com", workspace_title="英语")
    ids = await _create_nodes(app_client, account_a, "A", "B")
    edge = (await _connect(app_client, account_a, ids["A"], ids["B"], "related_to")).json()

    url = f"/api/workspaces/{account_a.workspace_id}/relations/{edge['id']}"
    body = {"note": "偷偷改一下"} if method == "PATCH" else None

    theirs = await app_client.request(method, url, json=body, headers=account_b.headers)
    # 403 会确认"这个 id 存在但不属于你",等于把系统里有哪些空间告诉了任何人。
    assert theirs.status_code == 404, theirs.text
    assert theirs.status_code != 403
    assert theirs.json()["error"]["code"] == "WORKSPACE_NOT_FOUND"

    mine = await app_client.request(method, url, json=body, headers=account_a.headers)
    if method == "PATCH":
        assert mine.status_code == 200, mine.text
        assert mine.json()["note"] == "偷偷改一下"
    else:
        assert mine.status_code == 204, mine.text


# ---------------------------------------------------------------------------------
# 布局
# ---------------------------------------------------------------------------------
async def test_a_new_space_has_an_empty_layout(
    app_client: httpx.AsyncClient, make_account
) -> None:
    """没摆过就是空的 —— 不是 404,也不是一份"默认布局"。

    服务端不发明坐标:任何一种默认摆法在界面上的表现都是"用户第一次打开时节点
    已经被人摆过了",而那个人不存在。
    """
    account = await make_account()

    assert await _read_layout(app_client, account) == {"positions": [], "viewports": []}


async def test_layout_round_trips(app_client: httpx.AsyncClient, make_account) -> None:
    account = await make_account()
    ids = await _create_nodes(app_client, account, "A", "B")
    root = await _root(app_client, account)

    saved = await _put_layout(
        app_client,
        account,
        positions=[_position(ids["A"], 120.5, -40.0), _position(ids["B"], 300.0, 40.0)],
        viewports=[_viewport(root, 0.75, -120.0, 33.0)],
    )
    assert saved.status_code == 200, saved.text

    body = await _read_layout(app_client, account)
    assert _positions(body) == {ids["A"]: (120.5, -40.0), ids["B"]: (300.0, 40.0)}
    assert body["viewports"] == [_viewport(root, 0.75, -120.0, 33.0)]
    # 返回的是**提交之后的完整布局**,不是"成功"两个字 —— 客户端不必猜存进去的是什么。
    assert saved.json() == body


async def test_layout_is_not_a_plan_change(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession
) -> None:
    """**这一组里最重要的断言。** 布局不写 `plan_revisions`,也不发 `domain_events`。

    理由见 `services/layout_service.py` 开头:"顺手记一个版本"是个看起来负责的动作,
    代价是版本历史被拖动淹没 —— 而没有任何东西会报错,只会在用户想复盘"哪一版把我
    的排期改了"时才发现,那时他已经没法从两百个版本里翻出来了。
    """
    account = await make_account()
    ids = await _create_nodes(app_client, account, "A", "B")
    root = await _root(app_client, account)
    before_version = (await _plan(app_client, account))["revisionVersion"]

    before = await _counts(db)
    saved = await _put_layout(
        app_client,
        account,
        positions=[_position(ids["A"], 10.0, 10.0), _position(ids["B"], 20.0, 20.0)],
        viewports=[_viewport(root, 1.2, 5.0, 5.0)],
    )
    after = await _counts(db)

    assert saved.status_code == 200, saved.text
    assert after["plan_revisions"] == before["plan_revisions"]
    assert after["domain_events"] == before["domain_events"]
    # 版本号也不动 —— 它是"计划改过几次"的计数。
    assert (await _plan(app_client, account))["revisionVersion"] == before_version
    # 反向控制:这一批确实写进去了,否则上面几句可能只是因为什么都没发生。
    assert after["node_positions"] == before["node_positions"] + 2
    assert after["scope_viewports"] == before["scope_viewports"] + 1


async def test_layout_does_not_delete_what_it_was_not_given(
    app_client: httpx.AsyncClient, make_account
) -> None:
    """在子空间里保存一次,不能把别的层级的摆位删掉。

    位置按 `(用户, 空间, 节点)` 存,不按 scope 分。这里做全量替换的话,提交一次就会
    删掉其他所有层级的位置 —— 而它看起来完全正常,直到用户返回上一层发现节点全叠在
    一起了。
    """
    account = await make_account()
    ids = await _create_nodes(app_client, account, "A", "B")

    first = await _put_layout(
        app_client,
        account,
        positions=[_position(ids["A"], 1.0, 1.0), _position(ids["B"], 2.0, 2.0)],
    )
    assert first.status_code == 200, first.text

    second = await _put_layout(app_client, account, positions=[_position(ids["A"], 9.0, 9.0)])
    assert second.status_code == 200, second.text

    body = await _read_layout(app_client, account)
    assert _positions(body) == {ids["A"]: (9.0, 9.0), ids["B"]: (2.0, 2.0)}


async def test_saving_the_same_layout_twice_does_not_grow_the_tables(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession
) -> None:
    """幂等:同一份提交发两遍,结果一样,行数也不变(是 upsert,不是 insert)。"""
    account = await make_account()
    ids = await _create_nodes(app_client, account, "A")
    root = await _root(app_client, account)
    payload = {
        "positions": [_position(ids["A"], 3.0, 4.0)],
        "viewports": [_viewport(root, 1.0, 0.0, 0.0)],
    }

    assert (await _put_layout(app_client, account, **payload)).status_code == 200
    assert (await _put_layout(app_client, account, **payload)).status_code == 200

    counts = await _counts(db)
    assert counts["node_positions"] == 1
    assert counts["scope_viewports"] == 1


async def test_an_unknown_node_rejects_the_whole_batch(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession
) -> None:
    """一个 id 不认识 -> 整批拒绝,一条位置都不写。

    一次提交里的位置是**同一个瞬间的画面**。写一半会让画布显示出一个从未存在过的
    布局 —— 一半是新位置、一半是旧位置,而用户没有办法知道哪些生效了。
    """
    account = await make_account()
    ids = await _create_nodes(app_client, account, "A")
    ghost = str(uuid.uuid4())

    before = await _counts(db)
    response = await _put_layout(
        app_client,
        account,
        positions=[_position(ids["A"], 1.0, 1.0), _position(ghost, 2.0, 2.0)],
    )
    after = await _counts(db)

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "NODE_NOT_FOUND"
    assert after["node_positions"] == before["node_positions"], "整批拒绝了,却写了一半"
    assert (await _read_layout(app_client, account))["positions"] == []


async def test_a_node_from_another_workspace_cannot_be_placed(
    app_client: httpx.AsyncClient, make_account
) -> None:
    account = await make_account()
    other = await _new_workspace(app_client, account, "另一个空间")
    stranger = await _root(app_client, account, other)

    response = await _put_layout(app_client, account, positions=[_position(stranger, 1.0, 1.0)])

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "NODE_NOT_FOUND"


async def test_a_deleted_node_cannot_be_placed(
    app_client: httpx.AsyncClient, make_account
) -> None:
    """摆一个已经软删除的节点 -> 404,而不是"写进去但永远读不出来"。"""
    account = await make_account()
    ids = await _create_nodes(app_client, account, "A")
    deleted = await app_client.delete(
        f"/api/workspaces/{account.workspace_id}/nodes/{ids['A']}", headers=account.headers
    )
    assert deleted.status_code == 200, deleted.text

    response = await _put_layout(app_client, account, positions=[_position(ids["A"], 1.0, 1.0)])

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "NODE_NOT_FOUND"


async def test_a_soft_deleted_node_position_is_not_returned(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession
) -> None:
    """已删节点的位置不返回 —— 否则前端会去摆一批它看不见的节点。

    行仍然在库里(节点是软删除的),这里证明过滤确实发生在读取那一步。
    """
    account = await make_account()
    ids = await _create_nodes(app_client, account, "A")
    saved = await _put_layout(app_client, account, positions=[_position(ids["A"], 7.0, 8.0)])
    assert saved.status_code == 200, saved.text

    deleted = await app_client.delete(
        f"/api/workspaces/{account.workspace_id}/nodes/{ids['A']}", headers=account.headers
    )
    assert deleted.status_code == 200, deleted.text

    assert (await _read_layout(app_client, account))["positions"] == []
    assert (await _counts(db))["node_positions"] == 1


async def test_layout_rows_belong_to_one_user(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession
) -> None:
    """位置按 `(用户, 空间, 节点)` 存,读的时候也必须按用户过滤。

    这条**直接写库构造**了一个"B 在 A 的空间里有一行位置"的状态,因为产品上还没有
    共享空间,只有测试能造出它。它值得这么造:少了 `user_id` 那个过滤条件,当前
    一切都照常工作,而那个条件正是"每人一份布局"的全部实现 —— 将来真出现多人协作时,
    没有任何测试会红。

    正向控制也在里面:A 自己那一行必须读得到,否则"读不到 B 的"可能只是因为什么都没读出来。
    """
    account_a = await make_account(email="a@example.com")
    account_b = await make_account(email="b@example.com", workspace_title="英语")
    ids = await _create_nodes(app_client, account_a, "A")
    root = await _root(app_client, account_a)

    db.add_all(
        [
            NodePosition(
                user_id=uuid.UUID(account_a.id),
                workspace_id=uuid.UUID(account_a.workspace_id),
                node_id=uuid.UUID(ids["A"]),
                x=1.0,
                y=1.0,
            ),
            NodePosition(
                user_id=uuid.UUID(account_b.id),
                workspace_id=uuid.UUID(account_a.workspace_id),
                node_id=uuid.UUID(root),
                x=99.0,
                y=99.0,
            ),
        ]
    )
    await db.commit()

    body = await _read_layout(app_client, account_a)
    assert _positions(body) == {ids["A"]: (1.0, 1.0)}


async def test_layout_positions_are_counted_per_row_not_per_request(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession
) -> None:
    """同一批里同一个节点出现两次 -> 只留一行(后一个赢)。

    客户端节流时可能把同一个节点的两个中间位置打包发上来。真让它写两行的话,唯一
    约束会在 commit 时抛 —— 用户看到的是一句和拖动毫无关系的数据库异常。
    """
    account = await make_account()
    ids = await _create_nodes(app_client, account, "A")

    saved = await _put_layout(
        app_client,
        account,
        positions=[_position(ids["A"], 1.0, 1.0), _position(ids["A"], 5.0, 5.0)],
    )

    assert saved.status_code == 200, saved.text
    assert (await _counts(db))["node_positions"] == 1
    assert _positions(await _read_layout(app_client, account)) == {ids["A"]: (5.0, 5.0)}


async def test_positions_can_be_read_back_as_rows(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession
) -> None:
    """坐标真的落在 `node_positions` 上,单位是画布坐标原样存 —— 不做任何换算。"""
    account = await make_account()
    ids = await _create_nodes(app_client, account, "A")

    await _put_layout(app_client, account, positions=[_position(ids["A"], -12.25, 33.5)])

    rows = list((await db.execute(select(NodePosition))).scalars())
    assert len(rows) == 1
    assert (rows[0].node_id, rows[0].x, rows[0].y) == (uuid.UUID(ids["A"]), -12.25, 33.5)
    assert rows[0].updated_at is not None


async def test_a_relation_table_row_is_not_a_dependency(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession
) -> None:
    """「相关」「影响」只写 `node_relations` —— `dependencies` 一行都不多。

    这是整批设计的核心承诺:**排期链路零改动**。一条普通关联混进 `dependencies` 的
    话,排期会开始按它算先后,而它本来只是一句"这两件事有点关系"。
    """
    account = await make_account()
    ids = await _create_nodes(app_client, account, "A", "B")

    await _connect(app_client, account, ids["A"], ids["B"], "related_to")
    await _connect(app_client, account, ids["A"], ids["B"], "influences")

    rows = await db.scalar(select(func.count()).select_from(NodeRelation))
    assert rows == 2
    assert (await _counts(db))["dependencies"] == 0


# ---------------------------------------------------------------------------------
# AI 提案里的关系(create_relation)
#
# 这一组走的是**真实 HTTP 链路**:`/messages` 生成提案 -> 用户 confirm -> `/plan`。
# 脚本只替掉"谁来想出这个动作",后面的校验、事务写入、版本记账全是产品代码。
# ---------------------------------------------------------------------------------
async def _propose(
    client: httpx.AsyncClient,
    account,
    *,
    content: str = "看一下",
    context_node_id: str | None = None,
    scope_root_id: str | None = None,
) -> dict:
    payload: dict = {"content": content}
    if context_node_id is not None:
        payload["contextNodeId"] = context_node_id
    if scope_root_id is not None:
        payload["scopeRootId"] = scope_root_id
    response = await client.post(
        f"/api/workspaces/{account.workspace_id}/messages",
        json=payload,
        headers=account.headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _confirm(
    client: httpx.AsyncClient, account, proposal_id: str, key: str
) -> httpx.Response:
    return await client.post(
        f"/api/workspaces/{account.workspace_id}/proposals/{proposal_id}/confirm",
        json={"idempotencyKey": key},
        headers=account.headers,
    )


async def test_a_proposal_can_add_an_influences_edge(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    """两个主题互相影响 -> `create_relation` -> confirm -> 画布上出现一条**有向**边。

    验收 3。断言分三层:提案阶段只预览、confirm 之前 `/plan` 不变、confirm 之后
    真实写进 `node_relations`(origin=ai)且落在这一版 `PlanRevision.diff` 里。
    """
    account = await make_account()
    ids = await _create_nodes(app_client, account, "英语阅读", "数学建模")
    # 记号顺序就是加载顺序:n1 根目标,n2 英语阅读,n3 数学建模。
    use_reasoner(
        FakeReasoner(
            actions=(
                {
                    "op": "create_relation",
                    "sourceRef": "n2",
                    "targetRef": "n3",
                    "relationType": "influences",
                    "note": "读题速度会影响建模",
                },
            )
        )
    )

    body = await _propose(app_client, account)
    assert body["proposalErrors"] == [], body["proposalErrors"]
    assert body["proposal"] is not None
    summary = " ".join(item["summary"] for item in body["proposal"]["items"])
    assert "影响" in summary, summary

    # 确认之前:画布上没有这条边。
    assert (await _plan(app_client, account))["relations"] == []

    confirmed = await _confirm(app_client, account, body["proposal"]["id"], "rel-key-1")
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["applied"]["relationsAdded"] == 1
    # 排期侧的依赖没有被误记。
    assert confirmed.json()["applied"]["dependenciesAdded"] == 0

    plan = await _plan(app_client, account)
    edges = [edge for edge in plan["relations"] if edge["relationType"] == "influences"]
    assert len(edges) == 1, plan["relations"]
    edge = edges[0]
    assert edge["sourceId"] == ids["英语阅读"]
    assert edge["targetId"] == ids["数学建模"]
    assert edge["origin"] == "ai"
    assert edge["note"] == "读题速度会影响建模"

    # 版本账本里能查到这次关系写入,而且它**不在** addedDependencies 那一栏。
    revision = await db.scalar(
        select(PlanRevision)
        .where(PlanRevision.workspace_id == uuid.UUID(account.workspace_id))
        .order_by(PlanRevision.version.desc())
    )
    assert revision is not None
    assert revision.diff["addedRelations"] == [
        {
            "sourceId": ids["英语阅读"],
            "targetId": ids["数学建模"],
            "relationType": "influences",
        }
    ]
    assert revision.diff["addedDependencies"] == []
    assert (await _counts(db))["dependencies"] == 0

    # 重复 confirm 幂等:边只有一条。
    replay = await _confirm(app_client, account, body["proposal"]["id"], "rel-key-1")
    assert replay.status_code == 200, replay.text
    assert replay.json()["replayed"] is True
    assert len((await _plan(app_client, account))["relations"]) == 1


async def test_a_new_node_and_a_relation_in_the_same_proposal(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """先 `create_node`,再引用它建关系 —— 同一个提案里。

    验收 4。`resolved` 在 `_create` 里就把 `n2` 绑到了新节点上,所以后面那条关系
    解析得到它。这不只是一个便利:`create_relation` 必须允许引用**本条之前**刚建的
    节点,否则"新建一个主题并把它连到目标上"就永远要用户确认两次。
    """
    account = await make_account()
    use_reasoner(
        FakeReasoner(
            actions=(
                {
                    "op": "create_node",
                    "localId": "n2",
                    "parentRef": "n1",
                    "title": "英语阅读",
                },
                {
                    "op": "create_relation",
                    "sourceRef": "n2",
                    "targetRef": "n1",
                    "relationType": "related_to",
                },
            )
        )
    )

    body = await _propose(app_client, account)
    assert body["proposalErrors"] == [], body["proposalErrors"]
    assert body["proposal"] is not None
    assert body["proposal"]["itemCount"] == 2

    confirmed = await _confirm(app_client, account, body["proposal"]["id"], "rel-key-2")
    assert confirmed.status_code == 200, confirmed.text
    applied = confirmed.json()["applied"]
    assert applied["nodesCreated"] == 1
    assert applied["relationsAdded"] == 1

    plan = await _plan(app_client, account)
    new_node = next(node for node in plan["nodes"] if node["title"] == "英语阅读")
    assert len(plan["relations"]) == 1
    edge = plan["relations"][0]
    assert edge["relationType"] == "related_to"
    assert {edge["sourceId"], edge["targetId"]} == {new_node["id"], next(
        node["id"] for node in plan["nodes"] if node["parentId"] is None
    )}
    assert edge["origin"] == "ai"


async def test_related_to_is_deduplicated_against_an_existing_edge(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """`related_to` 无向:A→B 已经存在时,再提 B→A 算重复,不是第二条边。

    验收 5 的"重复关系"那一半。规范化按 UUID 的整数序,所以两种画法落到同一个键;
    没有规范化的话,唯一约束只拦得住"同一方向连两次",画布上会出现两条重叠的线。
    拒绝时不产生任何提案 —— 用户看到的是一条明确的原因,不是一次安静的成功。
    """
    account = await make_account()
    ids = await _create_nodes(app_client, account, "A", "B")
    # 用户自己先连了一条 A—B。
    existing = await _connect(app_client, account, ids["A"], ids["B"], "related_to")
    assert existing.status_code == 201, existing.text

    use_reasoner(
        FakeReasoner(
            actions=(
                {
                    "op": "create_relation",
                    "sourceRef": "n3",
                    "targetRef": "n2",
                    "relationType": "related_to",
                },
            )
        )
    )

    body = await _propose(app_client, account)
    assert body["proposal"] is None, "重复的关系不该产生提案"
    assert {error["code"] for error in body["proposalErrors"]} == {codes.RELATION_ALREADY_EXISTS}

    # 一行都没多写。
    assert len((await _plan(app_client, account))["relations"]) == 1


async def test_the_same_related_to_edge_twice_in_one_proposal_is_refused(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """同一份提案里 A—B 与 B—A 各写一遍 -> 拒绝,而不是写两条。"""
    account = await make_account()
    await _create_nodes(app_client, account, "A", "B")
    use_reasoner(
        FakeReasoner(
            actions=(
                {
                    "op": "create_relation",
                    "sourceRef": "n2",
                    "targetRef": "n3",
                    "relationType": "related_to",
                },
                {
                    "op": "create_relation",
                    "sourceRef": "n3",
                    "targetRef": "n2",
                    "relationType": "related_to",
                },
            )
        )
    )

    body = await _propose(app_client, account)
    assert body["proposal"] is None
    assert {error["code"] for error in body["proposalErrors"]} == {codes.RELATION_ALREADY_EXISTS}
    assert (await _plan(app_client, account))["relations"] == []


async def test_influences_in_both_directions_are_two_edges(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """`influences` 有向:A→B 与 B→A 是两条边,不能被去重成一条。"""
    account = await make_account()
    ids = await _create_nodes(app_client, account, "英语", "数学")
    use_reasoner(
        FakeReasoner(
            actions=(
                {
                    "op": "create_relation",
                    "sourceRef": "n2",
                    "targetRef": "n3",
                    "relationType": "influences",
                },
                {
                    "op": "create_relation",
                    "sourceRef": "n3",
                    "targetRef": "n2",
                    "relationType": "influences",
                },
            )
        )
    )

    body = await _propose(app_client, account)
    assert body["proposalErrors"] == [], body["proposalErrors"]
    confirmed = await _confirm(app_client, account, body["proposal"]["id"], "rel-key-3")
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["applied"]["relationsAdded"] == 2

    edges = (await _plan(app_client, account))["relations"]
    assert {(edge["sourceId"], edge["targetId"]) for edge in edges} == {
        (ids["英语"], ids["数学"]),
        (ids["数学"], ids["英语"]),
    }


async def test_a_relation_out_of_scope_writes_nothing(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """关系是两端的事:有一端在范围外,整条提案被拒,一行都不写。

    验收 5 的"越权"那一半。范围是一条权限,不是提示词里的一句建议 —— 模型看得见
    范围外的节点(用来解释问题),但不能拿它去改用户的图。
    """
    account = await make_account()
    inside = await _create_nodes(app_client, account, "在范围内")
    await _create_nodes(app_client, account, "在范围外")

    # n2 在范围内,n3 在范围外。
    use_reasoner(
        FakeReasoner(
            actions=(
                {
                    "op": "create_relation",
                    "sourceRef": "n2",
                    "targetRef": "n3",
                    "relationType": "influences",
                },
            )
        )
    )

    body = await _propose(
        app_client,
        account,
        content="把这两条连起来",
        context_node_id=inside["在范围内"],
        scope_root_id=inside["在范围内"],
    )
    assert body["proposal"] is None
    assert {error["code"] for error in body["proposalErrors"]} == {codes.OUT_OF_SCOPE}
    assert (await _plan(app_client, account))["relations"] == []


async def test_an_information_topic_may_take_part_in_an_influence_edge(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """信息主题**可以**参与「相关 / 影响」,虽然它不能当排期依赖的端点。

    这是两种关系最容易被合并的地方:一行"信息主题不能参与关系"的过度限制会挡住
    "我排名 38 影响这学期选课"这种**正是**信息主题要表达的结构。
    """
    account = await make_account()
    info = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/nodes",
        json={
            "parentId": await _root(app_client, account),
            "title": "我排名 38",
            "nodeType": "capability",
            "purpose": "information",
        },
        headers=account.headers,
    )
    assert info.status_code == 201, info.text
    info_id = info.json()["node"]["id"]
    task = await _create_nodes(app_client, account, "这学期选课")

    # n1 根,n2 我排名 38,n3 这学期选课。
    use_reasoner(
        FakeReasoner(
            actions=(
                {
                    "op": "create_relation",
                    "sourceRef": "n2",
                    "targetRef": "n3",
                    "relationType": "influences",
                },
            )
        )
    )

    body = await _propose(app_client, account)
    assert body["proposalErrors"] == [], body["proposalErrors"]
    confirmed = await _confirm(app_client, account, body["proposal"]["id"], "rel-key-4")
    assert confirmed.status_code == 200, confirmed.text

    edges = (await _plan(app_client, account))["relations"]
    assert len(edges) == 1
    assert edges[0]["sourceId"] == info_id
    assert edges[0]["targetId"] == task["这学期选课"]
