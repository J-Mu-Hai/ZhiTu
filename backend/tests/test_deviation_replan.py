"""偏差检测与按执行情况重规划。

## 这一阶段最重要的产品判断,就钉在这个文件里

**没有记录 ≠ 没完成。** 一场过去的安排、没有任何执行记录时,系统能给的最准确的东西
是一句**提问**(`isQuestion=true`),不是一条"未完成"。把它当结论,用户从第一天起就会
被系统按一个他从未确认过的事实去重排计划,而他纠正不了 —— 他甚至看不出系统是依据
什么算的。所以这里有三处断言:

- `/deviations` 里 `isQuestion` 为真;
- 那句话本身不含"没完成";
- **送给模型的提示词里明确写了这一条**(模型看不到库,它只能看到我们渲染的那段文字)。

## 重规划不另开写入路径

`POST /replan` 最后调的是阶段 4 那个 `proposal_service.build_from_actions` —— 同一套
校验、同一张 `proposals` 表、同一个确认事务。所以这里逐条验的是**它没有绕过去**:

- 生成之后 `/plan` 一行没变(模型提的调整不会自动生效);
- 确认之后才出现,而且 `plan_revisions.trigger_type` 是 `execution_deviation`
  (版本历史里看得见"这次调整是因为执行情况")。

## 模型不可用时不编方案

`degraded=true` + 空提案 + 一句如实的说明,**不是**一份规则生成的调整方案。一个凭空
生成的调整方案会被用户当成系统的判断,而它其实什么依据都没有。
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import httpx
from sqlalchemy import select, update

from backend.agent.runtime.base import BriefClaim
from backend.db.models import ExecutionRecord, PlanNode, PlanRevision, ScheduledSession
from backend.db.models.enums import RevisionTrigger
from backend.services.timeutil import today_in
from backend.tests.conftest import FakeReasoner

#: 与执行反馈那个文件同构的计划:一个阶段 + 一个带工时的任务。
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

#: 复盘那一轮模型要提的变更:在计划里补一个新任务。
REPLAN_ACTIONS = (
    {
        "op": "create_node",
        "localId": "n9",
        "parentRef": "n2",
        "title": "补一场:把上次卡住的环境练熟",
        "nodeType": "task",
        "estimateMinutes": 60,
    },
)


async def _account_with_schedule(client: httpx.AsyncClient, make_account, use_reasoner, *, claims=()):
    """一个已经排好期的账号 —— 有真实的场次可以制造偏差。"""
    account = await make_account()
    use_reasoner(FakeReasoner(actions=PLAN_ACTIONS, claims=claims))

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


async def _plan(client: httpx.AsyncClient, account) -> dict:
    response = await client.get(
        f"/api/workspaces/{account.workspace_id}/plan", headers=account.headers
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _deviations(client: httpx.AsyncClient, account) -> dict:
    response = await client.get(
        f"/api/workspaces/{account.workspace_id}/deviations", headers=account.headers
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _replan(client: httpx.AsyncClient, account) -> httpx.Response:
    return await client.post(
        f"/api/workspaces/{account.workspace_id}/replan", headers=account.headers
    )


async def _backdate_one_session(client: httpx.AsyncClient, account, db, *, days: int = 1) -> str:
    """把第一场安排挪到 N 天前,返回它的 id。

    "过去、又没有记录"这个状态只能这么造 —— 排期算法按定义不会排出过去的场次
    (`first = max(earliest, today)`),所以它必须由时间流逝产生,而测试里没有时间机器。
    """
    plan = await _plan(client, account)
    session = plan["sessions"][0]
    await db.execute(
        update(ScheduledSession)
        .where(ScheduledSession.id == uuid.UUID(session["id"]))
        .values(scheduled_date=today_in("Asia/Shanghai") - timedelta(days=days))
    )
    await db.commit()
    return session["id"]


# ---------------------------------------------------------------------------------
# 1. 没有偏差就不请模型
# ---------------------------------------------------------------------------------
async def test_no_deviations_does_not_consult_the_model(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """一个刚排好期、什么都没落下的空间:0 条偏差,模型**一次都没被调用**。

    为了"一切正常"去问一次模型,既花钱,又会换回一个为了有话可说而硬凑出来的调整
    建议 —— 而用户会把它当成系统的判断。
    """
    account = await _account_with_schedule(app_client, make_account, use_reasoner)
    # 换一个全新的假模型:**刚才建计划用掉的那几次调用不算在这一次里**。
    model = use_reasoner(FakeReasoner())

    deviations = await _deviations(app_client, account)
    assert deviations["deviations"] == [], f"刚排完期不该有偏差: {deviations}"
    assert deviations["questionCount"] == 0

    response = await _replan(app_client, account)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["consultedModel"] is False
    assert body["proposal"] is None
    assert body["proposalErrors"] == []
    assert "没有发现需要调整" in body["message"]

    assert model.calls == [], "没有偏差却调了模型"


# ---------------------------------------------------------------------------------
# 2. 未记录 -> 提问,而不是结论
# ---------------------------------------------------------------------------------
async def test_unrecorded_past_session_is_asked_not_concluded(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db
) -> None:
    """过去、没有记录的一场 -> `isQuestion=true`,而且**提示词里明确说了这一点**。

    提示词那一条是这里最容易被省略、却最要紧的断言:模型看不到库,它只能看到我们渲染
    的那段文字。那段文字如果把"没有记录"和"没做到"并列摆着,再聪明的模型也会顺着
    往下推 —— 而它推出来的东西会变成一份用户从没确认过的重排方案。
    """
    account = await _account_with_schedule(app_client, make_account, use_reasoner)
    session_id = await _backdate_one_session(app_client, account, db)
    model = use_reasoner(FakeReasoner())

    deviations = await _deviations(app_client, account)
    item = next(
        (entry for entry in deviations["deviations"] if entry["sessionId"] == session_id), None
    )
    assert item is not None, f"过去又没记录的场次应当出现在偏差里: {deviations}"
    assert item["code"] == "UNRECORDED_PAST_SESSION"
    assert item["isQuestion"] is True, "没有记录只能问,不能当结论"
    assert item["daysAgo"] == 1
    assert "没完成" not in item["detail"]
    assert deviations["questionCount"] == 1
    assert "没有记录不等于没完成" in deviations["note"]

    response = await _replan(app_client, account)
    assert response.status_code == 200, response.text
    assert response.json()["consultedModel"] is True

    assert len(model.calls) == 1, "这一轮应当恰好请模型看一次"
    prompt = model.calls[0].user_message
    assert "没有记录不等于没完成" in prompt, (
        "送给模型的提示词没有说明「未记录 ≠ 未完成」—— 模型会把它当成用户没做到"
    )
    assert "系统发起" in prompt, (
        "这段文字不是用户说的话,必须标明来源 —— 否则模型会以为用户确认过这些事实"
    )


# ---------------------------------------------------------------------------------
# 3. 用户自己报过的结果才算偏差
# ---------------------------------------------------------------------------------
async def test_a_recorded_skip_is_a_deviation(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db
) -> None:
    """用户说了"这场没做" -> 一条**结论**,不是提问。

    与上一条是同一个数据集的两种状态:区别只在于"用户说没说"。这正是整个复盘要
    分开处理的那条线。
    """
    account = await _account_with_schedule(app_client, make_account, use_reasoner)
    session_id = await _backdate_one_session(app_client, account, db)

    recorded = await app_client.post(
        f"/api/sessions/{session_id}/executions",
        json={
            "result": "skipped",
            "idempotencyKey": "exec-key-replan-skip-1",
            "delayReason": "临时出差",
        },
        headers=account.headers,
    )
    assert recorded.status_code == 200, recorded.text

    deviations = await _deviations(app_client, account)
    item = next(
        (entry for entry in deviations["deviations"] if entry["sessionId"] == session_id), None
    )
    assert item is not None, deviations
    assert item["code"] == "RECORDED_SETBACK"
    assert item["isQuestion"] is False, "用户说过的事是结论,不是提问"
    assert item["facts"]["result"] == "skipped"
    assert "临时出差" in item["detail"]
    assert deviations["questionCount"] == 0


async def test_overdue_node_is_a_deviation(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db
) -> None:
    """截止日过了、节点还没完成 —— 一条客观事实,不需要用户报告。"""
    account = await _account_with_schedule(app_client, make_account, use_reasoner)
    plan = await _plan(app_client, account)
    task = next(node for node in plan["nodes"] if node["nodeType"] == "task")

    await db.execute(
        update(PlanNode)
        .where(PlanNode.id == uuid.UUID(task["id"]))
        .values(deadline=today_in("Asia/Shanghai") - timedelta(days=3))
    )
    await db.commit()

    deviations = await _deviations(app_client, account)
    item = next(
        (entry for entry in deviations["deviations"] if entry["nodeId"] == task["id"]), None
    )
    assert item is not None, deviations
    assert item["code"] == "OVERDUE_NODE"
    assert item["isQuestion"] is False
    assert item["daysAgo"] == 3


# ---------------------------------------------------------------------------------
# 4. 提案不自动生效,确认走的还是那一个事务
# ---------------------------------------------------------------------------------
async def test_replan_proposes_but_writes_nothing_until_confirmed(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db
) -> None:
    """模型提了调整 -> `/plan` **一行没变**;确认之后才出现,且 triggerType 对得上。

    两件事在同一条测试里,是因为分开写会让"生成时不写"这条断言变得可疑:一个只
    断言"节点数没变"的测试,在实现真的把节点写进去了、只是写到了别的地方时仍然是绿的。
    所以这里连着走完:生成 -> 没变 -> 确认 -> 出现。
    """
    account = await _account_with_schedule(app_client, make_account, use_reasoner)
    await _backdate_one_session(app_client, account, db)
    use_reasoner(FakeReasoner(actions=REPLAN_ACTIONS))

    before = await _plan(app_client, account)
    before_ids = {node["id"] for node in before["nodes"]}

    response = await _replan(app_client, account)
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["consultedModel"] is True
    assert body["degraded"] is False
    proposal = body["proposal"]
    assert proposal is not None, f"模型提了变更却没生成提案: {body}"
    assert proposal["itemCount"] == 1
    assert proposal["status"] in {"validated", "pending_confirmation"}
    assert proposal["triggerType"] == "execution_deviation"
    assert "确认" in body["message"], "必须说清这份方案还没有生效"

    unchanged = await _plan(app_client, account)
    assert {node["id"] for node in unchanged["nodes"]} == before_ids, (
        "提案还没确认,计划就已经变了"
    )
    assert unchanged["revisionVersion"] == before["revisionVersion"]

    # 确认走的是阶段 4 那个接口,**不是复盘专用的一条捷径**。
    confirmed = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/proposals/{proposal['id']}/confirm",
        json={"idempotencyKey": "confirm-key-replan-1"},
        headers=account.headers,
    )
    assert confirmed.status_code == 200, confirmed.text

    after = await _plan(app_client, account)
    assert len(after["nodes"]) == len(before["nodes"]) + 1
    assert after["revisionVersion"] == before["revisionVersion"] + 1

    revision = (
        await db.execute(
            select(PlanRevision)
            .where(PlanRevision.workspace_id == uuid.UUID(account.workspace_id))
            .order_by(PlanRevision.version.desc())
            .limit(1)
        )
    ).scalar_one()
    assert revision.trigger_type is RevisionTrigger.EXECUTION_DEVIATION, (
        "版本历史里必须分得清「这次调整是因为执行情况」"
    )


# ---------------------------------------------------------------------------------
# 5. 模型不可用时给事实,不给编出来的方案
# ---------------------------------------------------------------------------------
async def test_degraded_model_reports_no_proposal(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db
) -> None:
    """降级 -> 偏差照给,提案为空,并且**如实说明这次没能给出方案**。

    悄悄返回一个空提案,用户会把它读成"系统看过之后认为不需要调整" —— 而系统其实
    根本没看。这两件事在界面上长得一模一样,含义完全相反。
    """
    account = await _account_with_schedule(app_client, make_account, use_reasoner)
    await _backdate_one_session(app_client, account, db)
    use_reasoner(FakeReasoner(reply="模型暂时不可用。", degraded=True, retryable=True))

    messages_before = await app_client.get(
        f"/api/workspaces/{account.workspace_id}/messages", headers=account.headers
    )
    before_count = len(messages_before.json()["messages"])

    response = await _replan(app_client, account)
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["degraded"] is True
    assert body["retryable"] is True
    assert body["proposal"] is None
    assert body["proposalErrors"] == []
    assert body["deviations"], "模型挂了不影响事实那一半"
    assert "没能给出调整方案" in body["message"]

    after = await app_client.get(
        f"/api/workspaces/{account.workspace_id}/messages", headers=account.headers
    )
    assert len(after.json()["messages"]) == before_count, (
        "降级时不该往对话里塞一条助手消息 —— 用户在这轮里一句话都没说"
    )


async def test_a_silent_no_op_is_not_reported_as_no_change_needed(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db
) -> None:
    """模型说了话、但没提任何变更 -> **不能说成"不需要改动计划"**。

    这是实测撞出来的:一个还没说过截止时间和每周投入的空间,模型看完偏差之后回的是
    "先告诉我这三个条件,我再动手改",一个 op 都不发。原来的文案把这种情形写成
    "模型看过之后认为不需要改动计划" —— 用户于是放心地关掉页面,而模型那三个问题
    正躺在对话里等回答。调整既没做,也没人知道它没做。

    所以这一刻只能如实说它没提变更,并把用户指到对话里那句解释上去。
    """
    account = await _account_with_schedule(app_client, make_account, use_reasoner)
    await _backdate_one_session(app_client, account, db)
    use_reasoner(FakeReasoner(reply="先告诉我截止时间和每周能投入多少,我再动手改。"))

    response = await _replan(app_client, account)
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["consultedModel"] is True
    assert body["degraded"] is False
    assert body["proposal"] is None
    assert "不需要改动计划" not in body["message"], (
        "模型没提变更不等于它认为不需要调整 —— 它多半是在问条件"
    )
    assert "没有提出变更" in body["message"]
    assert "对话" in body["message"], "要告诉用户那句解释在哪,否则他不知道下一步做什么"

    # 它说的那句话确实存进了对话 —— 指过去是真的指得到,不是一句空指引。
    conversation = await app_client.get(
        f"/api/workspaces/{account.workspace_id}/messages", headers=account.headers
    )
    assert any("我再动手改" in message["content"] for message in conversation.json()["messages"])


async def test_replan_does_not_write_brief_claims(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db
) -> None:
    """复盘那一轮**不更新规划条件**,哪怕模型标了 `user_stated`。

    模型看到的是一段系统替用户写的文字。它完全可能把它读成"用户说过每周 4 小时"
    并标成 `user_stated` —— 那会静默改掉真正驱动排期的那一列,而用户从没说过这句话。
    """
    from backend.services.brief_service import load_brief

    # 先让对话那一条路径**真的**把 300 写进简报 —— 否则"复盘没改它"这条断言在
    # 简报本来就是空的时候也能通过,那它什么都没验证。
    account = await _account_with_schedule(
        app_client,
        make_account,
        use_reasoner,
        claims=(BriefClaim("weekly_available_minutes", 300, "user_stated"),),
    )
    brief = await load_brief(db, uuid.UUID(account.workspace_id))
    assert brief is not None and brief.weekly_available_minutes == 300, (
        f"前提不成立:对话那一轮应当把 300 写进简报,实际是 {brief}"
    )

    await _backdate_one_session(app_client, account, db)
    use_reasoner(
        FakeReasoner(
            actions=REPLAN_ACTIONS,
            # 模型把那段**系统写的**文字读成了用户的话,还标成 user_stated。
            # 这正是"复盘那一轮不能复用 apply_claims"要挡的事。
            claims=(BriefClaim("weekly_available_minutes", 240, "user_stated"),),
        )
    )

    response = await _replan(app_client, account)
    assert response.status_code == 200, response.text

    unchanged = await load_brief(db, uuid.UUID(account.workspace_id))
    assert unchanged is not None
    assert unchanged.weekly_available_minutes == 300, (
        "复盘那一轮把模型替用户「说」的条件写进了排期依据"
    )


async def test_replan_history_is_readable(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db
) -> None:
    """重放到 `execution_records` 上的那条约束:复盘不产生执行记录。

    偏差检测是**只读**的。它跑在 `execution_records` 上,如果顺手往里写点什么
    (比如"这次复盘时发现这场没做"),台账就不再是"用户报过什么"的记录,而复盘会
    开始把自己的推断当成用户的话再读回去 —— 一个自我强化的循环。
    """
    account = await _account_with_schedule(app_client, make_account, use_reasoner)
    await _backdate_one_session(app_client, account, db)
    use_reasoner(FakeReasoner(actions=REPLAN_ACTIONS))

    before = await db.scalar(
        select(ExecutionRecord.id).where(
            ExecutionRecord.workspace_id == uuid.UUID(account.workspace_id)
        )
    )
    assert before is None, "这个测试的前提是还没有任何执行记录"

    assert (await _replan(app_client, account)).status_code == 200
    await db.rollback()

    remaining = (
        await db.execute(
            select(ExecutionRecord).where(
                ExecutionRecord.workspace_id == uuid.UUID(account.workspace_id)
            )
        )
    ).scalars().all()
    assert list(remaining) == [], "复盘往执行台账里写了东西"
