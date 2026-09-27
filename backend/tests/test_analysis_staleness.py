"""AI 分析的三条承诺:只增不改、"过期"是现算的、以及"基于旧版本"那句话必须是真话。

## 这一批要钉死的,是"分析记录不会变成第二份真相"

规范要求 AI 在这个节点上做出的判断能被记住、能在内容变化之后被标成过期、能一键
重新分析。最容易走歪的实现是**存一个 `stale` 布尔列**:写进去之后就没有人能回答
"为什么过期",而用户看到的只是一个角标。所以这里的过期判断是**每次读的时候现算**的
—— 拿当时那份输入跟此刻库里的样子比一遍,把差异说成人话。这样:

- 同一个 id 隔一会儿再读,`freshness` 可以变。**这是对的**,它描述的是"现在还成不
  成立",不是"它出生时成不成立"。
- 理由是一句界面能直接显示的话("「阶段二」的正文改过了"),不是一个布尔值。

## 三条边界,每条都有一组用例

1. **只增不改。** 重新分析一次不抹掉上一次 —— 用户常问的是"它上次为什么那么说"。
2. **无关的变化不许连坐。** 布局、视口是用户偏好,拖动一下画布不该让分析作废;
   而在**别的分支**上加一个节点也不该让这一支的分析一起失效(规范点名的那条)。
3. **模型回答期间用户改了输入。** 那时候模型这段话说的已经不是现在的事:它只能
   作为一条标着「基于旧版本」的历史分析留下,**不能**变成一份点了确认就能生效的提案。
   而确认时那道版本校验仍然保留 —— 它管的是另一段时间(提案生成到用户点确认)。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select, update

from backend.agent.runtime.base import AnalysisDraft, BriefClaim
from backend.db.models import NodeAnalysis, PlanNode, UserCapacityProfile
from backend.db.session import SessionLocal
from backend.tests.conftest import FakeReasoner


#: 一份能被记下的判断。七栏不必都填 —— `known` 够用,其余留空元组。
def _draft(**overrides) -> AnalysisDraft:
    base = {"known": ("用户已经会 Python 的语法",)}
    base.update(overrides)
    return AnalysisDraft(**base)


#: 一条能过校验的动作。`localId` 与 `parentRef` 是记号,`n1` 是根目标。
_ONE_ACTION = (
    {
        "op": "create_node",
        "localId": "n9",
        "parentRef": "n1",
        "title": "第一周:把环境跑起来",
        "nodeType": "stage",
        "estimateMinutes": 60,
    },
)


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
) -> str:
    response = await client.post(
        f"/api/workspaces/{account.workspace_id}/nodes",
        json={"parentId": parent_id, "title": title, "description": description},
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


async def _analyses(client: httpx.AsyncClient, account, **params) -> dict:
    response = await client.get(
        f"/api/workspaces/{account.workspace_id}/analyses",
        params=params or None,
        headers=account.headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _analyze(
    client: httpx.AsyncClient,
    account,
    use_reasoner,
    *,
    focus: str | None = None,
    scope: str | None = None,
    content: str = "帮我看看这个节点",
    **draft_overrides,
) -> dict:
    """聊一轮,让假模型留下一份分析,然后把它读回来。返回那一行。

    `focus` 是"这一轮在聊哪个节点",`scope` 是"这一轮允许改哪一片" ——
    **两个都要能单独给**,因为"无关分支不许连坐"那条只在前者不缩小范围、
    后者缩小时才验得出来。
    """
    reasoner = use_reasoner(FakeReasoner(analysis=_draft(**draft_overrides)))
    extra = {}
    if focus:
        extra["contextNodeId"] = focus
    if scope:
        extra["scopeRootId"] = scope
    response = await _send(client, account, content, **extra)
    assert response.status_code == 200, response.text
    assert reasoner.calls, "假模型没有被调用 —— 这一条测的就不是分析记录了"

    listed = (
        await _analyses(client, account, focusNodeId=focus) if focus else await _analyses(
            client, account
        )
    )
    assert listed["analyses"], "分析没有被记下来"
    return listed["analyses"][0]


def _reasons(analysis: dict) -> str:
    """把过期理由拼成一段,方便断言"里面有没有提到那件事"。"""
    return " / ".join(analysis["staleReasons"])


# ---------------------------------------------------------------------------------
# ① 只增不改
# ---------------------------------------------------------------------------------
async def test_a_second_analysis_does_not_erase_the_first(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """同一个节点分析两次 —— 两条都在,而且第一次的原话完好。

    "只增不改"不是洁癖。用户最常问的一句是**"它上次为什么那么说"**,而那句话只有
    上一次那条记录里才有。用 UPDATE 覆盖的话这个问题永远答不了,而且没有任何地方
    会报错 —— 界面上只是少了一行。
    """
    account = await make_account("analysis-append@example.com")
    focus = await _root_id(app_client, account)

    first = await _analyze(
        app_client, account, use_reasoner, focus=focus, risks=("第一版的判断",)
    )
    second = await _analyze(
        app_client, account, use_reasoner, focus=focus, risks=("第二版的判断",)
    )

    assert first["id"] != second["id"], "第二次分析覆盖了第一次"

    listed = await _analyses(app_client, account, focusNodeId=focus)
    assert len(listed["analyses"]) == 2
    # 新的在前。
    assert [row["id"] for row in listed["analyses"]] == [second["id"], first["id"]]
    # 第一版的原话还在,没有被第二版改写。
    assert listed["analyses"][1]["risks"] == ["第一版的判断"]


async def test_the_source_of_a_degraded_reply_is_recorded(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """降级时那段话不是模型说的 —— 分析记录里必须分得出来。

    `rule_fallback` 是本地规则拼出来的。它的判断和模型的判断在界面上长得一样的话,
    用户就没有任何办法知道自己读的是谁的话。
    """
    from backend.db.models.enums import ModelSource

    account = await make_account("analysis-source@example.com")
    focus = await _root_id(app_client, account)

    use_reasoner(
        FakeReasoner(
            analysis=_draft(),
            degraded=True,
            source=ModelSource.RULE_FALLBACK,
        )
    )
    response = await _send(app_client, account, "看看这个节点", contextNodeId=focus)
    assert response.status_code == 200, response.text

    listed = await _analyses(app_client, account, focusNodeId=focus)
    assert listed["analyses"][0]["modelSource"] == "rule_fallback"


# ---------------------------------------------------------------------------------
# ② 过期是现算的,而且说得出是谁变了
# ---------------------------------------------------------------------------------
@dataclass
class _Changed:
    """一组"改动"的描述:怎么改、以及理由里必须出现的那句话。

    `mutate` 收的是**分析当时就已经存在的那些节点**(见 `_Nodes`)。这一点是有意的:
    改动必须发生在一个**已经被这次分析读到过**的对象上,否则比的就是"多出来一个
    新东西",测不到"这个对象变了"那条路径 —— 而后者才是理由里那一句具体的话。
    """

    label: str
    mutate: object
    expected: str


@dataclass
class _Nodes:
    """分析开始之前就建好、因此被这次分析读到过的那些节点。"""

    focus: str
    first: str
    second: str


async def _edit_body(client, account, nodes: _Nodes):
    response = await _patch(client, account, nodes.focus, description="改成只有周末能做")
    assert response.status_code == 200, response.text


async def _add_child(client, account, nodes: _Nodes):
    await _create(client, account, nodes.focus, "新加的一步")


async def _archive_a_child(client, account, nodes: _Nodes):
    response = await client.delete(
        f"/api/workspaces/{account.workspace_id}/nodes/{nodes.first}",
        params={"mode": "archive"},
        headers=account.headers,
    )
    assert response.status_code == 200, response.text


async def _rename_a_child(client, account, nodes: _Nodes):
    response = await _patch(client, account, nodes.first, title="改名后")
    assert response.status_code == 200, response.text


async def _move_a_child(client, account, nodes: _Nodes):
    """把一个节点挪到另一个父节点下面。

    **这条直接写库,因为目前没有任何接口能移动节点。** `UpdateNodeRequest` 里没有
    `parentId`,提案动作里也没有"移动"这一种 —— 移动归属是后续批次的能力(见规划里
    的步骤 3)。但 `_diff` 里那段父子关系比较**现在就在**,而且它必须是对的:等移动
    能力上线的那一天,一条早已作废的分析还在标着"最新"是没人会发现的。
    """
    async with SessionLocal() as session:
        await session.execute(
            update(PlanNode)
            .where(PlanNode.id == uuid.UUID(nodes.first))
            .values(parent_id=uuid.UUID(nodes.second))
        )
        await session.commit()


async def _link_a_relation(client, account, nodes: _Nodes):
    response = await client.post(
        f"/api/workspaces/{account.workspace_id}/relations",
        json={
            "sourceId": nodes.first,
            "targetId": nodes.second,
            "relationType": "related_to",
        },
        headers=account.headers,
    )
    assert response.status_code == 201, response.text


@pytest.mark.parametrize(
    "change",
    [
        _Changed("正文改过", _edit_body, "正文改过了"),
        _Changed("多了一个子节点", _add_child, "多出了"),
        _Changed("子节点被归档", _archive_a_child, "被归档了"),
        _Changed("标题改了", _rename_a_child, "标题从"),
        _Changed("节点被移走", _move_a_child, "被移到别的地方了"),
        _Changed("加了一条关系", _link_a_relation, "关系变了"),
    ],
    ids=lambda change: change.label if isinstance(change, _Changed) else None,
)
async def test_a_change_to_the_input_makes_the_analysis_stale_with_a_reason(
    app_client: httpx.AsyncClient, make_account, use_reasoner, change: _Changed
) -> None:
    """输入变了 -> 过期,而且**说得出是哪件事变了**。

    这一组是本文件的核心:六种改法各自走一条不同的代码路径(节点的列、结构、
    软删除、标题、父子关系、关系表),而它们必须落在同一句人话上 —— "过期了"这三个
    字对用户没有用,他还得知道**是哪件事**让这份判断不再成立。
    """
    account = await make_account(f"analysis-stale-{uuid.uuid4().hex[:8]}@example.com")
    focus = await _root_id(app_client, account)
    # 两个先建好的子节点:改动要落在"被这次分析读到过"的对象上。
    nodes = _Nodes(
        focus=focus,
        first=await _create(app_client, account, focus, "甲"),
        second=await _create(app_client, account, focus, "乙"),
    )

    analysis = await _analyze(app_client, account, use_reasoner, focus=focus)
    assert analysis["freshness"] == "fresh"
    assert analysis["staleReasons"] == []

    await change.mutate(app_client, account, nodes)

    after = (await _analyses(app_client, account, focusNodeId=focus))["analyses"][0]
    assert after["id"] == analysis["id"], "读回来的应该是同一条分析"
    assert after["freshness"] == "stale", f"{change.label} 之后它还算最新的"
    assert change.expected in _reasons(after), _reasons(after)


async def test_a_changed_time_budget_makes_the_analysis_stale(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """用户说出了自己的**跨空间共享**的时间预算 —— 这里的分析同样作废。

    **这条直接写库,因为目前没有任何接口能写它。** 这不是偷懒:排期相关的写入操作
    本批明确不开放(见规范里那六个操作),所以"用户从界面填预算"这条路径现在还不
    存在。而注册时**刻意不建**这份档案(见 auth_service:把一个猜出来的预算当成
    用户确认过的事实,比没有更坏),所以这里要 INSERT 一条 —— 那就是"用户说出来了"。

    要验的是**快照里到底有没有这一栏**:少了它,用户说出预算之后所有旧分析都还标着
    "最新",而它们是基于一个已经不成立的假设做出的。
    """
    account = await make_account("analysis-budget@example.com")
    focus = await _root_id(app_client, account)

    analysis = await _analyze(app_client, account, use_reasoner, focus=focus)
    assert analysis["freshness"] == "fresh"

    async with SessionLocal() as session:
        session.add(
            UserCapacityProfile(
                user_id=uuid.UUID(account.id),
                weekly_total_minutes=120,
                safety_factor=Decimal("0.80"),
            )
        )
        await session.commit()

    after = (await _analyses(app_client, account, focusNodeId=focus))["analyses"][0]
    assert after["freshness"] == "stale"
    assert "整体时间预算变了" in _reasons(after), _reasons(after)


async def test_moving_the_canvas_does_not_make_the_analysis_stale(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """拖一下画布、缩放一下 —— 分析仍然是"最新"。

    这是**反向**的那一条,而它同样重要:布局与视口是用户偏好,不影响任何判断。
    把它们算进输入的话,用户每次整理画布都会看到一片"已过期" —— 而他会因此
    不再相信这个角标,连带真正过期的那几条也一起忽略掉。
    """
    account = await make_account("analysis-layout@example.com")
    focus = await _root_id(app_client, account)

    await _analyze(app_client, account, use_reasoner, focus=focus)

    response = await app_client.put(
        f"/api/workspaces/{account.workspace_id}/layout",
        json={
            "positions": [{"nodeId": focus, "x": 320.5, "y": -140.25}],
            "viewports": [{"scopeNodeId": focus, "zoom": 1.4, "panX": 12, "panY": -8}],
        },
        headers=account.headers,
    )
    assert response.status_code == 200, response.text

    after = (await _analyses(app_client, account, focusNodeId=focus))["analyses"][0]
    assert after["freshness"] == "fresh", _reasons(after)
    assert after["staleReasons"] == []


async def test_a_node_added_in_another_branch_does_not_invalidate_this_analysis(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """在**别的分支**上加一个节点,这一支的分析不算过期。

    规范点名的那条:没有读取也不影响本次决策的无关分支,不应使所有分析一起失效。
    这条能成立靠的是两件事,缺一条就会红:

    1. **范围真的收窄了**(`scopeRootId=左分支`)。不收的话整个空间都是"这一轮读到的
       范围",在右分支上加东西当然算输入变过 —— 那是对的,不是缺陷。
    2. **快照里没有"空间里一共有多少节点"这种全局计数**,正文版本也没混进结构摘要。
       混进去的话,一次与本次判断毫无关系的写入会让这一支的分析全部作废,而症状是
       "我什么都没干,分析怎么都过期了" —— 很难查。
    """
    account = await make_account("analysis-unrelated@example.com")
    root = await _root_id(app_client, account)
    left = await _create(app_client, account, root, "左分支")
    right = await _create(app_client, account, root, "右分支")

    analysis = await _analyze(app_client, account, use_reasoner, focus=left, scope=left)
    assert analysis["scopeRootId"] == left

    await _create(app_client, account, right, "右分支下面的新节点")

    after = (await _analyses(app_client, account, focusNodeId=left))["analyses"][0]
    assert after["id"] == analysis["id"]
    assert after["freshness"] == "fresh", _reasons(after)


async def test_a_change_inside_the_scope_still_invalidates(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """上一条的反面:同样收窄了范围,但改动落在**里面** —— 那就必须过期。

    没有这一条的话,上面那个"不连坐"的断言其实什么都没证明:一个永远返回
    `fresh` 的实现(比如根本没实现比较)同样能让它绿。
    """
    account = await make_account("analysis-in-scope@example.com")
    root = await _root_id(app_client, account)
    left = await _create(app_client, account, root, "左分支")
    await _create(app_client, account, root, "右分支")

    await _analyze(app_client, account, use_reasoner, focus=left, scope=left)

    await _create(app_client, account, left, "左分支里新加的一步")

    after = (await _analyses(app_client, account, focusNodeId=left))["analyses"][0]
    assert after["freshness"] == "stale", _reasons(after)
    assert "多出了" in _reasons(after), _reasons(after)


async def test_a_partial_reading_says_so_without_calling_itself_stale(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """只读到范围的一部分时,`coverageNote` 如实说明 —— 但**不影响新鲜度**。

    这两件事必须分开:"这份判断覆盖多大范围"和"它过期了吗"是不同的问题。混在一起
    的话,一条覆盖不全但完全没过期的分析会被标成过期,用户点"重新分析"之后拿到
    一份一模一样的东西(因为读窗没变)。
    """
    account = await make_account("analysis-coverage@example.com")
    focus = await _root_id(app_client, account)
    await _create(app_client, account, focus, "一个子节点")

    analysis = await _analyze(app_client, account, use_reasoner, focus=focus)

    assert analysis["freshness"] == "fresh"
    assert analysis["staleReasons"] == []
    # 这一轮读全了,所以没有说明。**没有说明不等于"覆盖不全"** —— 界面上不显示
    # 这一栏,正是"读全了"的表达。
    assert analysis["coverageNote"] is None


# ---------------------------------------------------------------------------------
# ③ 模型回答期间用户改了输入
# ---------------------------------------------------------------------------------
@dataclass
class _EditingReasoner(FakeReasoner):
    """在**被调用的那一刻**先改一次正文 —— 模拟用户在另一个标签页里改了东西。

    这是唯一能真实复现"模型思考期间输入变了"的办法:模型调用本身是一段真实的时间,
    而在测试里没法等几十秒。所以让假模型在回答之前先打一次真实的 HTTP 请求,走的是
    和用户完全同一条写入路径(同一套版本校验、同一个版本号递增点)。
    """

    client: object = None
    account: object = None
    edits: tuple = ()
    analysis: object = None
    actions: tuple = ()

    async def reason(self, turn):
        for node_id, fields in self.edits:
            response = await self.client.patch(
                f"/api/workspaces/{self.account.workspace_id}/nodes/{node_id}",
                json=fields,
                headers=self.account.headers,
            )
            assert response.status_code == 200, response.text
        return await super().reason(turn)


async def test_an_edit_during_the_call_kills_the_proposal_and_says_why(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """用户在模型思考期间改了正文 -> **不生成提案**,而且每条动作都有一条说明。

    为什么必须在这里拦,而不是等用户点确认:等到那时候,他已经完整读过一份基于
    旧数据的建议了,甚至可能已经照着改了自己的计划。

    为什么每条动作都要配一条错误:只说一句总述的话,界面上"这次打算改 1 处"和
    错误条数就对不上,而用户会以为剩下的已经生效了。
    """
    account = await make_account("analysis-input-changed@example.com")
    focus = await _root_id(app_client, account)

    reasoner = use_reasoner(
        _EditingReasoner(
            client=app_client,
            account=account,
            edits=((focus, {"description": "我在它回答的时候改了正文"}),),
            analysis=_draft(),
            actions=_ONE_ACTION,
        )
    )
    response = await _send(app_client, account, "帮我拆一下", contextNodeId=focus)
    assert response.status_code == 200, response.text
    body = response.json()

    assert reasoner.calls, "假模型没有被调用"
    assert body["inputChanged"] is True
    assert body["proposal"] is None
    assert len(body["proposalErrors"]) == len(_ONE_ACTION)
    assert {error["code"] for error in body["proposalErrors"]} == {"INPUT_CHANGED"}
    # 说明里必须告诉用户下一步做什么 —— 只说"没执行"的话他只能干等。
    assert "重新分析" in body["proposalErrors"][0]["message"]

    # 计划一行都没变。
    plan = await _plan(app_client, account)
    assert "第一周:把环境跑起来" not in {node["title"] for node in plan["nodes"]}


async def test_the_analysis_from_that_turn_is_kept_but_marked_stale(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """同一轮的那份分析**照收**,但一出生就标着"基于旧版本"。

    这是规范 §2.3 的原话:结果只能作为标有「基于旧版本」的历史分析。丢掉它是错的
    —— 模型确实说了那些话,用户要能回看;当成最新也是错的 —— 它说的不是现在的事。
    """
    account = await make_account("analysis-born-stale@example.com")
    focus = await _root_id(app_client, account)

    use_reasoner(
        _EditingReasoner(
            client=app_client,
            account=account,
            edits=((focus, {"description": "改过了"}),),
            analysis=_draft(risks=("基于旧版本的判断",)),
            actions=_ONE_ACTION,
        )
    )
    response = await _send(app_client, account, "帮我拆一下", contextNodeId=focus)
    assert response.status_code == 200, response.text

    listed = await _analyses(app_client, account, focusNodeId=focus)
    assert len(listed["analyses"]) == 1, "分析被丢掉了 —— 用户回看不到模型说过什么"
    row = listed["analyses"][0]
    assert row["risks"] == ["基于旧版本的判断"]
    assert row["freshness"] == "stale"
    assert "正文改过了" in _reasons(row), _reasons(row)


async def test_a_turn_with_nothing_to_propose_reports_no_errors(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """输入变过、但模型这一轮什么变更都没提 -> `inputChanged` 仍为真,错误为空。

    界面不能因为"错误为空"就把这件事咽下去:它要给出的下一步是"重新分析",
    而那个提示的依据是 `inputChanged`,不是 `proposalErrors`。两者混用的话,
    一次纯聊天的轮次里用户永远得不到任何提示。
    """
    account = await make_account("analysis-no-actions@example.com")
    focus = await _root_id(app_client, account)

    use_reasoner(
        _EditingReasoner(
            client=app_client,
            account=account,
            edits=((focus, {"description": "改过了"}),),
            analysis=_draft(),
            actions=(),
        )
    )
    response = await _send(app_client, account, "你觉得这个方向对吗", contextNodeId=focus)
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["inputChanged"] is True
    assert body["proposal"] is None
    assert body["proposalErrors"] == []


async def test_the_models_own_claims_do_not_kill_its_own_proposal(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """**模型自己这一轮记下的条件,不算"用户在它回答时改了输入"。**

    这是一条回归测试,对着一个真实发生过的缺陷:那次比较被放在 `apply_claims`
    **之后**,而记条件会改 `brief.version` 与 `weekly_available_minutes`,两者都在
    快照里。于是每一个"一边记条件一边提方案"的回合都自我作废 —— 而那恰好是
    **最常见**的那个回合(用户说"我每周能投 10 小时,帮我拆一下")。

    症状还很隐蔽:模型答得头头是道,提案卡片却永远不出现,而错误里写着"你在它回答
    之前改了正文"—— 用户根本没改任何东西。
    """
    account = await make_account("analysis-self-claim@example.com")
    focus = await _root_id(app_client, account)

    use_reasoner(
        FakeReasoner(
            analysis=_draft(),
            actions=_ONE_ACTION,
            claims=(BriefClaim("weekly_available_minutes", 600, "user_stated"),),
        )
    )
    response = await _send(app_client, account, "我每周能投 10 小时,帮我拆一下", contextNodeId=focus)
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["inputChanged"] is False
    assert body["proposalErrors"] == []
    assert body["proposal"] is not None, "模型自己记下的条件把它自己的提案作废了"
    assert body["changedFields"] == ["weekly_available_minutes"]


# ---------------------------------------------------------------------------------
# ④ 确认时那道校验仍然在
# ---------------------------------------------------------------------------------
async def test_a_proposal_built_before_the_edit_cannot_be_confirmed(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """提案生成之后用户改了正文 -> 确认被拒。

    `inputChanged` 那道闸管的是"模型思考期间"(建完上下文 → 模型返回),
    这一条管的是**另一段时间**(提案生成 → 用户点确认)。两段都要有,少一段就有
    一段时间是空的 —— 而这段通常是用户读提案、犹豫、然后点了确认的那几分钟。
    """
    account = await make_account("analysis-confirm@example.com")
    focus = await _root_id(app_client, account)

    use_reasoner(FakeReasoner(analysis=_draft(), actions=_ONE_ACTION))
    response = await _send(app_client, account, "帮我拆一下", contextNodeId=focus)
    assert response.status_code == 200, response.text
    proposal = response.json()["proposal"]
    assert proposal is not None, response.text

    # 用户读完提案、改了正文,才点了确认。
    assert (await _patch(app_client, account, focus, description="我又想了一下")).status_code == 200

    confirmed = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/proposals/{proposal['id']}/confirm",
        json={"idempotencyKey": "confirm-after-edit"},
        headers=account.headers,
    )
    assert confirmed.status_code == 409, confirmed.text
    assert confirmed.json()["error"]["code"] == "STALE_BASE_REVISION"

    # 一条都没落地。
    plan = await _plan(app_client, account)
    assert "第一周:把环境跑起来" not in {node["title"] for node in plan["nodes"]}


# ---------------------------------------------------------------------------------
# ⑤ 读接口本身
# ---------------------------------------------------------------------------------
async def test_an_empty_workspace_says_it_has_nothing_not_that_it_failed(
    app_client: httpx.AsyncClient, make_account
) -> None:
    """没分析过 -> 200 + 空列表 + 一句"还没有",不是 404,也不是沉默。

    空列表单独出现时有两种可能:真的没有,或者查询条件写错了。`note` 就是用来
    分开这两件事的 —— 界面上"这里还没有分析"和"什么都没查到"长得一模一样。
    """
    account = await make_account("analysis-empty@example.com")
    focus = await _root_id(app_client, account)

    listed = await _analyses(app_client, account, focusNodeId=focus)
    assert listed["analyses"] == []
    assert listed["focusNodeId"] == focus
    assert "还没有被分析过" in listed["note"], listed["note"]


async def test_the_list_filters_by_focus(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """`focusNodeId` 只给**这一个节点**的分析,不给它的祖先或后代。

    不做"这个节点及其后代"的展开是有意的:那会把一条关于祖辈的判断混进子节点的
    列表里,而用户点开一个节点时想看的是关于**这个**节点的判断。
    """
    account = await make_account("analysis-filter@example.com")
    root = await _root_id(app_client, account)
    child = await _create(app_client, account, root, "子节点")

    await _analyze(app_client, account, use_reasoner, focus=root, content="看看根")
    await _analyze(app_client, account, use_reasoner, focus=child, content="看看子节点")

    only_child = await _analyses(app_client, account, focusNodeId=child)
    assert len(only_child["analyses"]) == 1
    assert only_child["analyses"][0]["focusNodeId"] == child

    # 不给焦点就是这个空间的全部。
    everything = await _analyses(app_client, account)
    assert len(everything["analyses"]) == 2


async def test_the_limit_is_bounded(
    app_client: httpx.AsyncClient, make_account
) -> None:
    """`limit` 有上下界 —— 一个 `limit=100000` 不该被默默接受。

    接受的话,它会变成一个"一次读出全部分析"的接口,而每条分析都要重新取一次快照
    才能算新鲜度 —— 几百条就是几百次比较,而用户只是想看看最近那几条。
    """
    account = await make_account("analysis-limit@example.com")

    too_big = await app_client.get(
        f"/api/workspaces/{account.workspace_id}/analyses",
        params={"limit": 100000},
        headers=account.headers,
    )
    assert too_big.status_code == 422, too_big.text

    zero = await app_client.get(
        f"/api/workspaces/{account.workspace_id}/analyses",
        params={"limit": 0},
        headers=account.headers,
    )
    assert zero.status_code == 422, zero.text


async def test_reading_analyses_writes_nothing(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """读分析列表**不产生任何写入** —— 不重新生成、不动正文、不碰计划。

    过期的分析只标不改:要拿到基于最新内容的判断,得显式发起一次重新分析。
    这个接口顺手"刷新一下"是很自然的诱惑,而那样一来用户永远看不到自己那条
    历史判断,也永远不知道该不该重新分析。
    """
    account = await make_account("analysis-readonly@example.com")
    focus = await _root_id(app_client, account)

    await _analyze(app_client, account, use_reasoner, focus=focus)
    await _patch(app_client, account, focus, description="改一下让它过期")

    async with SessionLocal() as session:
        before = len((await session.execute(select(NodeAnalysis.id))).scalars().all())

    for _ in range(3):
        listed = await _analyses(app_client, account, focusNodeId=focus)
        assert listed["analyses"][0]["freshness"] == "stale"

    async with SessionLocal() as session:
        after = len((await session.execute(select(NodeAnalysis.id))).scalars().all())
    assert after == before, "读一次分析列表就多出一条记录"


# ---------------------------------------------------------------------------------
# ⑥ 归属:别人的空间读不到
# ---------------------------------------------------------------------------------
async def test_b_cannot_read_a_analyses_through_a_workspace_id(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """B 拿 A 的空间 id 去读分析 -> 404(不是空列表)。

    空列表是**最坏的**那个答案:它看起来完全正常,而 B 会以为自己看的是一个
    "还没有分析过"的空间。分析是按 `workspace_id` 查的,而归属校验在
    `get_workspace_context` 的 SQL 里 —— 那个环节一旦被绕开,这里就会静默返回空。
    """
    account_a = await make_account("analysis-owner@example.com")
    account_b = await make_account("analysis-other@example.com")
    focus = await _root_id(app_client, account_a)
    await _analyze(app_client, account_a, use_reasoner, focus=focus)

    # 前提:A 自己读自己的一定是 200(反向断言成立,这条用例才有意义)。
    assert (
        await app_client.get(
            f"/api/workspaces/{account_a.workspace_id}/analyses", headers=account_a.headers
        )
    ).status_code == 200

    stolen = await app_client.get(
        f"/api/workspaces/{account_a.workspace_id}/analyses", headers=account_b.headers
    )
    assert stolen.status_code == 404, stolen.text
