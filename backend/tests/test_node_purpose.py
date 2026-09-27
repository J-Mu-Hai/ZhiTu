"""节点的「用途」是一条**正交的轴**,不是第二套节点类型(§2.5)。

## 这个文件在防什么

「信息」这个用途的全部价值在于它**不占日历**。而"不占日历"这件事做错的方式是无声的:
信息主题照样排进了周三下午、照样出现在进度条的分子分母里、照样能当前置依赖 ——
界面上一切正常,只是用户的时间表里多了一件他根本没打算做的事。

所以这里的断言全都落在**排期与统计的产出**上,而不是"字段存下来了":
字段存下来是必要条件,但不是那件真正要保证的事。

## 为什么信息节点**故意**带着工时

`INFORMATION_NODE_MUST_NOT_BE_SCHEDULABLE`(第二批 B2.5)会从写入时拦住"信息节点带工时"
这条路。**这一批还没有那道闸**(见交付报告的"批次内的一致性缺口")。所以这里造的
信息节点**带 120 分钟工时** —— 于是下面"它没被排进去"就只可能有一个原因:
`schedule_service` 那个收口真的在按用途过滤,而不是"它没有工时所以排不进去"。

那份守卫上线之后,这个文件会红在"建不出来"上。**那时要改的是本文件的构造方式,
不是删掉这些断言** —— "带工时的信息节点也排不进去"这条性质在守卫之后依然成立
(守卫挡在门口,收口挡在排期里,两道都在)。
"""

from __future__ import annotations

import httpx

# ---------------------------------------------------------------------------------
# 小工具:这个文件里每次都要的那几步
# ---------------------------------------------------------------------------------


async def _plan(client: httpx.AsyncClient, account) -> dict:
    response = await client.get(f"/api/workspaces/{account.workspace_id}/plan", headers=account.headers)
    assert response.status_code == 200, response.text
    return response.json()


async def _root_id(client: httpx.AsyncClient, account) -> str:
    return (await _plan(client, account))["nodes"][0]["id"]


async def _create(
    client: httpx.AsyncClient,
    account,
    *,
    title: str,
    node_type: str,
    purpose: str | None = None,
    parent_id: str | None = None,
    estimate_minutes: int | None = None,
) -> dict:
    """建一个节点,**不替调用方决定 `purpose`** —— 不给就是不给,让默认值自己说话。"""
    body: dict = {
        "parentId": parent_id or await _root_id(client, account),
        "title": title,
        "nodeType": node_type,
    }
    if purpose is not None:
        body["purpose"] = purpose
    if estimate_minutes is not None:
        body["estimateMinutes"] = estimate_minutes
    response = await client.post(
        f"/api/workspaces/{account.workspace_id}/nodes", json=body, headers=account.headers
    )
    assert response.status_code == 201, response.text
    return response.json()["node"]


