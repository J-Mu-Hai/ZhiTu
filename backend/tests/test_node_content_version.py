"""正文的乐观锁:`plan_nodes.content_version` 的**写侧**。

## 这一组在防什么

`content_version` 这一列、`PlanNodePayload.content_version` 这个契约字段、以及
`CONCURRENCY_CONFLICT` 这个错误码**都已经在了**,`/plan` 也一直在把它发出去 ——
但 `PATCH /nodes/{id}` 既不收它、也不比对、更不推进它。也就是说这套锁**只有读侧**:
客户端手里拿着一个版本号,而服务端从来不问它是哪一版。

后果是那条最难发现的坏:**两个标签页各打开一次同一个节点,后保存的那个把先保存的
正文整段吃掉,而两边都显示"保存成功"。** 谁都不会收到任何提示 —— 用户是在几天后
回头看的时候才发现自己写的那一段没了。

## 三条边界,都是有意的

1. **不带版本号就是不检查。** 内部调用方(提案确认、排期、归档恢复)本来就持有工作区
   锁,不该被一个"你手上那份旧了"挡住。所以这是**可选前置条件**,不是必填字段。
2. **只有正文(`description` / `acceptance_criteria`)推进版本号。** 标题、优先级、
   工时是逐字段 PATCH 的,两个标签页分别改这两类字段互不覆盖 —— 让对方被判成冲突
   只会逼用户做一次没有意义的"覆盖"。
3. **版本号不是一个能赋值的字段。** 它在 `EDITABLE_FIELDS` 之外;传进来只当断言用,
   传一个和库里不一样的数只会得到 409,而**不会**把列改成那个数(见最后一条用例)。
"""

from __future__ import annotations

import uuid

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models import PlanRevision


