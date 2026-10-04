"""首页「本周计划 / 本日计划」聚合接口。

## 这一条钉的是什么

首页要同时看到**两个不同空间**的本周计划与今天安排,而且不能把未确认的提案或归档
历史画成"当前计划"。所以测试在两个空间里各建一份正式周计划与任务,再断言接口把它们
聚合到一起、按空间分得开、没有日期的任务也在、归档的不在。

走真实 HTTP:节点、周计划、场次都由产品自己的接口写出来。直接往库里塞行只能验证
测试自己构造的世界。
"""

from __future__ import annotations

from datetime import date, timedelta

import httpx

from backend.services.timeutil import today_in


def _monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


async def _root_id(client: httpx.AsyncClient, account, workspace_id: str) -> str:
    plan = await client.get(f"/api/workspaces/{workspace_id}/plan", headers=account.headers)
    assert plan.status_code == 200, plan.text
    root = next(node for node in plan.json()["nodes"] if node["parentId"] is None)
    return root["id"]


async def _make_phase(client: httpx.AsyncClient, account, workspace_id: str, root_id: str, title: str) -> str:
    response = await client.post(
        f"/api/workspaces/{workspace_id}/nodes",
        json={"parentId": root_id, "title": title, "nodeType": "stage"},
        headers=account.headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["node"]["id"]


async def _make_task(
    client: httpx.AsyncClient,
    account,
    workspace_id: str,
    parent_id: str,
    title: str,
    *,
    deadline: str | None = None,
) -> str:
    payload = {"parentId": parent_id, "title": title, "nodeType": "task"}
    if deadline is not None:
        payload["deadline"] = deadline
    response = await client.post(
        f"/api/workspaces/{workspace_id}/nodes", json=payload, headers=account.headers
    )
    assert response.status_code == 201, response.text
    return response.json()["node"]["id"]


async def _make_week_plan(client: httpx.AsyncClient, account, workspace_id: str, phase_id: str, week_start: str) -> str:
    response = await client.post(
        f"/api/workspaces/{workspace_id}/week-plans",
        json={"parentId": phase_id, "weekStart": week_start},
        headers=account.headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["node"]["id"]


async def test_today_plans_aggregates_week_and_day_across_workspaces(
    app_client: httpx.AsyncClient, make_account
) -> None:
    account = await make_account("plans-a@example.com", workspace_title="空间甲")
    second = await app_client.post(
        "/api/workspaces",
        json={"title": "空间乙", "intent": "第二个空间"},
        headers=account.headers,
    )
    assert second.status_code == 201, second.text
    workspace_b = second.json()["workspace"]["id"]

    today = today_in("Asia/Shanghai")
    monday = _monday(today).isoformat()

    # 空间甲:阶段 -> 本周计划 -> 两个任务(一个没日期,一个排到今天)
    root_a = await _root_id(app_client, account, account.workspace_id)
    phase_a = await _make_phase(app_client, account, account.workspace_id, root_a, "阶段甲")
    week_a = await _make_week_plan(app_client, account, account.workspace_id, phase_a, monday)
    no_date_a = await _make_task(app_client, account, account.workspace_id, week_a, "甲·没有日期的任务")
    scheduled_a = await _make_task(app_client, account, account.workspace_id, week_a, "甲·今天要做的")
    session = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/sessions",
        json={"nodeId": scheduled_a, "scheduledDate": today.isoformat(), "plannedMinutes": 40},
        headers=account.headers,
    )
    assert session.status_code == 201, session.text

    # 空间乙:阶段 -> 本周计划 -> 一个今天截止、还没排的任务
    root_b = await _root_id(app_client, account, workspace_b)
    phase_b = await _make_phase(app_client, account, workspace_b, root_b, "阶段乙")
    week_b = await _make_week_plan(app_client, account, workspace_b, phase_b, monday)
    deadline_b = await _make_task(
        app_client, account, workspace_b, week_b, "乙·今天截止", deadline=today.isoformat()
    )

    response = await app_client.get("/api/today/plans", headers=account.headers)
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["today"] == today.isoformat()
    assert body["weekStart"] == monday

    # 两个空间的本周计划都在,并且分得开。
    workspace_titles = {plan["workspaceTitle"] for plan in body["weekPlans"]}
    assert workspace_titles == {"空间甲", "空间乙"}

    plan_a = next(plan for plan in body["weekPlans"] if plan["workspaceId"] == account.workspace_id)
    assert plan_a["planNodeId"] == week_a
    assert plan_a["stageTitle"] == "阶段甲"
    task_titles_a = {task["title"] for task in plan_a["tasks"]}
    assert task_titles_a == {"甲·没有日期的任务", "甲·今天要做的"}
    # 没有具体日期的任务仍然出现在本周计划里。
    no_date = next(task for task in plan_a["tasks"] if task["nodeId"] == no_date_a)
    assert no_date["sessions"] == []

    # 今天:空间甲的工作块 + 空间乙今天截止、未排程的任务。
    kinds = {(item["workspaceId"], item["nodeId"]): item["kind"] for item in body["todayItems"]}
    assert kinds[(account.workspace_id, scheduled_a)] == "block"
    assert kinds[(workspace_b, deadline_b)] == "task"
    block = next(item for item in body["todayItems"] if item["nodeId"] == scheduled_a)
    assert block["plannedMinutes"] == 40
    # 每个节点的所属阶段来自父链,而不是当前空间。
    assert block["stageTitle"] == "阶段甲"

    # 添加表单的目标:两个空间的阶段都能选到。
    target_titles = {
        workspace["workspaceTitle"]: [phase["stageTitle"] for phase in workspace["phases"]]
        for workspace in body["targets"]
    }
    assert target_titles["空间甲"] == ["阶段甲"]
    assert target_titles["空间乙"] == ["阶段乙"]
    target_a = next(workspace for workspace in body["targets"] if workspace["workspaceId"] == account.workspace_id)
    assert target_a["phases"][0]["weekPlanId"] == week_a


async def test_today_plans_excludes_archived_and_unconfirmed(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    from backend.tests.conftest import FakeReasoner

    account = await make_account("plans-archived@example.com", workspace_title="排除空间")
    today = today_in("Asia/Shanghai")
    monday = _monday(today).isoformat()

    root = await _root_id(app_client, account, account.workspace_id)
    phase = await _make_phase(app_client, account, account.workspace_id, root, "阶段排除")
    week = await _make_week_plan(app_client, account, account.workspace_id, phase, monday)
    archived = await _make_task(app_client, account, account.workspace_id, week, "已归档的任务")
    patched = await app_client.patch(
        f"/api/workspaces/{account.workspace_id}/nodes/{archived}",
        json={"status": "archived"},
        headers=account.headers,
    )
    assert patched.status_code == 200, patched.text

    # 一份**未确认**的提案:它里面的节点只在 `proposal_items` 里,`plan_nodes` 没有。
    use_reasoner(
        FakeReasoner(
            actions=(
                {
                    "op": "create_node",
                    "localId": "n99",
                    "parentRef": "n1",
                    "title": "提案里的阶段",
                    "nodeType": "stage",
                },
            )
        )
    )
    proposed = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/messages",
        json={"content": "帮我把这个目标拆成计划"},
        headers=account.headers,
    )
    assert proposed.status_code == 200, proposed.text
    body_proposal = proposed.json()
    assert body_proposal["proposal"] is not None, body_proposal["proposalErrors"]

    response = await app_client.get("/api/today/plans", headers=account.headers)
    assert response.status_code == 200, response.text
    body = response.json()

    titles = {
        task["title"]
        for plan in body["weekPlans"]
        for task in plan["tasks"]
    }
    assert "已归档的任务" not in titles
    assert "提案里的阶段" not in titles
