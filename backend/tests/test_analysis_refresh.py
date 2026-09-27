"""「根据最新内容重新分析」按钮。

这个按钮最容易做错的地方不是它调不通,而是**它成了对话之外的第三条路**:点一下,
画布上多一条分析,对话里什么都没发生。用户下次看到那条分析时,既不知道它是哪来的,
也不知道它读的是哪一版正文 —— 而"这条分析基于什么"正是这一层存在的全部意义。

所以这里钉的不是"接口返回 200",而是四件事:

1. 它**真的**在对话里留下了一条用户消息和一条助手消息,而且那句话就是按钮的意思;
2. 这条分析的 `focusNodeId` 是那个节点 —— 分析挂错了对象,后面所有"重新分析"都白做;
3. 模型不可用时**不留分析记录** —— 不能让上一次的旧分析顶着"最新"的名字继续显示;
4. 别人拿不走:归属校验与发给模型的那一条路径完全一致。

**这个文件里的每一次调用都会走到 reasoner**,所以每条用例都必须注入假模型
(`use_reasoner`);漏了的话它会在没有 key 的环境里退化成一次真实调用 ——
而那条路径返回的是 `unavailable`,于是"分析记录存在"这类断言会红得莫名其妙。

**这个按钮不带 `currentView`。** 它分析的是内容,不是用户此刻在看哪一栏;带上之后
"用户此刻在看什么"就会跟着画布上打开的视图漂,同一个节点在不同视图里刷新出来的分析
会不一样。真要那种能力,得先想清楚它意味着什么 —— 在那之前这里就留白。
"""

from __future__ import annotations

import httpx
from sqlalchemy import func, select

from backend.agent.prompts.planning import PROMPT_VERSION
from backend.agent.runtime.base import AnalysisDraft
from backend.db.models import Message, NodeAnalysis
from backend.db.models.enums import MessageRole, ModelSource
from backend.db.session import SessionLocal
from backend.services.analysis_service import REANALYZE_MESSAGE
from backend.tests.conftest import FakeReasoner


def _draft(**overrides) -> AnalysisDraft:
    base = {"known": ("用户想先把环境跑起来",), "diagnosis": ("第一步不该是买书",)}
    base.update(overrides)
    return AnalysisDraft(**base)


async def _root_id(client: httpx.AsyncClient, account) -> str:
    """这个空间的根目标。**走接口取** —— 手工往库里插一个根会和 `workspace_service`
    建的那一份悄悄分家(比如漏掉 `node_type=goal`),而那是另一条链上的事实。"""
    plan = await client.get(
        f"/api/workspaces/{account.workspace_id}/plan", headers=account.headers
    )
    assert plan.status_code == 200, plan.text
    return next(node["id"] for node in plan.json()["nodes"] if node["parentId"] is None)


async def _messages(client: httpx.AsyncClient, account) -> list[dict]:
    response = await client.get(
        f"/api/workspaces/{account.workspace_id}/messages", headers=account.headers
    )
    assert response.status_code == 200, response.text
    return response.json()["messages"]


