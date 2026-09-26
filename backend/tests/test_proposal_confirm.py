"""确认一份提案:写入、幂等、失效保护、归属。

这是整个产品里最重的一次写入 —— 它一次改动整棵计划树。所以这里逐条钉住四件事:

1. **确认真的写进去了。** 不是"接口返回 200",而是库里多了几行、`/plan` 里能读到、
   版本号前进了。第二句尤其重要:`AppliedChangeView` 里的数字如果只来自提案本身,
   它就只是一句复述。
2. **双击只写一次。** 用户手抖点两下、或者网络超时后重试,都只该得到一个结果。
3. **基于旧版本的提案不会被应用。** 用户在提案生成后改过计划,照单应用会静默回退
   他的改动。
4. **别人的提案确认不了。**
"""

from __future__ import annotations

import uuid

import httpx
from sqlalchemy import select

from backend.db.models import PlanRevision
from backend.tests.conftest import FakeReasoner, snapshot

#: 一份正常的计划:一个阶段 + 两个任务 + 一条依赖。
#:
#: `n1` 是建空间时那条根目标 —— 记号从它开始编号(见 turn_context)。
PLAN_ACTIONS = (
    {
        "op": "create_node",
        "localId": "n2",
        "parentRef": "n1",
        "title": "阶段一:基础语法",
        "nodeType": "stage",
        "estimateMinutes": 600,
    },
    {
        "op": "create_node",
        "localId": "n3",
        "parentRef": "n2",
        "title": "变量与类型",
        "nodeType": "task",
        "estimateMinutes": 120,
    },
    {
        "op": "create_node",
        "localId": "n4",
        "parentRef": "n2",
        "title": "控制流",
        "nodeType": "task",
        "estimateMinutes": 120,
    },
    {"op": "create_dependency", "predecessorRef": "n3", "successorRef": "n4"},
)

KEY_A = "confirm-key-aaaa"
KEY_B = "confirm-key-bbbb"


async def _propose(client: httpx.AsyncClient, account, actions=PLAN_ACTIONS, content="帮我排一下"):
    """走真实接口让模型"提"一份计划,返回提案视图。"""
    response = await client.post(
        f"/api/workspaces/{account.workspace_id}/messages",
        json={"content": content},
        headers=account.headers,
    )
    assert response.status_code == 200, response.text
    proposal = response.json()["proposal"]
    assert proposal is not None, f"提案没有被建立: {response.text}"
    return proposal


async def _confirm(
    client: httpx.AsyncClient, account, proposal_id: str, key: str = KEY_A
) -> httpx.Response:
    return await client.post(
        f"/api/workspaces/{account.workspace_id}/proposals/{proposal_id}/confirm",
        json={"idempotencyKey": key},
        headers=account.headers,
    )


async def _plan(client: httpx.AsyncClient, account) -> dict:
    response = await client.get(f"/api/workspaces/{account.workspace_id}/plan", headers=account.headers)
    assert response.status_code == 200, response.text
    return response.json()


