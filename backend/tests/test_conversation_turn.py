"""一次对话轮次:两次提交、幂等、以及"不许把猜测存成事实"。

这一组里最重要的三条:

1. `test_user_message_survives_a_crashing_model` —— 模型炸了,用户那句话还在不在。
2. `test_retry_after_failure_actually_reruns_the_model` —— 重试按钮是不是真的有用。
3. `test_an_assumed_value_never_reaches_the_column` —— 模型自己猜的数字会不会被
   当成用户确认过的事实存下来。
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import func, select

from backend.agent.runtime.base import BriefClaim
from backend.db.models import Conversation, Message, PlanningBrief
from backend.db.models.enums import MessageRole
from backend.tests.conftest import ExplodingReasoner, FakeReasoner


async def _send(client, account, content: str, **extra):
    return await client.post(
        f"/api/workspaces/{account.workspace_id}/messages",
        json={"content": content, **extra},
        headers=account.headers,
    )


async def _count(db, model) -> int:
    return int(await db.scalar(select(func.count()).select_from(model)) or 0)


# ---------------------------------------------------------------------------------
# 空空间
# ---------------------------------------------------------------------------------
async def test_a_new_space_has_an_empty_conversation(
    app_client: httpx.AsyncClient, make_account, db
) -> None:
    """刚建的空间,对话列表是空的 —— 而且是 `[]`,不是 404。"""
    account = await make_account()

    response = await app_client.get(
        f"/api/workspaces/{account.workspace_id}/messages", headers=account.headers
    )
    assert response.status_code == 200, response.text
    assert response.json()["messages"] == []
    assert response.json()["id"] is None
    assert await _count(db, Message) == 0


async def test_reading_the_conversation_does_not_create_one(
    app_client: httpx.AsyncClient, make_account, db
) -> None:
    """**读接口不能有副作用。**

    如果 GET 顺手建了一条空对话,那么用户只要打开一次工作台,库里就多出一行
    他从没说过话的对话。这件事不会报错,只会让计数悄悄不对 —— 所以单独测一条。
    """
    account = await make_account()
    for _ in range(3):
        await app_client.get(
            f"/api/workspaces/{account.workspace_id}/messages", headers=account.headers
        )

    assert await _count(db, Conversation) == 0, "读接口把对话建出来了"


async def test_the_conversation_appears_only_after_the_first_message(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db
) -> None:
    account = await make_account()
    use_reasoner(FakeReasoner())

    await _send(app_client, account, "你好")

    assert await _count(db, Conversation) == 1
    assert await _count(db, Message) == 2  # 一问一答


# ---------------------------------------------------------------------------------
# 模型失败
# ---------------------------------------------------------------------------------
async def test_user_message_survives_a_crashing_model(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db
) -> None:
    """**模型崩了,用户说的话必须还在库里。**

    原来的顺序是"调模型 -> 成功才写库",模型一超时就什么都不剩:用户认真写的三行字
    连同他的时间一起消失了。现在用户消息先提交,再调模型。

    这里用的是一个**直接抛异常**的假模型。真实实现按契约不会抛,但"顺序写反了"
    这个 bug 只在抛异常的那条路上才会暴露成可观测的事实 —— 返回降级结果的假模型
    会让这个测试永远绿,从而什么都验证不了。
    """
    account = await make_account()
    exploding = use_reasoner(ExplodingReasoner())

    with pytest.raises(RuntimeError):
        await _send(app_client, account, "我今天学了 40 分钟递归")

    assert exploding.calls, "假模型没有被调用,这个测试什么都没验证到"

    stored = (
        await db.execute(select(Message).where(Message.role == MessageRole.USER))
    ).scalars().all()
    assert len(stored) == 1
    assert stored[0].content == "我今天学了 40 分钟递归"


async def test_retry_after_failure_actually_reruns_the_model(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db
) -> None:
    """失败后重试要真的重新调模型,而不是返回"这条已经存过了"。**"""
    account = await make_account()
    use_reasoner(ExplodingReasoner())

    with pytest.raises(RuntimeError):
        await _send(app_client, account, "每周 4 小时", clientMessageId="retry-1")

    fake = use_reasoner(FakeReasoner(reply="这次成功了。"))
    response = await _send(app_client, account, "每周 4 小时", clientMessageId="retry-1")

    assert response.status_code == 200, response.text
    assert response.json()["replayed"] is False
    assert fake.calls, "重试没有重新调用模型 —— 用户会永远卡在失败的那一轮"
    assert response.json()["reply"] == "这次成功了。"
    # 用户消息只有一条,没有因为重试变成两条。
    assert await _count(db, Message) == 2


# ---------------------------------------------------------------------------------
# 幂等
# ---------------------------------------------------------------------------------
async def test_resending_the_same_message_is_idempotent(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db
) -> None:
    """同一个 clientMessageId 再发一次,不产生第二条消息,也不重新调模型。"""
    account = await make_account()
    fake = use_reasoner(FakeReasoner(reply="第一次的回答。"))

    first = await _send(app_client, account, "帮我看看", clientMessageId="same-1")
    second = await _send(app_client, account, "帮我看看", clientMessageId="same-1")

    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["replayed"] is False
    assert second.json()["replayed"] is True
    assert second.json()["reply"] == "第一次的回答。"
    # 只调了一次模型 —— 重放不该花第二份钱,也不该让用户看到两个不同的回复。
    assert len(fake.calls) == 1
    assert await _count(db, Message) == 2


async def test_messages_without_a_client_id_just_append(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db
) -> None:
    """不带 clientMessageId 时不去重 —— 用户确实可以说两遍同一句话。"""
    account = await make_account()
    use_reasoner(FakeReasoner())

    await _send(app_client, account, "好的")
    await _send(app_client, account, "好的")

    assert await _count(db, Message) == 4


async def test_conversation_history_is_returned_in_order(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    account = await make_account()
    use_reasoner(FakeReasoner(reply="回应。"))

    await _send(app_client, account, "第一句")
    await _send(app_client, account, "第二句")

    response = await app_client.get(
        f"/api/workspaces/{account.workspace_id}/messages", headers=account.headers
    )
    messages = response.json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant"]
    assert [m["seq"] for m in messages] == [1, 2, 3, 4]
    assert messages[0]["content"] == "第一句"
    assert messages[2]["content"] == "第二句"


# ---------------------------------------------------------------------------------
# 消息窗口:取最近的那一批
# ---------------------------------------------------------------------------------
async def test_a_long_conversation_returns_the_newest_messages(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """**超出窗口时留下的必须是最近的话,不是最早的话。**

    这里原来取的是最旧的 100 条(`order_by(seq.asc()).limit(100)`)。后果是对话
    一超过 100 条,用户新说的话就再也不会出现在界面上 —— 他发一条,刷新之后它
    不见了,看起来像"我这条没发出去"。而它确实发出去了,也存下来了。

    这个测试只发到刚好越过窗口,断言三件事:收到的是最后 100 条、最旧的那条
    不在里面、`truncated` 为真。
    """
    account = await make_account()
    use_reasoner(FakeReasoner(reply="嗯。"))

    # 一轮 = 两条消息。发 51 轮 = 102 条,刚好越过 100 这条线。
    for index in range(51):
        await _send(app_client, account, f"第 {index + 1} 句")

    response = await app_client.get(
        f"/api/workspaces/{account.workspace_id}/messages", headers=account.headers
    )
    body = response.json()
    messages = body["messages"]

    assert len(messages) == 100
    assert body["truncated"] is True, "更早的消息被丢掉了,却没有告诉任何人"

    # 按时间正序,而且窗口的起点正好是被截掉的第 3 条。
    assert [m["seq"] for m in messages] == list(range(3, 103))
    # 最后一句在,第一句不在 —— 这就是整个改动的意义。
    assert messages[-2]["content"] == "第 51 句"
    assert all(m["content"] != "第 1 句" for m in messages)


async def test_a_short_conversation_is_not_flagged_as_truncated(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """没被截断时 `truncated` 必须是假。

    它一旦恒真,上面那条说明就变成了一句永远挂在那儿的废话,用户会开始忽略它 ——
    而它存在的前提恰恰是"它很少出现"。
    """
    account = await make_account()
    use_reasoner(FakeReasoner(reply="嗯。"))

    await _send(app_client, account, "只有这一句")

    response = await app_client.get(
        f"/api/workspaces/{account.workspace_id}/messages", headers=account.headers
    )
    assert response.json()["truncated"] is False


# ---------------------------------------------------------------------------------
# 简报:不许把猜测存成事实
# ---------------------------------------------------------------------------------
async def test_a_stated_value_reaches_the_column(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db
) -> None:
    account = await make_account()
    use_reasoner(
        FakeReasoner(
            claims=(
                BriefClaim("weekly_available_minutes", 240, "user_stated"),
                BriefClaim("goal", "做出一个能跑的小项目", "user_stated"),
            )
        )
    )

    response = await _send(app_client, account, "每周 4 小时,想做个小项目")

    brief = (await db.execute(select(PlanningBrief))).scalar_one()
    assert brief.weekly_available_minutes == 240
    assert brief.goal == "做出一个能跑的小项目"
    assert sorted(response.json()["changedFields"]) == ["goal", "weekly_available_minutes"]


async def test_an_assumed_value_never_reaches_the_column(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db
) -> None:
    """**这是整条链路里最重要的一条断言。**

    模型自己猜的"每周 5 小时"如果进了 `weekly_available_minutes` 这一列,
    用户看到的每一份周计划都会建立在一个他从未同意过的前提上,而且界面上
    看不出任何异常。所以它只能进审计表。

    `weekly_available_minutes` 在用户亲口说出一个数字之前,**恒为 NULL**。
    """
    account = await make_account()
    use_reasoner(
        FakeReasoner(
            claims=(
                BriefClaim("weekly_available_minutes", 300, "model_assumed"),
                BriefClaim("current_level", "大概有点基础", "model_assumed"),
            )
        )
    )

    response = await _send(app_client, account, "我想学 Python")

    brief = (await db.execute(select(PlanningBrief))).scalar_one()
    assert brief.weekly_available_minutes is None, "模型猜的时间进了排期字段"
    assert brief.current_level is None
    assert response.json()["changedFields"] == []

    # 但它必须留在审计里 —— 界面上要能显示"AI 是按每周 5 小时估的,点这里纠正"。
    audit = brief.assumptions["audit"]
    assert {entry["field"] for entry in audit} == {
        "weekly_available_minutes",
        "current_level",
    }
    assert all(entry["source"] == "model_assumed" for entry in audit)
    # 而且它仍然算"还没问到",下一轮还会问。
    assert "weekly_available_minutes" in response.json()["brief"]["missing"]


async def test_a_stated_value_wins_over_a_later_assumption(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db
) -> None:
    """用户说过的值,不会被模型后来的推断覆盖。"""
    account = await make_account()

    use_reasoner(FakeReasoner(claims=(BriefClaim("weekly_available_minutes", 600, "user_stated"),)))
    await _send(app_client, account, "每周 10 小时")

    use_reasoner(FakeReasoner(claims=(BriefClaim("weekly_available_minutes", 180, "model_assumed"),)))
    await _send(app_client, account, "嗯")

    brief = (await db.execute(select(PlanningBrief))).scalar_one()
    assert brief.weekly_available_minutes == 600


async def test_the_user_can_correct_a_stated_value(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db
) -> None:
    """用户改口"每周 10 小时 -> 4 小时",后续每一轮用的都必须是 4。"""
    account = await make_account()

    use_reasoner(FakeReasoner(claims=(BriefClaim("weekly_available_minutes", 600, "user_stated"),)))
    await _send(app_client, account, "每周 10 小时")

    use_reasoner(FakeReasoner(claims=(BriefClaim("weekly_available_minutes", 240, "user_stated"),)))
    corrected = await _send(app_client, account, "说错了,每周只有 4 小时")

    assert corrected.json()["brief"]["weeklyAvailableMinutes"] == 240

    # 而且下一轮组装的上下文里用的就是 4 小时 —— 不是停留在旧值。
    later = use_reasoner(FakeReasoner())
    await _send(app_client, account, "那帮我看看")

    assert later.calls[0].known.weekly_available_minutes == 240


async def test_an_invalid_date_claim_is_dropped_not_stored(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db
) -> None:
    """模型把截止日期写成"三个月后"时,这一项丢掉,但回复照常给用户看。

    直接写进 `Date` 列在 SQLite 上可能被静默接受(SQLite 的列类型是建议性的),
    到 PostgreSQL 上才炸 —— 所以在 agent 层就按类型拒掉。
    """
    from backend.agent.runtime.response import parse_claims

    claims = parse_claims(
        {
            "deadline": {"value": "三个月后", "source": "user_stated"},
            "goal": {"value": "学完基础语法", "source": "user_stated"},
        }
    )
    assert [c.field for c in claims] == ["goal"]
