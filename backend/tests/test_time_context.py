"""模型看到的时间底盘 —— 以及**它看不到的那部分有没有被说出来**。

## 这一批修的是"时间能力"最贵的那一种错

排期不由模型决定,这一点在提示词里早就写清楚了。真正缺的是它**看得见**的东西:
它看不到这个人每周有多少分钟、哪天有空、日历上已经排了什么、上次实际花了多久。
于是"时间不够"这件事它只能说成一句没有依据的担心,而"够不够"它连判断的材料都没有。

所以这一版给它的全是**事实**:预算、可用时段、已排的场次、做过的记录。**一条结论都没有**
—— 缺口要它自己把预计工时加起来算,而那是它的一次估算,不是系统的判决。

## 三种"没有"必须分开,这是本文件的重点

    ① 这次没读时间信息(手工构造的 TurnContext)
    ② 个人容量表里没有那一行 —— 数字来自默认值,不是用户设的
    ③ 没记过可用时段 / 没排过场次 / 没做过任何事

它们全都是"空",但把任意两个混起来都会造出一句假话:把 ② 当成"用户说他每周 600 分钟",
把 ③ 当成"他很闲",把 ① 当成"他没有预算"。所以这里逐种钉一条断言,而不是抽一条
`assert "时间" in prompt` 了事。

## 还有一条:截断过就必须说

场次、执行记录、可用时段都有行数上限。只印列出来的那几条,模型会把"我看到 12 场"
当成"一共 12 场",然后据此说"这周还挺空"。所以每一段都带总数,截断时另有一句
"上面列了 M 条"。这里用 `sessions_total=30` 之类的构造直接验那句话。
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
from sqlalchemy import func, select

from backend.agent.prompts.planning import (
    MAX_SESSION_ROWS,
    SYSTEM_PROMPT,
    render_time_section,
)
from backend.agent.runtime.base import (
    AvailableWindowView,
    ExecutionFactView,
    SessionFactView,
    TimeView,
)
from backend.agent.runtime.response import render_turn
from backend.db.models import (
    AvailabilityException,
    AvailabilityRule,
    ExecutionRecord,
    PlanNode,
    ScheduledSession,
    UserCapacityProfile,
)
from backend.db.models.enums import ExecutionResult, ScheduledSessionStatus
from backend.db.session import SessionLocal
from backend.tests.conftest import FakeReasoner


# ---------------------------------------------------------------------------------
# 助手
# ---------------------------------------------------------------------------------
async def _send(client: httpx.AsyncClient, account, content: str, **extra) -> httpx.Response:
    return await client.post(
        f"/api/workspaces/{account.workspace_id}/messages",
        json={"content": content, **extra},
        headers=account.headers,
    )


async def _turn(fake):
    assert fake.calls, "假模型没有被调用 —— 依赖注入漏了"
    return fake.calls[-1]


async def _root_id(client: httpx.AsyncClient, account) -> str:
    response = await client.get(
        f"/api/workspaces/{account.workspace_id}/plan", headers=account.headers
    )
    assert response.status_code == 200, response.text
    return next(
        node["id"] for node in response.json()["nodes"] if node["parentId"] is None
    )


async def _add(session, row) -> None:
    session.add(row)


def _session(
    account,
    workspace_id: str,
    node_id: str,
    day: date,
    minutes: int,
    *,
    status: ScheduledSessionStatus = ScheduledSessionStatus.PLANNED,
) -> ScheduledSession:
    return ScheduledSession(
        user_id=uuid.UUID(account.id),
        workspace_id=uuid.UUID(workspace_id),
        node_id=uuid.UUID(node_id),
        scheduled_date=day,
        planned_minutes=minutes,
        seq=0,
        status=status,
    )


async def _other_workspace(client: httpx.AsyncClient, account) -> tuple[str, str]:
    """再建一个空间,返回 `(空间 id, 根节点 id)`。

    "时间池按人算"这条只有在**真的有两个空间**时才验得出来,所以这个空间必须走真实
    接口建出来,不能只往库里插一行。
    """
    created = await client.post(
        "/api/workspaces",
        json={"title": "另一个空间", "intent": "另一件事"},
        headers=account.headers,
    )
    assert created.status_code == 201, created.text
    workspace_id = created.json()["workspace"]["id"]
    plan = await client.get(f"/api/workspaces/{workspace_id}/plan", headers=account.headers)
    assert plan.status_code == 200, plan.text
    root_id = next(node["id"] for node in plan.json()["nodes"] if node["parentId"] is None)
    return workspace_id, root_id


async def _write(build) -> None:
    """直接写库,另起一个会话、写完就提交。

    **不用 `db` fixture。** 那个会话会把一个写事务握到测试结束,而 HTTP 那一路走的是
    另一条连接 —— SQLite 上表现为 `database is locked`,而这条报错和"我插了一条排期"
    看起来毫无关系。这里要造的几种状态(容量、可用时段、场次、执行记录)目前都没有
    对应的接口,只能直接写,所以更要把写法固定下来。
    """
    async with SessionLocal() as session:
        await build(session)
        await session.commit()


async def _counts() -> dict[str, int]:
    async with SessionLocal() as session:
        return {
            name: int(
                await session.scalar(select(func.count()).select_from(model.__table__)) or 0
            )
            for name, model in (
                ("sessions", ScheduledSession),
                ("executions", ExecutionRecord),
                ("rules", AvailabilityRule),
                ("profiles", UserCapacityProfile),
            )
        }


def _time(**overrides) -> TimeView:
    """一份"什么都有"的时间底盘。逐条测试只改它关心的那一项。

    默认值刻意选成**有内容**的那种:一个全空的默认值会让"这一条断言真的在测那件事"
    变成"它在测一个空对象",而空对象对什么断言都成立不了。
    """
    base: dict[str, object] = {
        "horizon_days": 84,
        "horizon_last_day": "2026-12-20",
        "horizon_at_limit": False,
        "capacity_minutes": 5760,
        "weekly_total_minutes": 600,
        "weekly_budget_minutes": 480,
        "safety_factor": "0.80",
        "daily_cap_minutes": 69,
        "min_session_minutes": 15,
        "max_session_minutes": 120,
        "default_buffer_minutes": 10,
        "capacity_configured": True,
        # 需求那一侧也给上内容:默认全 0 的话,那两条渲染分支在别的测试里根本走不到,
        # 而"这一条断言真的在测那件事"就变成了"它在测一个空值"。
        "open_task_minutes": 1440,
        "open_tasks_without_estimate": 0,
        "other_active_workspaces": 0,
    }
    base.update(overrides)
    return TimeView(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------------
# 三种"没有"各说各的话
# ---------------------------------------------------------------------------------
def test_no_time_view_at_all_forbids_conclusions() -> None:
    """这一轮压根没读时间信息 —— 那就明说,并且禁止下结论。

    留白的话,模型会把"我没看到时间"当成"没有时间限制",然后给一个自己都没底的
    可行性判断 —— 那是这一批要修的毛病换了个地方复发。
    """
    section = render_time_section(None)

    assert "不要对" in section
    assert "没有读取时间信息" in section


def test_an_unconfigured_capacity_does_not_pretend_to_be_the_users_number() -> None:
    """个人容量表里没有那一行时,那 600 分钟是**系统的默认值**,不是用户说的。

    注册时刻意不建这一行(见 `auth_service`),所以这是绝大多数账号的真实状态。
    把默认值印成一个光秃秃的数字,模型下一句就是"按你每周 600 分钟……" ——
    而用户从没说过 600。
    """
    section = render_time_section(_time(capacity_configured=False, weekly_total_minutes=600))

    assert "个人容量表里没有设置" in section
    assert "不是用户说的数字" in section
    assert "你不知道他每周有多少时间" in section


def test_a_configured_capacity_says_it_is_the_users_own_setting() -> None:
    """用户设过的时候,要说成"用户设置的",并且把两个数都给出来。

    只给总量不给乘过安全系数的那个,模型会拿 600 去和预计工时比 —— 而真正生效的是
    480(见 `scheduler.calendar.weekly_budget`:系数在整份算法里只乘这一次)。
    """
    section = render_time_section(
        _time(capacity_configured=True, weekly_total_minutes=1000, safety_factor="0.50",
              weekly_budget_minutes=500)
    )

    assert "用户设置的每周总量:1000 分钟" in section
    assert "0.50" in section
    assert "实际按 500 分钟/周 排" in section


def test_no_availability_is_not_the_same_as_free_every_evening() -> None:
    """一条可用时段都没记过时,要说"只知总量、不知哪几天"。

    这一条直接挡掉"那我们把这件事安排在周三晚上" —— 那种话在没读过可用时段时
    只能是编的。
    """
    section = render_time_section(_time())

    assert "一条都没记过" in section
    assert "不知道具体哪几天" in section


# ---------------------------------------------------------------------------------
# 截断:列出来的和一共的,是两个数
# ---------------------------------------------------------------------------------
def test_a_truncated_session_list_still_reports_the_total() -> None:
    """列了 12 场,一共 30 场 —— 两个数都要在,否则模型会以为日历上就这 12 场。"""
    shown = tuple(
        SessionFactView(handle=f"n{i}", day="2026-10-01", minutes=60, status="planned")
        for i in range(MAX_SESSION_ROWS)
    )
    section = render_time_section(_time(sessions=shown, sessions_total=30))

    assert f"上面列了 {MAX_SESSION_ROWS} 场" in section
    assert "共 30 场" in section


def test_a_truncated_execution_list_still_reports_the_total() -> None:
    shown = (ExecutionFactView(handle="n1", result="completed", actual_minutes=45),)
    section = render_time_section(_time(executions=shown, executions_total=9))

    assert "共 9 条" in section


def test_a_truncated_availability_list_still_reports_the_total() -> None:
    shown = (AvailableWindowView(weekday=2, start_minute=19 * 60, end_minute=22 * 60),)
    section = render_time_section(_time(windows=shown, windows_total=6))

    assert "可用时段共 6 条" in section


def test_an_empty_calendar_is_not_reported_as_a_free_week() -> None:
    """一场都没排过 —— 这**不等于**时间很空。可能是还没排过。

    把"空"直接读成"闲"是最自然也最伤人的一次误判:它会让模型在一个被别的空间
    占满的星期里说"这里还很空"。
    """
    section = render_time_section(_time())

    assert "这不等于「时间很空」" in section


def test_sessions_in_other_workspaces_are_named() -> None:
    """别的空间还排着场次 —— 时间池是按人算的,必须说出来。"""
    section = render_time_section(_time(sessions_other_workspaces=3))

    assert "别的空间还排着 3 场" in section


def test_a_locked_session_is_marked_locked() -> None:
    """锁定过的场次不能被自动挪走,它必须能被看见。"""
    shown = (
        SessionFactView(
            handle="n2", day="2026-10-03", minutes=90, status="planned", locked=True
        ),
    )
    section = render_time_section(_time(sessions=shown, sessions_total=1))

    assert "n2 2026-10-03 90 分钟(planned,用户锁定)" in section


def test_a_horizon_at_the_limit_is_named() -> None:
    """视界到了上限时必须说出来,否则那个容量会被读成"总共能拿出多少"。"""
    assert "视界已经到上限" in render_time_section(_time(horizon_at_limit=True))
    assert "视界已经到上限" not in render_time_section(_time(horizon_at_limit=False))


def test_the_capacity_number_is_not_a_verdict_about_the_plan() -> None:
    """那个容量数字必须写明它是"能拿出多少",不是"这份计划排得开"。

    这一条是本文件里最要紧的一句:模型最容易犯的错就是把总容量念成结论,而用户
    听到"够"之后不会再去「排期」里看一眼。
    """
    section = render_time_section(_time(capacity_minutes=5760))

    assert "5760 分钟" in section
    assert "不是「这份计划排不排得开」" in section


def test_the_workload_total_is_not_a_verdict_about_the_plan() -> None:
    """需求侧那个合计也是一个数,不是结论 —— 和容量那一侧一样要说清楚。

    服务端替模型把几十个节点加起来,是为了不让它自己算错;但"加起来"和"排得开"之间
    隔着安全系数、缓冲和前置关系,后者只在「排期」预览里算。数字好看了就当结论说,
    是这一批最想拦住的那句话。
    """
    section = render_time_section(_time(open_task_minutes=1440))

    assert "1440 分钟" in section
    assert "不是结论" in section


def test_a_task_without_an_estimate_makes_the_total_a_lower_bound() -> None:
    """有任务没填预计工时 —— 那个合计只是**下限**,必须当场说出来。

    不说的话,"一共 1440 分钟"会被读成"他一共要做这么多",而真实工作量只会更大。
    少报的量恰好是模型最容易用来下"排得开"结论的那一部分。
    """
    with_gap = render_time_section(
        _time(open_task_minutes=1440, open_tasks_without_estimate=3)
    )
    assert "3 个任务**还没填预计工时**" in with_gap
    assert "下限" in with_gap

    without_gap = render_time_section(
        _time(open_task_minutes=1440, open_tasks_without_estimate=0)
    )
    assert "下限" not in without_gap, "没有缺口却报了个下限,那也是在编"


def test_a_zero_workload_still_states_the_rule_it_was_counted_by() -> None:
    """一个待做任务都没有时,也要说出**口径** —— 否则会被读成"你没别的事要做"。

    它只数了这个空间里、类型是任务、状态是待做/进行中的节点。目标不算,别的空间不算,
    已经做完的不算。口径不说清楚,那个 0 就变成了一句关于用户生活的断言。
    """
    section = render_time_section(_time(open_task_minutes=0))

    assert "没有还没做完的任务" in section
    assert "口径" in section


def test_work_in_other_workspaces_is_named_as_sharing_the_same_time() -> None:
    """别的空间里的任务不在这个合计里 —— 但它们花的是同一份时间。

    只印"合计 1440 分钟"而不说还有别的空间,模型会把这个数当成这个人的全部工作量。
    而容量那一侧是按**人**算的、跨空间共用的,两边口径不一致就会推出"他很空"。
    """
    section = render_time_section(_time(other_active_workspaces=2))

    assert "还有 2 个活动中的空间" in section
    assert "不在上面的合计里" in section
    assert "同一份时间" in section

    assert "活动中的空间" not in render_time_section(_time(other_active_workspaces=0))


# ---------------------------------------------------------------------------------
# 提示词里的硬规矩
# ---------------------------------------------------------------------------------
def test_the_system_prompt_forbids_saying_the_schedule_was_changed() -> None:
    """不许说"我已经调整了日程"。"""
    assert "已经调整了日程" in SYSTEM_PROMPT
    assert "不能说成已经发生" in SYSTEM_PROMPT


def test_the_system_prompt_says_the_time_section_is_read_only() -> None:
    assert "它是事实,不是你的权限" in SYSTEM_PROMPT


def test_the_system_prompt_forbids_claiming_a_check_was_run() -> None:
    """没有跑过任何工时/冲突检查 —— 不许说"已经检查过"。

    这一条对应一个具体的陷阱:`proposals` 表里那两列 `workload_check` /
    `conflict_check` 到今天**没有任何代码写它们**,也没有任何契约暴露它们。所以现在
    还没有人说"已通过",但它离被误用只差一次顺手渲染 —— 规矩先写进提示词。
    """
    assert "我已经检查过工时" in SYSTEM_PROMPT
    assert "没有发现问题" in SYSTEM_PROMPT


def test_the_rendered_turn_carries_the_time_section() -> None:
    """整段提示词里那一段真的在,而且标题就是提示词里引用的那一个。"""
    turn = _plain_turn(time=_time())
    prompt = render_turn(turn)

    assert "## 时间与已经排进去的安排(只读)" in prompt
    assert "### 每周能投入多少" in prompt


def _plain_turn(**overrides):
    """一个最小的 TurnContext。只用来验渲染,不碰数据库。"""
    from backend.agent.runtime.base import KnownConditions, TurnContext

    base: dict[str, object] = {
        "current_date": "2026-09-27",
        "weekday": "日",
        "timezone": "Asia/Shanghai",
        "workspace_title": "测试空间",
        "workspace_intent": "把一件事做完",
        "known": KnownConditions(),
    }
    base.update(overrides)
    return TurnContext(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------------
# 从真实数据库读出来的那一份
# ---------------------------------------------------------------------------------
async def test_a_fresh_account_has_no_capacity_and_the_model_is_told_so(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """刚注册的账号:容量表里没有那一行。模型看到的必须是"没有设置",不是 600。

    **这条走的是真实接口**,不是构造一个 TimeView。理由和别处一样:自己构造出来的
    状态证明不了服务端会怎么填它。
    """
    account = await make_account()
    fake = use_reasoner(FakeReasoner(reply="好。"))

    assert (await _send(app_client, account, "我想学 Python")).status_code == 200
    time = (await _turn(fake)).time

    assert time is not None
    assert time.capacity_configured is False
    assert "个人容量表里没有设置" in render_time_section(time)


async def test_the_users_own_capacity_profile_becomes_the_budget(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """用户在设置里填过容量之后,模型看到的是**乘过安全系数**的那个数。"""
    account = await make_account()
    await _write(
        lambda session: _add(
            session,
            UserCapacityProfile(
                user_id=uuid.UUID(account.id),
                weekly_total_minutes=1000,
                safety_factor=Decimal("0.50"),
                daily_max_minutes=180,
            ),
        )
    )

    fake = use_reasoner(FakeReasoner(reply="好。"))
    assert (await _send(app_client, account, "继续")).status_code == 200
    time = (await _turn(fake)).time

    assert time.capacity_configured is True
    assert time.weekly_total_minutes == 1000
    assert time.weekly_budget_minutes == 500, "安全系数没有生效,或者被乘了两次"
    assert time.daily_cap_minutes == 180


async def test_an_already_scheduled_session_reaches_the_model(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """日历上已经排好的场次,模型要看得见它挂在哪个节点上、多少分钟。"""
    account = await make_account()
    root_id = await _root_id(app_client, account)
    await _write(
        lambda session: _add(
            session,
            _session(account, account.workspace_id, root_id, date(2026, 10, 5), 90),
        )
    )

    fake = use_reasoner(FakeReasoner(reply="好。"))
    assert (await _send(app_client, account, "这周还排得下吗")).status_code == 200
    time = (await _turn(fake)).time

    assert time.sessions_total == 1
    assert len(time.sessions) == 1
    assert time.sessions[0].day == "2026-10-05"
    assert time.sessions[0].minutes == 90
    assert time.sessions[0].handle.startswith("n"), "场次没有带上记号,模型引用不了它"

    # 场次行里带着真实节点 id,而提示词里**只能出现记号**。这一条与节点那几条是同一个
    # 不变量:模型看不见真实主键,它就没有办法指涉一个它没见过的节点。
    prompt = render_turn(await _turn(fake))
    assert root_id not in prompt
    assert str(uuid.UUID(root_id)) not in prompt


async def test_a_day_off_reaches_the_model(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """请过假的那一天要说出来 —— 否则模型会在那一天安排事情。

    "下周三是空的"这种结论只有在**例外也被读到**时才成立。
    """
    account = await make_account()
    await _write(
        lambda session: _add(
            session,
            AvailabilityException(
                user_id=uuid.UUID(account.id),
                on_date=date(2026, 10, 7),
                is_unavailable=True,
            ),
        )
    )

    fake = use_reasoner(FakeReasoner(reply="好。"))
    assert (await _send(app_client, account, "下周安排一下")).status_code == 200
    prompt = render_turn(await _turn(fake))

    assert "2026-10-07 整天不可用" in prompt


async def test_an_execution_record_reaches_the_model(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """做过的记录要看得见 —— 用户说"我做不完"时,上次实际花了多久是最硬的依据。"""
    account = await make_account()
    root_id = await _root_id(app_client, account)
    await _write(
        lambda session: _add(
            session,
            ExecutionRecord(
                user_id=uuid.UUID(account.id),
                workspace_id=uuid.UUID(account.workspace_id),
                node_id=uuid.UUID(root_id),
                result=ExecutionResult.PARTIAL,
                actual_minutes=45,
                created_at=datetime(2026, 9, 26, 12, 0, tzinfo=UTC),
            ),
        )
    )

    fake = use_reasoner(FakeReasoner(reply="好。"))
    assert (await _send(app_client, account, "我做不完")).status_code == 200
    time = (await _turn(fake)).time

    assert time.executions_total == 1
    assert time.executions[0].result == "partial"
    assert time.executions[0].actual_minutes == 45


async def test_availability_rules_reach_the_prompt(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """记过可用时段之后,"周三晚上"这句话才说得出来。"""
    account = await make_account()
    await _write(
        lambda session: _add(
            session,
            AvailabilityRule(
                user_id=uuid.UUID(account.id),
                weekday=2,  # 周三
                start_minute=19 * 60,
                end_minute=22 * 60,
            ),
        )
    )

    fake = use_reasoner(FakeReasoner(reply="好。"))
    assert (await _send(app_client, account, "帮我安排一下")).status_code == 200
    prompt = render_turn(await _turn(fake))

    assert "每周三 19:00–22:00" in prompt
    assert "一条都没记过" not in prompt


async def test_sessions_in_another_workspace_are_counted_not_hidden(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """另一个空间里排着的场次要被数出来 —— 时间池是这个人共用的。

    没有这个数,模型会以为每个子空间各有一份每周预算,于是对一个已经被别的空间
    占满的星期说"这里还很空"。**跨空间这件事在界面上完全看不出来**,
    只有这句话能说破它。
    """
    account = await make_account()
    other_id, other_root = await _other_workspace(app_client, account)
    await _write(
        lambda session: _add(
            session,
            _session(account, other_id, other_root, date(2026, 10, 6), 60),
        )
    )

    fake = use_reasoner(FakeReasoner(reply="好。"))
    assert (await _send(app_client, account, "这周还有空吗")).status_code == 200
    time = (await _turn(fake)).time

    assert time.sessions_total == 0, "别的空间的场次不该混进当前空间那一栏"
    assert time.sessions_other_workspaces == 1
    assert "别的空间还排着 1 场" in render_time_section(time)


async def test_a_cancelled_session_elsewhere_does_not_occupy(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """取消掉的场次不占时间,所以不该被算进"别的空间还排着"。

    这条挡的是"取消了一场,AI 却说那个晚上还是满的"。
    """
    account = await make_account()
    other_id, other_root = await _other_workspace(app_client, account)
    await _write(
        lambda session: _add(
            session,
            _session(
                account,
                other_id,
                other_root,
                date(2026, 10, 6),
                60,
                status=ScheduledSessionStatus.CANCELED,
            ),
        )
    )

    fake = use_reasoner(FakeReasoner(reply="好。"))
    assert (await _send(app_client, account, "这周还有空吗")).status_code == 200
    assert (await _turn(fake)).time.sessions_other_workspaces == 0


async def test_a_session_on_an_archived_node_is_counted_but_not_listed(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """归档节点上的场次:模型没有它的记号,所以列不出来 —— 但**要算进总数**。

    静默丢掉的话,"一共排了几场"就少了一个;而列出来又会让模型引用一个它看不见的
    记号(服务端会按悬空引用拒绝它)。所以总数照给,渲染层说明"上面列了 M 场"。
    """
    account = await make_account()
    root_id = await _root_id(app_client, account)
    created = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/nodes",
        json={"parentId": root_id, "title": "会被归档的那件事"},
        headers=account.headers,
    )
    assert created.status_code == 201, created.text
    node_id = created.json()["node"]["id"]

    await _write(
        lambda session: _add(
            session,
            _session(account, account.workspace_id, node_id, date(2026, 10, 7), 30),
        )
    )

    archived = await app_client.delete(
        f"/api/workspaces/{account.workspace_id}/nodes/{node_id}?mode=archive",
        headers=account.headers,
    )
    assert archived.status_code in (200, 204), archived.text

    fake = use_reasoner(FakeReasoner(reply="好。"))
    assert (await _send(app_client, account, "继续")).status_code == 200
    time = (await _turn(fake)).time

    assert time.sessions_total == 1
    assert time.sessions == ()

    section = render_time_section(time)
    assert "上面列了 0 场" in section
    assert "一场都没有" not in section, "把「我列不出来」说成了「没有」"


async def test_the_horizon_follows_the_furthest_deadline(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """最远的截止日在 200 天之后 —— 视界要覆盖到它,否则那个任务会被判成超出视界。

    这条用的是排期预览那一套 `horizon_days`(同一个函数、同一个余量),所以 AI 报的
    容量和预览报的容量是同一个口径。
    """
    account = await make_account()
    deadline = date(2026, 9, 27) + timedelta(days=200)
    root_id = await _root_id(app_client, account)

    async def _set_deadline(session) -> None:
        node = await session.get(PlanNode, uuid.UUID(root_id))
        node.deadline = deadline

    await _write(_set_deadline)

    fake = use_reasoner(FakeReasoner(reply="好。"))
    assert (await _send(app_client, account, "继续")).status_code == 200
    time = (await _turn(fake)).time

    assert time.horizon_days == 200 + 8, "截止日的余量没有被算进去"
    assert time.horizon_last_day == deadline.isoformat()
    assert time.capacity_minutes > 0


async def test_open_task_estimates_are_summed_and_labelled_honestly(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """需求侧的那个合计由服务端加出来,而且"有几个没填预计工时"一起给。

    让模型自己去加几十个节点,它会算错 —— 而算错的方向看不出来,理由听起来和算对的
    时候一模一样。服务端给出两个可核对的数,模型要做的是**说清楚口径**,不是重算。
    """
    account = await make_account()
    root_id = await _root_id(app_client, account)

    filled = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/nodes",
        json={"parentId": root_id, "title": "有工时的那件事", "estimateMinutes": 120},
        headers=account.headers,
    )
    assert filled.status_code == 201, filled.text
    blank = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/nodes",
        json={"parentId": root_id, "title": "没填工时的那件事"},
        headers=account.headers,
    )
    assert blank.status_code == 201, blank.text

    fake = use_reasoner(FakeReasoner(reply="好。"))
    assert (await _send(app_client, account, "还做得完吗")).status_code == 200
    time = (await _turn(fake)).time

    assert time is not None
    assert time.open_task_minutes == 120, "没填工时的任务被按 0 算了,或者被漏掉了"
    # 空间根节点是 `goal` 类型,不在口径里 —— 这一条同时钉住了"只数任务"。
    assert time.open_tasks_without_estimate == 1

    section = render_time_section(time)
    assert "120 分钟" in section
    assert "下限" in section


async def test_a_finished_task_is_not_counted_as_work_left(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """已经做完的任务不算"还要做多少"。

    口径取自排期器自己声明的"未完成"(`ScheduleNode.is_open`),不在这里另立一套 ——
    两套口径迟早会分家,那时 AI 说的"还要做多少"和排期器算的就对不上了。
    """
    account = await make_account()
    root_id = await _root_id(app_client, account)

    created = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/nodes",
        json={"parentId": root_id, "title": "已经做完的事", "estimateMinutes": 999},
        headers=account.headers,
    )
    assert created.status_code == 201, created.text
    node_id = created.json()["node"]["id"]
    done = await app_client.patch(
        f"/api/workspaces/{account.workspace_id}/nodes/{node_id}",
        json={"status": "completed"},
        headers=account.headers,
    )
    assert done.status_code == 200, done.text

    fake = use_reasoner(FakeReasoner(reply="好。"))
    assert (await _send(app_client, account, "继续")).status_code == 200
    time = (await _turn(fake)).time

    assert time is not None
    assert time.open_task_minutes == 0, "做完的任务的工时被算进了「还要做多少」"


async def test_reading_the_time_context_writes_nothing(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """读时间底盘是只读的 —— 它一个字段都不改。

    值得单独钉一条,是因为它读的东西(容量、可用时段、场次)**全都由排期那一侧写**。
    这一轮要是顺手改了其中任何一个,用户看到的就是"聊了两句,我的排期变了"。
    """
    account = await make_account()
    fake = use_reasoner(FakeReasoner(reply="好。"))
    assert (await _send(app_client, account, "第一句")).status_code == 200

    before = await _counts()
    assert (await _send(app_client, account, "第二句")).status_code == 200
    assert await _counts() == before
    assert len(fake.calls) == 2
