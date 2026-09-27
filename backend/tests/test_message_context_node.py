"""发消息时 `contextNodeId` 的归属校验。

## 这条测试针对的是一个真实的缺口

`POST /workspaces/{id}/messages` 里的 `contextNodeId` 来自请求体,而它一路都被当成
事实在用:写进 `messages.context_node_id` 那一行、决定这一轮的范围与焦点、还决定分析
记录的 `focusNodeId`。**它此前一个校验都没有。**

后果不是崩溃,是**静默错位** —— 这才是它难被发现的原因。`load_scope` 只把落在范围内的
焦点认下来,范围外的**悄悄当成没给**(那个判断的职责是防止"上一处的选中"被当成"这一轮
在聊什么",不是鉴权)。于是拿别人空间的节点 id 发消息,拿到的是 200、一条记着那个 id
的用户消息、和一轮**没有焦点**的对话:没有任何迹象说明那个 id 被丢掉了。用户以为自己
说的是那个节点,模型以为他没指。

现在两条路(发消息、重新分析)在 `submit_turn` 里共用同一处校验,非法 id 一律 404。

## 四种非法 id 在修之前**不是同一种表现**,这也是它们分成四条用例的理由

`messages.context_node_id` 上有个外键,于是:

| 情况 | 修之前 | 为什么 |
| --- | --- | --- |
| 别的空间 / 已归档 | **200**,id 原样落库并回显 | 那些行真的在 `plan_nodes` 里,外键拦不住 |
| 从没存在过 | 写入抛 IntegrityError,从错误路径上炸出去 | 外键拦得住,但拦法是一场崩溃,不是一个答案 |

所以"不校验"这件事在这里有两种味道,一种是**静默**,一种是**崩**。分开测是因为
修法可能只堵住其中一种:只加外键处理就只解决后者,只加归属判断就只解决前者。
(顺便:`ForeignKey` 管的是"这一行在不在",管不了"这一行属不属于你"。)

## 四件事一起断言,因为它们各有各的失效方式

1. **404 而不是 403** —— 403 会确认"这个节点存在但不属于你",等于把别人空间里有哪些
   节点告诉了任何人(理由见 `NodeNotFound`);
2. **一次模型都没调** —— 假模型记下它收到的每一个 `TurnContext`,"节点正文进了提示词"
   这件事只有在这里才看得见;
3. **一行都没写** —— `snapshot(db)` 逐表对比,比逐个表去数更可靠(漏掉一张表就等于
   漏掉一条回归);
4. **合法节点必须真的能用** —— 少了这一半,前三条可能只是"因为 id 是假的所以 404"。

第 4 条不是凑数:跨用户那条用例里 A 用**同一个 id** 先成功一次,就是在证明这个 id 是
真的存在、真的能用,而不是随便编的。
"""

from __future__ import annotations

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from backend.tests.conftest import FakeReasoner, snapshot

#: 一个格式合法、但任何库里都没有的 id。
#: 用 `00000000-0000-4000-8000-...` 而不是随机 uuid:随机的话失败信息里那串数字
#: 每次都不一样,而且它偶尔会碰巧撞上一个真 id(概率低,但不是零)。
ABSENT = "00000000-0000-4000-8000-0000000000ff"


async def _root_id(client: httpx.AsyncClient, account) -> str:
    """这个空间的根目标。**走接口取** —— 手工往库里插一个根会和 `workspace_service`
    建的那一份悄悄分家(比如漏掉 `node_type=goal`),而那是另一条链上的事实。"""
    plan = await client.get(
        f"/api/workspaces/{account.workspace_id}/plan", headers=account.headers
    )
    assert plan.status_code == 200, plan.text
    return next(node["id"] for node in plan.json()["nodes"] if node["parentId"] is None)


async def _send(
    client: httpx.AsyncClient, account, *, context_node_id: str | None = None, content: str = "这个节点帮我看看"
) -> httpx.Response:
    payload: dict[str, object] = {"content": content}
    if context_node_id is not None:
        payload["contextNodeId"] = context_node_id
    return await client.post(
        f"/api/workspaces/{account.workspace_id}/messages",
        json=payload,
        headers=account.headers,
    )


