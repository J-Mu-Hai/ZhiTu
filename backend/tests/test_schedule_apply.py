"""排期:预览、应用、幂等、失效保护、跨空间共享池。

## 这个文件为什么全部走真实 HTTP 接口

排期服务要读十几张表,拼出一份 `ScheduleRequest`,再把它写回去。**手工往库里塞几个
节点**然后断言"排出了 3 场",验证的是一个测试自己构造出来的世界 —— 而库里真实的
节点是"模型提案 + 用户确认"两步写进去的,字段可能和手工塞的不一样(比如
`estimate_minutes` 来自提案的哪个字段)。所以这里先走完整的提案确认流程把计划建出来,
再让排期去读它。

## 每个测试都在钉的那件事

1. 预览**一行都不写**。
2. 应用真的写进去了 —— 不是"接口返回 200",而是 `/plan` 里能读到那些场次。
3. 双击只写一次。
4. 预览之后输入变过 -> 409,并且**一行都不写**。
5. 已经完成的场次**原样不动** —— 这是本模块最不该造成的事。
6. 池子是**按人**共享的:从一个空间点"应用",另一个空间也被排上。
"""

from __future__ import annotations

import uuid

import httpx
from sqlalchemy import update

from backend.db.base import utcnow
from backend.db.models import ScheduledSession
from backend.db.models.enums import ScheduledSessionStatus
from backend.tests.conftest import FakeReasoner, snapshot

#: 一份有工时的计划:一个阶段 + 两个任务。**必须带 `estimateMinutes`** ——
#: 没有工时的节点排不进任何一天(那会变成 `NoEstimate` 缺口),而这个文件要验的是
#: "排进去的场次被正确地写下来"。
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
        "estimateMinutes": 300,
    },
)

APPLY_KEY = "schedule-apply-key-aaaa"


async def _propose_and_confirm(
    client: httpx.AsyncClient, account, workspace_id: str, *, idempotency_key: str
) -> None:
    """走真实接口把一个带工时的计划建出来。"""
    proposed = await client.post(
        f"/api/workspaces/{workspace_id}/messages",
        json={"content": "帮我把这个目标拆成计划"},
        headers=account.headers,
    )
    assert proposed.status_code == 200, proposed.text
    proposal = proposed.json()["proposal"]
    assert proposal is not None, f"提案没有被建立: {proposed.text}"

    confirmed = await client.post(
        f"/api/workspaces/{workspace_id}/proposals/{proposal['id']}/confirm",
        json={"idempotencyKey": idempotency_key},
        headers=account.headers,
    )
    assert confirmed.status_code == 200, confirmed.text


