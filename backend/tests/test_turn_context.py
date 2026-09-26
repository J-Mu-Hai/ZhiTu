"""模型到底看见了什么。

## 这些断言针对的是一个具体的、已经发生过的缺陷

原来的前端 `send()` 只发四个字段(空间 id、当前节点 id、视图名、消息正文),后端拿不到
用户的计划,也拿不到历史。所以模型眼里的"这个空间"是空的,它只能凭用户这一句话现编。
用户说"把刚才那个阶段往后挪一周",它没有"刚才那个阶段"可指。

这一组测试不看拼出来的提示词字符串(那种断言会被一次措辞调整全部打碎),而是**直接断言
传给模型的 `TurnContext` 结构体**:里面有今天几号、有用户时区、有已确认的条件、有最近
几轮对话、有这个空间真实存在的节点。
"""

from __future__ import annotations

import uuid

import httpx

from backend.agent.runtime.base import BriefClaim
from backend.services.timeutil import today_in
from backend.tests.conftest import FakeReasoner


async def _send(client: httpx.AsyncClient, account, content: str, **extra):
    return await client.post(
        f"/api/workspaces/{account.workspace_id}/messages",
        json={"content": content, **extra},
        headers=account.headers,
    )


async def test_context_carries_todays_date_in_the_user_timezone(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """今天是几号,必须由**用户时区**决定,不能由 UTC 时间戳推导。

    UTC 的 2026-09-25T22:00 在东八区已经是 09-26。用 UTC 日期算"还剩几周",
    东八区用户在晚上八点以后看到的每一份周计划都会错一天。
    """
    account = await make_account()
    fake = use_reasoner(FakeReasoner())

    response = await _send(app_client, account, "我想学 Python")
    assert response.status_code == 200, response.text

    assert len(fake.calls) == 1, "假模型没有被调用 —— 依赖注入漏了"
    turn = fake.calls[0]
    assert turn.current_date == today_in("Asia/Shanghai").isoformat()
    assert turn.timezone == "Asia/Shanghai"
    assert turn.weekday in "一二三四五六日"


async def test_context_carries_the_real_plan_nodes(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """空间里真实存在的节点必须进上下文。

    这是"AI 读不到我的计划"那个缺陷的直接回归:建空间时有一个根目标,
    模型至少要看得见它。
    """
    account = await make_account(workspace_title="考研准备")
    fake = use_reasoner(FakeReasoner())

    await _send(app_client, account, "帮我看看")

    turn = fake.calls[0]
    assert turn.workspace_title == "考研准备"
    titles = [node.title for node in turn.nodes]
    assert titles, "模型看到的节点列表是空的 —— 它又只能在真空里编计划了"
    assert any(node.node_type == "goal" for node in turn.nodes)


async def test_context_carries_the_known_conditions(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """已经确认过的条件必须带进去,否则模型会把问过的问题再问一遍。"""
    account = await make_account()

    first = use_reasoner(
        FakeReasoner(
            claims=(BriefClaim("weekly_available_minutes", 240, "user_stated"),),
            reply="记下了。",
        )
    )
    await _send(app_client, account, "每周 4 小时")
    assert first.calls

    # 换一个假模型继续下一轮,检查上一条信息有没有传下去。
    second = use_reasoner(FakeReasoner())
    await _send(app_client, account, "然后呢")

    turn = second.calls[0]
    assert turn.known.weekly_available_minutes == 240
    assert "weekly_available_minutes" not in turn.known.missing


async def test_context_carries_recent_turns_but_not_the_current_one_twice(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """历史要带上,但**这一轮的用户消息不能既在历史里、又单独出现一次**。

    重复出现会让模型以为用户把同一句话说成了两遍,于是它也回两遍。
    """
    account = await make_account()
    fake = use_reasoner(FakeReasoner(reply="好的。"))

    await _send(app_client, account, "第一句")
    await _send(app_client, account, "第二句")

    turn = fake.calls[1]
    assert turn.user_message == "第二句"

    contents = [content for _, content in turn.history]
    assert "第一句" in contents, "上一轮的用户消息没有进历史"
    assert "好的。" in contents, "上一轮的助手回复没有进历史"
    assert contents.count("第二句") == 0, "这一轮的消息在历史里又出现了一次"


async def test_unknown_timezone_still_produces_a_date(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db
) -> None:
    """时区名认不出来时,不能因此让接口 500。

    用户只是想跟 AI 说句话,不该因为一个时区字符串拼错就打不开工作台。
    """
    from sqlalchemy import select

    from backend.db.models import Workspace

    account = await make_account()
    workspace = (
        await db.execute(select(Workspace).where(Workspace.id == uuid.UUID(account.workspace_id)))
    ).scalar_one()
    workspace.timezone = "Mars/Olympus"
    await db.commit()

    fake = use_reasoner(FakeReasoner())
    response = await _send(app_client, account, "在吗")

    assert response.status_code == 200, response.text
    assert fake.calls[0].current_date  # 有日期,没炸


async def test_the_current_view_reaches_the_model(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """用户在哪个视图,要能传下去 —— 这是指代消解的依据之一。"""
    account = await make_account()
    fake = use_reasoner(FakeReasoner())

    await _send(app_client, account, "这个阶段展开讲讲", currentView="path")

    assert fake.calls[0].current_view == "path"


async def test_the_highlighted_node_reaches_the_model(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db
) -> None:
    """用户正看着哪个节点,模型要知道它**叫什么**。

    注意传进去的是标题而不是 id:模型看不到 UUID,也就不可能产出一个指向别的空间
    节点的 id(见 runtime/base.py 里 TurnContext 的说明)。
    """
    from sqlalchemy import select

    from backend.agent.runtime.response import render_turn
    from backend.db.models import PlanNode

    account = await make_account(workspace_title="英语提升")
    root = (
        await db.execute(
            select(PlanNode).where(PlanNode.workspace_id == uuid.UUID(account.workspace_id))
        )
    ).scalar_one()

    fake = use_reasoner(FakeReasoner())
    await _send(app_client, account, "这个阶段展开讲讲", contextNodeId=str(root.id))

    turn = fake.calls[0]
    assert turn.context_node_title == root.title

    # 反过来也要成立:模型看不到任何真实主键。
    #
    # 断言的是**实际会被发出去的那段文本**,不是 `repr(turn)`。区别是实质性的:
    # TurnContext 里现在有 `node_handles` 这份"记号 -> 真实 id"的服务端映射(见
    # runtime/base.py),它本来就**不该**出现在提示词里,但它确实存在于结构体上。
    # 盯住 repr 会要求把这份映射藏到别处去,而问题从来不是"它存在",是"它有没有
    # 被渲染进去"。渲染函数是这一切的唯一出口,所以断言它。
    prompt = render_turn(turn)
    assert str(root.id) not in prompt
    # 同时确认记号确实送到了 —— 否则上面的否定断言在"节点根本没被渲染"时也会通过。
    assert "n1" in prompt
    assert root.title in prompt