async def _set_body(client: httpx.AsyncClient, account, node_id: str, body: str) -> None:
    response = await client.patch(
        f"/api/workspaces/{account.workspace_id}/nodes/{node_id}",
        json={"description": body},
        headers=account.headers,
    )
    assert response.status_code == 200, response.text


def _refused(
    response: httpx.Response,
    fake: FakeReasoner,
    before: dict[str, int],
    after: dict[str, int],
    *,
    calls_before: int = 0,
) -> None:
    """非法请求的四条共同断言。

    合成一个函数是因为**它们必须一起成立**:只查状态码的话,一条"404 但已经把用户
    那句话写进库了"的实现在这条用例里仍然是绿的。

    `calls_before` 是给"先让合法的那一次成功、再发非法的那一次"的用例用的
    (跨用户那条必须这么做,否则它的 404 无法与"id 是编的"区分开)。用**次数没涨**
    而不是"列表是空的"来表达,是因为要断言的一直是"这一轮没有再调一次模型"。
    """
    assert response.status_code == 404, response.text
    # 403 会确认"这个节点存在但不属于你" —— 拿 id 逐个试就能测绘出别人空间里有什么。
    assert response.status_code != 403
    assert response.json()["error"]["code"] == "NODE_NOT_FOUND"
    assert len(fake.calls) == calls_before, "节点都没确认下来,却已经调过一次模型"
    assert after == before, "非法请求产生了写入"


