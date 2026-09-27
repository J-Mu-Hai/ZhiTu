"""`GET /plan` 的投影契约:库里有什么,界面上就能看到什么。

## 这个文件在防什么

它防的是一类**不会报错**的 bug。计划树在后端建好了、确认成功、接口也返回 200,
但界面上少一个节点、或者多一条连到不存在节点的线。没有任何东西会因此抛异常,
日志里也没有一行异常栈 —— 用户看到的是"我明明排了三个阶段,只出来两个",
而开发者看到的是一切正常。

这个仓库里就发生过这件事的镜像版本:AI 生成的 `stage` 被前端的两处过滤挡掉了,
后端全是好的,界面上它下面的任务全都不可达。所以这里的断言刻意选在**投影**这一层:
不是"接口返回了 200",而是"载荷里的节点集合与库里的活节点集合逐字相等"。

## 为什么断言的是集合相等,不是"包含"

"包含"是弱断言:投影多返回一行(比如把软删除的节点也带出来了)它照样通过。
而多出来的那一行在界面上是一个用户点得开、但后端不认的节点 —— 那正是要防的东西。
"""

from __future__ import annotations

import uuid

import httpx
from sqlalchemy import func, select

from backend.db.models import Dependency, PlanNode
from backend.tests.conftest import FakeReasoner

#: 一棵三层、带依赖的小树。刻意让深度不同(阶段 -> 任务 -> 子任务),
#: 因为"父链终止于根"这件事只在有深度的树上才是个真命题。
DEEP_TREE = (
    {
        "op": "create_node",
        "localId": "n2",
        "parentRef": "n1",
        "title": "阶段一:基础",
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
        "parentRef": "n3",
        "title": "读官方教程前三节",
        "nodeType": "task",
        "estimateMinutes": 45,
    },
    {
        "op": "create_node",
        "localId": "n5",
        "parentRef": "n2",
        "title": "控制流",
        "nodeType": "task",
        "estimateMinutes": 120,
    },
    {"op": "create_dependency", "predecessorRef": "n3", "successorRef": "n5"},
)


async def _propose_and_confirm(
    client: httpx.AsyncClient, account, *, actions=DEEP_TREE, key: str = "projection-key"
) -> dict:
    """让假模型提一份计划并确认掉,返回确认响应。

    走真实接口而不是直接写库:`/plan` 要投影的是**确认事务真正写出来的那些行**,
    绕过事务手工插几行,测的就不是同一条路径了。
    """
    proposed = await client.post(
        f"/api/workspaces/{account.workspace_id}/messages",
        json={"content": "帮我排一版"},
        headers=account.headers,
    )
    assert proposed.status_code == 200, proposed.text
    proposal = proposed.json()["proposal"]
    assert proposal is not None, proposed.text

    confirmed = await client.post(
        f"/api/workspaces/{account.workspace_id}/proposals/{proposal['id']}/confirm",
        json={"idempotencyKey": key},
        headers=account.headers,
    )
    assert confirmed.status_code == 200, confirmed.text
    return confirmed.json()


async def _plan(client: httpx.AsyncClient, account) -> dict:
    response = await client.get(f"/api/workspaces/{account.workspace_id}/plan", headers=account.headers)
    assert response.status_code == 200, response.text
    return response.json()


async def _live_node_ids(db, workspace_id: str) -> set[str]:
    rows = await db.execute(
        select(PlanNode.id).where(
            PlanNode.workspace_id == uuid.UUID(workspace_id), PlanNode.deleted_at.is_(None)
        )
    )
    return {str(row) for row in rows.scalars()}


async def test_every_node_in_the_database_shows_up_in_the_payload(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db
) -> None:
    """载荷里的节点集合 == 库里的活节点集合。**双向**。

    只断言"库里的都在载荷里"会漏掉多返回;只断言"载荷里的都在库里"会漏掉少返回。
    这个测试存在的理由就是这一类不对称,所以它必须两边都查。
    """
    account = await make_account()
    use_reasoner(FakeReasoner(actions=DEEP_TREE))
    await _propose_and_confirm(app_client, account)

    plan = await _plan(app_client, account)
    in_payload = {node["id"] for node in plan["nodes"]}
    in_database = await _live_node_ids(db, account.workspace_id)

    assert in_payload == in_database
    # 4 个新建 + 建空间时那一条根目标。数字写死是有意的:如果确认事务哪天少写了一行,
    # 上面那个集合相等仍然成立(两边都少),这个数字是唯一能抓住它的地方。
    assert len(in_payload) == 5
    assert plan["totalNodes"] == 5


