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

时间在这里是要被**指定**的:`use_clock` 把 `get_now`(见 `api/dependencies/clock.py`)
钉在一个固定的绝对时刻上。因为免打扰的"此刻"和周末判断都依赖钟点,拿真实时钟当输入
的话,测的就不是规则,而是"我们碰巧在哪个钟点跑的它" —— 22:00–08:00 之间跑,安静的
用例必红;换个钟点跑,压制的用例必红。两边都是确定的,只是判据挂在墙上。

注入的是**绝对时刻**,不是本地时间:用户的时区仍然是账号自己的属性(`user.timezone`),
"同一个瞬间,两个时区的用户一个在免打扰里、一个不在"是产品行为,得能验。

行的时间戳则**相对于被注入的那一刻**来造(比如"把这句话挪到那一刻的 5 天前"),而不是
相对于真实此刻 —— 两者差几小时,`(now - last).days` 就会在 4 和 5 之间跳。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select, update

from backend.db.models import AvailabilityRule, Message, ReminderState, ScheduledSession
from backend.services.timeutil import resolve_zone
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


def _next_day_at(hour: int, minute: int = 0, *, tz: str = "Asia/Shanghai") -> datetime:
    """明天这个钟点,按给定时区。**这就是被注入的"此刻"。**

    两个"不用"值得记下来:

    - **不用固定的日历时刻**(比如硬写 `2026-03-10 12:00`)。库里的行是**刚刚**建出来的,
      一个早于它们的"此刻"会让 `now - created_at` 变成负数 —— 那不是真实会发生的状态,
      在它上面做断言等于在验一个不存在的世界。
    - **不用真实现在的小时数**。那样这些用例验的是"跑测试的钟点",而不是免打扰规则。

    "明天"同时满足这两条:它严格晚于刚写进库的那些行,而钟点完全由参数决定。
    """
    zone = resolve_zone(tz)
    return (datetime.now(zone) + timedelta(days=1)).replace(
        hour=hour, minute=minute, second=0, microsecond=0
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
    app_client: httpx.AsyncClient, make_account, use_clock
) -> None:
    """稍后 24 小时 -> 现在不出现;到点之后**重新出现**。

    "到点重新出现"这一半才是重点。把稍后实现成"软性关掉"最省事,也最伤:用户点
    "稍后"的意思是"过会儿再跟我说",不是"这事别再提了"。

    时间在这里是**走过去的**,不是把库里的到期时刻改到过去:注入的"此刻"往前挪
    24 小时零 1 分,被压住的那条就该回来。这样这个用例同时钉住了另一件事 ——
    **落库的到期时刻和之后拿它比大小的"此刻"来自同一个钟**:点"稍后"的那一次请求
    和读提醒的这几次请求,两边都由 `get_now` 决定。各取各的钟的话,24 小时会凭空
    变成"已经过期"或者"永远不到期",而下面两条断言都会红。
    """
    account = await make_account()
    noon = _next_day_at(12)
    use_clock(noon)

    before = await _reminders(app_client, account)
    key = next(item["key"] for item in before["reminders"] if item["kind"] == "workspace_empty")

    snoozed = await app_client.post(
        "/api/reminders/snooze", json={"key": key, "hours": 24}, headers=account.headers
    )
    assert snoozed.status_code == 200, snoozed.text
    until = snoozed.json()["snoozedUntil"]
    assert until is not None
    assert datetime.fromisoformat(until) == noon + timedelta(hours=24), (
        "到期时刻不是从被注入的那一刻算的 —— 那说明这条写入路径自己在取真实时钟"
    )

    assert "workspace_empty" not in _kinds(await _reminders(app_client, account))

    # 还差一分钟:仍然安静。
    use_clock(noon + timedelta(hours=23, minutes=59))
    assert "workspace_empty" not in _kinds(await _reminders(app_client, account))

    # 过了那一刻:回来。**不睡 24 小时** —— 挪的是钟。
    use_clock(noon + timedelta(hours=24, minutes=1))
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
    app_client: httpx.AsyncClient, make_account, use_reasoner, db, use_clock
) -> None:
    """深夜:催人的那条被压住并如实报数,而"有事情在等你"照常显示。

    这两类提醒的区别是这里要验的全部内容。如果一律压住,一个只在深夜有时间规划的用户
    永远看不到自己的提醒;如果一律不压,用户会在凌晨被"你已经 5 天没来了"叫住。

    这个账号是**故意**造得这么素的:一句话说过、一个空空间。于是"这一刻会有哪些候选"
    是确定的 —— 一条催促(`user_returned`)、一条陈述(`workspace_empty`),没有周末提醒
    (从没排过场次),也没有计划更新。用例要断言的是**压制的边界**,输入越少越好。
    """
    account = await make_account()
    use_reasoner(FakeReasoner())
    posted = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/messages",
        json={"content": "我回来了,先看看"},
        headers=account.headers,
    )
    assert posted.status_code == 200, posted.text

    day = _next_day_at(0)
    night = day.replace(hour=23)
    noon = day.replace(hour=12)

    # 把用户说过的那句话挪到**被注入那一刻的 5 天前** —— 这是"确实离开了几天"在库里
    # 唯一的样子。相对注入的时刻算,而不是相对真实此刻:两个钟点差几小时,`.days`
    # 就会在 4 和 5 之间跳。
    await db.execute(
        update(Message)
        .where(Message.workspace_id == uuid.UUID(account.workspace_id))
        .values(created_at=night.astimezone(UTC) - timedelta(days=5))
    )
    await db.commit()

    use_clock(night)
    deep_night = await _reminders(app_client, account)
    assert deep_night["quietHours"]["active"] is True, deep_night["quietHours"]
    assert "user_returned" not in _kinds(deep_night), "深夜不该催「你已经几天没来了」"
    assert "workspace_empty" in _kinds(deep_night), (
        "「有事情在等你」不该被免打扰吞掉 —— 否则一个只在深夜有时间规划的用户"
        "永远看不到自己的提醒"
    )
    assert deep_night["suppressedCount"] == 1, deep_night
    assert "免打扰" in deep_night["note"], (
        "被压住的条数必须说出来 —— 否则用户会把「现在没有提醒」读成「系统认为一切正常」"
    )

    # 同一个账号、同一句话,换成白天:免打扰不生效,那条催促就出现了。
    use_clock(noon)
    daytime = await _reminders(app_client, account)
    assert daytime["quietHours"]["active"] is False, daytime["quietHours"]
    assert "user_returned" in _kinds(daytime), daytime
    assert daytime["suppressedCount"] == 0, daytime


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
    app_client: httpx.AsyncClient, make_account, use_reasoner, db, use_clock
) -> None:
    """说过话但还很近 -> 不提醒;同一句话挪到五天前 -> 提醒,而且说的是 5 天。

    "没有可回来的过去"与"确实离开了几天"是两件事。一个刚注册的用户收到"有 5 天没有
    你的消息了"会立刻失去对整套提醒的信任。

    钟点由 `use_clock` 指定,不是真实此刻:这条提醒属于会被免打扰压住的那一类,所以
    它在 22:00–08:00 之间必红 —— 而它**是规则对**,不是代码错。要验规则,就得能指定
    那一刻。
    """
    account = await _account_with_schedule(app_client, make_account, use_reasoner)

    noon = _next_day_at(12)
    use_clock(noon)

    fresh = await _reminders(app_client, account)
    assert "user_returned" not in _kinds(fresh), fresh

    # 挪到**被注入那一刻的 5 天前**。相对真实此刻算的话,两个钟点差几小时,
    # `(now - last).days` 会在 4 和 5 之间跳,而下面断言的是"5 天"。
    await db.execute(
        update(Message)
        .where(Message.workspace_id == uuid.UUID(account.workspace_id))
        .values(created_at=noon.astimezone(UTC) - timedelta(days=5))
    )
    await db.commit()

    back = await _reminders(app_client, account)
    assert "user_returned" in _kinds(back), back
    reminder = next(item for item in back["reminders"] if item["kind"] == "user_returned")
    assert "5 天" in reminder["title"]
    assert reminder["forDate"] == noon.date().isoformat()