# ---------------------------------------------------------------------------------
# 非法:别人的节点
# ---------------------------------------------------------------------------------
async def test_b_cannot_point_at_a_node_in_as_space(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    """B 拿 A 的节点 id 发消息 -> 404,而且这个 id 对 A 自己是好用的。

    后半句是这条用例的支点:少了它,"404"可能只是因为那个 id 根本不存在,
    于是这条用例在**任何**实现下都是绿的 —— 包括完全没有校验的那一版(不,那一版是
    200;但包括一个"把所有 contextNodeId 都拒掉"的过度修正)。
    """
    account_a = await make_account(email="a@example.com", workspace_title="Python 学习")
    account_b = await make_account(email="b@example.com", workspace_title="英语提升")
    node_id = await _root_id(app_client, account_a)
    secret = "这段正文只属于 A,不该出现在任何别人的请求里。"
    await _set_body(app_client, account_a, node_id, secret)

    fake = use_reasoner(FakeReasoner())

    mine = await _send(app_client, account_a, context_node_id=node_id)
    assert mine.status_code == 200, mine.text
    assert len(fake.calls) == 1
    # 焦点真的生效了 —— 服务端把节点**标题**给了模型(不是 id,见 runtime/base.py),
    # 所以这里断言标题。它同时证明了"合法 id 会被认下来"这件事本身是通的。
    assert fake.calls[0].context_node_title == "Python 学习"

    calls_after_a = len(fake.calls)
    before = await snapshot(db)
    theirs = await _send(app_client, account_b, context_node_id=node_id)
    after = await snapshot(db)

    _refused(theirs, fake, before, after, calls_before=calls_after_a)
    # 模型没被调用,所以正文不可能进过提示词;这一条查的是另一个出口 ——
    # 它有没有被回显在响应里。两个出口都堵上,才叫"没读到"。
    assert secret not in theirs.text, "A 的节点正文出现在了 B 的响应里"


# ---------------------------------------------------------------------------------
# 非法:自己的另一个空间
# ---------------------------------------------------------------------------------
async def test_a_node_from_another_of_my_own_spaces_is_refused(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    """同一个人,空间不对 -> 同样 404。

    这条与"别人的节点"分开写,是因为它们的**过滤维度**不同:那条靠 `ctx.owner_id`,
    这条靠 `ctx.id`。只按用户过滤、忘了按空间过滤的实现,在那条用例上是绿的 ——
    而它会让用户在"英语提升"里说的一句话悄悄记到"Python 学习"的某个节点上。
    """
    account = await make_account(workspace_title="Python 学习")
    second = await app_client.post(
        "/api/workspaces", json={"title": "英语提升"}, headers=account.headers
    )
    assert second.status_code == 201, second.text
    other_root = second.json()["rootNode"]["id"]
    assert other_root != await _root_id(app_client, account)

    fake = use_reasoner(FakeReasoner())
    before = await snapshot(db)
    response = await _send(app_client, account, context_node_id=other_root)
    after = await snapshot(db)

    _refused(response, fake, before, after)


# ---------------------------------------------------------------------------------
# 非法:不存在
# ---------------------------------------------------------------------------------
async def test_a_node_that_never_existed_is_refused(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    """编一个 id -> 与"别人的节点"同一个错误、同一个状态码。

    这一条在修之前**不是静默的,是崩的**:外键拦住了这次写入,但拦法是
    `_insert_user_message` 里一次 IntegrityError,被那层的重试循环当成 seq 冲突又试了
    几次,最后从错误路径上炸出去。所以它和上面那条要分开测 —— 一个只加了归属判断的
    修法会让上面那条绿、这条仍然炸(反之亦然)。"""
    account = await make_account()
    fake = use_reasoner(FakeReasoner())
    before = await snapshot(db)
    response = await _send(app_client, account, context_node_id=ABSENT)
    after = await snapshot(db)

    _refused(response, fake, before, after)


# ---------------------------------------------------------------------------------
# 非法:已归档
# ---------------------------------------------------------------------------------
async def test_an_archived_node_is_refused(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    """归档过的节点,答案和"不存在"一样 —— 因为在这个空间看来它就是不在。

    与"重新分析"那条路同一条规则(`load_node` 的 WHERE 里有 `deleted_at IS NULL`),
    这正是这次改动要的东西:**同一个 id 在两条路上只有一个答案**。

    **界面上选不中已归档的节点**(它不在计划里,`PathView` 那条"聚焦所选"也是按
    `nodes.some(...)` 判的),所以这不是一条日常路径;钉住它是因为它是"一个 id 两种
    待遇"最容易被重新引入的地方。
    """
    account = await make_account()
    root = await _root_id(app_client, account)
    created = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/nodes",
        json={"parentId": root, "title": "查文献", "nodeType": "task"},
        headers=account.headers,
    )
    assert created.status_code == 201, created.text
    node_id = created.json()["node"]["id"]

    archived = await app_client.delete(
        f"/api/workspaces/{account.workspace_id}/nodes/{node_id}", headers=account.headers
    )
    assert archived.status_code == 200, archived.text

    fake = use_reasoner(FakeReasoner())
    before = await snapshot(db)
    response = await _send(app_client, account, context_node_id=node_id)
    after = await snapshot(db)

    _refused(response, fake, before, after)


# ---------------------------------------------------------------------------------
# 合法:自己这个空间里的节点,一切照旧
# ---------------------------------------------------------------------------------
async def test_a_node_of_my_own_space_still_works(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """合法的 id 照常走完:200、焦点送到模型、那句话带着 `contextNodeId` 落库。

    这一条是上面四条的地基。没有它,四条"拒绝"用例都能被一个"把 `contextNodeId`
    整个禁掉"的实现满足 —— 而那会把指代消解一起关掉:下一轮用户说"再展开说说",
    模型看到的上一句是一句**没有对象**的话,它只能猜。
    """
    account = await make_account(workspace_title="英语提升")
    node_id = await _root_id(app_client, account)
    fake = use_reasoner(FakeReasoner())

    response = await _send(app_client, account, context_node_id=node_id)

    assert response.status_code == 200, response.text
    assert len(fake.calls) == 1
    assert fake.calls[0].context_node_title == "英语提升"
    # 落库的那个字段是下一轮指代消解的锚,不能只活在响应里。
    assert response.json()["userMessage"]["contextNodeId"] == node_id


async def test_a_message_without_any_node_still_works(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """不带 `contextNodeId` 的消息不受影响 —— 校验只针对"给了的东西"。

    这些用户没有在看任何节点,或者在顶层空间说话。把他们一起拦掉的话,新空间里
    第一句话就说不出来。
    """
    account = await make_account()
    fake = use_reasoner(FakeReasoner())

    response = await _send(app_client, account, content="我想三个月内学会 Python")

    assert response.status_code == 200, response.text
    assert len(fake.calls) == 1
    assert response.json()["userMessage"]["contextNodeId"] is None