async def _plan(client: httpx.AsyncClient, account, workspace_id: str | None = None) -> dict:
    response = await client.get(
        f"/api/workspaces/{workspace_id or account.workspace_id}/plan",
        headers=account.headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _preview(client: httpx.AsyncClient, account, workspace_id: str | None = None) -> dict:
    response = await client.post(
        f"/api/workspaces/{workspace_id or account.workspace_id}/schedule/preview",
        headers=account.headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _apply(
    client: httpx.AsyncClient, account, version: str, key: str = APPLY_KEY, workspace_id=None
) -> httpx.Response:
    return await client.post(
        f"/api/workspaces/{workspace_id or account.workspace_id}/schedule/apply",
        json={"scheduleVersion": version, "idempotencyKey": key},
        headers=account.headers,
    )


async def _ready_account(client, make_account, use_reasoner, *, plan=PLAN_ACTIONS):
    """一个已经有一份带工时计划的账号。"""
    account = await make_account()
    use_reasoner(FakeReasoner(actions=plan))
    await _propose_and_confirm(
        client, account, account.workspace_id, idempotency_key="seed-confirm-key-1"
    )
    return account, FakeReasoner(actions=plan)


# ---------------------------------------------------------------------------------
# 1. 预览不写任何东西
# ---------------------------------------------------------------------------------
async def test_preview_writes_nothing(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db
) -> None:
    """预览是只读的。**两张表都不许动**:场次表要空着,应用台账也要空着。

    这一条如果只是"预览之后 /plan 没变"就太弱了 —— 排期可能把场次写进了库而
    `/plan` 因为别的原因没显示出来。数行数才是那句断言。
    """
    account, _ = await _ready_account(app_client, make_account, use_reasoner)

    before = await snapshot(db)
    preview = await _preview(app_client, account)

    await db.rollback()  # 开一个新事务再数,否则数的是上一个快照
    assert await snapshot(db) == before, "预览写库了"

    # 而它确实算出了东西 —— 否则上面那句会以"什么都没算"的方式通过。
    assert preview["scheduleVersion"], preview
    assert preview["sessions"], "一个有工时的计划应当排出至少一场"
    assert preview["truncated"] is False


# ---------------------------------------------------------------------------------
# 2. 应用真的写进去了
# ---------------------------------------------------------------------------------
async def test_apply_writes_sessions_that_the_plan_then_shows(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """应用的场次要能在 `/plan` 里读到 —— 这是"写进去了"的唯一可信证据。"""
    account, _ = await _ready_account(app_client, make_account, use_reasoner)

    assert (await _plan(app_client, account))["sessions"] == [], "排期之前不该有任何场次"

    preview = await _preview(app_client, account)
    response = await _apply(app_client, account, preview["scheduleVersion"])
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["replayed"] is False
    assert body["applied"]["created"] == len(preview["sessions"])

    plan = await _plan(app_client, account)
    assert len(plan["sessions"]) == len(preview["sessions"])

    stored = {session["scheduledDate"] for session in plan["sessions"]}
    computed = {session["scheduledDate"] for session in preview["sessions"]}
    assert stored == computed, "库里那天和算出来那天不一致"
    # 场次是"哪天做"的表示,不是"要做几个任务"。一个节点可以有很多场,而
    # `planNodes` 里始终只有一行 —— 这条如果失守,界面上的任务列表会开始重复。
    assert len(plan["nodes"]) == len({n["id"] for n in plan["nodes"]})


# ---------------------------------------------------------------------------------
# 3. 双击只写一次
# ---------------------------------------------------------------------------------
async def test_double_click_applies_once(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db
) -> None:
    """同一个幂等键点两次:两次都 200,第二次 `replayed=true`,行数不变。

    **"第二次返回错误"是错的实现方式。** 用户双击看到的应该和第一次一模一样 ——
    一个 409 会让他以为自己的计划没保存上。
    """
    account, _ = await _ready_account(app_client, make_account, use_reasoner)
    preview = await _preview(app_client, account)

    first = await _apply(app_client, account, preview["scheduleVersion"])
    assert first.status_code == 200, first.text
    assert first.json()["replayed"] is False

    await db.rollback()
    after_first = await snapshot(db)

    second = await _apply(app_client, account, preview["scheduleVersion"])
    assert second.status_code == 200, second.text
    assert second.json()["replayed"] is True
    # 重放返回的是**上次那个响应体**,不是一次重新计算的结果。
    assert second.json()["applied"] == first.json()["applied"]

    await db.rollback()
    assert await snapshot(db) == after_first, "双击写了两遍"


async def test_same_key_with_a_different_version_is_rejected(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """同一个键配另一份排期 -> 409 `IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_BODY`。

    不然的话,客户端把所有请求共用一个固定字符串时,用户会看到**另一份排期**被
    "应用成功" —— 那是比失败严重得多的结果。
    """
    account, _ = await _ready_account(app_client, make_account, use_reasoner)
    preview = await _preview(app_client, account)
    await _apply(app_client, account, preview["scheduleVersion"])

    reused = await _apply(app_client, account, "f" * 32, key=APPLY_KEY)
    assert reused.status_code == 409, reused.text
    assert reused.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_BODY"


# ---------------------------------------------------------------------------------
# 4. 预览之后输入变了
# ---------------------------------------------------------------------------------
async def test_stale_schedule_version_is_rejected(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db
) -> None:
    """预览之后改计划 -> 409,并且**一行都不写**。

    用户点头的是他在屏幕上看到的那一份。输入变了之后重排出来的可能是另一份(任务挪到了
    别的日子),而他没有看过它 —— 那正是"不能直接覆盖"要挡的事。
    """
    account, _ = await _ready_account(app_client, make_account, use_reasoner)
    preview = await _preview(app_client, account)

    # 用户自己改了一个节点的说明 —— 计划版本前进,这一份预览就失效了。
    plan = await _plan(app_client, account)
    task = next(node for node in plan["nodes"] if node["nodeType"] == "task")
    edited = await app_client.patch(
        f"/api/workspaces/{account.workspace_id}/nodes/{task['id']}",
        json={"description": "我自己改了说明"},
        headers=account.headers,
    )
    assert edited.status_code == 200, edited.text

    await db.rollback()
    before = await snapshot(db)

    response = await _apply(app_client, account, preview["scheduleVersion"])
    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == "STALE_SCHEDULE_VERSION"
    assert error["details"]["previewedVersion"] == preview["scheduleVersion"]
    # 报出来的"现在是什么版本"必须是**此刻现算的**,不是照抄请求里那个 ——
    # 照抄的话这个字段一点信息量都没有。
    assert error["details"]["currentVersion"] != preview["scheduleVersion"]

    await db.rollback()
    assert await snapshot(db) == before, "版本对不上却写了东西"


# ---------------------------------------------------------------------------------
# 5. 已完成的场次原样不动
# ---------------------------------------------------------------------------------
async def test_completed_session_is_never_moved_or_canceled(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db
) -> None:
    """把一场标成已完成,再重排一次 —— 那一行必须**一字不改**。

    `execution_records` 与已完成的场次是本产品最不该被自动化改掉的东西:用户勾过的
    完成记录如果被一次重排抹掉,他会发现自己的进度凭空倒退,而且没有任何地方能解释
    这件事。算法侧已经保证冻结的场次不进输出(有单元测试),这里验的是**写入路径**:
    `sessions` 里没有它的时候,它是不是真的没被碰。

    直接用 `db` 把状态改成 `done`,因为"用户勾完成"那条接口属于阶段 7。这里要构造的
    是**状态本身**,它长什么样与它是怎么来的无关。
    """
    account, _ = await _ready_account(app_client, make_account, use_reasoner)
    preview = await _preview(app_client, account)
    applied = await _apply(app_client, account, preview["scheduleVersion"])
    assert applied.status_code == 200, applied.text

    plan = await _plan(app_client, account)
    frozen = plan["sessions"][0]

    await db.execute(
        update(ScheduledSession)
        # 接口上是字符串,列上是 `Uuid` —— 不转的话 SQLAlchemy 会在绑参时报
        # `'str' object has no attribute 'hex'`,而那和"排期有没有动完成记录"毫无关系。
        .where(ScheduledSession.id == uuid.UUID(frozen["id"]))
        .values(status=ScheduledSessionStatus.DONE, completed_at=utcnow())
    )
    await db.commit()

    # 重新预览:这一次的输入里那一场是"既成事实"。
    # 注意版本号与上一次**不同** —— 既有场次是输入之一,这是对的。
    #
    # 换一个幂等键:用户这里点的是**第二次**"应用",不是一个重试。拿旧键配新版本会被
    # 正确地判成 `IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_BODY`(上面那条测试钉着这个
    # 行为),而那和本测试要验的事无关。
    fresh = await _preview(app_client, account)
    again = await _apply(app_client, account, fresh["scheduleVersion"], key="schedule-apply-key-bbbb")
    assert again.status_code == 200, again.text

    after = await _plan(app_client, account)
    still_there = next(
        (session for session in after["sessions"] if session["id"] == frozen["id"]), None
    )
    assert still_there is not None, "已完成的场次从计划里消失了"
    assert still_there["status"] == "done", "已完成的状态被重排改掉了"
    assert still_there["scheduledDate"] == frozen["scheduledDate"], "已完成的场次被挪到了别的日子"
    assert still_there["plannedMinutes"] == frozen["plannedMinutes"]


# ---------------------------------------------------------------------------------
# 6. 池子是按人共享的
# ---------------------------------------------------------------------------------
async def test_time_pool_is_shared_across_workspaces(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """同一个账号的两个空间争的是同一个晚上。

    **这是本阶段最容易做错、也最看不出来的一件事。** 逐空间排的话,两个空间各自都能
    排下,界面上两边都正常,只有把 `dailyLoad` 加起来才发现超出用户的时间预算。所以
    断言落在两处:

    1. 从一个空间发起,排期范围是**两个**空间(`scopeWorkspaceIds` / `applied.workspaces`)。
    2. 每一天的跨空间总量 ≤ 那一天的容量。
    """
    account = await make_account()
    use_reasoner(FakeReasoner(actions=PLAN_ACTIONS))

    second = await app_client.post(
        "/api/workspaces",
        json={"title": "英语提升", "intent": "半年内能读原版书"},
        headers=account.headers,
    )
    assert second.status_code == 201, second.text
    second_id = second.json()["workspace"]["id"]

    await _propose_and_confirm(
        app_client, account, account.workspace_id, idempotency_key="seed-confirm-key-1"
    )
    await _propose_and_confirm(app_client, account, second_id, idempotency_key="seed-confirm-key-2")

    # 从**第一个**空间发起。
    preview = await _preview(app_client, account)
    assert set(preview["scopeWorkspaceIds"]) == {account.workspace_id, second_id}, (
        "排期只看了发起的那一个空间 —— 另一个空间的安排会被当成不存在"
    )
    assert {session["workspaceId"] for session in preview["sessions"]} == {
        account.workspace_id,
        second_id,
    }, "两个空间都该被排上"

    for day in preview["dailyLoad"]:
        assert day["plannedMinutes"] <= day["capacityMinutes"], (
            f"{day['date']} 排了 {day['plannedMinutes']} 分钟,容量只有 "
            f"{day['capacityMinutes']} —— 跨空间的总量没有对着同一个池子算"
        )

    applied = await _apply(app_client, account, preview["scheduleVersion"])
    assert applied.status_code == 200, applied.text
    assert applied.json()["applied"]["workspaces"] == 2

    # 另一个空间**从它自己的接口**也要能看到那些场次。
    other_plan = await _plan(app_client, account, second_id)
    assert other_plan["sessions"], "另一个空间被排了,但它自己的计划里看不到"
