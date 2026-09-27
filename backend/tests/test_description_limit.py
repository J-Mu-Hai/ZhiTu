"""简述的 300 码点上限,以及**用户选的那条豁免**(v1.2 §2.1)。

## 这个文件在防什么

上限本身很好写,写错的方式却全在边上:

1. **数错了东西**。`len()` 在 Python 上数的是**码点**,而 JavaScript 的 `s.length`
   数的是 UTF-16 码元 —— 一个星平面字符在前者是 1、后者是 2。前后端要是各按各的
   数,同一次粘贴就会一边收下、一边拒掉,而界面上没有任何东西能解释这件事。
   所以这里**故意混排**:CJK、ASCII、换行、星平面 emoji,凑到正好 300。
2. **把检查放在锁外面**。300 是一条**条件规则**(存量超限则豁免),要比的那一份
   "存量"只有读到节点才知道 —— 而节点是在工作区锁里面读的。写在锁外面,两个
   标签页就能把"一份 900 字的正文"挤成一个 301 字的新值。
3. **把豁免做成"只读"**。用户选的不是"冻结那些老正文",而是"它继续想写多长写多长,
   改到 300 以内之后才按 300 算"。所以第 3 节验的是**它能被继续改长** —— 如果哪天
   有人把豁免实现成"超限的节点只能读",那条用例会红。

## 为什么走 HTTP

`description_length_error` 是一个纯函数,单独调它并不能证明"接口会拒"。这里的
断言都落在响应码与库里那一行的真实内容上。
"""

from __future__ import annotations

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

#: 一个星平面字符(码点 1,UTF-16 码元 2)。挑它是因为上下限的错配**只**在这种字符上
#: 显形:如果哪一层改成数 UTF-16,下面那条"正好 300"会变成 301 而被拒。
_ASTRAL = "🛫"

MAX = 300


def _mixed(length: int) -> str:
    """CJK + ASCII + 换行 + 星平面字符,凑到正好 `length` **码点**。

    换行放在中间而不是两端:`node_service._clean` 会去掉首尾空白,而这里要验的是
    上限,不该被一段无关的归一化搅进来。
    """
    unit = f"学{_ASTRAL}z\n"
    assert len(unit) == 4
    repeats, rest = divmod(length, 4)
    if rest == 0:
        # 别以换行结尾:`node_service._clean` 会去掉首尾空白,于是一段"正好 300"
        # 的串存下来会变成 299。那是**归一化**,不是上限 —— 混进同一条断言里,
        # 将来它红了会让人以为上限错了。
        repeats -= 1
        rest = 4
    return unit * repeats + "好" * rest


