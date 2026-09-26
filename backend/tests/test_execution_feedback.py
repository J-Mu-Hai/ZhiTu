"""执行反馈与「今天」。

## 这个文件在钉的六件事

1. 反馈真的写进去了,而且**改到了场次行上**(不是只往台账里插一行)。
2. 双击只写一次,第二次 `replayed=true`。
3. 同一个键配另一份内容 -> 409。不拦的话,客户端共用一个固定字符串时,用户会看到
   "另一场的结果"覆盖掉自己刚才那次反馈。
4. 四种结果对计划的含义**各不相同**:`failed` 不会把这场关掉(事情还欠着),
   `skipped` 会冻结它(排期不该再动一场用户说没做的安排)。
5. **没有记录 ≠ 没完成。** 过去、又没有记录的场次进的是 `checkInQuestions`,
   不是"未完成"。
6. 数据库写不进去时响应里是 `saved: false` + 503,**不是** 200。

第 6 条是这一阶段唯一"必须响亮地失败"的地方,所以它值得一个真的让 commit 抛异常的
测试 —— 而不是读一遍代码确认它没写 `except: pass`。
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import httpx
from sqlalchemy import update
from sqlalchemy.exc import OperationalError

from backend.db.models import ScheduledSession
from backend.db.models.enums import ScheduledSessionStatus
from backend.services.timeutil import today_in
from backend.tests.conftest import FakeReasoner

PLAN_ACTIONS = (
    {
        "op": "create_node",
        "localId": "n2",
        "parentRef": "n1",
        "title": "阶段一:基础语法",
        "nodeType": "stage",
        "estimateMinutes": 300,
    },
    {
        "op": "create_node",
        "localId": "n3",
        "parentRef": "n2",
        "title": "变量与类型",
        "nodeType": "task",
        "estimateMinutes": 240,
    },
)


async def _ready_account(client: httpx.AsyncClient, make_account, use_reasoner):
    """一个已经排好期的账号 —— 今天真的有场次可反馈。"""
    account = await make_account()
    use_reasoner(FakeReasoner(actions=PLAN_ACTIONS))

    proposed = await client.post(
        f"/api/workspaces/{account.workspace_id}/messages",
        json={"content": "帮我把这个目标拆成计划"},
        headers=account.headers,
    )
    assert proposed.status_code == 200, proposed.text
    proposal = proposed.json()["proposal"]
    assert proposal is not None, proposed.text

    confirmed = await client.post(
        f"/api/workspaces/{account.workspace_id}/proposals/{proposal['id']}/confirm",
        json={"idempotencyKey": "seed-confirm-key-1"},
        headers=account.headers,
    )
    assert confirmed.status_code == 200, confirmed.text

    preview = await client.post(
        f"/api/workspaces/{account.workspace_id}/schedule/preview",
        headers=account.headers,
    )
    assert preview.status_code == 200, preview.text
    applied = await client.post(
        f"/api/workspaces/{account.workspace_id}/schedule/apply",
        json={
            "scheduleVersion": preview.json()["scheduleVersion"],
            "idempotencyKey": "seed-apply-key-1",
        },
        headers=account.headers,
    )
    assert applied.status_code == 200, applied.text
    return account


async def _today(client: httpx.AsyncClient, account) -> dict:
    response = await client.get("/api/today", headers=account.headers)
    assert response.status_code == 200, response.text
    return response.json()


async def _first_item(client: httpx.AsyncClient, account) -> dict:
    today = await _today(client, account)
    items = [item for group in today["workspaces"] for item in group["items"]]
    assert items, f"今天一场都没排上,后面的断言没有意义: {today}"
    return items[0]


async def _record(
    client: httpx.AsyncClient,
    account,
    session_id: str,
    *,
    result: str,
    key: str,
    **extra,
) -> httpx.Response:
    return await client.post(
        f"/api/sessions/{session_id}/executions",
        json={"result": result, "idempotencyKey": key, **extra},
        headers=account.headers,
    )


async def _history(client: httpx.AsyncClient, account, session_id: str) -> dict:
    response = await client.get(
        f"/api/sessions/{session_id}/executions", headers=account.headers
    )
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------------
# 1. 记录真的落到了场次行上
# ---------------------------------------------------------------------------------
async def test_completed_closes_the_session(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """报"做完了" -> 场次变成 done,而且 `/today` 读到的也是 done。

    只断言接口返回 200 是不够的:响应可以是服务端拼出来的,而库里什么都没变。
    所以再读一次 `/today` —— 那是另一条查询路径读出来的同一个事实。
    """
    account = await _ready_account(app_client, make_account, use_reasoner)
    item = await _first_item(app_client, account)

    response = await _record(
        app_client,
        account,
        item["sessionId"],
        result="completed",
        key="exec-key-completed-1",
        actualMinutes=item["plannedMinutes"],
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["saved"] is True, "成功响应里也必须有 saved —— 见 contracts/execution.py"
    assert body["replayed"] is False
    assert body["record"]["result"] == "completed"
    assert body["session"]["status"] == "done"
    assert body["record"]["actualMinutes"] == item["plannedMinutes"]

    after = await _today(app_client, account)
    stored = next(
        entry
        for group in after["workspaces"]
        for entry in group["items"]
        if entry["sessionId"] == body["record"]["sessionId"]
    )
    assert stored["recorded"] is True
    assert stored["result"] == "completed"
    assert stored["status"] == "done"


# ---------------------------------------------------------------------------------
# 2. 双击只写一次
# ---------------------------------------------------------------------------------
async def test_double_submit_records_once(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """同一个幂等键发两次:两次都 200,第二次 `replayed=true`,历史里只有一条。

    **第二次返回错误是错的实现方式** —— 用户双击看到的应该和第一次一模一样。
    """
    account = await _ready_account(app_client, make_account, use_reasoner)
    item = await _first_item(app_client, account)

    first = await _record(
        app_client, account, item["sessionId"], result="partial", key="exec-key-double-1",
        actualMinutes=20,
    )
    assert first.status_code == 200, first.text
    assert first.json()["replayed"] is False

    second = await _record(
        app_client, account, item["sessionId"], result="partial", key="exec-key-double-1",
        actualMinutes=20,
    )
    assert second.status_code == 200, second.text
    assert second.json()["replayed"] is True
    assert second.json()["record"]["id"] == first.json()["record"]["id"]

    history = await _history(app_client, account, item["sessionId"])
    assert len(history["records"]) == 1, f"双击写了两条: {history['records']}"


async def test_same_key_with_a_different_result_is_rejected(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """同一个键配另一份内容 -> 409。

    客户端若把所有请求共用一个固定字符串,不拦的话第二次会**静默成功**并返回
    第一条记录 —— 用户看到的是"我报的没生效,而系统说我报过了"。
    """
    account = await _ready_account(app_client, make_account, use_reasoner)
    item = await _first_item(app_client, account)

    await _record(
        app_client, account, item["sessionId"], result="skipped", key="exec-key-reuse-1"
    )
    reused = await _record(
        app_client, account, item["sessionId"], result="completed", key="exec-key-reuse-1"
    )

    assert reused.status_code == 409, reused.text
    assert reused.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_BODY"


# ---------------------------------------------------------------------------------
# 3. 四种结果对计划的含义不一样
# ---------------------------------------------------------------------------------
async def test_failed_keeps_the_session_open(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """`failed` **不会**把这场标成完成 —— 这件事仍然欠着。

    这是一个容易做反的地方:把 failed 和 skipped 一起当成"这场过去了",于是任务
    在系统眼里已经了结,而用户其实什么都没拿到。代价是那个目标从此不再出现在
    任何一天的安排里。
    """
    account = await _ready_account(app_client, make_account, use_reasoner)
    item = await _first_item(app_client, account)

    response = await _record(
        app_client, account, item["sessionId"], result="failed",
        key="exec-key-failed-1", delayReason="环境装不上,卡了两小时",
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["record"]["result"] == "failed"
    assert body["record"]["delayReason"] == "环境装不上,卡了两小时"
    assert body["session"]["status"] != "done", "failed 不该把这场关掉"
    assert body["session"]["status"] != "skipped", "failed 与 skipped 是两回事"


async def test_skipped_freezes_the_session(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """`skipped` -> 场次冻结。

    冻结是有意的:用户明确说了这场没做,排期算法就不该再把这一行当成"可以搬来搬去
    的候选"。搬走它等于把用户说过的话抹掉。
    """
    account = await _ready_account(app_client, make_account, use_reasoner)
    item = await _first_item(app_client, account)

    response = await _record(
        app_client, account, item["sessionId"], result="skipped", key="exec-key-skipped-1"
    )
    assert response.status_code == 200, response.text
    assert response.json()["session"]["status"] == "skipped"


async def test_two_reports_sum_into_one_total(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """分两次做完一场:行上的分钟数是**合计**,而两次反馈都还在历史里。

    合计是可导出的,反过来不成立 —— 只留最后一次的话,一去不返的是过程。
    """
    account = await _ready_account(app_client, make_account, use_reasoner)
    item = await _first_item(app_client, account)

    first = await _record(
        app_client, account, item["sessionId"], result="partial",
        key="exec-key-sum-1", actualMinutes=20,
    )
    assert first.status_code == 200, first.text
    assert first.json()["session"]["status"] != "done", "只做了一部分不该算完成"

    second = await _record(
        app_client, account, item["sessionId"], result="completed",
        key="exec-key-sum-2", actualMinutes=30,
    )
    assert second.status_code == 200, second.text
    assert second.json()["session"]["status"] == "done"
    assert second.json()["session"]["actualMinutes"] == 50, "分钟数应当是合计"

    history = await _history(app_client, account, item["sessionId"])
    assert [record["result"] for record in history["records"]] == ["completed", "partial"]


# ---------------------------------------------------------------------------------
# 4. 「今天」:只统计有记录的,没记录的去提问
# ---------------------------------------------------------------------------------
async def test_today_counts_only_recorded_minutes(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """`actualMinutes` 只算有记录的那些。

    把没记录的算成 0 会让"今天投入了多久"在下午就变成一个假数字 —— 那时候用户可能
    只是还没来得及记。
    """
    account = await _ready_account(app_client, make_account, use_reasoner)
    before = await _today(app_client, account)
    assert before["actualMinutes"] == 0
    assert before["recordedCount"] == 0
    assert before["itemCount"] == sum(len(group["items"]) for group in before["workspaces"])

    item = await _first_item(app_client, account)
    await _record(
        app_client, account, item["sessionId"], result="completed",
        key="exec-key-count-1", actualMinutes=35,
    )

    after = await _today(app_client, account)
    assert after["actualMinutes"] == 35
    assert after["recordedCount"] == 1
    assert after["itemCount"] == before["itemCount"], "记录了不该改变今天的场次数"


async def test_unrecorded_past_session_becomes_a_question(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db
) -> None:
    """一场过去的安排没有记录 -> 是一句**提问**,不是一条"未完成"。

    `result` 为空与 `result == "skipped"` 是两件完全不同的事:后者是用户说了"我没做",
    前者是我们**不知道**。把它当成没完成,用户从第一天起就会被系统按一个他从未确认过
    的事实去重排计划,而他纠正不了。
    """
    account = await _ready_account(app_client, make_account, use_reasoner)
    item = await _first_item(app_client, account)

    yesterday = today_in("Asia/Shanghai") - timedelta(days=1)
    await db.execute(
        update(ScheduledSession)
        # 接口上是字符串,列上是 `Uuid` —— 不转的话 SQLAlchemy 会在绑参时报
        # `'str' object has no attribute 'hex'`。
        .where(ScheduledSession.id == uuid.UUID(item["sessionId"]))
        .values(scheduled_date=yesterday)
    )
    await db.commit()

    today = await _today(app_client, account)
    assert item["sessionId"] not in {
        entry["sessionId"] for group in today["workspaces"] for entry in group["items"]
    }, "昨天的场次不该出现在今天的清单里"

    questions = today["checkInQuestions"]
    question = next(
        (entry for entry in questions if entry["sessionId"] == item["sessionId"]), None
    )
    assert question is not None, f"过去又没记录的场次应当被提问: {questions}"
    assert question["daysAgo"] == 1
    assert question["plannedMinutes"] == item["plannedMinutes"]
    assert "没完成" not in question["question"], "提问不能预设结论"
    assert "没完成" not in today["note"], "服务端的说明不能把未记录说成未完成"


# ---------------------------------------------------------------------------------
# 5. 归属:别人的场次写不进去
# ---------------------------------------------------------------------------------
async def test_b_cannot_record_into_a_sessions(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """B 拿 A 的场次 id -> 404,而 A 自己写得进去。

    单独一条,是因为 `test_authz_matrix` 的跨账号表覆盖不到它:那张表用同一个
    `{session_id}` 占位符表示**登录会话**,而这里要的是**排期场次**的 id。

    **反向断言在这里格外重要。** 少了"先证明 A 写得进去",这个测试完全可能是因为
    场次 id 拼错了才 404 —— 那样它是绿的,而归属校验一行都没被验证。
    """
    account_a = await _ready_account(app_client, make_account, use_reasoner)
    item = await _first_item(app_client, account_a)

    account_b = await make_account(email="b@example.com", workspace_title="英语")

    theirs = await _record(
        app_client, account_b, item["sessionId"], result="completed", key="exec-key-b-1"
    )
    assert theirs.status_code == 404, theirs.text
    # 403 会确认"这个 id 存在但不属于你",等于把系统里有哪些场次告诉了任何人。
    assert theirs.status_code != 403
    assert theirs.json()["error"]["code"] == "SESSION_NOT_FOUND"

    mine = await _record(
        app_client, account_a, item["sessionId"], result="completed", key="exec-key-a-1"
    )
    assert mine.status_code == 200, mine.text


async def test_canceled_session_cannot_be_recorded(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db
) -> None:
    """已经取消/搬走的场次 -> 400。

    它上面的"实际发生了什么"没有意义:那一行已经不在任何人的日历上了。允许往里写
    的话,复盘会把一场被取消的安排算成一次真实的执行。
    """
    account = await _ready_account(app_client, make_account, use_reasoner)
    item = await _first_item(app_client, account)

    await db.execute(
        update(ScheduledSession)
        .where(ScheduledSession.id == uuid.UUID(item["sessionId"]))
        .values(status=ScheduledSessionStatus.CANCELED)
    )
    await db.commit()

    response = await _record(
        app_client, account, item["sessionId"], result="completed", key="exec-key-canceled-1"
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "INVALID_INPUT"


# ---------------------------------------------------------------------------------
# 6. 写不进去时必须响亮地失败
# ---------------------------------------------------------------------------------
class _CommitFails:
    """一个只在 `commit()` 上失败的会话。

    其他一切照常委托给真会话 —— 这样 `record_execution` 里那条插入路径真的会走完,
    失败发生在最后那一下。模拟"库在写到一半的时候不见了"。
    """

    def __init__(self, real) -> None:
        self._real = real

    def __getattr__(self, name: str):
        return getattr(self._real, name)

    async def commit(self) -> None:
        raise OperationalError(
            "INSERT INTO execution_records ...", {}, Exception("database is locked")
        )


async def test_database_failure_is_reported_as_not_saved(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """写入失败 -> 503 + `saved: false`,**不是** 200,也不是静默切内存。

    这是本阶段唯一"必须响亮地失败"的地方。谎报成功会让那条记录永久消失,而用户以为
    它在 —— 复盘、排期、偏差检测从此都在一个错误的假设上往下算,他没有任何办法发现。
    """
    from backend.api.main import app
    from backend.db.session import get_db

    account = await _ready_account(app_client, make_account, use_reasoner)
    item = await _first_item(app_client, account)

    async def broken_db():
        async for session in get_db():
            yield _CommitFails(session)

    app.dependency_overrides[get_db] = broken_db
    try:
        response = await _record(
            app_client, account, item["sessionId"], result="completed",
            key="exec-key-broken-1", actualMinutes=30,
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 503, response.text
    body = response.json()
    assert body["error"]["code"] == "DB_UNAVAILABLE"
    # 这个字段是给客户端读的,不是给人读的说明文字 —— 界面据此决定要不要把这条
    # 反馈标成"未同步"。
    assert body["saved"] is False
