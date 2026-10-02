"""问题节点:创建、回答、跳过/稍后、以及回答之后的后续提案。

这一组走的是**真实 HTTP 链路**(`/messages`、`/questions`、`/questions/{id}/answer`),
脚本化/Fake reasoner 只替掉"谁来想出这句话",校验、状态机、事务写入都是产品代码。

覆盖:
1. 问题被持久化,重新读取仍在;
2. 四种 responseMode 各自的答案校验,以及跳过 / 稍后;
3. 回答后模型失败,答案与 investigating 状态仍保留;
4. 重复提交幂等,跨用户 / 跨空间 / 无效选项 / 已归档问题都被拒;
5. 一轮最多 2 个问题,同一个 pending 问题不重复创建;
6. 回答后由脚本产生提案,确认前计划不变,确认后才写入。
"""

from __future__ import annotations

import json

import httpx

from backend.agent.runtime.scripted import ScriptedReasoner
from backend.db.models.enums import DegradedReason
from backend.tests.conftest import FakeReasoner

QUESTION = {
    "question": "这学期你希望把重心放在哪一边?",
    "whyNow": "它决定我先拆英语还是先拆数学",
    "responseMode": "single_select",
    "options": [{"id": "english", "label": "英语"}, {"id": "math", "label": "数学"}],
    "allowCustomInput": True,
}


def _question(
    *,
    question: str = QUESTION["question"],
    why_now: str = QUESTION["whyNow"],
    mode: str = "single_select",
    options: list[dict] | None = None,
    allow_custom: bool = True,
) -> dict:
    return {
        "question": question,
        "whyNow": why_now,
        "responseMode": mode,
        "options": options if options is not None else QUESTION["options"],
        "allowCustomInput": allow_custom,
    }


async def _send(client: httpx.AsyncClient, account, content: str, client_message_id: str):
    return await client.post(
        f"/api/workspaces/{account.workspace_id}/messages",
        json={"content": content, "clientMessageId": client_message_id},
        headers=account.headers,
    )


async def _questions(client: httpx.AsyncClient, account, *, include_decided: bool = False) -> list[dict]:
    url = f"/api/workspaces/{account.workspace_id}/questions"
    if include_decided:
        url += "?includeDecided=true"
    response = await client.get(url, headers=account.headers)
    assert response.status_code == 200, response.text
    return response.json()["questions"]


async def _answer(
    client: httpx.AsyncClient,
    account,
    question_id: str,
    *,
    selected: list[str] | None = None,
    custom: str | None = None,
    key: str = "answer-key-1",
):
    return await client.post(
        f"/api/workspaces/{account.workspace_id}/questions/{question_id}/answer",
        json={
            "selectedOptionIds": selected or [],
            "customInput": custom,
            "clientAnswerId": key,
        },
        headers=account.headers,
    )


async def _question_action(
    client: httpx.AsyncClient, account, question_id: str, action: str, key: str = "action-key-1"
):
    return await client.post(
        f"/api/workspaces/{account.workspace_id}/questions/{question_id}/{action}",
        json={"clientActionId": key},
        headers=account.headers,
    )