async def test_the_ai_nodes_all_appear_and_keep_their_titles(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db
) -> None:
    """新建的每个节点都在,标题是模型给的那个,来源标着 `ai`。

    标题这一条不是冗余的:投影里把 `title` 和 `description` 接反、或者某个字段
    在 `node_to_dict` 里被漏掉,集合相等一样通过。
    """
    account = await make_account()
    use_reasoner(FakeReasoner(actions=DEEP_TREE))
    await _propose_and_confirm(app_client, account)

    plan = await _plan(app_client, account)
    by_title = {node["title"]: node for node in plan["nodes"]}

    for expected in ("阶段一:基础", "变量与类型", "读官方教程前三节", "控制流"):
        assert expected in by_title, f"「{expected}」没有出现在 /plan 里"
        assert by_title[expected]["origin"] == "ai"

    # 建空间时那条根目标是用户自己给的,不能也被标成 ai。
    root = next(node for node in plan["nodes"] if node["parentId"] is None)
    assert root["origin"] == "user"


async def test_every_parent_chain_terminates_at_the_root(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """从任一节点往上走,必须**无环地**走到唯一的根。

    这是路径视图渲染一棵树的前提。父链断在半路,界面上那一段就是孤立的;
    父链成环,前端的递归渲染会直接爆栈 —— 而这两种情况在服务端不会报任何错。
    """
    account = await make_account()
    use_reasoner(FakeReasoner(actions=DEEP_TREE))
    await _propose_and_confirm(app_client, account)

    plan = await _plan(app_client, account)
    parents = {node["id"]: node["parentId"] for node in plan["nodes"]}
    roots = [node_id for node_id, parent in parents.items() if parent is None]

    assert len(roots) == 1, f"根节点应当恰好一个,实际 {len(roots)} 个"
    root_id = roots[0]

    for node_id in parents:
        seen: set[str] = set()
        cursor: str | None = node_id
        while cursor is not None:
            assert cursor not in seen, f"从 {node_id} 出发的父链成环:{seen | {cursor}}"
            seen.add(cursor)
            parent = parents.get(cursor)
            # `parent` 不在表里 -> 指向了一个载荷里不存在的节点。
            # 允许的终点只有 None(根)。
            assert parent is None or parent in parents, (
                f"{cursor} 的父节点 {parent} 不在载荷里"
            )
            cursor = parent
        assert root_id in seen, f"从 {node_id} 出发的父链没有走到根 {root_id}"


async def test_depth_matches_the_actual_distance_to_the_root(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """`depth` 是算出来的,不是从模型那儿抄的。

    模型没有输出 depth —— 它是服务端在确认时按父节点推出来的。如果哪天它变成一个
    由模型提供的字段,或者推的时候漏了 +1,树就会在界面上缩成一团,而所有接口
    仍然返回 200。
    """
    account = await make_account()
    use_reasoner(FakeReasoner(actions=DEEP_TREE))
    await _propose_and_confirm(app_client, account)

    plan = await _plan(app_client, account)
    parents = {node["id"]: node["parentId"] for node in plan["nodes"]}
    by_title = {node["title"]: node for node in plan["nodes"]}

    def depth_of(node_id: str) -> int:
        steps = 0
        cursor = parents[node_id]
        while cursor is not None:
            steps += 1
            cursor = parents[cursor]
        return steps

    for node in plan["nodes"]:
        assert node["depth"] == depth_of(node["id"]), f"「{node['title']}」的 depth 不对"

    # 顺带把层级关系钉住:阶段 1 层、任务 2 层、子任务 3 层。
    assert by_title["阶段一:基础"]["depth"] == 1
    assert by_title["变量与类型"]["depth"] == 2
    assert by_title["读官方教程前三节"]["depth"] == 3


async def test_dependencies_are_projected_with_both_ends_alive(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """依赖只投影两端都还在的边。"""
    account = await make_account()
    use_reasoner(FakeReasoner(actions=DEEP_TREE))
    await _propose_and_confirm(app_client, account)

    plan = await _plan(app_client, account)
    by_title = {node["title"]: node["id"] for node in plan["nodes"]}

    assert len(plan["dependencies"]) == 1
    edge = plan["dependencies"][0]
    assert edge["predecessorId"] == by_title["变量与类型"]
    assert edge["successorId"] == by_title["控制流"]


async def test_a_deleted_node_disappears_along_with_its_dangling_edges(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db
) -> None:
    """软删除的节点从载荷里消失,连到它的边也一起消失。

    这一条是"界面上不会出现指向不存在节点的连线"的**唯一**保证。软删除不会级联
    清理 `dependencies` 行(历史版本的快照还引用着它们),所以投影必须自己过滤。
    不过滤的话,时间线视图会画出一条终点为 `undefined` 的箭头。
    """
    account = await make_account()
    use_reasoner(FakeReasoner(actions=DEEP_TREE))
    await _propose_and_confirm(app_client, account)

    before = await _plan(app_client, account)
    by_title = {node["title"]: node["id"] for node in before["nodes"]}

    # 删掉依赖的前驱。删完之后那条边两端只剩一端活着。
    deleted = await app_client.request(
        "DELETE",
        f"/api/workspaces/{account.workspace_id}/nodes/{by_title['变量与类型']}",
        headers=account.headers,
    )
    assert deleted.status_code == 200, deleted.text

    after = await _plan(app_client, account)

    assert by_title["变量与类型"] not in {node["id"] for node in after["nodes"]}
    assert after["dependencies"] == []
    # 删除的是**子树**:`读官方教程前三节` 挂在被删的任务下面。
    assert by_title["读官方教程前三节"] not in {node["id"] for node in after["nodes"]}
    # 没有关系的节点不受影响 —— 否则"删一个"会变成"删一片"。
    assert by_title["控制流"] in {node["id"] for node in after["nodes"]}
    assert after["totalNodes"] == len(after["nodes"])

    # 节点是**软删**的:行还在库里,只是 `deleted_at` 有值。物理删除会让历史版本的
    # 快照(`plan_revisions.snapshot` 里记着这些 id)指向空气。
    live = await db.scalar(
        select(func.count(PlanNode.id)).where(
            PlanNode.workspace_id == uuid.UUID(account.workspace_id),
            PlanNode.deleted_at.is_(None),
        )
    )
    total = await db.scalar(
        select(func.count(PlanNode.id)).where(
            PlanNode.workspace_id == uuid.UUID(account.workspace_id)
        )
    )
    assert live == 3, "删掉的是子树,库里应该还剩 3 个活节点"
    assert total == 5, "被删的两行必须还在库里,只是被标记了"

    # 而**依赖行留着**(2026-09-27 改,提交见 `docs/10-NEXT-BATCH-SCOPE.md` 第 5 节)。
    # 这里原来断言的是反面:"指向已删除节点的依赖行必须清掉",理由是"不清掉的话,
    # `POST /dependencies` 的幂等检查会返回一条悬空的行"。**那个理由不成立**:
    # `add_dependency` 是先用 `load_node` 取两端、再查有没有重复的,归档的那一端根本
    # 取不出来,幂等检查到不了那条悬空的行。
    #
    # 而"清掉"的代价很大:删除默认是**归档**(可恢复),恢复时那条边要原样回来 ——
    # 行被物理删掉之后,恢复拿到的是一个一条前置都没有的节点,排期正是按前置算的。
    # 所以现在归档不动行,界面上看不到它靠的是投影那侧"两端都活着"的过滤
    # (上面那半条断言已经钉住了这一点)。
    rows = await db.execute(
        select(Dependency).where(Dependency.workspace_id == uuid.UUID(account.workspace_id))
    )
    assert len(list(rows.scalars())) == 1, "归档不动边:那一行要留着,恢复时它还得回来"


async def test_completed_nodes_are_counted_by_the_server(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """`completedNodes` 由服务端数,并且会跟着完成操作走。

    前端数 `nodes.filter(...)` 现在恰好等价 —— 这个断言是为了让"将来按需分页时
    这两个数字悄悄变成当前页的数量"这件事在改动的当场就红掉。
    """
    account = await make_account()
    use_reasoner(FakeReasoner(actions=DEEP_TREE))
    await _propose_and_confirm(app_client, account)

    before = await _plan(app_client, account)
    assert before["completedNodes"] == 0

    by_title = {node["title"]: node["id"] for node in before["nodes"]}
    completed = await app_client.patch(
        f"/api/workspaces/{account.workspace_id}/nodes/{by_title['读官方教程前三节']}",
        json={"status": "completed"},
        headers=account.headers,
    )
    assert completed.status_code == 200, completed.text

    after = await _plan(app_client, account)
    assert after["completedNodes"] == 1
    assert after["totalNodes"] == before["totalNodes"], "勾完成不该改变节点总数"
    # 勾选完成是一次明确的用户操作,它确实产生一个新版本 —— 计划变了。
    assert after["revisionVersion"] == before["revisionVersion"] + 1


#: 访谈共建那条路:一条行动 + 一条信息主题。信息主题**不带工时** ——
#: 带了它会被 `INFORMATION_NODE_MUST_NOT_BE_SCHEDULABLE` 拒掉(见
#: `test_proposal_dedup.py`),而这里要验的是投影,不是那道闸。
INTERVIEW_TREE = (
    {
        "op": "create_node",
        "localId": "n2",
        "parentRef": "n1",
        "title": "写文献综述",
        "nodeType": "task",
        "estimateMinutes": 120,
    },
    {
        "op": "create_node",
        "localId": "n3",
        "parentRef": "n2",
        "title": "我排名 38",
        "nodeType": "capability",
        "purpose": "information",
    },
)


async def test_an_information_node_is_on_the_canvas_but_out_of_the_totals(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """信息用途的节点**在画布上**,但不在计时里 —— 两件事必须同时成立。

    ## 为什么这条在投影这一层

    排除信息节点有两种做法,只有一种是对的:

    - 把它从 `nodes` 里过滤掉 —— 用户看不到它,**而它正是他刚答出来的那个事实**;
      §2.5 的访谈共建就白做了。
    - 从**排期与统计**里排除它 —— 界面上它还在,只是不占日历、不进分子分母。

    所以这里三件事一起断言:`purpose` 到了线格式上(前端靠它决定不画勾选框)、
    它没进 `totalNodes`、而它的父链**照常连到根**(画布上仍然是一棵树,不会因为
    用途不同就飘在外面)。
    """
    account = await make_account()
    use_reasoner(FakeReasoner(actions=INTERVIEW_TREE, reply="访谈记下来了。"))
    await _propose_and_confirm(app_client, account, key="interview-key")

    plan = await _plan(app_client, account)
    by_title = {node["title"]: node for node in plan["nodes"]}
    assert "我排名 38" in by_title, "信息节点被从画布上过滤掉了 —— 它是个事实,不是一个不该显示的东西"
    assert by_title["我排名 38"]["purpose"] == "information"
    assert by_title["写文献综述"]["purpose"] == "planning", (
        "没提用途的那一条应当默认是 planning"
    )

    # 每一个节点都带这个字段,而且值合法 —— 前端只需要读它,不需要兜底。
    assert {node["purpose"] for node in plan["nodes"]} <= {"planning", "information"}

    # 父链:信息节点挂在任务下面,一路连到根。
    info = by_title["我排名 38"]
    assert info["parentId"] == by_title["写文献综述"]["id"]
    assert info["depth"] == by_title["写文献综述"]["depth"] + 1
    parents = {node["id"]: node["parentId"] for node in plan["nodes"]}
    chain = 0
    cursor = info["parentId"]
    while cursor is not None:
        chain += 1
        cursor = parents[cursor]
        assert chain <= len(parents), "父链成环或者没有终止在根上"
    assert chain == info["depth"]

    # 统计只数 planning:根 + 任务 = 2,信息节点不进分母。
    assert plan["totalNodes"] == 2, f"信息节点进了分母:totalNodes={plan['totalNodes']}"
    assert plan["completedNodes"] == 0