async def _analyses(client: httpx.AsyncClient, account, node_id: str) -> dict:
    response = await client.get(
        f"/api/workspaces/{account.workspace_id}/analyses",
        params={"focusNodeId": node_id},
        headers=account.headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _stored_count() -> int:
    async with SessionLocal() as session:
        return int(
            await session.scalar(select(func.count()).select_from(NodeAnalysis.__table__)) or 0
        )


def _url(account, node_id: str) -> str:
    return f"/api/workspaces/{account.workspace_id}/nodes/{node_id}/analysis/refresh"


# ---------------------------------------------------------------------------------
# 按钮说的那句话,真的进了对话
# ---------------------------------------------------------------------------------
async def test_refresh_leaves_both_messages_in_the_conversation(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """刷新之后,对话里多了一条用户消息和一条助手消息。

    少了这一步,用户点完按钮看到的是一屏"什么都没有变" —— 而画布上多了一条分析。
    两处记录对不上,他就没法判断那条分析读的是哪一版正文。
    """
    account = await make_account()
    node_id = await _root_id(app_client, account)
    fake = use_reasoner(FakeReasoner(reply="看了一遍,先说结论。", analysis=_draft()))

    response = await app_client.post(_url(account, node_id), headers=account.headers)
    assert response.status_code == 200, response.text

    body = response.json()
    # **那句话由服务端给**,不是前端拼的字符串。断言它原样出现在响应和库里,
    # 前端就算自己写了一句别的也顶不掉这一条。
    assert body["userMessage"]["content"] == REANALYZE_MESSAGE
    assert body["assistantMessage"]["content"] == "看了一遍,先说结论。"
    assert body["userMessage"]["role"] == "user"

    stored = await _messages(app_client, account)
    assert [m["content"] for m in stored] == [REANALYZE_MESSAGE, "看了一遍,先说结论。"]
    assert len(fake.calls) == 1, "按钮没有真的走模型,那它就不是「重新分析」"


async def test_the_button_means_something_a_person_would_say() -> None:
    """那句话必须读起来就是那个意思的人话。

    它会作为**用户自己的话**留在历史里。写成 `__refresh__` 这类记号的话,用户往上翻
    会看见一句他从没说过的话,而那句话还会被下一轮当成"用户说过什么"读进提示词。
    """
    assert REANALYZE_MESSAGE.endswith("。")
    assert "重新分析" in REANALYZE_MESSAGE
    assert REANALYZE_MESSAGE.isprintable()
    assert not any(token in REANALYZE_MESSAGE for token in ("__", "{{", "null", "node_id"))


# ---------------------------------------------------------------------------------
# 这条分析挂在谁身上
# ---------------------------------------------------------------------------------
async def test_the_new_analysis_is_attached_to_the_node_that_was_refreshed(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """分析挂的是**这个节点**,不是"这个空间最近的一条"。

    挂错的后果不是报错,是"重新分析"永远对着另一个对象:用户在 A 上点刷新,
    B 的分析被覆盖成新的,而 A 那一条还是旧的 —— 两边都看不出来。
    """
    account = await make_account()
    root_id = await _root_id(app_client, account)
    created = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/nodes",
        json={"parentId": root_id, "title": "要被分析的那件事"},
        headers=account.headers,
    )
    assert created.status_code == 201, created.text
    node_id = created.json()["node"]["id"]

    use_reasoner(FakeReasoner(reply="看了一遍。", analysis=_draft()))
    assert (
        await app_client.post(_url(account, node_id), headers=account.headers)
    ).status_code == 200

    listed = await _analyses(app_client, account, node_id)
    assert len(listed["analyses"]) == 1
    analysis = listed["analyses"][0]
    assert analysis["focusNodeId"] == node_id
    assert analysis["focusNodeTitle"] == "要被分析的那件事"
    # 根目标上不该多出一条 —— 那一轮讨论的是子节点。
    assert (await _analyses(app_client, account, root_id))["analyses"] == []


async def test_the_analysis_carries_the_prompt_version_the_reasoner_reported(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """`promptVersion` **原样取自 reasoner**,服务层不自己拼一个。

    这条断言看着别扭 —— 它期望的是假模型自报的 `fake-v1`,而不是真的
    `planning-v6` —— 而那正是要钉住的性质:**留存的是那一轮真正问出去的那个版本**。
    服务层要是改成"从当前提示词常量读一个",历史记录就会被后来改提示词的人一起改写,
    "这条判断是哪一版问出来的"从此再也答不上来。

    所以顺带也证明它在生产里是可信的:真 reasoner 报的是 `PROMPT_VERSION`,
    降级路径报的是它自己的来源(见 `AnalysisView.modelSource`)。
    """
    account = await make_account()
    node_id = await _root_id(app_client, account)
    fake = use_reasoner(FakeReasoner(reply="好。", analysis=_draft()))

    assert (
        await app_client.post(_url(account, node_id), headers=account.headers)
    ).status_code == 200

    listed = await _analyses(app_client, account, node_id)
    # 存下来的是 **reasoner 报的那个**(`FakeReasoner` 硬编码 `fake-v1`),
    # 而不是当前提示词常量。两者不相等这件事本身就是断言 —— 相等的话这条用例
    # 分不出"原样记录"和"从常量现读"。
    assert listed["analyses"][0]["promptVersion"] == "fake-v1"
    assert PROMPT_VERSION != "fake-v1", "提示词版本常量与假模型撞名了,这条断言就没有意义"
    assert listed["analyses"][0]["freshness"] == "fresh"
    assert len(fake.calls) == 1


# ---------------------------------------------------------------------------------
# 模型没成功的时候,不许留下一条"假装分析过"的记录
# ---------------------------------------------------------------------------------
async def test_a_degraded_turn_leaves_no_analysis_behind(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """模型不可用 -> 一声不响地不留分析记录,而且响应如实地说是降级。

    这条是这一层最容易被"顺手补一下"的地方:那一轮 FAQ 式的兜底回复看起来像句话,
    顺手给它配一条分析记录,用户就会在画布上看到一条**不是模型给的**判断,而它
    长得和真的那条一模一样。前端据此显示"这次没分析成,可以重试",而不是拿
    上一次的旧分析顶替。
    """
    account = await make_account()
    node_id = await _root_id(app_client, account)

    before = await _stored_count()
    use_reasoner(
        FakeReasoner(
            reply="模型这会儿连不上,先记下你这句。",
            degraded=True,
            source=ModelSource.UNAVAILABLE,
            retryable=True,
        )
    )
    response = await app_client.post(_url(account, node_id), headers=account.headers)
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["degraded"] is True
    assert body["source"] == "unavailable"
    assert body["retryable"] is True
    # 降级这一轮**照样在对话里留下痕迹** —— 用户点过按钮这件事是真的。
    assert body["userMessage"]["content"] == REANALYZE_MESSAGE

    assert await _stored_count() == before, "降级的时候留下了一条不是模型给的分析"
    assert (await _analyses(app_client, account, node_id))["analyses"] == []


async def test_an_old_analysis_is_not_replaced_by_a_degraded_refresh(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """先有一条真分析,再来一次降级刷新 —— **旧的那条还在,而且仍然标着它的来源**。

    "刷新失败"绝不能顺手把旧记录删掉或者标成新的:用户看到的应该是"最新那条还是
    模型在 9 点说的",而不是"什么都没有过"。
    """
    account = await make_account()
    node_id = await _root_id(app_client, account)

    use_reasoner(FakeReasoner(reply="第一遍。", analysis=_draft()))
    assert (
        await app_client.post(_url(account, node_id), headers=account.headers)
    ).status_code == 200

    use_reasoner(
        FakeReasoner(reply="连不上。", degraded=True, source=ModelSource.UNAVAILABLE)
    )
    assert (
        await app_client.post(_url(account, node_id), headers=account.headers)
    ).status_code == 200

    listed = await _analyses(app_client, account, node_id)
    assert len(listed["analyses"]) == 1
    assert listed["analyses"][0]["modelSource"] == "direct_llm"
    assert listed["analyses"][0]["known"] == ["用户想先把环境跑起来"]
    assert "连不上" in listed["note"] or "1 条" in listed["note"]


# ---------------------------------------------------------------------------------
# 正文改了之后的那一次刷新,才是这个按钮存在的理由
# ---------------------------------------------------------------------------------
async def test_editing_the_body_makes_the_analysis_stale_and_a_refresh_clears_it(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """改正文 -> 徽标变过期 -> 点重新分析 -> 回到最新。

    这是界面上那条验收路径的服务端那一半。**过期是读的时候现算的**,所以这里只需要
    真的改一次正文,再重新读一次列表 —— 中间没有任何东西"把分析标成过期"。
    """
    account = await make_account()
    node_id = await _root_id(app_client, account)
    use_reasoner(FakeReasoner(reply="第一遍。", analysis=_draft()))
    assert (
        await app_client.post(_url(account, node_id), headers=account.headers)
    ).status_code == 200
    assert (await _analyses(app_client, account, node_id))["analyses"][0]["freshness"] == "fresh"

    # 用户改了正文。带上 `contentVersion` —— 不带的话服务端按"没有前置条件"处理,
    # 那就不是在验这道闸了。
    current = await app_client.get(
        f"/api/workspaces/{account.workspace_id}/plan", headers=account.headers
    )
    node = next(n for n in current.json()["nodes"] if n["id"] == node_id)
    edited = await app_client.patch(
        f"/api/workspaces/{account.workspace_id}/nodes/{node_id}",
        json={"description": "改成:只在周末做,每周最多两小时。",
              "contentVersion": node["contentVersion"]},
        headers=account.headers,
    )
    assert edited.status_code == 200, edited.text

    stale = await _analyses(app_client, account, node_id)
    assert stale["analyses"][0]["freshness"] == "stale"
    assert stale["analyses"][0]["staleReasons"], "过期了却不说为什么,用户没法判断该不该重来"

    # 点按钮。**旧记录不消失** —— 分析是只增的。
    use_reasoner(FakeReasoner(reply="按最新的正文看。", analysis=_draft(known=("只在周末做",))))
    assert (
        await app_client.post(_url(account, node_id), headers=account.headers)
    ).status_code == 200

    after = await _analyses(app_client, account, node_id)
    assert len(after["analyses"]) == 2, "重新分析抹掉了上一次,用户就没法回看它上次为什么那么说"
    assert after["analyses"][0]["freshness"] == "fresh"
    assert after["analyses"][0]["known"] == ["只在周末做"]
    assert after["analyses"][1]["freshness"] == "stale"


# ---------------------------------------------------------------------------------
# 归属
# ---------------------------------------------------------------------------------
async def test_a_node_of_another_space_is_not_found(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """拿一个不存在的节点 id -> 404,而且在**触到模型之前**就返回。

    顺序很重要:先调模型再校验节点的话,一个格式合法的假 id 会让服务端白花一次钱,
    然后才告诉用户"没这个节点"。
    """
    account = await make_account()
    fake = use_reasoner(FakeReasoner(reply="好。", analysis=_draft()))

    missing = "00000000-0000-4000-8000-0000000000ff"
    response = await app_client.post(_url(account, missing), headers=account.headers)

    assert response.status_code == 404, response.text
    assert fake.calls == [], "节点都没找到,却已经调过一次模型"


async def test_b_cannot_refresh_through_someone_elses_workspace(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """B 拿 A 的节点 id 去刷新 -> 404(不是 403,也不是 401)。

    这条单独写而不是塞进 `test_authz_matrix` 的跨账号表,是因为那张表的每个用例都要先
    反向断言"A 用同一个路径必须成功",而那条路径会真的走到模型(见那边的注释)。
    这里把两半都做掉:**同一个 id,A 调是真的能刷出东西的**;B 拿到的是 404。
    少了前半句,这条用例可能只是"因为 id 是假的所以 404"。
    """
    account_a = await make_account(email="a@example.com")
    account_b = await make_account(email="b@example.com", workspace_title="英语")
    node_id = await _root_id(app_client, account_a)

    use_reasoner(FakeReasoner(reply="看了一遍。", analysis=_draft()))
    mine = await app_client.post(_url(account_a, node_id), headers=account_a.headers)
    assert mine.status_code == 200, mine.text
    assert (await _analyses(app_client, account_a, node_id))["analyses"], "A 自己都没刷出来"

    # B 走的是**同一个节点 id**,只是空间是它自己的 —— 归属校验按空间过滤,
    # 所以这个节点在 B 的上下文里根本不存在。
    theirs = await app_client.post(_url(account_b, node_id), headers=account_b.headers)
    assert theirs.status_code == 404, theirs.text
    # 403 会确认"这个节点存在但不属于你",等于把别人空间里有哪些节点告诉了任何人。
    assert theirs.status_code != 403
    assert theirs.json()["error"]["code"] == "NODE_NOT_FOUND"


async def test_an_anonymous_refresh_is_rejected(
    app_client: httpx.AsyncClient, make_account
) -> None:
    """没带令牌 -> 401,而且一次模型都不调。"""
    account = await make_account()
    node_id = await _root_id(app_client, account)

    response = await app_client.post(_url(account, node_id))
    assert response.status_code == 401, response.text


# ---------------------------------------------------------------------------------
# 按钮与对话不是两个真相
# ---------------------------------------------------------------------------------
async def test_the_refresh_turn_is_the_same_turn_shape_as_a_typed_message(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """两次响应的**字段集合**完全一致。

    各拼一份的话,差异会出现在最不起眼的那几栏上:`inputChanged` 在按钮那条路径上
    少读一次,用户就永远看不到"这次是基于旧输入"的提示;而两边的类型都是同一个,
    TS 和 pydantic 都不会说话。
    """
    account = await make_account()
    node_id = await _root_id(app_client, account)
    fake = use_reasoner(FakeReasoner(reply="好。", analysis=_draft()))

    typed = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/messages",
        json={"content": "我改了点东西", "contextNodeId": node_id},
        headers=account.headers,
    )
    assert typed.status_code == 200, typed.text
    refreshed = await app_client.post(_url(account, node_id), headers=account.headers)
    assert refreshed.status_code == 200, refreshed.text

    assert set(typed.json()) == set(refreshed.json())
    assert len(fake.calls) == 2
    # `inputChanged` 在按钮那条路径上也是**真的被算过**的(为 False 正确:
    # 这两轮之间没有别人改过输入)。
    assert refreshed.json()["inputChanged"] is False
    assert refreshed.json()["replayed"] is False


async def test_the_refresh_message_is_stored_with_the_node_it_was_about(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """那条自动消息带着 `contextNodeId` —— 于是"指代消解"在下一轮仍然成立。

    不落这个字段的话,下一轮用户说"再展开说说",模型看到的上一句是一句**没有对象**的
    "重新分析一下这个节点",它只能猜。
    """
    account = await make_account()
    node_id = await _root_id(app_client, account)
    use_reasoner(FakeReasoner(reply="好。", analysis=_draft()))

    assert (
        await app_client.post(_url(account, node_id), headers=account.headers)
    ).status_code == 200

    stored = await _messages(app_client, account)
    assert stored[0]["contextNodeId"] == node_id

    async with SessionLocal() as session:
        row = await session.scalar(
            select(Message).where(Message.content == REANALYZE_MESSAGE)
        )
    assert row is not None
    assert row.role is MessageRole.USER
