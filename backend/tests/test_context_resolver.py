"""模型到底看见了什么 —— 第二版:正文、范围、以及"没读到"有没有被说出来。

## 这一版补的是一个具体的、已经发生过的缺陷

第一版把节点的标题送进了提示词,但**没有正文**。用户在自己写的正文里写下
"只能周末做""已经会 C 语言""没有设备",模型一条都看不到 —— 它只能照着标题猜,
然后把用户刚写过的事情再问一遍。用户看到的感受是"它根本没读我写的东西",
而这是对的:它确实没读。

所以这里钉四件事:

1. **正文真的进了提示词** —— 焦点、祖先、直属子节点各按预算读。
2. **没读到的说得出** —— 范围外、太远的祖先、超出预算的子节点、被截断的正文:
   每一条都要在提示词里留下痕迹。**没读到和没有,在模型眼里长得一模一样**,
   不区分的话它会拿半份上下文当全份用。这里有一条反向断言(范围外的正文一个字
   都不许出现在提示词里)和几条正向断言(逐条标着"本次只读了标题")。
3. **范围是一条权限,不是一句建议** —— 范围外的动作会被服务端拒成 `OUT_OF_SCOPE`,
   而且**连同这一轮的其他变更一起不落地**:部分执行比整份拒绝更坏,用户会以为
   他确认的那一份是完整的。
4. **快照记下了这次分析的输入** —— 供"过期了吗"事后比较用。布局与视口不在其中,
   这一条也在这里验(拖一下画布不该让分析过期)。
"""

from __future__ import annotations

import uuid

import httpx
from sqlalchemy import func, select, update

from backend.agent.prompts.planning import (
    FOCUS_BODY_CHARS,
    MAX_CHILD_BODIES,
    NOTES_BODY_CHARS,
    NOTES_LABEL,
    NOTES_PRESENT_NOTE,
    READ_ONLY_NOTE,
    TITLE_ONLY_NOTE,
    TRUNCATED_NOTE,
)
from backend.agent.runtime.base import LAYER_ANCESTOR, LAYER_CHILD, LAYER_FOCUS, LAYER_OUTSIDE
from backend.agent.runtime.response import render_turn
from backend.db.models import PlanNode, PlanRevision
from backend.db.session import SessionLocal
from backend.tests.conftest import FakeReasoner


# ---------------------------------------------------------------------------------
# 助手
# ---------------------------------------------------------------------------------
async def _send(client: httpx.AsyncClient, account, content: str, **extra) -> httpx.Response:
    return await client.post(
        f"/api/workspaces/{account.workspace_id}/messages",
        json={"content": content, **extra},
        headers=account.headers,
    )