async def _plan(client: httpx.AsyncClient, account) -> dict:
    response = await client.get(
        f"/api/workspaces/{account.workspace_id}/plan", headers=account.headers
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _root_id(client: httpx.AsyncClient, account) -> str:
    return (await _plan(client, account))["nodes"][0]["id"]


async def _create(
    client: httpx.AsyncClient, account, *, title: str, description: str | None = None
) -> httpx.Response:
    body: dict = {
        "parentId": await _root_id(client, account),
        "title": title,
        "nodeType": "task",
    }
    if description is not None:
        body["description"] = description
    return await client.post(
        f"/api/workspaces/{account.workspace_id}/nodes", json=body, headers=account.headers
    )


async def _patch(
    client: httpx.AsyncClient, account, node_id: str, **fields: object
) -> httpx.Response:
    return await client.patch(
        f"/api/workspaces/{account.workspace_id}/nodes/{node_id}",
        json=fields,
        headers=account.headers,
    )


async def _node(client: httpx.AsyncClient, account, title: str) -> dict:
    nodes = (await _plan(client, account))["nodes"]
    matches = [node for node in nodes if node["title"] == title]
    assert len(matches) == 1, [node["title"] for node in nodes]
    return matches[0]


# ---------------------------------------------------------------------------------
# 1. 上限本身
# ---------------------------------------------------------------------------------


async def test_exactly_300_codepoints_is_accepted(app_client: httpx.AsyncClient, make_account) -> None:
    """正好 300 码点能存,而且**一个字符都没被改动**地存下来。

    "混排"是这个用例的全部要点:同一段文字里同时有 1 码点、2 码元(星平面)、
    以及作为内容一部分的换行。
    """
    account = await make_account()
    description = _mixed(MAX)
    assert len(description) == MAX

    response = await _create(app_client, account, title="正好三百", description=description)
    assert response.status_code == 201, response.text

    stored = (await _node(app_client, account, "正好三百"))["description"]
    assert stored == description, "存下来的和写进去的不是同一个字符串"


async def test_301_codepoints_is_rejected_and_nothing_is_written(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession
) -> None:
    """301 被拒(400 `INVALID_INPUT`),节点**没有**被建出来。

    消息里必须同时有上限和这一份的长度 —— 只说"太长了"等于让用户自己数。
    """
    account = await make_account()
    description = _mixed(MAX + 1)

    response = await _create(app_client, account, title="三百零一", description=description)
    assert response.status_code == 400, response.text
    error = response.json()["error"]
    assert error["code"] == "INVALID_INPUT"
    assert str(MAX) in error["message"], error["message"]
    assert str(MAX + 1) in error["message"], error["message"]

    assert "三百零一" not in [node["title"] for node in (await _plan(app_client, account))["nodes"]]
    assert await db.scalar(text("SELECT count(*) FROM plan_nodes WHERE title = '三百零一'")) == 0


async def test_the_limit_counts_codepoints_not_utf16_units(
    app_client: httpx.AsyncClient, make_account
) -> None:
    """**这条是上下限错配的哨兵。**

    300 个星平面字符:按码点是 300(该通过),按 UTF-16 码元是 600(会被误拒)。
    只要有人把计数改成 `s.length` 那一套,这条就红 —— 而它在界面上表现为
    "输入框明明没满,保存却说太长了"。
    """
    account = await make_account()
    description = _ASTRAL * MAX
    assert len(description) == MAX
    assert len(description.encode("utf-16-le")) // 2 == MAX * 2

    assert (await _create(app_client, account, title="三百个飞机", description=description)).status_code == 201
    assert (await _node(app_client, account, "三百个飞机"))["description"] == description

    # 反过来:301 个星平面字符必须被拒。
    too_long = await _create(app_client, account, title="三百零一个飞机", description=_ASTRAL * (MAX + 1))
    assert too_long.status_code == 400, too_long.text
    assert str(MAX + 1) in too_long.json()["error"]["message"]


async def test_the_limit_applies_to_updates_too(app_client: httpx.AsyncClient, make_account) -> None:
    """新写的说明同样受限,不只是创建那一刻。"""
    account = await make_account()
    created = (await _create(app_client, account, title="短说明", description="一句话")).json()["node"]

    response = await _patch(app_client, account, created["id"], description=_mixed(MAX + 1))
    assert response.status_code == 400, response.text

    assert (await _node(app_client, account, "短说明"))["description"] == "一句话"


# ---------------------------------------------------------------------------------
# 2. 豁免:存量已经超限的,继续想写多长写多长
# ---------------------------------------------------------------------------------


async def _force_long(db: AsyncSession, node_id: str, body: str) -> None:
    """绕过接口,直接把一行写成超限的存量。

    这是**故意的**:那条豁免规则存在的全部理由,正是"上限出现之前用户已经写下的
    长说明"。要造出那种存量,只能绕过接口 —— 而绕过接口也正是它的现实来源
    (老版本的服务端没有这条上限)。
    """
    await db.execute(
        text("UPDATE plan_nodes SET description = :body WHERE id = :id"),
        {"body": body, "id": node_id.replace("-", "")},
    )
    await db.commit()


async def test_a_long_existing_description_is_exempt_and_survives_edits(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession
) -> None:
    """**豁免回归**(用户明确选过的那条)。

    一次走完四步,顺序本身就是断言:
    1. 700 码点的存量,`GET /plan` 原样返回;
    2. 只改标题 —— 说明一个字都不动;
    3. 把它**改得更长**(701 码点)也通过:豁免是"继续想写多长写多长";
    4. 但把它**改到 200 码点**之后,再写 301 就被拒 —— 豁免跟着那份长正文一起消失。
    """
    account = await make_account()
    created = (await _create(app_client, account, title="老正文")).json()["node"]
    long_body = _mixed(700)
    await _force_long(db, created["id"], long_body)

    # 1. 读得到,一个字不差。
    assert (await _node(app_client, account, "老正文"))["description"] == long_body

    # 2. 改标题:说明不动。
    renamed = await _patch(app_client, account, created["id"], title="老正文(改过标题)")
    assert renamed.status_code == 200, renamed.text
    assert (await _node(app_client, account, "老正文(改过标题)"))["description"] == long_body

    # 3. 还可以更长。
    longer = _mixed(701)
    grown = await _patch(app_client, account, created["id"], description=longer)
    assert grown.status_code == 200, grown.text
    assert (await _node(app_client, account, "老正文(改过标题)"))["description"] == longer

    # 4. 收进 300 之后,豁免结束 —— 301 从此被拒。
    assert (await _patch(app_client, account, created["id"], description=_mixed(200))).status_code == 200
    refused = await _patch(app_client, account, created["id"], description=_mixed(MAX + 1))
    assert refused.status_code == 400, refused.text
    # 被拒之后库里还是那 200 码点,不是"半写进去"的状态。
    assert (await _node(app_client, account, "老正文(改过标题)"))["description"] == _mixed(200)


async def test_a_description_of_exactly_limit_plus_one_on_an_exempt_node_still_grows(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession
) -> None:
    """301 这一个具体数字上的边界。

    存量 299(没超限)→ 写 301 被拒;存量 301(超限)→ 写 301 通过。同一个数字,
    两个结果 —— 差别只在那份**存量**。条件规则的全部含义就在这一点上,而它最容易
    被后来的人"顺手改成一条普通上限"。
    """
    account = await make_account()
    created = (await _create(app_client, account, title="边界")).json()["node"]

    await _force_long(db, created["id"], _mixed(299))
    assert (await _patch(app_client, account, created["id"], description=_mixed(301))).status_code == 400

    await _force_long(db, created["id"], _mixed(301))
    assert (await _patch(app_client, account, created["id"], description=_mixed(301))).status_code == 200