async def _preview(client: httpx.AsyncClient, account) -> dict:
    response = await client.post(
        f"/api/workspaces/{account.workspace_id}/schedule/preview", headers=account.headers
    )
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------------
# 1. 字段能往返,且默认是 planning
# ---------------------------------------------------------------------------------
async def test_purpose_round_trips_and_defaults_to_planning(
    app_client: httpx.AsyncClient, make_account
) -> None:
    """三个方向都要验:显式给 information、不给、以及改回来。

    "不给"那一半是**兼容性**那一半:这一批之前建的全部节点(开发库里有 601 个)
    都是走的这条路。它们必须仍然读作 planning —— 否则升级之后它们会集体退出排期,
    而那是一次没有任何人要求过的语义迁移。
    """
    account = await make_account()

    info = await _create(
        app_client, account, title="我排名 38", node_type="capability", purpose="information"
    )
    assert info["purpose"] == "information", info

    # **不给 `purpose`** —— 走的是 `CreateNodeRequest.purpose` 的默认值。
    plain = await _create(app_client, account, title="读三篇文献", node_type="task")
    assert plain["purpose"] == "planning", (
        f"没给 purpose 的新节点应当默认是 planning(兼容存量行为),实际是 {plain['purpose']!r}"
    )

    # 落到库里之后再读一次:响应体说自己是什么不算数,`/plan` 说什么才算。
    stored = {node["id"]: node for node in (await _plan(app_client, account))["nodes"]}
    assert stored[info["id"]]["purpose"] == "information"
    assert stored[plain["id"]]["purpose"] == "planning"

    # 只改标题不该动用途 —— 一个"PATCH 总是把 purpose 写回默认值"的实现会红在这里。
    renamed = await app_client.patch(
        f"/api/workspaces/{account.workspace_id}/nodes/{info['id']}",
        json={"title": "我排名 38(已核对)"},
        headers=account.headers,
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["node"]["purpose"] == "information", "只改标题把用途改掉了"

    # 改用途本身也要能改 —— 用户把一个信息主题改成要排期的行动,是 §2.5 明确留的路。
    flipped = await app_client.patch(
        f"/api/workspaces/{account.workspace_id}/nodes/{info['id']}",
        json={"purpose": "planning"},
        headers=account.headers,
    )
    assert flipped.status_code == 200, flipped.text
    assert flipped.json()["node"]["purpose"] == "planning"


async def test_illegal_purpose_is_a_chinese_invalid_input(
    app_client: httpx.AsyncClient, make_account
) -> None:
    """非法值走的是 `INVALID_INPUT`(400)而且是**中文**,和 `node_type` 同一套。

    不用 422:422 在这个仓里是"形状不对"的意思(见 `api/errors.py`),而
    `purpose` 收到一个字符串是形状对的、只是值不在允许集合里 —— 和 `node_type`
    收到 `"banana"` 一模一样。状态码分家的话,客户端要为同一个意思分两个支。
    """
    account = await make_account()
    response = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/nodes",
        json={"parentId": await _root_id(app_client, account), "title": "随便", "purpose": "banana"},
        headers=account.headers,
    )
    assert response.status_code == 400, response.text
    error = response.json()["error"]
    assert error["code"] == "INVALID_INPUT", error
    # 消息里要出现**中文的字段名**,不是 "purpose"。用户读得懂的是"用途"。
    assert "用途" in error["message"], error


# ---------------------------------------------------------------------------------
# 2. 排期:信息节点不占日历
# ---------------------------------------------------------------------------------
async def test_information_node_is_absent_from_schedule_and_counts(
    app_client: httpx.AsyncClient, make_account
) -> None:
    """一条测试同时钉三件事:不进 `sessions`、不进 `gaps`、不进统计。

    三件事必须一起钉。只验 `sessions` 的话,"被排期算成了 NoEstimate 缺口而不占时间"
    这种半对实现照样全绿 —— 而它在界面上是一条天天催用户"给「我排名 38」估个工时"
    的提示,比排进去还糟。

    **信息节点带着 120 分钟工时**(见文件头):所以它出局只可能是用途那一道过滤起的作用。
    """
    account = await make_account()
    todo = await _create(
        app_client, account, title="写文献综述", node_type="task", estimate_minutes=120
    )
    info = await _create(
        app_client,
        account,
        title="我排名 38",
        node_type="capability",
        purpose="information",
        estimate_minutes=120,
    )

    preview = await _preview(app_client, account)
    # 线格式是 camelCase(`contracts/common.py::ApiModel` 的 `to_camel`),而这里读的是
    # 原始 JSON —— 所以是 `nodeId`,不是 `node_id`。
    scheduled = {session["nodeId"] for session in preview["sessions"]}
    assert todo["id"] in scheduled, (
        "对照组的任务没有被排进去 —— 那说明这条测试的前提(fit 出了东西)不成立,"
        f"别把它读成信息节点的问题。payload={preview}"
    )
    assert info["id"] not in scheduled, "信息节点被排进了日历"

    # 缺口里也不许有它。`gaps` 是"排不下的部分",而信息节点不是"排不下",
    # 是"根本不该排" —— 两者在界面上长得完全不一样。
    gap_nodes = {gap["nodeId"] for gap in preview["gaps"]}
    assert info["id"] not in gap_nodes, "信息节点变成了一个排期缺口"

    # 统计:分子分母都只数 planning 的。
    plan = await _plan(app_client, account)
    # 先把"信息节点确实在计划里"钉死 —— 少了这一句,一个把信息节点整个丢掉的实现
    # 会让下面那个 2 以"它根本不在这儿"的方式通过,而那验的不是同一件事。
    assert len(plan["nodes"]) == 3, [node["title"] for node in plan["nodes"]]
    assert plan["totalNodes"] == 2, (
        f"根 + 任务 = 2,信息节点不参与计时;实际 {plan['totalNodes']}"
        f"(节点总数是 {len(plan['nodes'])})"
    )

    # 完成一个 planning 的任务之后分子要动,而信息节点**没有"完成"这回事**。
    done = await app_client.patch(
        f"/api/workspaces/{account.workspace_id}/nodes/{todo['id']}",
        json={"status": "completed"},
        headers=account.headers,
    )
    assert done.status_code == 200, done.text
    plan = await _plan(app_client, account)
    assert plan["completedNodes"] == 1, plan
    assert plan["totalNodes"] == 2, "标完成之后分母变了"


