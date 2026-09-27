"""访谈共建:同一主题不重复建节点(§2.5 / §4.4)、信息用途的服务端约束、长笔记提案。

这三件事在实现上是一件:**模型提的动作要经过一个纯校验器,而那个校验器会被跑两遍**
(生成一次、确认一次)。所以这里的每个用例都同时钉住两侧:

1. **合并是可见的。** 模型想新建一个已经存在的主题时,这一条被改写成"补充已有节点",
   而预览里必须说出来 —— 悄悄把新建改成更新,用户确认的就是一件他没看过的事(§7.2)。
   断言**不只看 `coalescedFrom`**,还看 `/plan` 里真的没有多出第二个节点:前者是那句话,
   后者是那件事。
2. **信息用途不是一句提示词。** "信息主题不能带工时/截止"要拦两条路:一次新建,
   以及**一次被合并成修改的"新建"** —— 后者会换一件衣服从 `_update` 进来,而只在
   `_create` 里判的守卫恰好漏掉它。
3. **笔记有它自己的版本号。** 生成之后、用户点确认之前笔记被人改过,那次确认必须失败
   并说清楚是哪一份笔记;而"计划没有变"(笔记不属于计划)意味着它不会先报成
   `STALE_BASE_REVISION`,那句话会把用户指向错误的方向。

假模型(`FakeReasoner`)在这里不是权宜之计:`actions` 这个字段本来就是为"脚本化一段
模型输出"设计的(见 conftest 的注释),而这里要的正是"模型说了一件具体的话"。
"""

from __future__ import annotations

import httpx

from backend.tests.conftest import FakeReasoner

#: 一个**信息用途**的主题。§2.5 的访谈闭环里,用户回答"我排名 38"之后应该长出来的
#: 就是这一个 —— `capability` 是它的载体类型(它不是一个能力,但类型轴只有那三个值,
#: 而"信息"这件事由 `purpose` 单独表达)。
INFORMATION_NODE = {
    "op": "create_node",
    "localId": "n2",
    "parentRef": "n1",
    "title": "学业情况",
    "nodeType": "capability",
    "purpose": "information",
    "description": "排名 38/120。",
}


