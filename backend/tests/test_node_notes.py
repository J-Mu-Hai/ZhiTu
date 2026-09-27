"""节点长正文(「笔记」)的读写。§2.2。

## 这个文件在防什么

一张新表加两条新路由,能出错的地方**不在"能不能存"上**,而在它和已有账本的关系里:

1. **它不该动版本号。** 笔记不是计划的一部分 —— 写一份 20,000 字的正文不该新增
   `plan_revisions` 行、不该推进 `revision_version`。推了的话,用户一边看 AI 的分析
   一边补两句笔记,再点确认,会得到"计划已经变了",而计划一个字都没变。这一条由
   第 5 节钉住(它比读起来重要:它是**静默**的,只有后果会显形)。
2. **它该有自己的版本号。** 共用 `plan_nodes.content_version` 会变成"有人改了 300 字
   的简述 → 你 20,000 字的笔记保存失败"—— 冲突检测的范围必须和冲突的范围一样大。
3. **两个标签页同时发第一次保存**必须得到一个 409,而不是一次 500。第一次保存是
   唯一会 INSERT 的那一次,而没有锁的话两边都会读到"还没有行",第二个撞唯一约束 ——
   `api/errors.py` 刻意让 `IntegrityError` 照常 500,于是用户看到的是"服务器错误"。
   这是 `note_service.save` 里"先加锁、再读、再比、再写"那四步存在的唯一理由,
   第 3 节用一个真的并发请求验它。

## 上限是**拒绝**,不是截断

截断一段用户打的字比拒绝它糟得多:他会以为自己的东西存下来了。第 2 节因此不只
断言"被拒",还断言**库里原来那一份一个字符都没变**。
"""

from __future__ import annotations

import asyncio
import uuid

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.contracts.plan import MAX_NOTE_CODEPOINTS
from backend.db.models import NodeNote, PlanRevision