# ---------------------------------------------------------------------------------
# 3. 依赖边:信息节点不能当端点
# ---------------------------------------------------------------------------------
async def test_information_node_cannot_be_a_dependency_endpoint(
    app_client: httpx.AsyncClient, make_account
) -> None:
    """§2.5:仅作为信息的节点不能成为硬排期依赖端点。

    ## 为什么这条必须在**写入时**拒,而不是排期时静默丢掉

    `schedule_service` 的查询按用途过滤之后,`live_ids` 也跟着变小 —— 触及信息节点的
    那条边会**静默地**从排期里消失。那是最糟的一种:dependencies 表里有那一行、`/plan`
    里也看得见那条线,而它永远不产生任何排期效果。用户看着一条"前置"线,以为自己
    排好了顺序。

    所以两端各验一次。只验一端的话,一个只检查了 `successor` 的实现在这里照样全绿。
    """
    account = await make_account()
    info = await _create(
        app_client, account, title="我排名 38", node_type="capability", purpose="information"
    )
    info2 = await _create(
        app_client, account, title="每周可用 10 小时", node_type="capability", purpose="information"
    )
    task = await _create(app_client, account, title="写文献综述", node_type="task")

    async def _depend(predecessor: str, successor: str) -> httpx.Response:
        return await app_client.post(
            f"/api/workspaces/{account.workspace_id}/dependencies",
            json={"predecessorId": predecessor, "successorId": successor},
            headers=account.headers,
        )

    for label, before, after in (
        ("信息节点当后继", task["id"], info["id"]),
        ("信息节点当前置", info2["id"], task["id"]),
    ):
        refused = await _depend(before, after)
        assert refused.status_code == 409, f"{label}:应当被拒,实际 {refused.status_code} {refused.text}"
        error = refused.json()["error"]
        assert error["code"] == "INFORMATION_NODE_NOT_DEPENDABLE", error
        # 消息里要有**那个节点的标题**,否则用户不知道该去改哪一个。
        assert "信息主题" in error["message"], error

    # 一条都不许留下 —— "先写进去再报错"是最糟的形状。
    plan = await _plan(app_client, account)
    assert plan["dependencies"] == [], "被拒的依赖还是写进去了"

    # 而**两个 planning 节点之间**照常能连。少了这半句,"所有依赖都拒掉"也全绿。
    ok_task = await _create(app_client, account, title="再写一稿", node_type="task")
    allowed = await _depend(task["id"], ok_task["id"])
    assert allowed.status_code == 201, allowed.text
    assert len((await _plan(app_client, account))["dependencies"]) == 1