async def _proposals(client: httpx.AsyncClient, account) -> list[dict]:
    response = await client.get(
        f"/api/workspaces/{account.workspace_id}/proposals", headers=account.headers
    )
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------------
# 确认真的写进去了
# ---------------------------------------------------------------------------------
async def test_confirming_a_proposal_actually_writes_the_plan(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db
) -> None:
    account = await make_account()
    use_reasoner(FakeReasoner(actions=PLAN_ACTIONS))

    before = await _plan(app_client, account)
    assert before["totalNodes"] == 1, "空空间应该只有一条根目标"

    proposal = await _propose(app_client, account)
    # 生成提案**不该**改动计划 —— 它只是"打算做什么"。
    assert (await _plan(app_client, account))["totalNodes"] == 1

    response = await _confirm(app_client, account, proposal["id"])
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["replayed"] is False
    applied = body["applied"]
    assert applied["nodesCreated"] == 3
    assert applied["dependenciesAdded"] == 1
    assert applied["nodesUpdated"] == 0
    assert applied["nodesDeleted"] == 0

    # 真正算数的是库里和 /plan 里有什么,不是上面那几个数字。
    plan = await _plan(app_client, account)
    titles = {node["title"] for node in plan["nodes"]}
    assert {"阶段一:基础语法", "变量与类型", "控制流"} <= titles
    assert plan["totalNodes"] == 4
    assert len(plan["dependencies"]) == 1

    # 版本号前进:变更前是 1(还没发生过任何变更),这次变更**是** V1,下一次才拿到 2。
    assert applied["revisionVersion"] == 1
    assert plan["revisionVersion"] == 2

    # AI 建的节点标成 ai —— 界面要能区分"我写的"和"AI 帮我定的"。
    ai_nodes = [n for n in plan["nodes"] if n["origin"] == "ai"]
    assert len(ai_nodes) == 3

    # 父子关系真的建立了,不是三个平铺的孤儿。
    by_title = {node["title"]: node for node in plan["nodes"]}
    parent = by_title["阶段一:基础语法"]
    assert by_title["变量与类型"]["parentId"] == parent["id"]
    assert by_title["控制流"]["depth"] == 2

    # 提案本身的状态也跟着走。
    assert body["proposal"]["status"] == "applied"

    # 一次变更 = 一条版本历史,带着那个版本的完整快照。
    revision = (await db.execute(select(PlanRevision))).scalar_one()
    assert revision.version == 1
    assert revision.proposal_id == uuid.UUID(proposal["id"])
    assert len(revision.snapshot["nodes"]) == 4, "快照要能回答'V1 长什么样'"


# ---------------------------------------------------------------------------------
# 双击只写一次
# ---------------------------------------------------------------------------------
async def test_double_click_confirm_writes_exactly_once(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db
) -> None:
    """用户双击"确认"。

    两次都该得到 200,第二次 `replayed=true`,而且**一行都没有多写**。
    返回"这份提案已经处理过了"也能挡住重复写入,但用户会怀疑第一次到底成功了没有 ——
    他要的是看到那个结果,不是被拒绝。
    """
    account = await make_account()
    use_reasoner(FakeReasoner(actions=PLAN_ACTIONS))
    proposal = await _propose(app_client, account)

    first = await _confirm(app_client, account, proposal["id"])
    assert first.status_code == 200, first.text
    after_first = await snapshot(db)

    second = await _confirm(app_client, account, proposal["id"])
    assert second.status_code == 200, second.text
    assert second.json()["replayed"] is True

    assert await snapshot(db) == after_first, "第二次点击又写了一遍"

    # 两次看到的东西必须一致 —— 除开"这是重放"那个标记本身。
    a = first.json()
    b = second.json()
    assert a["applied"] == b["applied"]
    assert a["proposal"]["id"] == b["proposal"]["id"]
    assert a["proposal"]["status"] == b["proposal"]["status"]
    assert a["replayed"] != b["replayed"]

    assert (await _plan(app_client, account))["totalNodes"] == 4


async def test_a_second_key_on_the_same_proposal_is_a_conflict(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db
) -> None:
    """换一个幂等键再确认同一份提案 -> 409,不带任何写入。

    这不是幂等命中(键不同),而是一次真的重复确认:提案已经应用过了。
    """
    account = await make_account()
    use_reasoner(FakeReasoner(actions=PLAN_ACTIONS))
    proposal = await _propose(app_client, account)

    assert (await _confirm(app_client, account, proposal["id"], KEY_A)).status_code == 200
    after = await snapshot(db)

    second = await _confirm(app_client, account, proposal["id"], KEY_B)
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "PROPOSAL_ALREADY_DECIDED"
    assert await snapshot(db) == after


