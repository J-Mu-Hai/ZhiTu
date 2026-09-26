"""站内提醒:触发、处置、免打扰。

## 这个文件在钉的四件事

1. **关掉是永久的,稍后是真的稍后。** 两者都是落库的处置,不是界面上的状态 ——
   刷新之后必须还是那个结果,否则用户会以为按钮坏了。
2. **免打扰时段是从用户的可用时段推出来的**,而不是一个写死的常量。
3. **免打扰只压住"催促",不压住"有事情在等你"。** 这一条是一个产品判断,不是技术
   细节:提醒只在用户打开界面时出现,他此刻醒着这个前提已经成立;真正会伤人的是
   "深夜了还在催你今天没记录",而"你新建的空间还是空的"并不催人。
4. **没有记录不触发"连续漏做"。** 与复盘那条线是同一条纪律:系统不能把"不知道"
   当成"没做到"。这里用一个"先断言不出现、再记录两次、再断言出现"的两段式来钉它 ——
   只断言后者的话,一个"凡是有过去的场次就提醒"的错误实现也能通过。

时间在这里是要被控制的:`now_in` 被打桩,因为免打扰的"此刻"和周末判断都依赖它。
`today_in` **不打桩** —— 那会让"过去几天"的窗口跟着漂,而本文件里那些场次是按真实
的今天排出来的。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import httpx
from sqlalchemy import select, update

from backend.db.base import utcnow
from backend.db.models import AvailabilityRule, Message, PlanNode, ReminderState, ScheduledSession
from backend.db.models.enums import NodeStatus
from backend.services import reminder_service
from backend.services.timeutil import now_in, today_in
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


async def _reminders(client: httpx.AsyncClient, account) -> dict:
    response = await client.get("/api/reminders", headers=account.headers)
    assert response.status_code == 200, response.text
    return response.json()


def _kinds(body: dict) -> set[str]:
    return {item["kind"] for item in body["reminders"]}


async def _account_with_schedule(client: httpx.AsyncClient, make_account, use_reasoner):
    account = await make_account()
    use_reasoner(FakeReasoner(actions=PLAN_ACTIONS))

    proposed = await client.post(
        f"/api/workspaces/{account.workspace_id}/messages",
        json={"content": "帮我把这个目标拆成计划"},
        headers=account.headers,
    )
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


async def _planned_sessions(client: httpx.AsyncClient, account) -> list[dict]:
    """计划里的场次,按日期先后。

    不取 `/today`:一天的容量就那么点(默认一天一场 120 分钟),所以"今天"常常只有
    一场 —— 而需要两场的断言要的是**两场安排**,它们可以落在不同的日子。
    """
    response = await client.get(
        f"/api/workspaces/{account.workspace_id}/plan", headers=account.headers
    )
    assert response.status_code == 200, response.text
    sessions = response.json()["sessions"]
    assert sessions, "一场都没排上,后面的断言没有意义"
    return sessions


def _next_weekend_at(hour: int) -> datetime:
    """下一个周六的这个钟点。

    挑周六是因为周末提醒只在周六周日触发;用"下一个"而不是"上一个",是为了让这个
    时刻**晚于**刚刚建出来的那些行 —— 一个早于它们的"此刻"会让"这周刚更新过"这类
    窗口判断出现负的时间差,而那不是真实会发生的状态。
    """
    now = now_in("Asia/Shanghai")
    ahead = (5 - now.weekday()) % 7
    if ahead == 0 and now.hour >= hour:
        ahead = 7
    return (now + timedelta(days=ahead)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )


# ---------------------------------------------------------------------------------
# 1. 关掉是永久的
# ---------------------------------------------------------------------------------
async def test_empty_workspace_reminder_can_be_dismissed_permanently(
    app_client: httpx.AsyncClient, make_account, db
) -> None:
    """新建的空空间会被提醒;关掉之后,刷新多少次都不再出现。

    "关掉"必须落库。只在内存里记一笔的话,用户刷新页面它就回来了 —— 而他会以为
    这个按钮坏了,然后开始无视所有提醒。
    """
    account = await make_account()

    first = await _reminders(app_client, account)
    assert "workspace_empty" in _kinds(first), first
    key = next(item["key"] for item in first["reminders"] if item["kind"] == "workspace_empty")
    assert key.startswith("workspace_empty:")

    dismissed = await app_client.post(
        "/api/reminders/dismiss", json={"key": key}, headers=account.headers
    )
    assert dismissed.status_code == 200, dismissed.text
    assert dismissed.json()["dismissed"] is True

    assert "workspace_empty" not in _kinds(await _reminders(app_client, account))

    # 再点一次 —— 幂等,不是 409,也不是第二条状态行。
    again = await app_client.post(
        "/api/reminders/dismiss", json={"key": key}, headers=account.headers
    )
    assert again.status_code == 200, again.text
    assert "workspace_empty" not in _kinds(await _reminders(app_client, account))

    rows = (
        await db.execute(select(ReminderState).where(ReminderState.reminder_key == key))
    ).scalars().all()
    assert len(rows) == 1, "同一个键被处置了两次却留下了两行"


# ---------------------------------------------------------------------------------
# 2. "稍后"是一个真实的承诺
# ---------------------------------------------------------------------------------
async def test_snooze_hides_it_until_the_promised_time(
    app_client: httpx.AsyncClient, make_account, db
) -> None:
    """稍后 24 小时 -> 现在不出现;到点之后**重新出现**。

    "到点重新出现"这一半才是重点。把稍后实现成"软性关掉"最省事,也最伤:用户点
    "稍后"的意思是"过会儿再跟我说",不是"这事别再提了"。
    """
    account = await make_account()
    before = await _reminders(app_client, account)
    key = next(item["key"] for item in before["reminders"] if item["kind"] == "workspace_empty")

    snoozed = await app_client.post(
        "/api/reminders/snooze", json={"key": key, "hours": 24}, headers=account.headers
    )
    assert snoozed.status_code == 200, snoozed.text
    until = snoozed.json()["snoozedUntil"]
    assert until is not None

    assert "workspace_empty" not in _kinds(await _reminders(app_client, account))

    # 让时间走过去。**不睡 24 小时** —— 直接把那个时刻改到过去,这是"到点了"在库里
    # 唯一的样子。
    await db.execute(
        update(ReminderState)
        .where(ReminderState.reminder_key == key)
        .values(snoozed_until=utcnow() - timedelta(minutes=1))
    )
    await db.commit()

    assert "workspace_empty" in _kinds(await _reminders(app_client, account)), (
        "稍后到点了却没有回来"
    )


# ---------------------------------------------------------------------------------
# 3. 免打扰时段是从可用时段推出来的
# ---------------------------------------------------------------------------------
async def test_quiet_hours_are_derived_from_availability(
    app_client: httpx.AsyncClient, make_account, db
) -> None:
    """没有可用时段 -> 默认 22:00–08:00;说过"晚上到 23:30" -> 从 23:30 开始。

    上界从用户的可用时段推(他自己给出的边界),下界**不推** —— 可用时段说的是
    "我什么时候有空",它推不出"我什么时候起床"。一个只在周末白天有空的用户,时段是
    09:00–17:00,拿它的开始时刻当免打扰下界,等于凌晨三点把他叫醒。
    """
    account = await make_account()

    default = (await _reminders(app_client, account))["quietHours"]
    assert default["source"] == "default"
    assert default["fromMinute"] == 22 * 60
    assert default["toMinute"] == 8 * 60
    assert default["description"] == "22:00–08:00"

    db.add(
        AvailabilityRule(
            user_id=uuid.UUID(account.id),
            weekday=0,
            start_minute=19 * 60,
            end_minute=23 * 60 + 30,
        )
    )
    await db.commit()

    derived = (await _reminders(app_client, account))["quietHours"]
    assert derived["source"] == "availability", derived
    assert derived["fromMinute"] == 23 * 60 + 30, "上界应当取用户可用时段里最晚的结束时刻"
    assert derived["toMinute"] == 8 * 60, "下界不从可用时段推"
    assert derived["description"] == "23:30–08:00"


# ---------------------------------------------------------------------------------
# 4. 免打扰只压住催促
# ---------------------------------------------------------------------------------
async def test_quiet_hours_suppress_nudges_but_not_pending_things(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db, monkeypatch
) -> None:
    """深夜:催人的那条被压住并如实报数,而"有计划在等你"照常显示。

    这两类提醒的区别是这里要验的全部内容。如果一律压住,一个只在深夜有时间规划的用户
    永远看不到自己的提醒;如果一律不压,用户会在凌晨被"你这周有 3 场没记录"叫住。
    """
    account = await _account_with_schedule(app_client, make_account, use_reasoner)

    plan = (
        await app_client.get(
            f"/api/workspaces/{account.workspace_id}/plan", headers=account.headers
        )
    ).json()
    stage = next(node for node in plan["nodes"] if node["nodeType"] == "stage")
    await db.execute(
        update(PlanNode)
        .where(PlanNode.id == uuid.UUID(stage["id"]))
        .values(status=NodeStatus.COMPLETED, completed_at=utcnow())
    )
    await db.commit()

    # 周末深夜 23:00:周末提醒("这周有 N 场没记录")该被压住,阶段完成不该被压住。
    monkeypatch.setattr(reminder_service, "now_in", lambda _tz: _next_weekend_at(23))

    night = await _reminders(app_client, account)
    assert night["quietHours"]["active"] is True, night["quietHours"]
    assert "weekend" not in _kinds(night), "深夜不该催「这周还有几场没记录」"
    assert "stage_completed" in _kinds(night), "有事情在等你,不该被免打扰吞掉"
    assert night["suppressedCount"] >= 1
    assert "免打扰" in night["note"], (
        "被压住的条数必须说出来 —— 否则用户会把「现在没有提醒」读成「系统认为一切正常」"
    )

    # 同一个周末的白天:免打扰不生效,那条催促就出现了。
    monkeypatch.setattr(reminder_service, "now_in", lambda _tz: _next_weekend_at(12))

    day = await _reminders(app_client, account)
    assert day["quietHours"]["active"] is False, day["quietHours"]
    assert "weekend" in _kinds(day), day
    assert day["suppressedCount"] == 0


# ---------------------------------------------------------------------------------
# 5. "连续漏做"只由用户报过的结果触发
# ---------------------------------------------------------------------------------
async def test_repeated_skips_needs_recorded_results(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """没有任何记录时不提醒;记录两次"没做"之后才提醒。

    两段式是刻意的。只断言后半段的话,一个"凡是有过去的场次就提醒"的错误实现也能通过
    —— 而那正是把"不知道"当成"没做到"。
    """
    account = await _account_with_schedule(app_client, make_account, use_reasoner)
    sessions = await _planned_sessions(app_client, account)
    assert len(sessions) >= 2, "这个测试需要至少两场安排"

    # 第一段:一场都没记录 —— 不算漏做。
    quiet = await _reminders(app_client, account)
    assert "repeated_skips" not in _kinds(quiet), (
        "没有任何记录就报「连续漏做」,等于把不知道当成没做到"
    )

    # 第二段:用户自己说了两次"这场没做"。
    for index, item in enumerate(sessions[:2]):
        response = await app_client.post(
            f"/api/sessions/{item['id']}/executions",
            json={"result": "skipped", "idempotencyKey": f"exec-key-remind-skip-{index}"},
            headers=account.headers,
        )
        assert response.status_code == 200, response.text

    noisy = await _reminders(app_client, account)
    assert "repeated_skips" in _kinds(noisy), noisy
    reminder = next(item for item in noisy["reminders"] if item["kind"] == "repeated_skips")
    assert "按执行情况调整" in reminder["body"], "提醒要指向那个能解决它的动作"


# ---------------------------------------------------------------------------------
# 6. 隔了几天没来
# ---------------------------------------------------------------------------------
async def test_user_returned_after_a_gap(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db
) -> None:
    """新建账号不提醒"你回来了";说过话又隔了五天,才提醒。

    "没有可回来的过去"与"确实离开了几天"是两件事。一个刚注册的用户收到"有 5 天没有
    你的消息了"会立刻失去对整套提醒的信任。
    """
    account = await _account_with_schedule(app_client, make_account, use_reasoner)

    fresh = await _reminders(app_client, account)
    assert "user_returned" not in _kinds(fresh), fresh

    await db.execute(
        update(Message)
        .where(Message.workspace_id == uuid.UUID(account.workspace_id))
        .values(created_at=utcnow() - timedelta(days=5))
    )
    await db.commit()

    back = await _reminders(app_client, account)
    assert "user_returned" in _kinds(back), back
    reminder = next(item for item in back["reminders"] if item["kind"] == "user_returned")
    assert "5 天" in reminder["title"]
    assert reminder["forDate"] == today_in("Asia/Shanghai").isoformat()


async def test_scheduled_sessions_are_not_touched_by_reminders(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db
) -> None:
    """读提醒**不写任何业务数据** —— 它就是一次查询。

    这条断言防的是一种很自然的滑坡:提醒要"知道用户关掉了哪条",于是顺手把状态写回
    场次行或节点上。那样一来"打开界面"就成了一次写入,而它会被审计、会推进 revision、
    会让待确认的提案失效 —— 用户只是看了一眼提醒。
    """
    account = await _account_with_schedule(app_client, make_account, use_reasoner)

    before = (
        await db.execute(
            select(ScheduledSession.status, ScheduledSession.updated_at).where(
                ScheduledSession.workspace_id == uuid.UUID(account.workspace_id)
            )
        )
    ).all()

    assert (await _reminders(app_client, account))["reminders"] is not None
    await db.rollback()

    after = (
        await db.execute(
            select(ScheduledSession.status, ScheduledSession.updated_at).where(
                ScheduledSession.workspace_id == uuid.UUID(account.workspace_id)
            )
        )
    ).all()
    assert after == before, "读提醒改动了场次行"