async def _propose(client: httpx.AsyncClient, account, *, content="我刚答了一个问题"):
    """走真实接口让模型"提"一份变更,返回整个响应体。

    返回响应体而不是提案视图:这一批有**两种**结局都要看 —— 有提案(`proposal` 非空)
    和被拒(`proposalErrors` 非空)。只看其中一个会把另一个的断言写成"没崩"。
    """
    response = await client.post(
        f"/api/workspaces/{account.workspace_id}/messages",
        json={"content": content},
        headers=account.headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _confirm(client: httpx.AsyncClient, account, proposal_id: str) -> httpx.Response:
    return await client.post(
        f"/api/workspaces/{account.workspace_id}/proposals/{proposal_id}/confirm",
        json={"idempotencyKey": f"key-{proposal_id[:8]}"},
        headers=account.headers,
    )


async def _plan(client: httpx.AsyncClient, account) -> dict:
    response = await client.get(f"/api/workspaces/{account.workspace_id}/plan", headers=account.headers)
    assert response.status_code == 200, response.text
    return response.json()


def _nodes(plan: dict) -> list[dict]:
    return plan["nodes"]


def _by_title(plan: dict, title: str) -> dict:
    found = [node for node in _nodes(plan) if node["title"] == title]
    assert len(found) == 1, f"「{title}」应该恰好有一个,实际 {len(found)} 个"
    return found[0]


def _handle_of(plan: dict, node_id: str) -> str:
    """这个节点在这一轮里叫几号。

    **不是猜一个 `n2`。** 记号是服务端按 `(depth, order_index, created_at)` 的排序
    编出来的,而 `/plan` 的节点数组用的是**同一个排序**(两个地方各有一条
    `order_by`,见 `turn_context.load_nodes` 与 `plan_service.load_all_nodes`),
    所以第 i 个节点的记号就是 `n{i}`。这条例外的等价关系值得写下来 —— 它对不上时
    的失败长这样:一句"要修改的 n2 不是这个空间里已有的节点",和本用例想验证的
    事情毫无关系。真有那一天,`test_a_note_proposal_writes_the_body_and_is_visible`
    里的交叉断言会先红,并在消息里指出记号表本身变了。
    """
    for index, node in enumerate(_nodes(plan), start=1):
        if node["id"] == node_id:
            return f"n{index}"
    raise AssertionError(f"/plan 里没有 {node_id}")


async def _interview_turn(client, account, use_reasoner, actions, *, content="我刚答了一个问题"):
    """让假模型提一次、确认一次,返回 (假模型, 消息响应体, 确认响应体)。§2.5 的一轮访谈。"""
    fake = use_reasoner(FakeReasoner(actions=actions))
    body = await _propose(client, account, content=content)
    assert not body["proposalErrors"], body["proposalErrors"]
    confirmed = await _confirm(client, account, body["proposal"]["id"])
    assert confirmed.status_code == 200, confirmed.text
    return fake, body, confirmed.json()


# ---------------------------------------------------------------------------------
# 同一个主题不再建第二个节点
# ---------------------------------------------------------------------------------
async def test_a_second_create_for_the_same_topic_becomes_an_update(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """访谈第二轮又提同一个主题:合并成"补充",而且画布上只有一个。"""
    account = await make_account()
    await _interview_turn(app_client, account, use_reasoner, (INFORMATION_NODE,))

    after_first = await _plan(app_client, account)
    assert len(_nodes(after_first)) == 2, "根目标 + 一个信息主题"
    node_id = _by_title(after_first, "学业情况")["id"]

    # 第二轮:模型又说了一遍同一件事(这是访谈里最常见的一种模型行为 —— 用户
    # 补了一句"我数学 130",而模型把整个主题又提了一遍)。
    use_reasoner(
        FakeReasoner(
            actions=(
                {
                    "op": "create_node",
                    "localId": "n9",
                    "parentRef": "n1",
                    "title": "学业情况",
                    "nodeType": "capability",
                    "purpose": "information",
                    "description": "排名 38/120,数学 130。",
                },
            )
        )
    )
    body = await _propose(app_client, account)
    assert not body["proposalErrors"], body["proposalErrors"]
    proposal = body["proposal"]
    assert len(proposal["items"]) == 1

    item = proposal["items"][0]
    # 一、它被改写成了"补充",而且这件事在预览里看得见。
    assert item["op"] == "update_node", item
    assert item["coalescedFrom"] == "学业情况", item
    assert item["summary"].startswith("补充已有节点「学业情况」"), item["summary"]
    assert item["targetNodeId"] == node_id, "改的必须是**那一个**节点"
    assert proposal["changeSummary"]["counts"]["createNode"] == 0
    assert proposal["changeSummary"]["counts"]["updateNode"] == 1

    confirmed = await _confirm(app_client, account, proposal["id"])
    assert confirmed.status_code == 200, confirmed.text
    applied = confirmed.json()["applied"]
    assert applied["nodesCreated"] == 0
    assert applied["nodesUpdated"] == 1

    # 二、**画布上真的只有一个**(上面那句只是预览里的一句声明,这一句是那件事)。
    after = await _plan(app_client, account)
    assert len(_nodes(after)) == 2, [node["title"] for node in _nodes(after)]
    assert _by_title(after, "学业情况")["description"] == "排名 38/120,数学 130。"
    # 合并出来的那条 `update_node` 引用的是**这一层的记号**,而不是一个真实 uuid:
    # 那一层的语言只有记号,把一个 uuid 写进模型产出的 payload 里,没有任何地方认得它。
    assert item["payload"]["targetRef"] == _handle_of(after, node_id)


async def test_a_duplicate_inside_one_batch_collapses_to_one_item(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """同一批里的两条同名新建:并成一条。用户不该看到两个一模一样的框。"""
    account = await make_account()
    use_reasoner(
        FakeReasoner(
            actions=(
                {
                    "op": "create_node",
                    "localId": "n2",
                    "parentRef": "n1",
                    "title": "学业情况",
                    "nodeType": "capability",
                    "purpose": "information",
                },
                {
                    "op": "create_node",
                    "localId": "n3",
                    "parentRef": "n1",
                    # 全角数字与半角在这里是同一件事 —— NFKC 折叠正是为它存在的。
                    "title": "学业情况 ",
                    "nodeType": "capability",
                    "purpose": "information",
                    "description": "排名 38/120。",
                },
            )
        )
    )
    body = await _propose(app_client, account)
    assert not body["proposalErrors"], body["proposalErrors"]
    proposal = body["proposal"]
    assert proposal["itemCount"] == 1, proposal["items"]

    item = proposal["items"][0]
    assert item["op"] == "create_node"
    # 摘要要说它吸收了谁 —— 两条并成一条,如果不说,用户以为模型只提了一件事。
    assert "已并入同名的「学业情况」" in item["summary"], item["summary"]
    assert item["payload"]["description"] == "排名 38/120。", "后一条的字段要并进去"

    confirmed = await _confirm(app_client, account, proposal["id"])
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["applied"]["nodesCreated"] == 1
    assert len(_nodes(await _plan(app_client, account))) == 2


async def test_the_same_title_under_another_purpose_is_refused_not_merged(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """同名但用途不同:合并不了,那就**整份拒绝**,而不是放一个重复进去。

    §2.5 说用途与层级是两个维度,所以"信息主题「学业情况」"与"行动「学业情况」"
    是两个不同的对象 —— 合并键里有 `purpose` 正是为了不把它们混为一谈。而它们也
    不该同时出现在同一层里:用户看到的是两个他分不清哪个是哪个的框。
    """
    account = await make_account()
    await _interview_turn(app_client, account, use_reasoner, (INFORMATION_NODE,))

    use_reasoner(
        FakeReasoner(
            actions=(
                {
                    "op": "create_node",
                    "localId": "n9",
                    "parentRef": "n1",
                    "title": "学业情况",
                    "nodeType": "task",
                    "purpose": "planning",
                    "estimateMinutes": 60,
                },
            )
        )
    )
    body = await _propose(app_client, account)
    assert body["proposal"] is None, "重复不该被放进去"
    codes = [error["code"] for error in body["proposalErrors"]]
    assert codes == ["DUPLICATE_NODE_TITLE"], body["proposalErrors"]
    message = body["proposalErrors"][0]["message"]
    # 消息要给得出路(§7.2 的"用户要能行动"),而且要指名对方是什么。
    assert "学业情况" in message
    assert "信息" in message or "用途" in message, message
    assert "换个标题" in message, message


# ---------------------------------------------------------------------------------
# 信息用途:服务端约束,两条路都要拦
# ---------------------------------------------------------------------------------
async def test_an_information_node_cannot_be_created_with_an_estimate(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    account = await make_account()
    use_reasoner(
        FakeReasoner(
            actions=(
                {
                    "op": "create_node",
                    "localId": "n2",
                    "parentRef": "n1",
                    "title": "学业情况",
                    "nodeType": "capability",
                    "purpose": "information",
                    "estimateMinutes": 90,
                },
            )
        )
    )
    body = await _propose(app_client, account)
    assert body["proposal"] is None, "带工时的信息主题不该生成提案"
    assert [error["code"] for error in body["proposalErrors"]] == [
        "INFORMATION_NODE_MUST_NOT_BE_SCHEDULABLE"
    ], body["proposalErrors"]
    message = body["proposalErrors"][0]["message"]
    assert "信息主题" in message and "工时" in message, message
    # 两条出路都要在:有人是想改用途,有人是手滑点错了用途。
    assert "行动" in message and "清掉" in message, message
    assert len(_nodes(await _plan(app_client, account))) == 1, "拒了就是一个字都不写"


async def test_a_coalesced_create_still_cannot_smuggle_in_an_estimate(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """**这一条是那个换衣服进来的路径。**

    模型这一轮提的还是"新建一个带工时的信息主题",而那个主题已经存在了 —— 于是
    它先被合并成一条 `update_node`。只在 `_create` 里判守卫的话,这一次会**通过**,
    结果是「学业情况」这个信息主题上凭空多出 90 分钟,而画布上多了个它本来不该有的
    排期输入。合并发生在校验之前,所以守卫必须在**两条路**上都在。
    """
    account = await make_account()
    await _interview_turn(app_client, account, use_reasoner, (INFORMATION_NODE,))

    use_reasoner(
        FakeReasoner(
            actions=(
                {
                    "op": "create_node",
                    "localId": "n9",
                    "parentRef": "n1",
                    "title": "学业情况",
                    "nodeType": "capability",
                    "purpose": "information",
                    "estimateMinutes": 90,
                },
            )
        )
    )
    body = await _propose(app_client, account)
    assert body["proposal"] is None, "合并之后也不能带工时"
    assert [error["code"] for error in body["proposalErrors"]] == [
        "INFORMATION_NODE_MUST_NOT_BE_SCHEDULABLE"
    ], body["proposalErrors"]

    node = _by_title(await _plan(app_client, account), "学业情况")
    assert node["estimateMinutes"] is None, "一个字都不该写进去"


# ---------------------------------------------------------------------------------
# 长笔记:提案 + 版本闸
# ---------------------------------------------------------------------------------
_NOTE_BODY = "排名 38/120。\n\n" + "数学 130,英语 128。" * 500


async def test_a_note_proposal_writes_the_body_and_is_visible_in_the_preview(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    account = await make_account()
    await _interview_turn(app_client, account, use_reasoner, (INFORMATION_NODE,))
    plan = await _plan(app_client, account)
    node_id = _by_title(plan, "学业情况")["id"]
    handle = _handle_of(plan, node_id)

    # 第二轮:把那一大段正文写进笔记。它到不了 300 字的简述里,而它也不该去那儿。
    second = use_reasoner(
        FakeReasoner(
            actions=(
                {
                    "op": "update_note",
                    "targetRef": handle,
                    "body": _NOTE_BODY,
                },
            )
        )
    )
    body = await _propose(app_client, account)
    # **记号的交叉校验**(全文件只此一处)。上面那个 `n2` 是按 `/plan` 的排序推出来的,
    # 而这一句比对的是**服务端真正送给模型的那份记号表**。两者要是哪天分了家,这一句会
    # 直接指出"记号表变了",而不是让后面每个用例各自红在一句悬空引用上。
    assert dict(second.calls[-1].node_handles)[handle] == node_id
    assert not body["proposalErrors"], body["proposalErrors"]
    proposal = body["proposal"]
    item = proposal["items"][0]
    assert item["op"] == "update_note"
    assert item["summary"] == f"把 {len(_NOTE_BODY)} 字写进「学业情况」的长笔记", item["summary"]
    assert item["targetNodeId"] == node_id
    # 版本号是**服务端记的**:模型没给,而 payload 里必须有一个具体的号 ——
    # 那是确认时重校验要比的那一个。
    assert item["payload"]["_noteBaseVersion"] == 0, item["payload"]
    assert proposal["changeSummary"]["counts"]["updateNote"] == 1

    confirmed = await _confirm(app_client, account, proposal["id"])
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["applied"]["notesUpdated"] == 1

    read = await app_client.get(
        f"/api/workspaces/{account.workspace_id}/nodes/{node_id}/notes",
        headers=account.headers,
    )
    assert read.status_code == 200, read.text
    stored = read.json()
    assert stored["body"] == _NOTE_BODY, "正文原样存,不截断也不归一化"
    assert stored["contentVersion"] == 1


async def test_a_note_changed_between_generation_and_confirmation_is_refused(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """生成之后、确认之前笔记被人改过:那次确认必须失败,并且说得出是哪一份。

    **它不该报成 `STALE_BASE_REVISION`。** 笔记不属于计划 —— 写笔记不推进
    `revisionVersion`(见 `note_service` 的模块 docstring),所以"计划变了"那道闸
    在这里是**故意**不响的;响的是笔记自己那个版本号。这一条正是为了钉住这一点:
    两句错误信息把用户指向完全不同的两个动作(一个是"重新生成",另一个是"刷新一下
    看看别人写了什么")。
    """
    account = await make_account()
    await _interview_turn(app_client, account, use_reasoner, (INFORMATION_NODE,))
    plan = await _plan(app_client, account)
    node_id = _by_title(plan, "学业情况")["id"]

    use_reasoner(
        FakeReasoner(
            actions=(
                {
                    "op": "update_note",
                    "targetRef": _handle_of(plan, node_id),
                    "body": _NOTE_BODY,
                },
            )
        )
    )
    body = await _propose(app_client, account)
    assert not body["proposalErrors"], body["proposalErrors"]
    proposal_id = body["proposal"]["id"]

    # 用户在这中间自己写了两句(另一个标签页、或者就是同一个人顺手补的)。
    written = await app_client.put(
        f"/api/workspaces/{account.workspace_id}/nodes/{node_id}/notes",
        json={"body": "我自己补的一句话。", "expectedContentVersion": 0},
        headers=account.headers,
    )
    assert written.status_code == 200, written.text
    assert written.json()["note"]["contentVersion"] == 1

    confirmed = await _confirm(app_client, account, proposal_id)
    assert confirmed.status_code == 409, confirmed.text
    error = confirmed.json()["error"]
    assert error["code"] == "PROPOSAL_NO_LONGER_VALID", error
    problem = error["details"]["problems"][0]
    assert problem["code"] == "NOTE_CHANGED", problem
    assert "第 0 版" in problem["message"] and "第 1 版" in problem["message"], problem

    # 拒绝之后库里那一段**一个字都没被改**:不是"改了一部分"。
    read = await app_client.get(
        f"/api/workspaces/{account.workspace_id}/nodes/{node_id}/notes",
        headers=account.headers,
    )
    assert read.json()["body"] == "我自己补的一句话。"
    assert read.json()["contentVersion"] == 1


async def test_the_note_proposal_reads_the_version_the_server_saw(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """模型自己编的那个版本号**既不算数、也不该让它失败**。

    模型没有别的办法知道那个号(它是服务端的状态),所以整份提案因为一个它不该
    关心的字段被拒,就是"凭空多出一条错误"。真正被比的是服务端上一次校验写进
    payload 的那一个 —— 于是这里断言两件事:模型那个 99 被丢掉了,而写回去的是
    此刻库里的 1。
    """
    account = await make_account()
    await _interview_turn(app_client, account, use_reasoner, (INFORMATION_NODE,))
    plan = await _plan(app_client, account)
    node_id = _by_title(plan, "学业情况")["id"]

    # 先自己写一版,让库里那个号变成 1(模型看不到它)。
    await app_client.put(
        f"/api/workspaces/{account.workspace_id}/nodes/{node_id}/notes",
        json={"body": "第一版。"},
        headers=account.headers,
    )

    use_reasoner(
        FakeReasoner(
            actions=(
                {
                    "op": "update_note",
                    "targetRef": _handle_of(plan, node_id),
                    "body": "第二版。",
                    "expectedNoteVersion": 99,
                },
            )
        )
    )
    body = await _propose(app_client, account)
    assert not body["proposalErrors"], body["proposalErrors"]
    payload = body["proposal"]["items"][0]["payload"]
    assert payload["_noteBaseVersion"] == 1, payload
    # 模型写的那个 99 哪儿都不该留下 —— 它既不是前置条件,也不是展示信息。
    assert "expectedNoteVersion" not in payload, payload
    assert "expected_note_version" not in payload, payload

    confirmed = await _confirm(app_client, account, body["proposal"]["id"])
    assert confirmed.status_code == 200, confirmed.text
    read = await app_client.get(
        f"/api/workspaces/{account.workspace_id}/nodes/{node_id}/notes",
        headers=account.headers,
    )
    assert read.json()["body"] == "第二版。"
    assert read.json()["contentVersion"] == 2