async def _plan(client: httpx.AsyncClient, account) -> dict:
    response = await client.get(
        f"/api/workspaces/{account.workspace_id}/plan", headers=account.headers
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _seed_question(
    client: httpx.AsyncClient, account, reasoner, *, message: str = "我想提升一下"
) -> dict:
    """发一条消息,让假模型提一个问题,返回这个问题视图。"""
    response = await _send(client, account, message, f"seed-{message}")
    assert response.status_code == 200, response.text
    rows = await _questions(client, account)
    assert rows, f"问题没有落库: {response.text}"
    return rows[0]


# ---------------------------------------------------------------------------------
# 1. 持久化 / 刷新恢复
# ---------------------------------------------------------------------------------
async def test_a_scripted_question_is_persisted_and_survives_reload(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """ScriptedReasoner 返回一个问题 -> 落库 -> 重新读取仍在。"""
    account = await make_account()
    use_reasoner(
        ScriptedReasoner.from_env(
            json.dumps({"reply": "我想先确认一件事。", "questions": [QUESTION]})
        )
    )

    sent = await _send(app_client, account, "我想提升一下", "msg-1")
    assert sent.status_code == 200, sent.text
    body = sent.json()
    # 问题**不是提案**:它落库即成卡片,不等确认。
    assert body["proposal"] is None
    assert body["proposalErrors"] == []

    rows = await _questions(app_client, account)
    assert len(rows) == 1
    view = rows[0]
    assert view["question"] == QUESTION["question"]
    assert view["whyNow"] == QUESTION["whyNow"]
    assert view["responseMode"] == "single_select"
    assert view["status"] == "pending"
    assert view["allowCustomInput"] is True
    assert [option["id"] for option in view["options"]] == ["english", "math"]
    # 与产出它的助手消息关联(可审计)。
    assert view["sourceMessageId"] == body["assistantMessage"]["id"]

    # "刷新" = 重新读一次,同一个 id 还在。
    again = await _questions(app_client, account)
    assert [item["id"] for item in again] == [view["id"]]


# ---------------------------------------------------------------------------------
# 2. 四种 responseMode 的答案校验 + 跳过 / 稍后
# ---------------------------------------------------------------------------------
async def test_single_select_rejects_more_than_one_choice(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    account = await make_account()
    use_reasoner(FakeReasoner(questions=(_question(mode="single_select"),)))
    view = await _seed_question(app_client, account, None)

    refused = await _answer(app_client, account, view["id"], selected=["english", "math"])
    assert refused.status_code == 400, refused.text
    assert refused.json()["error"]["code"] == "QUESTION_ANSWER_INVALID"

    ok = await _answer(app_client, account, view["id"], selected=["english"])
    assert ok.status_code == 200, ok.text
    assert ok.json()["question"]["answer"]["selectedOptionIds"] == ["english"]


async def test_multi_select_accepts_several_options_and_rejects_unknown(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    account = await make_account()
    use_reasoner(FakeReasoner(questions=(_question(mode="multi_select"),)))
    view = await _seed_question(app_client, account, None)

    unknown = await _answer(app_client, account, view["id"], selected=["nope"])
    assert unknown.status_code == 400, unknown.text
    assert unknown.json()["error"]["code"] == "QUESTION_ANSWER_INVALID"

    ok = await _answer(
        app_client, account, view["id"], selected=["english", "math"]
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["question"]["answer"]["selectedOptionIds"] == ["english", "math"]


async def test_free_text_requires_custom_input(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    account = await make_account()
    use_reasoner(
        FakeReasoner(
            questions=(_question(mode="free_text", options=[], allow_custom=True),)
        )
    )
    view = await _seed_question(app_client, account, None)

    empty = await _answer(app_client, account, view["id"])
    assert empty.status_code == 400, empty.text
    assert empty.json()["error"]["code"] == "QUESTION_ANSWER_INVALID"

    ok = await _answer(app_client, account, view["id"], custom="我想先打好英语基础")
    assert ok.status_code == 200, ok.text
    assert ok.json()["question"]["answer"]["customInput"] == "我想先打好英语基础"


async def test_mixed_accepts_option_plus_custom_input(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    account = await make_account()
    use_reasoner(FakeReasoner(questions=(_question(mode="mixed"),)))
    view = await _seed_question(app_client, account, None)

    ok = await _answer(
        app_client, account, view["id"], selected=["english"], custom="但数学校内课也要跟上"
    )
    assert ok.status_code == 200, ok.text
    answer = ok.json()["question"]["answer"]
    assert answer["selectedOptionIds"] == ["english"]
    assert answer["customInput"] == "但数学校内课也要跟上"


async def test_skip_archives_and_later_keeps_pending(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    account = await make_account()
    use_reasoner(FakeReasoner(questions=(_question(),)))
    view = await _seed_question(app_client, account, None)

    later = await _question_action(app_client, account, view["id"], "later")
    assert later.status_code == 200, later.text
    assert later.json()["question"]["status"] == "pending"

    skipped = await _question_action(app_client, account, view["id"], "skip")
    assert skipped.status_code == 200, skipped.text
    assert skipped.json()["question"]["status"] == "archived"
    # 归档之后不再出现在 active 列表里,但 includeDecided 读得到(可审计)。
    assert await _questions(app_client, account) == []
    decided = await _questions(app_client, account, include_decided=True)
    assert decided[0]["status"] == "archived"


# ---------------------------------------------------------------------------------
# 3. 模型失败:答案与处理中状态不丢
# ---------------------------------------------------------------------------------
async def test_an_answer_survives_a_failed_follow_up_model_call(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """回答先落库,后续那一轮模型降级也不影响它 —— 刷新后仍读得回。"""
    account = await make_account()
    use_reasoner(FakeReasoner(questions=(_question(),)))
    view = await _seed_question(app_client, account, None)

    # 换成降级的 reasoner:后续处理一定跑失败。
    use_reasoner(
        FakeReasoner(degraded=True, degraded_reason=DegradedReason.MODEL_UNAVAILABLE)
    )
    answered = await _answer(
        app_client, account, view["id"], selected=["english"], key="answer-key-fail"
    )
    assert answered.status_code == 200, answered.text
    assert answered.json()["question"]["status"] == "investigating"
    assert answered.json()["question"]["answer"]["selectedOptionIds"] == ["english"]

    reloaded = (await _questions(app_client, account))[0]
    assert reloaded["status"] == "investigating"
    assert reloaded["answer"]["selectedOptionIds"] == ["english"]


# ---------------------------------------------------------------------------------
# 4. 幂等与拒绝
# ---------------------------------------------------------------------------------
async def test_repeating_the_same_answer_is_idempotent(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    account = await make_account()
    use_reasoner(FakeReasoner(questions=(_question(),)))
    view = await _seed_question(app_client, account, None)

    first = await _answer(app_client, account, view["id"], selected=["english"], key="same-key")
    assert first.status_code == 200, first.text
    assert first.json()["replayed"] is False

    second = await _answer(app_client, account, view["id"], selected=["english"], key="same-key")
    assert second.status_code == 200, second.text
    assert second.json()["replayed"] is True
    # 没有第二次后续处理。
    assert second.json()["turn"] is None

    # 换一个幂等键、答案不同 -> 拒绝(不覆盖已经答过的)。
    conflict = await _answer(
        app_client, account, view["id"], selected=["math"], key="different-key"
    )
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["error"]["code"] == "QUESTION_NOT_ANSWERABLE"


async def test_answering_an_archived_question_is_refused(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    account = await make_account()
    use_reasoner(FakeReasoner(questions=(_question(),)))
    view = await _seed_question(app_client, account, None)
    await _question_action(app_client, account, view["id"], "skip")

    refused = await _answer(app_client, account, view["id"], selected=["english"])
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "QUESTION_NOT_ANSWERABLE"


async def test_another_account_cannot_see_or_answer_your_question(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    account_a = await make_account(email="qa@example.com")
    account_b = await make_account(email="qb@example.com", workspace_title="另一个空间")
    use_reasoner(FakeReasoner(questions=(_question(),)))
    view = await _seed_question(app_client, account_a, None)

    # B 看不到 A 的问题。
    assert await _questions(app_client, account_b) == []
    # B 拿 A 的空间 id + 问题 id 也答不了(空间归属先挡下)。
    cross = await app_client.post(
        f"/api/workspaces/{account_a.workspace_id}/questions/{view['id']}/answer",
        json={"selectedOptionIds": ["english"], "clientAnswerId": "cross-key"},
        headers=account_b.headers,
    )
    assert cross.status_code == 404, cross.text

    # B 用自己的空间 + A 的问题 id -> 问题 404。
    wrong_space = await _answer(app_client, account_b, view["id"], selected=["english"])
    assert wrong_space.status_code == 404, wrong_space.text
    assert wrong_space.json()["error"]["code"] == "QUESTION_NOT_FOUND"


# ---------------------------------------------------------------------------------
# 5. 一轮上限与同源去重
# ---------------------------------------------------------------------------------
async def test_at_most_two_questions_are_created_in_one_turn(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    account = await make_account()
    questions = [
        _question(question="问题一?", options=[{"id": "a", "label": "A"}], allow_custom=False),
        _question(question="问题二?", options=[{"id": "b", "label": "B"}], allow_custom=False),
        _question(question="问题三?", options=[{"id": "c", "label": "C"}], allow_custom=False),
    ]
    use_reasoner(FakeReasoner(questions=tuple(questions)))

    await _send(app_client, account, "请一次问完", "many-questions")
    rows = await _questions(app_client, account)
    # 模型给了 3 个,服务端截到 2 个(且不是靠 prompt 自觉)。
    assert len(rows) == 2
    assert {row["question"] for row in rows} == {"问题一?", "问题二?"}


async def test_the_same_pending_question_is_not_created_twice(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    account = await make_account()
    use_reasoner(FakeReasoner(questions=(_question(),)))

    await _send(app_client, account, "第一句", "dup-1")
    await _send(app_client, account, "第二句", "dup-2")

    rows = await _questions(app_client, account)
    assert len(rows) == 1, "同一个待回答的问题被重复创建了"


# ---------------------------------------------------------------------------------
# 6. 回答 -> 提案 -> 用户确认 -> 计划写入
# ---------------------------------------------------------------------------------
async def test_answering_creates_a_proposal_that_still_needs_confirmation(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """回答只是"用户输入事实";模型后续提的变更仍然要用户点确认才进计划。"""
    account = await make_account()
    use_reasoner(
        ScriptedReasoner.from_env(
            json.dumps(
                {
                    "turns": [
                        {"reply": "先确认一下。", "questions": [QUESTION]},
                        {
                            "reply": "我把它记下来。",
                            "actions": [
                                {
                                    "op": "create_node",
                                    "localId": "n2",
                                    "parentRef": "n1",
                                    "title": "这学期重心:英语",
                                    "nodeType": "capability",
                                    "purpose": "information",
                                    "description": "用户选择了英语。",
                                }
                            ],
                        },
                    ]
                }
            )
        )
    )

    sent = await _send(app_client, account, "我想提升一下", "answer-flow-1")
    assert sent.status_code == 200, sent.text
    view = (await _questions(app_client, account))[0]

    answered = await _answer(
        app_client, account, view["id"], selected=["english"], key="answer-flow-key"
    )
    assert answered.status_code == 200, answered.text
    payload = answered.json()
    assert payload["question"]["status"] == "resolved"
    turn = payload["turn"]
    assert turn is not None
    assert turn["proposal"] is not None, turn
    proposal_id = turn["proposal"]["id"]

    # 确认之前:画布没有那个节点。
    plan = await _plan(app_client, account)
    assert all(node["title"] != "这学期重心:英语" for node in plan["nodes"])

    # 用户确认之后才写入。
    confirmed = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/proposals/{proposal_id}/confirm",
        json={"idempotencyKey": "answer-confirm-key"},
        headers=account.headers,
    )
    assert confirmed.status_code == 200, confirmed.text
    plan = await _plan(app_client, account)
    titles = {node["title"] for node in plan["nodes"]}
    assert "这学期重心:英语" in titles


async def test_answering_can_produce_a_relation_proposal_too(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """回答之后模型也可以提一条关系 —— 同样要等确认才写入。

    这一条盯的是"回答没有绕过提案链路"那半句在**关系**上同样成立:`create_relation`
    写的是 `node_relations`,而它仍然必须经过 `proposal -> 确认 -> 事务写入`。
    """
    account = await make_account()
    root = next(
        node["id"] for node in (await _plan(app_client, account))["nodes"]
        if node["parentId"] is None
    )
    for title in ("英语阅读", "数学建模"):
        created = await app_client.post(
            f"/api/workspaces/{account.workspace_id}/nodes",
            json={"parentId": root, "title": title},
            headers=account.headers,
        )
        assert created.status_code == 201, created.text

    use_reasoner(
        ScriptedReasoner.from_env(
            json.dumps(
                {
                    "turns": [
                        {"reply": "先确认一下。", "questions": [QUESTION]},
                        {
                            "reply": "它们确实互相影响。",
                            "actions": [
                                {
                                    "op": "create_relation",
                                    "sourceRef": "n2",
                                    "targetRef": "n3",
                                    "relationType": "influences",
                                }
                            ],
                        },
                    ]
                }
            )
        )
    )

    await _send(app_client, account, "我在准备出国", "relation-flow-1")
    question = (await _questions(app_client, account))[0]
    answered = await _answer(
        app_client, account, question["id"], selected=["english"], key="relation-answer-key"
    )
    assert answered.status_code == 200, answered.text
    turn = answered.json()["turn"]
    assert turn["proposal"] is not None, turn
    proposal_id = turn["proposal"]["id"]

    # 确认前:一条关系都没有。
    assert (await _plan(app_client, account))["relations"] == []

    confirmed = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/proposals/{proposal_id}/confirm",
        json={"idempotencyKey": "relation-answer-confirm"},
        headers=account.headers,
    )
    assert confirmed.status_code == 200, confirmed.text
    plan = await _plan(app_client, account)
    assert len(plan["relations"]) == 1
    assert plan["relations"][0]["relationType"] == "influences"
    assert plan["relations"][0]["origin"] == "ai"