async def _plan(client: httpx.AsyncClient, account) -> dict:
    response = await client.get(
        f"/api/workspaces/{account.workspace_id}/plan", headers=account.headers
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _root_id(client: httpx.AsyncClient, account) -> str:
    return next(node["id"] for node in (await _plan(client, account))["nodes"] if node["parentId"] is None)


async def _make_node(client: httpx.AsyncClient, account, title: str) -> str:
    created = await client.post(
        f"/api/workspaces/{account.workspace_id}/nodes",
        json={"parentId": await _root_id(client, account), "title": title},
        headers=account.headers,
    )
    assert created.status_code == 201, created.text
    return created.json()["node"]["id"]


def _url(account, node_id: str) -> str:
    return f"/api/workspaces/{account.workspace_id}/nodes/{node_id}/notes"


async def _get(client: httpx.AsyncClient, account, node_id: str) -> httpx.Response:
    return await client.get(_url(account, node_id), headers=account.headers)


async def _put(
    client: httpx.AsyncClient, account, node_id: str, body: str, version: int | None = None
) -> httpx.Response:
    payload: dict = {"body": body}
    if version is not None:
        payload["expectedContentVersion"] = version
    return await client.put(_url(account, node_id), json=payload, headers=account.headers)


async def _stored_body(db: AsyncSession, node_id: str) -> str | None:
    return await db.scalar(
        select(NodeNote.body).where(NodeNote.node_id == uuid.UUID(node_id))
    )


async def _revision_count(db: AsyncSession, workspace_id: str) -> int:
    total = await db.scalar(
        select(func.count())
        .select_from(PlanRevision)
        .where(PlanRevision.workspace_id == uuid.UUID(workspace_id))
    )
    return int(total or 0)


# ---------------------------------------------------------------------------------
# 1. 从来没有写过不是错误
# ---------------------------------------------------------------------------------
async def test_a_node_that_never_had_a_note_reads_as_empty_version_zero(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession
) -> None:
    """**不是 404。** 编辑器打开时的第一件事就是这个 GET。

    用 404 表达"这个节点还没有笔记",会把一次完全正常的初次加载变成界面上的错误
    路径 —— 而它一点都不特殊。第 0 版是这个协议里"还没有"的唯一表示。
    """
    account = await make_account()
    node_id = await _make_node(app_client, account, "还没写过")

    response = await _get(app_client, account, node_id)
    assert response.status_code == 200, response.text
    note = response.json()
    assert note == {
        "nodeId": node_id,
        "body": "",
        "contentVersion": 0,
        "updatedAt": None,
    }, note
    # 读一次不该把行建出来。
    assert await _stored_body(db, node_id) is None


async def test_saving_an_empty_note_creates_no_row(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession
) -> None:
    """一个从没写过笔记的节点,保存一份空正文 —— 什么都不必存,也不必从此有第 1 版。

    "没有笔记"和"笔记是空的"是两件用户看不出区别的事。给它们两种表示,只会让
    界面多出一个无法解释的状态。
    """
    account = await make_account()
    node_id = await _make_node(app_client, account, "存个空的")

    response = await _put(app_client, account, node_id, "", version=0)
    assert response.status_code == 200, response.text
    assert response.json()["note"]["contentVersion"] == 0
    assert await _stored_body(db, node_id) is None


async def test_the_body_is_stored_verbatim(app_client: httpx.AsyncClient, make_account, db: AsyncSession) -> None:
    """长正文**原样存**:不去首尾空白、不做归一化。

    简述那套 `_clean`(去首尾空白、空串归 `None`)是给 300 字用的;用在两万字的
    正文上会把用户粘贴进来的排版吃掉 —— 缩进和空行在长文里是内容的一部分。
    """
    account = await make_account()
    node_id = await _make_node(app_client, account, "排版")
    body = "\n\n    第一段,前面有缩进。\n\n\n    第二段。\n\n"

    assert (await _put(app_client, account, node_id, body, version=0)).status_code == 200
    assert await _stored_body(db, node_id) == body
    assert (await _get(app_client, account, node_id)).json()["body"] == body


# ---------------------------------------------------------------------------------
# 2. 上限:拒绝,不是截断
# ---------------------------------------------------------------------------------
async def test_exactly_the_limit_is_accepted_and_one_more_is_refused(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession
) -> None:
    """20,000 能存,20,001 被拒 —— 而且**原来那一份一个字符都没变**。

    长短混排(星平面字符只要一个码点)是为了让"数错东西"也会在这里红:按 UTF-16
    数的实现会把 20,000 个星平面字符读成 40,000 并拒掉。
    """
    account = await make_account()
    node_id = await _make_node(app_client, account, "两万字")
    exactly = "🛫" * (MAX_NOTE_CODEPOINTS - 4) + "结尾四字"
    assert len(exactly) == MAX_NOTE_CODEPOINTS

    accepted = await _put(app_client, account, node_id, exactly, version=0)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["note"]["contentVersion"] == 1

    too_long = exactly + "溢"
    refused = await _put(app_client, account, node_id, too_long, version=1)
    assert refused.status_code == 400, refused.text
    error = refused.json()["error"]
    assert error["code"] == "INVALID_INPUT"
    assert str(MAX_NOTE_CODEPOINTS) in error["message"], error["message"]
    assert str(MAX_NOTE_CODEPOINTS + 1) in error["message"], error["message"]

    # 被拒之后库里还是上一份,版本号**没有**前进(`_checked` 在加锁之前就抛了)。
    assert await _stored_body(db, node_id) == exactly
    assert (await _get(app_client, account, node_id)).json()["contentVersion"] == 1


# ---------------------------------------------------------------------------------
# 3. 版本号:过期就拒,而且给出两个数
# ---------------------------------------------------------------------------------
async def test_a_stale_save_is_refused_with_both_numbers(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession
) -> None:
    """409 `CONCURRENCY_CONFLICT`,消息里两个数都在。

    只说"冲突了"等于让用户自己猜该不该覆盖 —— 而"你手上是第几版、库里是第几版"
    正是他判断的依据。
    """
    account = await make_account()
    node_id = await _make_node(app_client, account, "两个标签页")
    assert (await _put(app_client, account, node_id, "第一版")).status_code == 200

    stale = await _put(app_client, account, node_id, "我以为还是第一版", version=0)
    assert stale.status_code == 409, stale.text
    error = stale.json()["error"]
    assert error["code"] == "CONCURRENCY_CONFLICT"
    assert "0" in error["message"] and "1" in error["message"], error["message"]
    # `details` 的键名跟**节点正文那条锁**(`PATCH /nodes/{id}`)一致,是 snake_case,
    # 而不是接口其余部分的 camelCase —— 那一条已经被 `test_node_content_version.py`
    # 钉住了,两条锁给客户端的是同一套键名,客户端处理冲突时只需要写一段。
    # (`StaleBaseRevision` 那边用的是 camelCase,全仓确实是混的;这不是本批要动的。)
    assert error["details"]["content_version"] == 1
    assert error["details"]["expected_content_version"] == 0

    # 别人刚写的那一份活着。
    assert await _stored_body(db, node_id) == "第一版"

    # 拿着新版本号重发就通过 —— 这正是 `details.contentVersion` 的用处。
    retried = await _put(app_client, account, node_id, "我以为还是第一版", version=1)
    assert retried.status_code == 200, retried.text
    assert retried.json()["note"]["contentVersion"] == 2


async def test_the_note_version_is_not_the_node_body_version(
    app_client: httpx.AsyncClient, make_account
) -> None:
    """**两个号。** 改节点简述不该让笔记的版本号作废,反过来也一样。

    共用会变成"有人改了 300 字的说明 → 你 20,000 字的笔记保存失败":冲突检测的
    范围必须和冲突的范围一样大。
    """
    account = await make_account()
    node_id = await _make_node(app_client, account, "两条账")
    node = next(node for node in (await _plan(app_client, account))["nodes"] if node["id"] == node_id)

    assert (await _put(app_client, account, node_id, "正文", version=0)).status_code == 200

    # 改简述(会推进**节点**的正文版本号)。
    patched = await app_client.patch(
        f"/api/workspaces/{account.workspace_id}/nodes/{node_id}",
        json={"description": "改一下说明", "contentVersion": node["contentVersion"]},
        headers=account.headers,
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["node"]["contentVersion"] == node["contentVersion"] + 1

    # 笔记那一版仍然是 1 —— 手里拿着 1 的编辑器还能存下去。
    assert (await _get(app_client, account, node_id)).json()["contentVersion"] == 1
    assert (await _put(app_client, account, node_id, "正文再写一句", version=1)).status_code == 200


async def test_two_tabs_saving_the_first_version_at_once_one_wins_one_gets_409(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession
) -> None:
    """**并发压测:两个标签页同时发第一次保存(都带 `contentVersion: 0`)。**

    这是全文件里唯一一条真的并发的用例,因为它是唯一会 INSERT 的那一次。没有那把
    空间锁的话,两个请求都会读到"还没有行",两边都 INSERT,第二个撞
    `uq_node_notes_node_id` —— 而 `api/errors.py` 刻意让 `IntegrityError` 照常 500,
    于是用户看到的是"服务器错误",而不是"有人在别处改了这份笔记"。

    断言的是**结果集合**,不是先后:必须恰好一个 200 一个 409,而且**绝不能有 500**。
    """
    account = await make_account()
    node_id = await _make_node(app_client, account, "同时保存")

    first, second = await asyncio.gather(
        _put(app_client, account, node_id, "甲写的那一份", version=0),
        _put(app_client, account, node_id, "乙写的那一份", version=0),
    )
    codes = sorted([first.status_code, second.status_code])
    assert codes == [200, 409], (first.text, second.text)

    # 落下去的那一份与赢家一致,而且只可能是一行。
    winner = first if first.status_code == 200 else second
    assert await _stored_body(db, node_id) == winner.json()["note"]["body"]
    assert await db.scalar(select(func.count()).select_from(NodeNote)) == 1


# ---------------------------------------------------------------------------------
# 4. 边界:归属、归档
# ---------------------------------------------------------------------------------
async def test_another_account_cannot_read_or_write_the_note(
    app_client: httpx.AsyncClient, make_account
) -> None:
    """笔记跟着节点走:别人的节点 id 读不到、也写不进去。

    两条路由都在 `test_authz_matrix.py` 里登记过"要登录",但"要登录"与"要归属"
    是两件事 —— 这一条验的是后者。
    """
    owner = await make_account(email="owner@example.com")
    other = await make_account(email="other@example.com", workspace_title="别人的空间")
    node_id = await _make_node(app_client, owner, "我的节点")
    assert (await _put(app_client, owner, node_id, "只有我能写")).status_code == 200

    # 换个空间去访问同一个 node_id:那个节点不在他的空间里。
    assert (await app_client.get(_url(other, node_id), headers=other.headers)).status_code == 404
    assert (
        await app_client.put(
            _url(other, node_id), json={"body": "越界"}, headers=other.headers
        )
    ).status_code == 404


async def test_an_archived_node_has_no_note_route(
    app_client: httpx.AsyncClient, make_account
) -> None:
    """已归档的节点同样 404 —— 与其它节点路由一致(走的是同一个 `load_node`)。"""
    account = await make_account()
    node_id = await _make_node(app_client, account, "要归档的")
    assert (await _put(app_client, account, node_id, "归档之前写的")).status_code == 200

    archived = await app_client.delete(
        f"/api/workspaces/{account.workspace_id}/nodes/{node_id}?mode=archive",
        headers=account.headers,
    )
    assert archived.status_code in (200, 204), archived.text

    assert (await _get(app_client, account, node_id)).status_code == 404


# ---------------------------------------------------------------------------------
# 5. 它**不是**计划的一部分
# ---------------------------------------------------------------------------------
async def test_writing_a_note_creates_no_revision_and_does_not_move_the_plan_version(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession
) -> None:
    """笔记写入不新增 `plan_revisions` 行、不推进 `revision_version`。

    这是全文件里最容易被"顺手统一一下"破坏的一条:让笔记走 `_each_change` 看起来
    更整齐(所有写操作同一个上下文),后果却是每次自动保存都记一版 —— 用户复盘时
    要问的"V7 把我哪个排期挪走了"得从几百版里翻。

    还有一个更直接的后果:`revision_version` 一推进,**待确认的提案就凭空失效**。
    用户一边看 AI 的分析一边补两句笔记,再点确认,得到"计划已经变了",而计划
    一个字都没变。
    """
    account = await make_account()
    node_id = await _make_node(app_client, account, "不该记版本")
    before_revisions = await _revision_count(db, account.workspace_id)
    before_version = (await _plan(app_client, account))["revisionVersion"]

    for index in range(3):
        assert (await _put(app_client, account, node_id, f"第 {index} 次自动保存")).status_code == 200

    assert await _revision_count(db, account.workspace_id) == before_revisions
    assert (await _plan(app_client, account))["revisionVersion"] == before_version
