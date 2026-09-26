"""归档与恢复。

## 这一组在防什么

在这之前,"删除"在数据库那一层**已经是**软删除(`plan_nodes.deleted_at`),所以
"改成软删除"是个假说法。真正缺的是三件,这一组一件一件钉住:

1. **边被物理删掉了。** `node_service.delete_node` 原来无条件删掉挂在被删节点上的
   `dependencies` 行,于是"恢复"只能还回一个**一条前置都没有**的节点 —— 而排期正是
   按前置算的。现在归档一行边都不动,"界面上看不到它"靠的是读路径那侧的过滤。
2. **没有恢复入口。** 归档之后没有任何地方能看到它、点回来。
3. **点下去之前不告诉你代价。** 影响范围要在**点之前**拿到
   (`GET /nodes/{id}/archive-impact`),不是删完之后才知道。

规则全文写在 `docs/10-NEXT-BATCH-SCOPE.md` 第 5 节;这里验的是那些规则真的成立。

## 有一条用例是故意"造出非法状态"的

`test_the_cycle_guard_refuses_to_restore_into_a_loop` 直接往库里插一条接口**不可能
允许**的边(成环),因为按写入规则那个状态到不了。它是"万一发生了,不许静默"那道
断言的证明 —— 一个永远不会被触发的守卫,和一个没有守卫是一样的。
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models import Dependency, NodeRelation, PlanNode, ScheduledSession
from backend.services.timeutil import today_in
from backend.tests.conftest import Account

#: 测试账号的时区(`make_account` 的默认值)。**"今天"只从 `today_in` 取** ——
#: 用 `date.today()` 的话,UTC 上的服务器在东八区 0:00–8:00 之间会算出前一天,
#: 于是"排在昨天的那一场"变成"排在今天",而过期那一条断言会跟着跑的时刻变化。
TZ = "Asia/Shanghai"


# ---------------------------------------------------------------------------------
# 助手
#
# 建节点、连边一律**走接口**,不直接写库:`make_account` 注释里那条理由在这里同样
# 成立 —— 测试里出现"只有测试能构造出来的状态"是最常见的自欺方式之一。
# 直接写库的只有两处(排期与那条反向依赖),各自在用例里说明了为什么非写不可。
# ---------------------------------------------------------------------------------
async def _plan(client: httpx.AsyncClient, account: Account) -> dict:
    response = await client.get(
        f"/api/workspaces/{account.workspace_id}/plan", headers=account.headers
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _titles(client: httpx.AsyncClient, account: Account) -> set[str]:
    return {node["title"] for node in (await _plan(client, account))["nodes"]}


async def _root(client: httpx.AsyncClient, account: Account) -> str:
    plan = await _plan(client, account)
    return next(node["id"] for node in plan["nodes"] if node["parentId"] is None)


async def _node(
    client: httpx.AsyncClient, account: Account, parent_id: str, title: str
) -> str:
    response = await client.post(
        f"/api/workspaces/{account.workspace_id}/nodes",
        json={"parentId": parent_id, "title": title},
        headers=account.headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["node"]["id"]


async def _edge(client: httpx.AsyncClient, account: Account, before: str, after: str) -> None:
    response = await client.post(
        f"/api/workspaces/{account.workspace_id}/dependencies",
        json={"predecessorId": before, "successorId": after},
        headers=account.headers,
    )
    assert response.status_code == 201, response.text


async def _relation(
    client: httpx.AsyncClient, account: Account, source: str, target: str
) -> None:
    response = await client.post(
        f"/api/workspaces/{account.workspace_id}/relations",
        json={"sourceId": source, "targetId": target, "relationType": "related_to"},
        headers=account.headers,
    )
    assert response.status_code == 201, response.text


async def _archive(
    client: httpx.AsyncClient, account: Account, node_id: str, mode: str | None = None
) -> httpx.Response:
    """`DELETE /nodes/{id}`。`mode=None` 就是**不传**这个参数 —— 验默认值是归档。"""
    url = f"/api/workspaces/{account.workspace_id}/nodes/{node_id}"
    if mode is not None:
        url = f"{url}?mode={mode}"
    return await client.request("DELETE", url, headers=account.headers)


async def _restore(
    client: httpx.AsyncClient, account: Account, node_id: str
) -> httpx.Response:
    return await client.post(
        f"/api/workspaces/{account.workspace_id}/nodes/{node_id}/restore",
        headers=account.headers,
    )


async def _archive_list(client: httpx.AsyncClient, account: Account) -> list[dict]:
    response = await client.get(
        f"/api/workspaces/{account.workspace_id}/archive", headers=account.headers
    )
    assert response.status_code == 200, response.text
    return response.json()


def _error_code(response: httpx.Response) -> str:
    return response.json()["error"]["code"]


async def _scheduled(
    db: AsyncSession, *, account: Account, node_id: str, on, minutes: int
) -> None:
    """直接写一场排期。

    这里走库而不是 `POST /schedule/apply`:那条路径要预览、版本、幂等键,还要一个真的
    排得下的计划 —— 而本文件要验的是"恢复的时候那几场会不会被悄悄改掉",不是排期器
    本身(它有自己的一整个文件)。

    `status` / `seq` 用模型默认值,不自己指定:恢复报告的两个口径(过期、超上限)都按
    "还没作废的场次"算,自己填一个状态就等于把这个前提抄进了测试。
    """
    db.add(
        ScheduledSession(
            user_id=uuid.UUID(account.id),
            workspace_id=uuid.UUID(account.workspace_id),
            node_id=uuid.UUID(node_id),
            scheduled_date=on,
            planned_minutes=minutes,
        )
    )
    await db.commit()


async def _archived_rows(
    db: AsyncSession, account: Account, node_ids: list[str]
) -> list[tuple[object, uuid.UUID, uuid.UUID | None]]:
    """这几行的 `(deleted_at, id, archive_batch_id)`。

    **只能从库里读**:批次号是恢复认哪一批的依据,但它不在任何响应里(界面不需要知道
    它)。断言画布或接口都碰不到它,而这一组要验的恰好是它。
    """
    return list(
        await db.execute(
            select(PlanNode.deleted_at, PlanNode.id, PlanNode.archive_batch_id)
            .where(
                PlanNode.workspace_id == uuid.UUID(account.workspace_id),
                PlanNode.id.in_([uuid.UUID(item) for item in node_ids]),
            )
            .execution_options(populate_existing=True)
        )
    )


async def _sessions(db: AsyncSession, account: Account) -> dict[str, tuple]:
    """这个空间里每一场排期的关键字段。

    两个"看起来多余"的写法都是必须的:

    - `populate_existing=True`:不加它,第二次调用拿回的是**身份映射里那两个对象
      本身**,于是"恢复前"与"恢复后"比的是同一个值 —— 一条永远通过的断言。
    - 比整行(日期、状态、分钟数、序号),不比"条数没变":场次被挪到别的日子、或者
      被标成作废,条数都不变。
    """
    rows = await db.execute(
        select(ScheduledSession)
        .where(ScheduledSession.workspace_id == uuid.UUID(account.workspace_id))
        .execution_options(populate_existing=True)
    )
    return {
        str(row.id): (row.scheduled_date, row.status.value, row.planned_minutes, row.seq)
        for row in rows.scalars()
    }


# ---------------------------------------------------------------------------------
# 归档:边和场次都留着
# ---------------------------------------------------------------------------------
async def test_archiving_keeps_the_edges_and_the_sessions(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession
) -> None:
    """归档不动边,也不动场次 —— 这是"恢复得回来"的全部前提。

    三条断言缺一不可:界面上(投影里)那个节点和它的边**都没了**;库里那些行**还在**;
    恢复之后它们**原样回来**(所以"还在"不是"留了一堆垃圾")。
    """
    account = await make_account()
    root = await _root(app_client, account)
    stage = await _node(app_client, account, root, "阶段")
    leaf = await _node(app_client, account, stage, "叶子")
    other = await _node(app_client, account, root, "别的")
    await _edge(app_client, account, leaf, root)
    await _edge(app_client, account, other, leaf)
    await _relation(app_client, account, other, leaf)
    await _scheduled(db, account=account, node_id=leaf, on=today_in(TZ), minutes=30)

    before = await _plan(app_client, account)
    assert len(before["dependencies"]) == 2

    archived = await _archive(app_client, account, leaf)  # 不传 mode:默认就是归档
    assert archived.status_code == 200, archived.text
    assert archived.json()["restorable"] is True
    assert archived.json()["removedDependencies"] == 0, (
        "归档一行边都不该删 —— 这个数字不是 0 的话,恢复回来的是个残的"
    )

    after = await _plan(app_client, account)
    assert "叶子" not in {node["title"] for node in after["nodes"]}
    assert after["dependencies"] == [], "连着它的边要一起从界面上消失"
    assert after["relations"] == [], "用户画的关系同理"
    assert after["sessions"] == []

    # 库里:节点是软的,但边**一行没少**,场次也还在。
    rows = await db.execute(
        select(Dependency).where(Dependency.workspace_id == uuid.UUID(account.workspace_id))
    )
    assert len(list(rows.scalars())) == 2, "归档不动边:两行都要留着,恢复时它们还得回来"
    relations = await db.execute(
        select(NodeRelation).where(NodeRelation.workspace_id == uuid.UUID(account.workspace_id))
    )
    assert len(list(relations.scalars())) == 1
    sessions = await db.execute(
        select(ScheduledSession).where(
            ScheduledSession.workspace_id == uuid.UUID(account.workspace_id)
        )
    )
    assert len(list(sessions.scalars())) == 1, "场次也不删:它是历史,恢复时要回来"

    listing = await _archive_list(app_client, account)
    assert [item["node"]["title"] for item in listing] == ["叶子"]
    assert listing[0]["restorable"] is True
    assert listing[0]["sessions"] == 1

    restored = await _restore(app_client, account, leaf)
    assert restored.status_code == 200, restored.text
    body = restored.json()
    assert body["restoredCount"] == 1
    assert body["relationsVisible"] == 3, "两条依赖 + 一条关系都重新可见"
    assert body["restoredSessions"] == 1
    assert body["overdueSessions"] == 0

    back = await _plan(app_client, account)
    assert {node["title"] for node in back["nodes"]} == {
        node["title"] for node in before["nodes"]
    }
    assert len(back["dependencies"]) == 2, "边不是重建的,是重新可见的"
    assert len(back["relations"]) == 3
    assert len(back["sessions"]) == 1
    assert await _archive_list(app_client, account) == []


async def test_archiving_carries_the_whole_subtree_and_restores_it_together(
    app_client: httpx.AsyncClient, make_account
) -> None:
    """归档一次带走一整棵子树,恢复也一次带回来 —— 列表里只出现那一支的根。"""
    account = await make_account()
    root = await _root(app_client, account)
    stage = await _node(app_client, account, root, "阶段")
    await _node(app_client, account, stage, "子项甲")
    await _node(app_client, account, stage, "子项乙")
    before = await _titles(app_client, account)

    archived = await _archive(app_client, account, stage)
    assert archived.status_code == 200, archived.text
    assert archived.json()["deletedCount"] == 3, "自己和两个子项"
    assert await _titles(app_client, account) == before - {"阶段", "子项甲", "子项乙"}

    listing = await _archive_list(app_client, account)
    assert [item["node"]["title"] for item in listing] == ["阶段"], (
        "同一批走的子孙不单独列 —— 列出来会让用户以为要一个一个恢复"
    )
    assert listing[0]["descendants"] == 2

    restored = await _restore(app_client, account, stage)
    assert restored.status_code == 200, restored.text
    assert restored.json()["restoredCount"] == 3
    assert await _titles(app_client, account) == before


# ---------------------------------------------------------------------------------
# 恢复:只认那一批,而且过三道校验
# ---------------------------------------------------------------------------------
async def test_restore_brings_back_only_the_batch_it_was_archived_with(
    app_client: httpx.AsyncClient, make_account
) -> None:
    """恢复认的是**那一次归档**,不是"现在这棵子树"。

    子项**先**被单独归档,阶段**后**被归档;恢复阶段不该顺手把子项也复活 —— 用户点的
    是一行,他以为只回来一项,而子项是他当时特意收起来的。
    """
    account = await make_account()
    root = await _root(app_client, account)
    stage = await _node(app_client, account, root, "阶段")
    child = await _node(app_client, account, stage, "先收起来的子项")

    assert (await _archive(app_client, account, child)).status_code == 200
    assert (await _archive(app_client, account, stage)).status_code == 200

    restored = await _restore(app_client, account, stage)
    assert restored.status_code == 200, restored.text
    assert restored.json()["restoredCount"] == 1, "只回来阶段自己,子项是更早那一批"
    assert "先收起来的子项" not in await _titles(app_client, account)

    listing = await _archive_list(app_client, account)
    assert [item["node"]["title"] for item in listing] == ["先收起来的子项"]
    # 阶段回来了,子项的上层就不再是归档的 —— 现在它自己能恢复了。
    assert listing[0]["restorable"] is True

    assert (await _restore(app_client, account, child)).status_code == 200
    assert "先收起来的子项" in await _titles(app_client, account)


async def test_two_archives_that_share_a_timestamp_stay_two_batches(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession
) -> None:
    """两次归档**时间戳一模一样**时,恢复仍然只认自己那一批。

    批次号以前就是"`deleted_at` 相等"。那条判据靠的是"两次归档不会落在同一个微秒上"
    —— 一条关于精度的默认性质,而它失效时的症状是**静默的**:用户点一行恢复,他当时
    特意单独收起来的子项也跟着回来了,界面上没有任何东西提示过这件事。

    这里把两批的 `deleted_at` 直接改成同一个值,把这个状态**造出来**(生产里要两次
    归档落在同一微秒才碰得到,和上面那条成环用例同一个理由:永远不会被触发的守卫,
    和没有守卫是一样的)。场景与 `test_restore_brings_back_only_the_batch_it_was_
    archived_with` 完全相同 —— 差别只在时间戳,所以两条一比就知道这个键到底有没有
    在起作用:把服务层换回按时间戳认批,这一条会红成 `restoredCount == 2`。
    """
    account = await make_account()
    root = await _root(app_client, account)
    stage = await _node(app_client, account, root, "阶段")
    child = await _node(app_client, account, stage, "先收起来的子项")

    assert (await _archive(app_client, account, child)).status_code == 200
    assert (await _archive(app_client, account, stage)).status_code == 200

    # 两批的时间戳抹成同一个。批次号不动 —— 它才是恢复该认的东西。
    collision = (await _archived_rows(db, account, [child, stage]))[0][0]
    await db.execute(
        update(PlanNode)
        .where(PlanNode.id.in_([uuid.UUID(child), uuid.UUID(stage)]))
        .values(deleted_at=collision)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    rows = await _archived_rows(db, account, [child, stage])
    assert {row[0] for row in rows} == {collision}, "前提没造出来:两行的时间戳不相等"
    assert len({row[2] for row in rows}) == 2, "前提没造出来:两行本来就是同一批"

    restored = await _restore(app_client, account, stage)
    assert restored.status_code == 200, restored.text
    assert restored.json()["restoredCount"] == 1, "时间戳撞上了也不许把更早那一批带回来"
    assert "先收起来的子项" not in await _titles(app_client, account)


async def test_archiving_stamps_one_batch_id_on_the_whole_subtree(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession
) -> None:
    """一次删除给整支打**同一个**批次号;两次删除的号不同;恢复之后清空。

    三条一起验,是因为它们合起来才是"批次"这个词的意思:同一个号的是一批,不同号
    的不是一批,而活着的节点没有批次可言(`deleted_at` 为空时那一列也该是空)。
    """
    account = await make_account()
    root = await _root(app_client, account)
    stage = await _node(app_client, account, root, "阶段")
    child = await _node(app_client, account, stage, "子项")
    lonely = await _node(app_client, account, root, "另一次收的")

    assert (await _archive(app_client, account, stage)).status_code == 200
    assert (await _archive(app_client, account, lonely)).status_code == 200

    batches = {row[1]: row[2] for row in await _archived_rows(db, account, [stage, child, lonely])}
    assert batches[uuid.UUID(stage)] == batches[uuid.UUID(child)], "同一支要同一个号"
    assert batches[uuid.UUID(stage)] != batches[uuid.UUID(lonely)], "两次归档不能同一个号"

    assert (await _restore(app_client, account, stage)).status_code == 200
    after = {row[1]: row[2] for row in await _archived_rows(db, account, [stage, child])}
    assert set(after.values()) == {None}, "恢复回来的节点不该还挂着批次号"


async def test_restore_refuses_while_the_parent_is_still_archived(
    app_client: httpx.AsyncClient, make_account
) -> None:
    """上层还在归档里就不给恢复,而且**在列表里就写出来**,不让用户点了才知道。

    不拒绝的后果是造出一个**活着的、但任何界面都到不了**的节点:父节点不出现,
    子节点就没有入口能导航到,而它真的存在。
    """
    account = await make_account()
    root = await _root(app_client, account)
    stage = await _node(app_client, account, root, "阶段")
    child = await _node(app_client, account, stage, "子项")

    assert (await _archive(app_client, account, child)).status_code == 200
    assert (await _archive(app_client, account, stage)).status_code == 200

    blocked = next(
        item
        for item in await _archive_list(app_client, account)
        if item["node"]["title"] == "子项"
    )
    assert blocked["restorable"] is False
    assert blocked["blockedReason"] == "PARENT_ARCHIVED"

    refused = await _restore(app_client, account, child)
    assert refused.status_code == 409, refused.text
    assert _error_code(refused) == "PARENT_ARCHIVED"
    assert "阶段" in refused.json()["error"]["message"], "要说清是谁挡着"

    assert "子项" not in await _titles(app_client, account)


async def test_purged_nodes_cannot_be_restored(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession
) -> None:
    """彻底删除是**不可恢复**的那一种,而且只有它才真的删边。

    库里两种删除只差 `purged_at` 一列。少了这一列,恢复入口根本分不出该不该放行 ——
    用户会拿到一个一条边都没有的节点,并且不会知道自己拿到的是残的。
    """
    account = await make_account()
    root = await _root(app_client, account)
    stage = await _node(app_client, account, root, "阶段")
    leaf = await _node(app_client, account, stage, "叶子")
    await _edge(app_client, account, leaf, root)

    purged = await _archive(app_client, account, leaf, mode="delete")
    assert purged.status_code == 200, purged.text
    assert purged.json()["restorable"] is False
    assert purged.json()["removedDependencies"] == 1, "彻底删除才物理删边"

    rows = await db.execute(
        select(Dependency).where(Dependency.workspace_id == uuid.UUID(account.workspace_id))
    )
    assert list(rows.scalars()) == []

    refused = await _restore(app_client, account, leaf)
    assert refused.status_code == 409, refused.text
    assert _error_code(refused) == "NODE_PURGED"

    assert await _archive_list(app_client, account) == [], (
        "彻底删除过的不进归档列表:它没有'恢复'这个动作可言"
    )

    # 锚在库里:行**还在**(历史版本的快照引用着这些 id),区别只在 `purged_at`。
    node = await db.scalar(select(PlanNode).where(PlanNode.id == uuid.UUID(leaf)))
    assert node is not None, "'彻底'指的是不可恢复,不是从历史里抹掉"
    assert node.deleted_at is not None and node.purged_at is not None


async def test_a_subtree_whose_parent_was_purged_says_so_instead_of_asking_to_restore_it(
    app_client: httpx.AsyncClient, make_account
) -> None:
    """上层被彻底删除时,提示不能是"先恢复上层" —— 那件事做不到。

    这一条和上一条分开,是因为**用户能做的事不一样**:一个是"先恢复它",一个是
    "没有别的办法"。给错提示比不给更糟:他会照着做,然后撞上第二句错误。
    """
    account = await make_account()
    root = await _root(app_client, account)
    stage = await _node(app_client, account, root, "阶段")
    child = await _node(app_client, account, stage, "先收起来的子项")

    assert (await _archive(app_client, account, child)).status_code == 200
    assert (await _archive(app_client, account, stage, mode="delete")).status_code == 200

    listing = await _archive_list(app_client, account)
    assert [item["node"]["title"] for item in listing] == ["先收起来的子项"]
    assert listing[0]["restorable"] is False
    assert listing[0]["blockedReason"] == "PARENT_PURGED"

    refused = await _restore(app_client, account, child)
    assert refused.status_code == 409, refused.text
    assert _error_code(refused) == "NODE_PURGED"


async def test_the_cycle_guard_refuses_to_restore_into_a_loop(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession
) -> None:
    """恢复之后会成环的话,整体回滚 —— 一行都不改。

    **那条反向的边是直接写库造出来的**,接口不可能允许它(`POST /dependencies` 会
    409 拒绝成环)。按现在的写入规则这个状态到不了:归档期间两端都不可见,加不了边。
    所以这道守卫是"万一发生了,不许静默"的那个万一 —— 而一个永远不会被触发的守卫,
    和一个没有守卫是一样的。这里必须真的把它触发一次。
    """
    account = await make_account()
    root = await _root(app_client, account)
    first = await _node(app_client, account, root, "甲")
    second = await _node(app_client, account, root, "乙")
    await _edge(app_client, account, first, second)  # 甲 -> 乙

    db.add(
        Dependency(
            workspace_id=uuid.UUID(account.workspace_id),
            predecessor_id=uuid.UUID(second),
            successor_id=uuid.UUID(first),
        )
    )
    await db.commit()

    assert (await _archive(app_client, account, second)).status_code == 200
    assert (await _archive(app_client, account, first)).status_code == 200
    # 先恢复甲:这时乙还在归档里,两条边都凑不齐两端,不成环。
    assert (await _restore(app_client, account, first)).status_code == 200

    before = await _plan(app_client, account)
    refused = await _restore(app_client, account, second)
    assert refused.status_code == 409, refused.text
    assert _error_code(refused) == "DEPENDENCY_CYCLE"
    assert "甲" in refused.json()["error"]["message"], "环上是谁要说出来"

    after = await _plan(app_client, account)
    assert after["revisionVersion"] == before["revisionVersion"], "整体回滚:版本号不动"
    assert "乙" not in {node["title"] for node in after["nodes"]}, "拒绝了就什么都没恢复"
    listing = await _archive_list(app_client, account)
    assert [item["node"]["title"] for item in listing] == ["乙"]


# ---------------------------------------------------------------------------------
# 影响范围与排期报告
# ---------------------------------------------------------------------------------
async def test_archive_impact_says_what_it_takes_before_you_commit(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession
) -> None:
    """点之前就知道会带走什么:后代、关系、依赖、场次 —— 而且数字由后端算。

    前端手里那份计划可能是几分钟前的,让它自己 `filter` 出这几个数字的话,用户在
    确认框里看到的和实际发生的事会对不上,而他正是照着那个数字做决定的。
    """
    account = await make_account()
    root = await _root(app_client, account)
    stage = await _node(app_client, account, root, "阶段")
    leaf = await _node(app_client, account, stage, "叶子")
    other = await _node(app_client, account, root, "别的")
    await _edge(app_client, account, other, leaf)
    await _relation(app_client, account, other, leaf)
    today = today_in(TZ)
    await _scheduled(db, account=account, node_id=leaf, on=today, minutes=45)
    await _scheduled(db, account=account, node_id=leaf, on=today - timedelta(days=3), minutes=120)

    response = await app_client.get(
        f"/api/workspaces/{account.workspace_id}/nodes/{stage}/archive-impact",
        headers=account.headers,
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "nodeId": stage,
        "title": "阶段",
        "descendants": 1,
        "relations": 1,
        "dependencies": 1,
        "sessions": 2,
        "sessionMinutes": 165,
        "overdueSessions": 1,
    }


async def test_restoring_reports_schedule_conflicts_and_changes_nothing(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession
) -> None:
    """恢复只**报**排期冲突,绝不自动重排,也绝不丢弃任何一场。

    两件冲突各出现一次:

    - 一场排在**过去**、还标着"待做"的:恢复之后需要用户自己处理(补做或者挪走)。
    - 一场和"归档之后新排的那一场"挤在同一天:加起来超了每日上限。默认每周预算
      600 分钟 × 0.8 = 480,平摊到 7 天 = 每天 69 分钟(见 `scheduler.calendar.daily_cap`),
      而那天有 120 分钟。

    而"什么都没改"这一半同样重要:两场的日期、状态、分钟数与恢复前逐字段相同。
    """
    account = await make_account()
    root = await _root(app_client, account)
    stage = await _node(app_client, account, root, "阶段")
    leaf = await _node(app_client, account, stage, "叶子")
    today = today_in(TZ)
    crowded = today + timedelta(days=1)
    await _scheduled(db, account=account, node_id=leaf, on=today - timedelta(days=2), minutes=30)
    await _scheduled(db, account=account, node_id=leaf, on=crowded, minutes=60)
    assert (await _archive(app_client, account, stage)).status_code == 200

    # 归档之后用户又排了一场,落在同一天 —— 恢复把那两场放回来时就挤在一起了。
    sibling = await _node(app_client, account, root, "归档之后新排的")
    await _scheduled(db, account=account, node_id=sibling, on=crowded, minutes=60)

    before = await _sessions(db, account)
    restored = await _restore(app_client, account, stage)
    assert restored.status_code == 200, restored.text
    body = restored.json()
    assert body["restoredCount"] == 2
    assert body["restoredSessions"] == 2
    assert body["restoredMinutes"] == 90
    assert body["overdueSessions"] == 1
    assert body["overbookedDays"] == [
        {
            "day": crowded.isoformat(),
            "plannedMinutes": 120,
            "dailyCap": 69,
            "overBy": 51,
        }
    ]
    assert await _sessions(db, account) == before, "报告归报告,一行都不许改"


# ---------------------------------------------------------------------------------
# 归属
# ---------------------------------------------------------------------------------
async def test_archived_nodes_are_scoped_to_their_workspace(
    app_client: httpx.AsyncClient, make_account
) -> None:
    """乙拿甲那个节点的 id 去恢复、去看影响范围,得到的都是 404 —— 和不存在同一个答案。

    与 `NodeNotFound` 那条纪律一致:分开返回的话,拿 id 逐个试就能测绘出别人空间里
    有哪些节点。
    """
    account_a = await make_account(email="a@example.com")
    account_b = await make_account(email="b@example.com", workspace_title="另一个空间")
    root = await _root(app_client, account_a)
    leaf = await _node(app_client, account_a, root, "叶子")
    assert (await _archive(app_client, account_a, leaf)).status_code == 200

    assert (await _restore(app_client, account_b, leaf)).status_code == 404
    impact = await app_client.get(
        f"/api/workspaces/{account_b.workspace_id}/nodes/{leaf}/archive-impact",
        headers=account_b.headers,
    )
    assert impact.status_code == 404
    assert await _archive_list(app_client, account_b) == []
