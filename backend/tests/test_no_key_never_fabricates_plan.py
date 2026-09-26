"""模型不可用时,系统绝不编造计划。

## 这条测试回归的是一个真实存在过的行为

原来的 `backend/agent/agents/main.py`:httpx 抛异常或 JSON 解析失败之后,静默落到一套
中文关键词规则上,凭"考研""英语"这类词**凭空造出节点**,回复的语气和真模型几乎一样。
用户没有任何线索能分辨刚才那段是规则产物。

这不是"功能没做完",是"做完了而且看起来像真的"—— 后者危险得多,因为用户会照着它
安排自己的时间。

所以这一组断言的核心只有一句:**模型不在时,计划一个节点都不许多。**

注意这里**不用假模型**:要验的正是那个真实兜底实现(`RuleFallbackReasoner`)的行为。
用假模型测就等于把被测对象换掉了。
"""

from __future__ import annotations

import httpx
from sqlalchemy import func, select

from backend.agent.runtime import RuleFallbackReasoner, build_reasoner
from backend.core.config import settings
from backend.db.models import PlanningBrief, PlanNode
from backend.tests.conftest import snapshot


async def _count(db, model) -> int:
    return int(await db.scalar(select(func.count()).select_from(model)) or 0)


async def _send(client, account, content: str):
    return await client.post(
        f"/api/workspaces/{account.workspace_id}/messages",
        json={"content": content},
        headers=account.headers,
    )


def test_the_factory_falls_back_to_rules_without_a_key(monkeypatch) -> None:
    monkeypatch.setattr(settings, "llm_api_key", "")
    monkeypatch.setattr(settings, "agent_reasoner", "auto")
    assert isinstance(build_reasoner(settings), RuleFallbackReasoner)


async def test_a_goal_without_a_model_creates_no_plan(
    app_client: httpx.AsyncClient, make_account, db
) -> None:
    """用户说出一个目标,模型不可用 —— 计划必须原封不动。"""
    account = await make_account()
    before = await snapshot(db)

    response = await _send(app_client, account, "我想三个月内学会 Python 并做出一个项目")

    assert response.status_code == 200, response.text
    body = response.json()

    # 1. 计划没有任何变化。**这是这条测试存在的全部理由。**
    nodes_before = await _count(db, PlanNode)
    assert nodes_before == 1, f"模型不可用时凭空多出了节点,现在有 {nodes_before} 个"

    # 2. 降级必须写在响应里,而且要能被界面显示出来。
    assert body["degraded"] is True
    assert body["source"] == "rule_fallback"
    assert body["degradedReason"] == "NO_API_KEY"
    assert body["retryable"] is True, "模型恢复后重试应该有用"

    # 3. 回复里不能出现"已经为你规划好了"这类声称。
    reply = body["reply"]
    for claim in ("已生成", "已经为", "计划如下", "为你安排"):
        assert claim not in reply, f"模型不在时声称做了事:{reply!r}"

    after = await snapshot(db)
    # 只多了用户消息和助手消息两行,别的表一行都没动。
    assert after["messages"] - before["messages"] == 2
    for table in ("plan_nodes", "dependencies", "proposals", "scheduled_sessions", "plan_revisions"):
        assert after[table] == before[table], f"{table} 被凭空写入了"


async def test_the_fallback_asks_about_what_is_actually_missing(
    app_client: httpx.AsyncClient, make_account
) -> None:
    """兜底问的问题来自 `KnownConditions.missing` 的确定性推导,不是关键词匹配。

    所以"问得对不对"是可断言的:没有截止日期就问截止日期,问过之后就不再问。
    """
    account = await make_account()

    first = await _send(app_client, account, "我想学 Python")
    reply = first.json()["reply"]
    body = first.json()

    # 新空间三样都缺,但一次只问前两样。
    assert body["brief"]["missing"] == [
        "deadline",
        "weekly_available_minutes",
        "current_level",
    ], "缺什么算错了"
    assert "什么时候" in reply, f"没有问截止时间:{reply!r}"
    assert "每周" in reply, f"没有问每周可投入时长:{reply!r}"
    # 第三样这一轮不问 —— 一次抛出三个问题就不是对话,是问卷。
    assert "水平" not in reply, f"一次问了太多:{reply!r}"

    # 而且问完一轮之后,缺失清单并没有凭空变短:兜底不写任何条件主张。
    assert body["brief"]["missing"] == first.json()["brief"]["missing"]


async def test_the_fallback_writes_no_brief_claims(
    app_client: httpx.AsyncClient, make_account, db
) -> None:
    """兜底不产生任何条件主张 —— 包括"用户想要什么"这种看起来最无害的。

    它没有能力判断哪句是用户说的、哪句是自己推的,所以它什么都不主张。
    """
    account = await make_account()

    await _send(app_client, account, "我想学 Python")

    brief = (await db.execute(select(PlanningBrief))).scalar_one_or_none()
    if brief is not None:
        assert not brief.goal
        assert brief.weekly_available_minutes is None
        assert brief.deadline is None


async def test_history_marks_which_replies_came_from_the_fallback(
    app_client: httpx.AsyncClient, make_account
) -> None:
    """往上翻历史时,要能看出哪几句是模型不在时留下的。

    这需要 `model_source` / `degraded` 落库 —— 只放在当次响应里的话,用户刷新一次
    就再也分不清了,而"分不清"正是原来那个缺陷的核心。
    """
    account = await make_account()
    await _send(app_client, account, "我想学 Python")

    response = await app_client.get(
        f"/api/workspaces/{account.workspace_id}/messages", headers=account.headers
    )
    assistant = [m for m in response.json()["messages"] if m["role"] == "assistant"]
    assert len(assistant) == 1
    assert assistant[0]["modelSource"] == "rule_fallback"
    assert assistant[0]["degraded"] is True
    assert assistant[0]["degradedReason"] == "NO_API_KEY"
