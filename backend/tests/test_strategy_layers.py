"""战略层与分层规划。

覆盖任务 F 的后端部分:

1. 存量节点 `planningLevel` 为 null,plan / 排期 / 提案不回归;
2. 手工与 AI 都能写合法层级;确认前不变、确认后持久化;
3. information 节点带层级被拒;
4. 非法父子层级被拒;strategy → phase → week 允许;
5. 没有已确认 strategy 时,月/周/日提案被拒;确认 strategy 后允许下一层;
6. 一次失败不触发战略复评;
7. 长期条件变化 / 连续失败只产生 strategy-review Question Node,不自动写战略;
8. 回答复评问题后的战略变更仍以 proposal 形式出现并要用户确认。
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agent.runtime.base import BriefClaim
from backend.agent.runtime.scripted import ScriptedReasoner
from backend.db.base import utcnow
from backend.db.models import ExecutionRecord, ScheduledSession
from backend.db.models.enums import ExecutionResult, ScheduledSessionOrigin, ScheduledSessionStatus
from backend.services import strategy_review
from backend.services.timeutil import today_in
from backend.tests.conftest import FakeReasoner


# ---------------------------------------------------------------------------------
# 助手
# ---------------------------------------------------------------------------------
async def _plan(client: httpx.AsyncClient, account) -> dict:
    response = await client.get(
        f"/api/workspaces/{account.workspace_id}/plan", headers=account.headers
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _root_id(client: httpx.AsyncClient, account) -> str:
    return next(
        node["id"] for node in (await _plan(client, account))["nodes"] if node["parentId"] is None
    )


async def _create(
    client: httpx.AsyncClient,
    account,
    *,
    parent_id: str,
    title: str,
    planning_level: str | None = None,
    purpose: str | None = None,
    node_type: str = "capability",
    estimate_minutes: int | None = None,
) -> httpx.Response:
    body: dict = {"parentId": parent_id, "title": title, "nodeType": node_type}
    if planning_level is not None:
        body["planningLevel"] = planning_level
    if purpose is not None:
        body["purpose"] = purpose
    if estimate_minutes is not None:
        body["estimateMinutes"] = estimate_minutes
    return await client.post(
        f"/api/workspaces/{account.workspace_id}/nodes", json=body, headers=account.headers
    )


async def _questions(client: httpx.AsyncClient, account) -> list[dict]:
    response = await client.get(
        f"/api/workspaces/{account.workspace_id}/questions", headers=account.headers
    )
    assert response.status_code == 200, response.text
    return response.json()["questions"]


async def _send(client: httpx.AsyncClient, account, content: str, message_id: str) -> dict:
    response = await client.post(
        f"/api/workspaces/{account.workspace_id}/messages",
        json={"content": content, "clientMessageId": message_id},
        headers=account.headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _confirm(client: httpx.AsyncClient, account, proposal_id: str, key: str) -> httpx.Response:
    return await client.post(
        f"/api/workspaces/{account.workspace_id}/proposals/{proposal_id}/confirm",
        json={"idempotencyKey": key},
        headers=account.headers,
    )


async def _confirmed_strategy(client: httpx.AsyncClient, account, title: str = "战略:先英语") -> dict:
    response = await _create(
        client, account, parent_id=await _root_id(client, account),
        title=title, planning_level="strategy", node_type="goal",
    )
    assert response.status_code == 201, response.text
    return response.json()["node"]


# ---------------------------------------------------------------------------------
# 1. 存量兼容:planningLevel 为 null 不回归
# ---------------------------------------------------------------------------------
async def test_legacy_nodes_have_null_level_and_still_work(
    app_client: httpx.AsyncClient, make_account
) -> None:
    account = await make_account()
    root = await _root_id(app_client, account)
    created = await _create(
        app_client, account, parent_id=root, title="写文献综述", node_type="task", estimate_minutes=120
    )
    assert created.status_code == 201, created.text
    assert created.json()["node"]["planningLevel"] is None

    plan = await _plan(app_client, account)
    node = next(item for item in plan["nodes"] if item["title"] == "写文献综述")
    assert node["planningLevel"] is None

    preview = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/schedule/preview", headers=account.headers
    )
    assert preview.status_code == 200, preview.text


# ---------------------------------------------------------------------------------
# 2. 手工与 AI 都能写合法层级
# ---------------------------------------------------------------------------------
async def test_manual_create_can_set_a_planning_level(
    app_client: httpx.AsyncClient, make_account
) -> None:
    account = await make_account()
    created = await _create(
        app_client, account, parent_id=await _root_id(app_client, account),
        title="战略:先英语", planning_level="strategy", node_type="goal",
    )
    assert created.status_code == 201, created.text
    assert created.json()["node"]["planningLevel"] == "strategy"


async def test_invalid_manual_planning_level_is_rejected(
    app_client: httpx.AsyncClient, make_account
) -> None:
    account = await make_account()
    refused = await _create(
        app_client, account, parent_id=await _root_id(app_client, account),
        title="随便", planning_level="quarter",
    )
    assert refused.status_code == 400, refused.text
    assert refused.json()["error"]["code"] == "INVALID_INPUT"


async def test_ai_strategy_is_persisted_only_after_confirmation(
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
                    "title": "战略:先英语",
                    "nodeType": "goal",
                    "planningLevel": "strategy",
                    "description": "优先:英语阅读;暂缓:数学建模;依据:目标需要阅读量。",
                },
            )
        )
    )
    body = await _send(app_client, account, "帮我定个方向", "strategy-msg")
    assert body["proposalErrors"] == [], body["proposalErrors"]
    proposal = body["proposal"]
    assert proposal is not None
    assert any("战略" in item["summary"] for item in proposal["items"]), proposal["items"]

    plan_before = await _plan(app_client, account)
    assert not any(n["planningLevel"] == "strategy" for n in plan_before["nodes"])

    confirmed = await _confirm(app_client, account, proposal["id"], "strategy-key")
    assert confirmed.status_code == 200, confirmed.text
    plan = await _plan(app_client, account)
    assert any(n["planningLevel"] == "strategy" for n in plan["nodes"])


# ---------------------------------------------------------------------------------
# 3. information 节点不能带层级
# ---------------------------------------------------------------------------------
async def test_information_node_cannot_carry_a_planning_level(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    account = await make_account()
    refused = await _create(
        app_client, account, parent_id=await _root_id(app_client, account),
        title="我排名 38", purpose="information", planning_level="strategy",
    )
    assert refused.status_code == 400, refused.text
    assert refused.json()["error"]["code"] == "INVALID_INPUT"

    use_reasoner(
        FakeReasoner(
            actions=(
                {
                    "op": "create_node",
                    "localId": "n2",
                    "parentRef": "n1",
                    "title": "我排名 38",
                    "nodeType": "capability",
                    "purpose": "information",
                    "planningLevel": "phase",
                },
            )
        )
    )
    body = await _send(app_client, account, "记一下", "info-level-msg")
    assert body["proposal"] is None
    assert "INFORMATION_NODE_MUST_NOT_BE_SCHEDULABLE" in {
        error["code"] for error in body["proposalErrors"]
    }


# ---------------------------------------------------------------------------------
# 4. 父子层级:从粗到细;允许跳级
# ---------------------------------------------------------------------------------
async def test_levels_must_go_from_coarse_to_fine(
    app_client: httpx.AsyncClient, make_account
) -> None:
    account = await make_account()
    strategy = await _confirmed_strategy(app_client, account)
    phase = await _create(
        app_client, account, parent_id=strategy["id"], title="阶段一", planning_level="phase"
    )
    assert phase.status_code == 201, phase.text
    week = await _create(
        app_client, account, parent_id=phase.json()["node"]["id"], title="第一周", planning_level="week"
    )
    assert week.status_code == 201, week.text

    reversed_level = await _create(
        app_client, account, parent_id=week.json()["node"]["id"],
        title="错误的反向战略", planning_level="strategy",
    )
    assert reversed_level.status_code == 400, reversed_level.text
    assert reversed_level.json()["error"]["code"] == "INVALID_INPUT"

    skipped = await _create(
        app_client, account, parent_id=strategy["id"], title="直接到周", planning_level="week"
    )
    assert skipped.status_code == 201, skipped.text


# ---------------------------------------------------------------------------------
# 5. 没有已确认战略时,不能下钻
# ---------------------------------------------------------------------------------
async def test_week_proposal_is_refused_until_a_strategy_is_confirmed(
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
                    "title": "第一周:阅读",
                    "planningLevel": "week",
                },
            )
        )
    )
    body = await _send(app_client, account, "帮我排一周", "week-before-strategy")
    assert body["proposal"] is None
    assert "STRATEGY_NOT_CONFIRMED" in {error["code"] for error in body["proposalErrors"]}

    # 确认一个战略之后,同样的周提案挂在战略下就被允许。
    strategy = await _confirmed_strategy(app_client, account)
    use_reasoner(
        FakeReasoner(
            actions=(
                {
                    "op": "create_node",
                    "localId": "n3",
                    "parentRef": "n2",
                    "title": "战略下的第一周",
                    "planningLevel": "week",
                },
            )
        )
    )
    ok = await _send(app_client, account, "现在挂在战略下", "week-after-strategy")
    assert ok["proposalErrors"] == [], ok["proposalErrors"]
    assert ok["proposal"] is not None
    assert strategy["title"] == "战略:先英语"


# ---------------------------------------------------------------------------------
# 6. 一次失败不触发战略复评
# ---------------------------------------------------------------------------------
async def test_a_single_setback_does_not_trigger_a_strategy_review() -> None:
    assert strategy_review.long_term_change_reason(("status", "estimate_minutes")) is None
    assert strategy_review.CONSECUTIVE_SETBACKS >= 2


# ---------------------------------------------------------------------------------
# 7. 长期条件变化 / 连续失败 -> 只产生复评问题
# ---------------------------------------------------------------------------------
async def test_a_long_term_change_creates_a_review_question_not_a_strategy_edit(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    account = await make_account()
    await _confirmed_strategy(app_client, account)
    before = await _plan(app_client, account)

    use_reasoner(
        FakeReasoner(
            claims=(BriefClaim(field="goal", value="改成先攻数学", source="user_stated"),),
            actions=(),
        )
    )
    await _send(app_client, account, "我改主意了,先把数学补上来", "goal-change-msg")

    questions = await _questions(app_client, account)
    assert any(q["question"] == strategy_review.REVIEW_QUESTION for q in questions), questions
    review = next(q for q in questions if q["question"] == strategy_review.REVIEW_QUESTION)
    assert strategy_review.REVIEW_WHY_PREFIX in review["whyNow"]

    after = await _plan(app_client, account)
    assert [(n["id"], n["planningLevel"]) for n in after["nodes"]] == [
        (n["id"], n["planningLevel"]) for n in before["nodes"]
    ]


async def test_consecutive_setbacks_create_a_review_question_via_replan(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    account = await make_account()
    strategy = await _confirmed_strategy(app_client, account)
    root = await _root_id(app_client, account)
    task = await _create(app_client, account, parent_id=root, title="每天读一篇", node_type="task")
    assert task.status_code == 201, task.text
    node_id = uuid.UUID(task.json()["node"]["id"])
    today = today_in("Asia/Shanghai")

    for index in range(strategy_review.CONSECUTIVE_SETBACKS):
        session = ScheduledSession(
            user_id=uuid.UUID(account.id),
            workspace_id=uuid.UUID(account.workspace_id),
            node_id=node_id,
            scheduled_date=today - timedelta(days=index),
            planned_minutes=60,
            status=ScheduledSessionStatus.PLANNED,
            origin=ScheduledSessionOrigin.SCHEDULER,
        )
        db.add(session)
        await db.flush()
        db.add(
            ExecutionRecord(
                user_id=uuid.UUID(account.id),
                workspace_id=uuid.UUID(account.workspace_id),
                session_id=session.id,
                node_id=node_id,
                result=ExecutionResult.SKIPPED,
                actual_minutes=None,
                created_at=utcnow(),
            )
        )
    await db.commit()

    use_reasoner(FakeReasoner(actions=()))
    replan = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/replan", headers=account.headers
    )
    assert replan.status_code == 200, replan.text

    questions = await _questions(app_client, account)
    assert any(q["question"] == strategy_review.REVIEW_QUESTION for q in questions), questions
    plan = await _plan(app_client, account)
    stored = next(n for n in plan["nodes"] if n["id"] == strategy["id"])
    assert stored["planningLevel"] == "strategy"
    assert stored["title"] == strategy["title"]


# ---------------------------------------------------------------------------------
# 8. 回答复评问题后的战略变更仍要确认
# ---------------------------------------------------------------------------------
async def test_answering_the_review_question_still_requires_proposal_confirmation(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    account = await make_account()
    strategy = await _confirmed_strategy(app_client, account)
    root = await _root_id(app_client, account)
    task = await _create(app_client, account, parent_id=root, title="每天读一篇", node_type="task")
    node_id = uuid.UUID(task.json()["node"]["id"])
    today = today_in("Asia/Shanghai")
    for index in range(strategy_review.CONSECUTIVE_SETBACKS):
        session = ScheduledSession(
            user_id=uuid.UUID(account.id),
            workspace_id=uuid.UUID(account.workspace_id),
            node_id=node_id,
            scheduled_date=today - timedelta(days=index),
            planned_minutes=60,
            status=ScheduledSessionStatus.PLANNED,
            origin=ScheduledSessionOrigin.SCHEDULER,
        )
        db.add(session)
        await db.flush()
        db.add(
            ExecutionRecord(
                user_id=uuid.UUID(account.id),
                workspace_id=uuid.UUID(account.workspace_id),
                session_id=session.id,
                node_id=node_id,
                result=ExecutionResult.SKIPPED,
                actual_minutes=None,
                created_at=utcnow(),
            )
        )
    await db.commit()

    use_reasoner(
        ScriptedReasoner.from_env(
            json.dumps(
                {
                    "turns": [
                        # 第一轮:普通消息,服务端据连续失败创建复评问题;模型不提变更。
                        {"reply": "记下了,我看到最近有几场没跟上。", "actions": []},
                        # 第二轮(回答之后):模型才提一条修改战略的 proposal。
                        {
                            "reply": "那我把战略重点改一下。",
                            "actions": [
                                {"op": "update_node", "targetRef": "n2", "title": "战略:先数学"}
                            ],
                        },
                    ]
                }
            )
        )
    )
    await _send(app_client, account, "最近有点跟不上", "review-trigger-msg")
    questions = await _questions(app_client, account)
    review = next((q for q in questions if q["question"] == strategy_review.REVIEW_QUESTION), None)
    assert review is not None, questions

    answered = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/questions/{review['id']}/answer",
        json={"selectedOptionIds": ["review"], "clientAnswerId": "review-answer-key"},
        headers=account.headers,
    )
    assert answered.status_code == 200, answered.text
    turn = answered.json()["turn"]
    assert turn is not None and turn["proposal"] is not None, turn
    proposal_id = turn["proposal"]["id"]

    plan = await _plan(app_client, account)
    assert next(n for n in plan["nodes"] if n["id"] == strategy["id"])["title"] == strategy["title"]

    confirmed = await _confirm(app_client, account, proposal_id, "review-confirm-key")
    assert confirmed.status_code == 200, confirmed.text
    plan = await _plan(app_client, account)
    assert next(n for n in plan["nodes"] if n["id"] == strategy["id"])["title"] == "战略:先数学"