async def test_one_key_cannot_be_reused_for_a_different_proposal(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """同一个键用在另一份提案上 -> 409。

    如果照样重放上次的结果,用户会看到**另一个提案**被"确认成功" —— 而它其实
    根本没被应用。这类错误不会报错,只会让用户对着一份没生效的计划以为它生效了。
    """
    account = await make_account()
    use_reasoner(FakeReasoner(actions=PLAN_ACTIONS))
    first = await _propose(app_client, account, content="排第一版")
    second = await _propose(app_client, account, content="排第二版")

    assert (await _confirm(app_client, account, first["id"], KEY_A)).status_code == 200

    reused = await _confirm(app_client, account, second["id"], KEY_A)
    assert reused.status_code == 409, reused.text
    assert reused.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_BODY"


# ---------------------------------------------------------------------------------
# 计划变了,提案就不能照样应用
# ---------------------------------------------------------------------------------
async def test_a_proposal_based_on_an_old_revision_is_refused(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """用户在这份提案生成之后,确认了另一份提案。

    照单应用第二份,会把第一次的成果覆盖掉(它的动作是基于"空间里只有一个根目标"
    那个状态生成的)。**这种情况必须挡住**,而不是"尽力而为地应用一部分"。
    """
    account = await make_account()
    use_reasoner(FakeReasoner(actions=PLAN_ACTIONS))

    first = await _propose(app_client, account, content="排第一版")
    second = await _propose(app_client, account, content="再排一版")
    assert first["baseRevisionVersion"] == 1
    assert second["baseRevisionVersion"] == 1, "两份都是基于同一个版本生成的"

    assert (await _confirm(app_client, account, first["id"], KEY_A)).status_code == 200

    stale = await _confirm(app_client, account, second["id"], KEY_B)
    assert stale.status_code == 409, stale.text
    error = stale.json()["error"]
    assert error["code"] == "STALE_BASE_REVISION"
    # 详情里两个版本号都要给 —— 否则用户只知道"不行",不知道差在哪。
    assert error["details"]["baseRevisionVersion"] == 1
    assert error["details"]["currentRevisionVersion"] == 2

    # 第一次的成果完好无损,第二次一条都没写。
    plan = await _plan(app_client, account)
    assert plan["totalNodes"] == 4
    assert plan["revisionVersion"] == 2

    # 而且这份提案的终局是"过期作废",不是一个还挂着"待确认"的僵尸 ——
    # 否则用户点开界面看到的还是一个可以点的确认按钮。
    stored = {p["id"]: p for p in await _proposals(app_client, account)}
    assert stored[second["id"]]["status"] == "stale"


async def test_confirming_after_a_manual_edit_is_refused(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db
) -> None:
    """用户在提案生成后**自己**改了计划。

    这一条模拟的是它最日常的形态:用户勾掉了一个任务。阶段 5 会给出节点编辑接口,
    但"改计划会让版本号前进"这件事已经在数据层成立了 —— 这里直接把版本号推一格,
    断言的是**确认路径看的是这个版本号**,而不是别的什么。

    这是"不能覆盖用户改动"这条承诺唯一可被验证的落点。
    """
    from sqlalchemy import update

    from backend.db.models import Workspace

    account = await make_account()
    use_reasoner(FakeReasoner(actions=PLAN_ACTIONS))
    proposal = await _propose(app_client, account)

    await db.execute(
        update(Workspace)
        .where(Workspace.id == uuid.UUID(account.workspace_id))
        .values(current_revision_version=Workspace.current_revision_version + 1)
    )
    await db.commit()

    refused = await _confirm(app_client, account, proposal["id"])
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "STALE_BASE_REVISION"

    # 一个字都没写进计划。
    assert (await _plan(app_client, account))["totalNodes"] == 1


# ---------------------------------------------------------------------------------
# 拒绝
# ---------------------------------------------------------------------------------
async def test_rejecting_a_proposal_writes_nothing_and_is_final(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db
) -> None:
    account = await make_account()
    use_reasoner(FakeReasoner(actions=PLAN_ACTIONS))
    proposal = await _propose(app_client, account)

    rejected = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/proposals/{proposal['id']}/reject",
        json={"reason": "这个排法太紧了"},
        headers=account.headers,
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "rejected"

    plan = await _plan(app_client, account)
    assert plan["totalNodes"] == 1
    # **版本号不前进。** "看过、决定不要"不该在版本历史上留下一个什么都没变的版本,
    # 否则用户回头数"改过几次计划"会数出一次不存在的改变。
    assert plan["revisionVersion"] == 1

    # 拒绝之后就不能再确认了。
    after = await _confirm(app_client, account, proposal["id"], KEY_B)
    assert after.status_code == 409, after.text
    assert after.json()["error"]["code"] == "PROPOSAL_ALREADY_DECIDED"


# ---------------------------------------------------------------------------------
# 归属
# ---------------------------------------------------------------------------------
async def test_another_account_cannot_confirm_your_proposal(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """B 拿 A 的提案 id 去确认。

    这条测试必须**真的存在一份提案**才写得出来 —— 这也正是它没有和其他跨账号用例
    放在一起的原因(那边每个用例都要先证明"A 自己访问同一路径是成功的",而确认一份
    不存在的提案,对 A 自己也是 404,那条反向断言根本不成立)。
    """
    account_a = await make_account(email="a@example.com")
    account_b = await make_account(email="b@example.com", workspace_title="英语")

    use_reasoner(FakeReasoner(actions=PLAN_ACTIONS))
    proposal = await _propose(app_client, account_a)

    # 用 B 自己的空间 id + A 的提案 id
    cross = await _confirm(app_client, account_b, proposal["id"])
    assert cross.status_code == 404, cross.text
    assert cross.json()["error"]["code"] == "PROPOSAL_NOT_FOUND"

    # 用 A 的空间 id(那就先撞在空间归属上)
    wrong_space = await app_client.post(
        f"/api/workspaces/{account_a.workspace_id}/proposals/{proposal['id']}/confirm",
        json={"idempotencyKey": KEY_A},
        headers=account_b.headers,
    )
    assert wrong_space.status_code == 404, wrong_space.text
    assert wrong_space.json()["error"]["code"] == "WORKSPACE_NOT_FOUND"

    # A 自己确认是成功的 —— 上面两个 404 才说明是权限,而不是"这个 id 根本没用"。
    assert (await _confirm(app_client, account_a, proposal["id"])).status_code == 200


async def test_confirming_an_unknown_proposal_is_404(
    app_client: httpx.AsyncClient, make_account
) -> None:
    account = await make_account()
    response = await _confirm(app_client, account, str(uuid.uuid4()))

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "PROPOSAL_NOT_FOUND"


# ---------------------------------------------------------------------------------
# 请求本身的形状
# ---------------------------------------------------------------------------------
async def test_confirm_requires_an_idempotency_key(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """幂等键必填。

    如果让它可选、缺了就由服务端补一个随机值,那么"双击不会建两遍节点"这个保证
    会被悄无声息地关掉:两次点击拿到两个随机键,两条节点。所以缺键必须是 422,
    而不是一个善意的默认值。
    """
    account = await make_account()
    use_reasoner(FakeReasoner(actions=PLAN_ACTIONS))
    proposal = await _propose(app_client, account)

    missing = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/proposals/{proposal['id']}/confirm",
        json={},
        headers=account.headers,
    )
    assert missing.status_code == 422
    assert missing.json()["error"]["code"] == "REQUEST_INVALID"

    too_short = await _confirm(app_client, account, proposal["id"], "short")
    assert too_short.status_code == 422

    # 两次畸形请求都不该留下任何东西。
    assert (await _plan(app_client, account))["totalNodes"] == 1


async def test_the_reply_points_at_the_proposal_it_brought(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """助手回复上带着 `proposalId`。

    界面据此把那张提案卡片挂在正确的消息下面。没有它,一张提案只能浮在对话最底部,
    而用户往上翻两轮之后就再也找不到它是谁提出来的了。
    """
    account = await make_account()
    use_reasoner(FakeReasoner(actions=PLAN_ACTIONS))

    payload = {"content": "帮我排一下", "clientMessageId": "same-message-id"}
    response = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/messages",
        json=payload,
        headers=account.headers,
    )
    body = response.json()

    assert body["replayed"] is False
    assert body["assistantMessage"]["proposalId"] == body["proposal"]["id"]

    # 带上同一个 clientMessageId 重发 —— 这是"网络超时后重试"的真实形状。
    # 提案必须一起回来:否则用户重试一次,回复还在、卡片没了,
    # 而回复里正说着"我列了个计划,你看一下"。
    replayed = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/messages",
        json=payload,
        headers=account.headers,
    )
    assert replayed.status_code == 200, replayed.text
    assert replayed.json()["replayed"] is True, "同一个 clientMessageId 应该被认出来"
    assert replayed.json()["proposal"] is not None, "重放时提案丢了"
    assert replayed.json()["proposal"]["id"] == body["proposal"]["id"]