async def _plan(client: httpx.AsyncClient, account) -> dict:
    response = await client.get(
        f"/api/workspaces/{account.workspace_id}/plan", headers=account.headers
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _by_title(client: httpx.AsyncClient, account) -> dict[str, dict]:
    plan = await _plan(client, account)
    return {node["title"]: node for node in plan["nodes"]}


async def _root_id(client: httpx.AsyncClient, account) -> str:
    plan = await _plan(client, account)
    return next(node["id"] for node in plan["nodes"] if node["parentId"] is None)


async def _create(
    client: httpx.AsyncClient,
    account,
    parent_id: str,
    title: str,
    *,
    description: str | None = None,
    acceptance_criteria: str | None = None,
) -> str:
    response = await client.post(
        f"/api/workspaces/{account.workspace_id}/nodes",
        json={
            "parentId": parent_id,
            "title": title,
            "description": description,
            "acceptanceCriteria": acceptance_criteria,
        },
        headers=account.headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["node"]["id"]


async def _patch(client: httpx.AsyncClient, account, node_id: str, **fields) -> httpx.Response:
    return await client.patch(
        f"/api/workspaces/{account.workspace_id}/nodes/{node_id}",
        json=fields,
        headers=account.headers,
    )


async def _put_note(client: httpx.AsyncClient, account, node_id: str, body: str) -> None:
    response = await client.put(
        f"/api/workspaces/{account.workspace_id}/nodes/{node_id}/notes",
        json={"body": body},
        headers=account.headers,
    )
    assert response.status_code == 200, response.text


async def _chain(
    client: httpx.AsyncClient, account, depth: int, *, body: str = "第 {index} 层的正文"
) -> list[str]:
    """从根往下建一条 depth 层的链,返回从根到最深那条的 id。

    祖先链的预算(最上面一条 + 最近几条给正文)只有在链够长时才看得出来,所以
    "深链"这件事必须真的建出来,不能靠两层的例子推。
    """
    ids = [await _root_id(client, account)]
    for index in range(1, depth):
        ids.append(
            await _create(
                client, account, ids[-1], f"第 {index} 层", description=body.format(index=index)
            )
        )
    return ids


async def _revisions(db) -> int:
    return int(await db.scalar(select(func.count()).select_from(PlanRevision)) or 0)


# ---------------------------------------------------------------------------------
# 正文真的进了提示词
# ---------------------------------------------------------------------------------
async def test_the_focused_node_body_reaches_the_prompt(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """用户正写着的那个节点,它的正文与验收标准要原样送到模型面前。"""
    account = await make_account()
    root = await _root_id(app_client, account)
    stage = await _create(
        app_client,
        account,
        root,
        "阶段一:语法基础",
        description="把语法过一遍,能读懂简单的程序。",
        acceptance_criteria="能独立写出 100 行以内的小程序",
    )

    fake = use_reasoner(FakeReasoner())
    response = await _send(
        app_client, account, "这个阶段展开讲讲", contextNodeId=stage, scopeRootId=root
    )
    assert response.status_code == 200, response.text

    turn = fake.calls[0]
    assert turn.context_node_title == "阶段一:语法基础"
    focused = next(node for node in turn.nodes if node.layer == LAYER_FOCUS)
    assert focused.body_read, "焦点节点没有读正文 —— 那这一轮又是在凭标题猜"
    assert focused.description == "把语法过一遍,能读懂简单的程序。"

    prompt = render_turn(turn)
    assert "把语法过一遍,能读懂简单的程序。" in prompt
    assert "能独立写出 100 行以内的小程序" in prompt


async def test_an_out_of_scope_body_never_reaches_the_prompt(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """**这一条是反向断言。** 范围之外那一支的正文,一个字都不许出现在提示词里。

    范围外的东西模型能**看见**(看见才知道整棵树长什么样),但它不该读到人家的正文 ——
    用户说的是"我只看这个阶段",读到别处的正文就是越过了他自己划的线。同时那一条
    要标着"范围外只读"和"本次只读了标题":让"我没看见"和"那里没有"分得开。
    """
    account = await make_account()
    root = await _root_id(app_client, account)
    inside = await _create(
        app_client, account, root, "在范围内", description="范围内的正文:先做实验。"
    )
    await _create(
        app_client,
        account,
        root,
        "在范围外",
        description="范围外的正文:我只有周末能做,平时要上课。",
    )

    fake = use_reasoner(FakeReasoner())
    response = await _send(
        app_client, account, "看看这一支", contextNodeId=inside, scopeRootId=inside
    )
    assert response.status_code == 200, response.text

    turn = fake.calls[0]
    outsider = next(node for node in turn.nodes if node.title == "在范围外")
    assert outsider.layer == LAYER_OUTSIDE and outsider.read_only
    assert not outsider.body_read

    prompt = render_turn(turn)
    assert "范围内的正文:先做实验。" in prompt, "范围内的正文没送进去"
    assert "我只有周末能做" not in prompt, (
        "范围外的正文进了提示词 —— 用户说过他只看这一支,这是越过他自己划的线。"
    )
    assert READ_ONLY_NOTE in prompt and TITLE_ONLY_NOTE in prompt, (
        "范围外的节点没有标出「只读 / 只读了标题」—— 模型会把'没读到'当成'那里没有'。"
    )


async def test_the_ancestor_chain_is_read_but_budgeted(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """祖先链:最上面那条(根目标,最硬的约束)与最近几条给正文,中间那些只给标题。

    一层一视同仁地读会把提示词撑满,而撑满的代价是焦点被淹掉;全都不读又会让模型
    不知道这个阶段是为了什么。预算就落在这两条规则上 —— 而"哪几条没读"必须印出来。
    """
    account = await make_account()
    await _patch(  # 根目标的正文:它是最上面那条,必须读到
        app_client, account, await _root_id(app_client, account), description="根目标正文:三个月做出一个能用的东西。"
    )
    ids = await _chain(app_client, account, depth=9)

    fake = use_reasoner(FakeReasoner())
    response = await _send(
        app_client,
        account,
        "这一层怎么拆",
        contextNodeId=ids[-1],
        scopeRootId=ids[0],
    )
    assert response.status_code == 200, response.text

    turn = fake.calls[0]
    ancestors = {node.title: node for node in turn.nodes if node.layer == LAYER_ANCESTOR}
    assert ancestors, "祖先链一个都没有 —— 模型看不到这一层挂在什么目标下面"

    read = {title for title, node in ancestors.items() if node.body_read}
    prompt = render_turn(turn)
    assert "根目标正文:三个月做出一个能用的东西。" in prompt, "最上面那条的正文没读到"
    assert "第 1 层" in prompt and "第 1 层的正文" not in prompt, (
        "链中间那一层的正文也读了 —— 祖先正文的预算没有生效"
    )
    assert read, "祖先链一条正文都没读"


async def test_child_bodies_are_capped_and_the_cap_is_stated(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """子节点的正文有上限,超出的仍然列出标题并标明"本次只读了标题"。"""
    account = await make_account()
    root = await _root_id(app_client, account)
    stage = await _create(app_client, account, root, "阶段", description="阶段正文")
    for index in range(MAX_CHILD_BODIES + 3):
        await _create(
            app_client, account, stage, f"子项{index:02d}", description=f"子项{index:02d}的正文"
        )

    fake = use_reasoner(FakeReasoner())
    assert (
        await _send(app_client, account, "帮我拆细一点", contextNodeId=stage, scopeRootId=root)
    ).status_code == 200

    turn = fake.calls[0]
    children = [node for node in turn.nodes if node.layer == LAYER_CHILD]
    assert len(children) == MAX_CHILD_BODIES + 3
    assert sum(1 for node in children if node.body_read) == MAX_CHILD_BODIES, (
        "子节点正文的预算没有被执行 —— 提示词会被一整层抄满"
    )

    prompt = render_turn(turn)
    assert prompt.count(TITLE_ONLY_NOTE) == 3, "超预算的那几个没有标出来"


async def test_a_long_body_is_cut_and_the_cut_is_stated(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """超长正文要截,而且**说得出截了多少** —— 悄悄截断等于让模型以为那就是全文。

    ## 这份正文是**直接写库**造出来的,不是 `POST /nodes`

    因为 300 码点的简述上限(§2.1)落地之后,没有接口能写出一份 4,500 字的正文了
    —— 而这恰好是**豁免**那条规则描述的情形:存量超限的节点继续合法地存在,
    没人能再造一个。所以这个前提现在只能由"库里本来就有一行"来表达。

    顺带也就扎住了豁免节点最容易被忘掉的一面:**豁免的是能不能写,不是提示词预算。**
    一份两万字的正文照样要在 `FOCUS_BODY_CHARS` 处被截断并说明,否则"旧正文完全豁免"
    会被读成"旧正文可以无限地灌进提示词"。
    """
    account = await make_account()
    root = await _root_id(app_client, account)
    body = "开头标记" + "甲" * (FOCUS_BODY_CHARS + 500) + "结尾标记"
    node = await _create(app_client, account, root, "长正文")
    async with SessionLocal() as session:
        await session.execute(
            update(PlanNode)
            .where(PlanNode.id == uuid.UUID(node))
            .values(description=body)
        )
        await session.commit()

    fake = use_reasoner(FakeReasoner())
    assert (
        await _send(app_client, account, "看看这个", contextNodeId=node, scopeRootId=root)
    ).status_code == 200

    prompt = render_turn(fake.calls[0])
    assert "开头标记" in prompt
    assert "结尾标记" not in prompt, "超长正文被整段塞进提示词了"
    assert TRUNCATED_NOTE.format(shown=FOCUS_BODY_CHARS, total=len(body)) in prompt, (
        "截断了却没说 —— 模型会把读到的半段当成全文"
    )


async def test_a_note_on_another_node_is_counted_but_not_read(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """别的节点有长笔记 -> 提示词里只说**有多大**,正文一个字都不进去。

    两种做法各有各的坏处,这里是两边都要挡住的那条线:正文全都塞进去会让提示词
    随节点数线性膨胀(80 个节点 × 两万字);而完全不提会让模型**根本不知道那里有
    一片内容** —— 它于是会重新问一遍用户已经写下来的事。

    所以只给一个数。模型拿到"有 12 字"之后该做什么是它自己的判断:够小就直接问,
    够大就先要。而 `notes_read` 是假,所以它不会声称自己读过了。
    """
    account = await make_account()
    root = await _root_id(app_client, account)
    sibling = await _create(app_client, account, root, "旁边那个")
    focus = await _create(app_client, account, root, "正在聊的那个")
    await _put_note(app_client, account, sibling, "只有这一段话在笔记里")

    fake = use_reasoner(FakeReasoner())
    assert (
        await _send(app_client, account, "看看这个", contextNodeId=focus, scopeRootId=root)
    ).status_code == 200

    prompt = render_turn(fake.calls[0])
    assert NOTES_PRESENT_NOTE.format(chars=len("只有这一段话在笔记里")) in prompt, (
        "有笔记却一个字都没提 —— 模型会去重新问用户已经写下来的事"
    )
    assert "只有这一段话在笔记里" not in prompt, "没读的笔记正文漏进提示词了"


async def test_the_focus_nodes_note_is_read_and_the_cut_is_stated(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """焦点节点的长笔记要读,而且**用它自己的预算**截、截了要说。

    与上面那条一起构成"读什么"的两半。这里额外钉住预算是分开的:长笔记是两万字量级,
    用 `FOCUS_BODY_CHARS`(给 300 字简述用的那个)去截它,等于每次只看到开头 ——
    而 `TRUNCATED_NOTE` 里的两个数会把这件事说清楚,所以断言里两个数都要对。
    """
    account = await make_account()
    root = await _root_id(app_client, account)
    focus = await _create(app_client, account, root, "正在聊的那个")
    body = "笔记开头" + "乙" * (NOTES_BODY_CHARS + 200) + "笔记结尾"
    await _put_note(app_client, account, focus, body)

    fake = use_reasoner(FakeReasoner())
    assert (
        await _send(app_client, account, "看看这个", contextNodeId=focus, scopeRootId=root)
    ).status_code == 200

    prompt = render_turn(fake.calls[0])
    # 只断言标签出现,不写 `长笔记:` —— 截断时那一行是「长笔记(只给了前 … 字…):」,
    # 标签和冒号之间夹着那句说明。写成带冒号的话,这条用例会在**它正想验的那条路上**红。
    assert NOTES_LABEL in prompt, "焦点节点的长笔记没有被读"
    assert "笔记开头" in prompt
    assert "笔记结尾" not in prompt, "长笔记被整段塞进提示词了"
    assert TRUNCATED_NOTE.format(shown=NOTES_BODY_CHARS, total=len(body)) in prompt, (
        "长笔记截断了却没说"
    )


async def test_the_view_and_the_focus_reach_the_prompt(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """`current_view` 与 `context_node_title` 这两个字段以前算了却从来不渲染。

    它们是"用户此刻在看哪儿"的唯一来源,而指代消解完全依赖它:"这个阶段展开讲讲"
    里的"这个"指的是谁,只有这两个字段知道。

    这里用 `workbench` 而不是编一个视图名:前端今天**确实**只发这一个值
    (provider.tsx),断言一个没人发过的值等于在测一份设想。
    """
    account = await make_account()
    root = await _root_id(app_client, account)
    stage = await _create(app_client, account, root, "阶段一")

    fake = use_reasoner(FakeReasoner())
    assert (
        await _send(
            app_client,
            account,
            "这个阶段展开讲讲",
            contextNodeId=stage,
            scopeRootId=root,
            currentView="workbench",
        )
    ).status_code == 200

    turn = fake.calls[0]
    prompt = render_turn(turn)
    assert "这次的作用范围" in prompt, "提示词里没有范围那一段"
    assert "阶段一" in prompt and turn.focus_handle in prompt, "焦点节点没有出现在范围那一段里"
    assert "用户此刻在:工作台" in prompt, "用户在哪个视图里没有送进去"


async def test_relations_are_rendered_and_precedence_is_not_confused_with_relatedness(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """前置与关联要分开说:只有前置会改变排期,而混成一类会让模型拿关联去推顺序。"""
    account = await make_account()
    root = await _root_id(app_client, account)
    first = await _create(app_client, account, root, "先做")
    second = await _create(app_client, account, root, "后做")
    third = await _create(app_client, account, root, "相关的那件")

    created = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/dependencies",
        json={"predecessorId": first, "successorId": second},
        headers=account.headers,
    )
    assert created.status_code == 201, created.text
    related = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/relations",
        json={"sourceId": third, "targetId": second, "relationType": "related_to"},
        headers=account.headers,
    )
    assert related.status_code == 201, related.text

    fake = use_reasoner(FakeReasoner())
    assert (
        await _send(app_client, account, "这两件事什么关系", scopeRootId=root)
    ).status_code == 200

    turn = fake.calls[0]
    kinds = {(edge.kind, edge.relation_type) for edge in turn.edges}
    assert ("dep", "finish_to_start") in kinds
    assert ("rel", "related_to") in kinds

    prompt = render_turn(turn)
    assert "前置" in prompt and "相关" in prompt
    assert "只有「前置」会影响排期" in prompt, (
        "没有把'哪一种边会影响排期'说清楚 —— 模型会把一条关联当成先后顺序"
    )


# ---------------------------------------------------------------------------------
# 范围是一条权限
# ---------------------------------------------------------------------------------
async def test_an_out_of_scope_action_is_refused_and_nothing_is_written(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db
) -> None:
    """范围外的变更必须被拒 —— **而且不能只丢那一条、照做其余的**。

    只丢越界那条的后果是:用户点确认,看到"AI 提了三条、执行了两条",而其中一条
    正好是他最在意的那一支被漏掉 —— 界面上看起来一切正常。所以整份拒绝,
    并逐条说明哪一条越了界。
    """
    account = await make_account()
    root = await _root_id(app_client, account)
    inside = await _create(app_client, account, root, "在范围内", description="原来的正文")
    await _create(app_client, account, root, "在范围外", description="不许动的正文")

    before = await _revisions(db)
    fake = use_reasoner(
        FakeReasoner(
            actions=(
                {"op": "update_node", "targetRef": "n2", "description": "范围内:改这句"},
                {"op": "update_node", "targetRef": "n3", "description": "范围外:也想改"},
            )
        )
    )
    response = await _send(
        app_client, account, "改一下这两条", contextNodeId=inside, scopeRootId=inside
    )
    assert response.status_code == 200, response.text

    # 记号是服务端这一轮分配的,先确认假模型引用的编号确实对应它想指的那两个节点。
    handles = {node.title: node.handle for node in fake.calls[0].nodes}
    assert (handles["在范围内"], handles["在范围外"]) == ("n2", "n3"), handles

    body = response.json()
    assert body["proposal"] is None, "越界的动作没有被拦住"
    codes = [(item["code"], item["message"]) for item in body["proposalErrors"]]
    assert any(code == "OUT_OF_SCOPE" for code, _ in codes), codes
    assert any("在范围外" in message for _, message in codes), (
        f"拒绝的理由里没有说清是哪个节点越界:{codes}"
    )

    after = await _by_title(app_client, account)
    assert after["在范围内"]["description"] == "原来的正文", "整份提案不该有部分落地"
    assert after["在范围外"]["description"] == "不许动的正文"
    assert await _revisions(db) == before, "被拒的那一轮不该产生计划版本"


async def test_creating_a_node_under_an_out_of_scope_parent_is_refused(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """往范围外的节点下面挂东西,也是改范围外的东西。"""
    account = await make_account()
    root = await _root_id(app_client, account)
    inside = await _create(app_client, account, root, "在范围内")
    await _create(app_client, account, root, "在范围外")

    use_reasoner(
        FakeReasoner(
            actions=(
                {"op": "create_node", "localId": "n9", "parentRef": "n3", "title": "挂到范围外"},
            )
        )
    )
    response = await _send(
        app_client, account, "加一条", contextNodeId=inside, scopeRootId=inside
    )
    assert response.status_code == 200, response.text
    assert response.json()["proposal"] is None
    assert [item["code"] for item in response.json()["proposalErrors"]] == ["OUT_OF_SCOPE"]
    assert "挂到范围外" not in await _by_title(app_client, account)


async def test_an_action_inside_the_scope_still_works(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """范围**之内**的动作不能被一起误伤 —— 否则上面那些拒绝就没有意义了。"""
    account = await make_account()
    root = await _root_id(app_client, account)
    inside = await _create(app_client, account, root, "在范围内", description="原来的正文")

    use_reasoner(
        FakeReasoner(
            actions=({"op": "update_node", "targetRef": "n2", "description": "改好的正文"},)
        )
    )
    response = await _send(
        app_client, account, "把正文写清楚", contextNodeId=inside, scopeRootId=inside
    )
    assert response.status_code == 200, response.text
    proposal = response.json()["proposal"]
    assert proposal is not None, response.json()["proposalErrors"]

    confirmed = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/proposals/{proposal['id']}/confirm",
        json={"idempotencyKey": "scope-ok"},
        headers=account.headers,
    )
    assert confirmed.status_code == 200, confirmed.text
    assert (await _by_title(app_client, account))["在范围内"]["description"] == "改好的正文"


async def test_a_scope_root_outside_this_workspace_is_refused(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """范围起点必须真的是这个空间里存活的节点。

    **悄悄放宽成"整个空间"是最坏的处理**:用户以为 AI 只在自己看的那一支里动,
    实际拿到了整棵树,而这件事没有任何迹象。宁可让他看到一句"这个范围起点不对"。
    """
    account = await make_account()
    fake = use_reasoner(FakeReasoner())

    unknown = await _send(
        app_client, account, "在吗", scopeRootId=str(uuid.uuid4())
    )
    assert unknown.status_code == 400, unknown.text
    assert unknown.json()["error"]["code"] == "INVALID_INPUT"

    other = await make_account(email="b@example.com", workspace_title="另一个空间")
    other_root = await _root_id(app_client, other)
    cross = await _send(app_client, account, "在吗", scopeRootId=other_root)
    assert cross.status_code == 400, cross.text
    assert "b@example.com" not in cross.text
    assert fake.calls == [], "范围起点不合法时不该已经调过模型"


# ---------------------------------------------------------------------------------
# 输入快照
# ---------------------------------------------------------------------------------
async def test_the_snapshot_records_what_this_analysis_read(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """快照记的是"这次分析建立在什么之上":范围、焦点、正文版本、关系。"""
    account = await make_account()
    root = await _root_id(app_client, account)
    inside = await _create(app_client, account, root, "在范围内", description="正文")
    second = await _create(app_client, account, root, "第二件")
    await app_client.post(
        f"/api/workspaces/{account.workspace_id}/dependencies",
        json={"predecessorId": inside, "successorId": second},
        headers=account.headers,
    )

    fake = use_reasoner(FakeReasoner())
    assert (
        await _send(app_client, account, "看一下", contextNodeId=inside, scopeRootId=root)
    ).status_code == 200
    snapshot = fake.calls[0].input_snapshot

    assert snapshot is not None, "这一轮没有留下输入快照 —— 以后没法判断它有没有过期"
    assert snapshot.scope_root_id == root
    assert snapshot.focus_node_id == inside
    focused = next(node for node in snapshot.nodes if node.node_id == inside)
    assert focused.content_version == 1
    assert focused.parent_id == root
    assert any(edge.startswith("dep:") and inside in edge for edge in snapshot.edges), snapshot.edges
    assert snapshot.live_node_count == 3

    # 存储形状要能原样读回来 —— 库里躺着的是历史行,读不懂就等于没记。
    from backend.services.input_snapshot import InputSnapshot

    assert InputSnapshot.from_payload(snapshot.to_payload()) == snapshot


async def test_the_snapshot_changes_when_the_body_changes(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """正文改了,快照就要变 —— 这是"旧分析过期"唯一可靠的判据。"""
    account = await make_account()
    root = await _root_id(app_client, account)
    node = await _create(app_client, account, root, "在范围内", description="第一版正文")

    fake = use_reasoner(FakeReasoner())
    await _send(app_client, account, "先看看", contextNodeId=node, scopeRootId=root)
    before = fake.calls[0].input_snapshot

    assert (
        await _patch(app_client, account, node, description="第二版正文")
    ).status_code == 200
    await _send(app_client, account, "再看一次", contextNodeId=node, scopeRootId=root)
    after = fake.calls[1].input_snapshot

    assert after != before, "正文改了,快照却没变 —— 那么旧分析永远看起来是新的"

    # **变化的不是结构摘要,这一点是有意的。** 结构摘要里刻意不含正文版本
    # (见 services/input_snapshot.py 的 `_node_key`):混进去的话,某个**这次没读到**
    # 的节点改了正文,会让整片范围里的分析一起作废 —— 正是规范点名要避免的
    # "无关分支不应使所有分析一起失效"。正文改动走的是**逐行比较**那条路
    # (`SnapshotNode.content_version`),能说出具体是哪个节点变了。
    assert after.structure_digest == before.structure_digest
    assert [n.content_version for n in after.nodes] != [n.content_version for n in before.nodes]


async def test_layout_and_viewport_do_not_invalidate_an_analysis(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """拖动节点、缩放画布**不算**输入变了。

    这条是产品要求:布局是"用户正在看哪儿",不是分析赖以成立的输入。算进去的后果
    是用户每拖一下画布就得到处点"重新分析",那个提示很快就没有人看了。
    """
    account = await make_account()
    root = await _root_id(app_client, account)
    node = await _create(app_client, account, root, "在范围内", description="正文")

    fake = use_reasoner(FakeReasoner())
    await _send(app_client, account, "先看看", contextNodeId=node, scopeRootId=root)
    before = fake.calls[0].input_snapshot

    moved = await app_client.put(
        f"/api/workspaces/{account.workspace_id}/layout",
        json={
            "positions": [{"nodeId": node, "x": 320.5, "y": -180.0}],
            "viewports": [{"scopeNodeId": root, "zoom": 1.4, "panX": 12.0, "panY": -8.0}],
        },
        headers=account.headers,
    )
    assert moved.status_code == 200, moved.text

    await _send(app_client, account, "再看一次", contextNodeId=node, scopeRootId=root)
    after = fake.calls[1].input_snapshot
    assert after == before, "拖了一下画布就说分析过期了 —— 那个提示很快会没人看"