async def test_quiet_hours_boundaries(
    app_client: httpx.AsyncClient, make_account, use_clock
) -> None:
    """边界:21:59 不算免打扰、22:00 算,07:59 算、08:00 不算。

    这四个值里,22:00 与 08:00 就是那个最容易写错的地方 —— 判据写成
    `minute > start or minute < end` 的话,整点那一分钟会漏掉,而它恰恰是用户说的
    "该休息了"。手工点界面几乎撞不上这一分钟,所以这里逐个钉住。

    这个用例只看 `quietHours`,所以用一个空账号就够 —— 不需要任何提醒真的出现。
    """
    account = await make_account()
    afternoon = _next_day_at(21, 59)

    for moment, expected in (
        (afternoon, False),
        (afternoon.replace(hour=22, minute=0), True),
        (afternoon.replace(hour=7, minute=59), True),
        (afternoon.replace(hour=8, minute=0), False),
    ):
        use_clock(moment)
        quiet = (await _reminders(app_client, account))["quietHours"]
        assert quiet["active"] is expected, f"{moment:%H:%M} 的免打扰判断反了:{quiet}"


async def test_quiet_hours_follow_the_users_own_timezone(
    app_client: httpx.AsyncClient, make_account, use_clock
) -> None:
    """同一个绝对时刻,两个时区的账号:一个在免打扰里,一个不在。

    免打扰说的是**用户那边的晚上**。拿服务器时间(或 UTC)去比的话,东八区的用户会在
    下午被免打扰、在半夜被提醒 —— 而界面上没有任何东西会显示这件事,用户只会觉得
    "这软件有时提醒我有时不提醒"。

    两个账号拿到的**窗口是同一个**("22:00–08:00"),差别全部来自 `user.timezone`。
    这半句断言是刻意的:否则一个"窗口也跟着用户漂"的实现也能让上面两条通过。
    """
    shanghai = await make_account()
    utc = await make_account(email="utc@example.com", timezone="UTC")

    # 东八区的 23:00 就是 UTC 的 15:00 —— 同一个瞬间。
    night_in_china = _next_day_at(23, tz="Asia/Shanghai")
    use_clock(night_in_china)

    sh = (await _reminders(app_client, shanghai))["quietHours"]
    other = (await _reminders(app_client, utc))["quietHours"]
    assert sh["active"] is True, sh
    assert other["active"] is False, other
    assert sh["description"] == other["description"], (sh, other)
    assert sh["fromMinute"] == other["fromMinute"], (sh, other)


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