async def _plan(client: httpx.AsyncClient, account) -> dict:
    response = await client.get(
        f"/api/workspaces/{account.workspace_id}/plan", headers=account.headers
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _root_id(client: httpx.AsyncClient, account) -> str:
    plan = await _plan(client, account)
    return next(node["id"] for node in plan["nodes"] if node["parentId"] is None)


async def _make_node(client: httpx.AsyncClient, account, title: str) -> dict:
    """建一个节点,返回它在 `/plan` 里的那一份 payload(带 `contentVersion`)。

    **走接口而不是直接写库**(同 `conftest.make_account` 的理由):手写一行的话,
    "版本号是 1"这件事就成了测试自己造的,而不是产品建出来的。
    """
    created = await client.post(
        f"/api/workspaces/{account.workspace_id}/nodes",
        json={"parentId": await _root_id(client, account), "title": title},
        headers=account.headers,
    )
    assert created.status_code == 201, created.text
    node_id = created.json()["node"]["id"]
    return await _node_payload(client, account, node_id)


async def _node_payload(client: httpx.AsyncClient, account, node_id: str) -> dict:
    plan = await _plan(client, account)
    return next(node for node in plan["nodes"] if node["id"] == node_id)


async def _patch(
    client: httpx.AsyncClient, account, node_id: str, body: dict
) -> httpx.Response:
    return await client.patch(
        f"/api/workspaces/{account.workspace_id}/nodes/{node_id}",
        json=body,
        headers=account.headers,
    )


async def _revision_count(db: AsyncSession, workspace_id: str) -> int:
    # 直接比字符串会炸在 UUID 的绑定处理上(`'str' object has no attribute 'hex'`)——
    # `account.workspace_id` 是**接口返回的字符串**,而列是 `Uuid`。
    total = await db.scalar(
        select(func.count())
        .select_from(PlanRevision)
        .where(PlanRevision.workspace_id == uuid.UUID(workspace_id))
    )
    return int(total or 0)


# ---------------------------------------------------------------------------------
# 成功路径:版本真的推进了,而且**新版本号回得来**
# ---------------------------------------------------------------------------------
async def test_saving_the_body_bumps_the_version_and_hands_the_new_one_back(
    app_client: httpx.AsyncClient, make_account
) -> None:
    """保存成功之后,响应里的版本号必须是**新**的那一个。

    回旧的话客户端会一直拿着一份过期的号,于是它下一次保存必然 409 —— 用户会看到
    "明明刚保存成功,现在又说冲突了"。这一条钉的是那个衔接点,不是"数字加一"。
    """
    account = await make_account()
    node = await _make_node(app_client, account, "要写正文的")
    assert node["contentVersion"] == 1, "新节点的正文版本从 1 开始"

    saved = await _patch(
        app_client,
        account,
        node["id"],
        {"description": "第一版正文:这周先做实验。", "contentVersion": 1},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["node"]["contentVersion"] == 2

    # 库里和 `/plan` 里都必须是 2 —— 只有响应体改了、`/plan` 还是 1 的话,
    # 用户一刷新就会拿到旧号,下一次保存又变成 409。
    assert (await _node_payload(app_client, account, node["id"]))["contentVersion"] == 2

    # 拿回来的新号能直接接着用:这是"连续保存"整条路的样子。
    again = await _patch(
        app_client,
        account,
        node["id"],
        {"description": "第二版正文:实验结果要补一组对照。", "contentVersion": 2},
    )
    assert again.status_code == 200, again.text
    assert again.json()["node"]["contentVersion"] == 3
    assert (
        await _node_payload(app_client, account, node["id"])
    )["description"] == "第二版正文:实验结果要补一组对照。"


# ---------------------------------------------------------------------------------
# 冲突:整条请求不生效,而不是"只改能改的那一半"
# ---------------------------------------------------------------------------------
async def test_a_stale_save_is_refused_and_the_other_writers_text_survives(
    app_client: httpx.AsyncClient, make_account
) -> None:
    """两个标签页同抢一份正文:后到的那个拿着旧号 → 409,**一个字都不写**。

    这一条就是这套锁存在的全部理由。它必须证明两件事:拒绝发生了,以及**赢家写进去
    的那段字还在** —— 只断言状态码的话,一个"先写进去再报 409"的实现也能绿。
    """
    account = await make_account()
    node = await _make_node(app_client, account, "两个人都在写")

    # 甲:我这边看到的是第 1 版,写下第一段。
    first = await _patch(
        app_client, account, node["id"], {"description": "甲写的这一段。", "contentVersion": 1}
    )
    assert first.status_code == 200, first.text

    # 乙:手上还是第 1 版(它没刷新过),写下第二段。
    stale = await _patch(
        app_client, account, node["id"], {"description": "乙写的这一段。", "contentVersion": 1}
    )
    assert stale.status_code == 409, stale.text
    body = stale.json()["error"]
    assert body["code"] == "CONCURRENCY_CONFLICT"
    # 报错要带上**此刻库里那一版**,否则客户端除了"刷新整页"没有别的选择。
    assert body["details"]["content_version"] == 2
    assert body["details"]["expected_content_version"] == 1
    # 消息里得有两个数字:用户要能判断"我手上这份到底旧了多少"。
    assert "2" in body["message"] and "1" in body["message"]

    # 赢家那段字没被吃掉,而且版本号没有因为那次失败的写入动过。
    after = await _node_payload(app_client, account, node["id"])
    assert after["description"] == "甲写的这一段。"
    assert after["contentVersion"] == 2


async def test_a_refused_save_does_not_half_apply(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession
) -> None:
    """被拒的那一次**一个字段都不许改**,也不许留下版本记录。

    半个请求生效比整个失败更难查:界面说"保存失败",刷新却发现工时变了。所以那条
    请求里除了正文还带着一个标题改动 —— 它也必须没生效。

    版本记录那一半同样是必需的:`_each_change` 是**每次编辑一个版本**的账本,一次
    回滚掉的编辑如果在账本里留下一个空版本,复盘时就会看到"有一版什么都没改"。
    """
    account = await make_account()
    node = await _make_node(app_client, account, "别改我一半")

    first = await _patch(
        app_client, account, node["id"], {"description": "第一次。", "contentVersion": 1}
    )
    assert first.status_code == 200, first.text
    before_revisions = await _revision_count(db, account.workspace_id)

    refused = await _patch(
        app_client,
        account,
        node["id"],
        {"title": "被顺手改掉的标题", "description": "第二段进不去的。", "contentVersion": 1},
    )
    assert refused.status_code == 409, refused.text

    after = await _node_payload(app_client, account, node["id"])
    assert after["description"] == "第一次。", "正文不该变"
    assert after["title"] == "别改我一半", "同一条请求里的标题也不许生效"
    assert after["contentVersion"] == 2
    assert await _revision_count(db, account.workspace_id) == before_revisions, (
        "被回滚掉的编辑不该在版本账本里留下一条"
    )


# ---------------------------------------------------------------------------------
# 边界:不带版本号 = 不检查(内部调用方走这条路)
# ---------------------------------------------------------------------------------
async def test_without_a_version_the_save_still_goes_through(
    app_client: httpx.AsyncClient, make_account
) -> None:
    """不带 `contentVersion` 时照旧写入 —— 这是**有意**留的,不是漏了。

    提案确认、排期、归档恢复都在服务层改节点,它们本来就持有工作区锁;要求它们
    也带一个客户端版本号,只会把"锁"变成"每一次内部写入都要先读一次正文"。
    """
    account = await make_account()
    node = await _make_node(app_client, account, "内部写入")

    response = await _patch(app_client, account, node["id"], {"description": "服务层写的。"})
    assert response.status_code == 200, response.text
    assert response.json()["node"]["contentVersion"] == 2
    assert (await _node_payload(app_client, account, node["id"]))["description"] == "服务层写的。"


async def test_a_title_only_edit_does_not_bump_the_body_version(
    app_client: httpx.AsyncClient, make_account
) -> None:
    """改标题不动正文版本号,另一个标签页的正文保存照旧能成。

    这是那条"只有正文推进版本"的规则**在用户身上的样子**:甲改标题、乙改正文,
    两个人碰的不是同一份数据,不该有人被判冲突。反过来(改标题也 +1)的表现是:
    乙明明什么都没被别人改过,却被要求"要么覆盖、要么放弃"。
    """
    account = await make_account()
    node = await _make_node(app_client, account, "标题归标题")

    renamed = await _patch(
        app_client, account, node["id"], {"title": "换了个名字", "contentVersion": 1}
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["node"]["contentVersion"] == 1, "只改标题不该推进正文版本"

    # 乙手上仍是第 1 版(它读到的就是那一版),保存正文——应该顺利。
    body = await _patch(
        app_client, account, node["id"], {"description": "乙的正文。", "contentVersion": 1}
    )
    assert body.status_code == 200, body.text
    after = await _node_payload(app_client, account, node["id"])
    assert after["title"] == "换了个名字", "乙保存正文不该把标题带回去"
    assert after["description"] == "乙的正文。"


# ---------------------------------------------------------------------------------
# 版本号是断言,不是可赋值的列
# ---------------------------------------------------------------------------------
async def test_the_version_cannot_be_used_to_overwrite_the_column(
    app_client: httpx.AsyncClient, make_account
) -> None:
    """传一个和库里不一样的版本号 → 409,而**列不会被改成那个数**。

    如果 `content_version` 混进了 `EDITABLE_FIELDS`,它就是一次普通赋值:谁都能把
    版本号写成任意数字 —— 包括**写大**,那就等于把锁拆了(下一次旧客户端带着一个
    小号来,反而永远对不上,或者写成一个"未来版"让真正的旧客户端误判成最新)。

    这一条同时钉住路由那一处 `pop`:混在 `patch` 里的话,它会先撞上白名单,
    报成 400 `InvalidInput`("这些字段不能直接改")—— 那会把"你手上那份旧了"
    报成"这个字段不能改",用户永远修不好。
    """
    account = await make_account()
    node = await _make_node(app_client, account, "版本号不是字段")

    attempt = await _patch(
        app_client, account, node["id"], {"title": "顺手改一下", "contentVersion": 99}
    )
    assert attempt.status_code == 409, attempt.text
    assert attempt.json()["error"]["code"] == "CONCURRENCY_CONFLICT"

    after = await _node_payload(app_client, account, node["id"])
    assert after["contentVersion"] == 1, "版本号没有被写成 99"
    assert after["title"] == "版本号不是字段", "同一条请求里的标题也没生效"
