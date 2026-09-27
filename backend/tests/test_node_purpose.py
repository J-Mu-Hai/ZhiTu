"""节点的「用途」是一条**正交的轴**,不是第二套节点类型(§2.5)。

## 这个文件在防什么

「信息」这个用途的全部价值在于它**不占日历**。而"不占日历"这件事做错的方式是无声的:
信息主题照样排进了周三下午、照样出现在进度条的分子分母里、照样能当前置依赖 ——
界面上一切正常,只是用户的时间表里多了一件他根本没打算做的事。

所以这里的断言全都落在**排期与统计的产出**上,而不是"字段存下来了":
字段存下来是必要条件,但不是那件真正要保证的事。

## 两道闸,分别验

第二批 B2.5 上线后,"信息节点带工时"这个状态**已经造不出来**了:写入时
`information_node_conflicts` 就拒(见下面第 4 节)。所以现在验的是两道闸各自的功效:

1. **写入时**:带工时的信息节点建不出来,改也改不出来(第 4 节)。
2. **排期时**:一个没有工时的信息节点**也不该**出现在 `gaps` 里 —— 而它本来会:
   `scheduler/schedule.py` 对"叶子节点没有工时"的处理是**报一条 `NO_ESTIMATE` 缺口**。
   于是"用途过滤还在不在"有了一个可证伪的问法:过滤没了,这个信息主题就会变成一条
   天天催用户"给「我排名 38」估个工时"的提示。

第一版这里造的信息节点**故意带着 120 分钟工时**,好让"它没被排进去"只可能有一个
原因。那道守卫上线时这个用例如期红了(见那次提交),改的是构造方式,断言一条没删。
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

    **注意这个信息节点没有工时**(有工时的那个已经建不出来了,见第 4 节):于是
    `gaps` 那一条断言问的是"用途过滤还在不在" —— 少了它,这个节点会以
    `NO_ESTIMATE` 缺口的形式出现。
    """
    account = await make_account()
    todo = await _create(
        app_client, account, title="写文献综述", node_type="task", estimate_minutes=120
    )
    info = await _create(
        app_client, account, title="我排名 38", node_type="capability", purpose="information"
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


# ---------------------------------------------------------------------------------
# 4. 写入时的闸:信息主题不能带工时/截止(§4.1,B2.5)
# ---------------------------------------------------------------------------------
async def test_an_information_node_cannot_carry_an_estimate_or_a_deadline(
    app_client: httpx.AsyncClient, make_account
) -> None:
    """§4.1:信息主题不需要具备工时、完成勾选或截止日期。

    ## 为什么这是在"写入"这一层挡,而不是排期时忽略

    排期那一层已经把它过滤掉了(第 2 节)。留在库里的"信息主题 + 90 分钟"会在**别处**
    冒出来:计划载荷里带着一个用户从没打算花的时间、AI 的上下文里它看起来像个任务、
    而用户把它改回「行动」的那一刻,一个他早忘了的工时突然开始占日历。

    ## 三条路都要堵

    建的时候带、改的时候加上去、以及**把一个已经有工时的任务改成信息主题** ——
    最后这条是最容易漏的:它一次都不提 `estimateMinutes`,而结果同样不合法。
    """
    account = await make_account()

    async def _create_raw(**extra: object) -> httpx.Response:
        body: dict = {
            "parentId": await _root_id(app_client, account),
            "title": "我排名 38",
            "nodeType": "capability",
            "purpose": "information",
        }
        body.update(extra)
        return await app_client.post(
            f"/api/workspaces/{account.workspace_id}/nodes", json=body, headers=account.headers
        )

    for label, extra in (("带工时", {"estimateMinutes": 90}), ("带截止", {"deadline": "2026-12-01"})):
        refused = await _create_raw(**extra)
        assert refused.status_code == 400, f"{label}:应当被拒,实际 {refused.status_code} {refused.text}"
        error = refused.json()["error"]
        assert error["code"] == "INVALID_INPUT", error
        # 消息里要**同时**给出两条出路:用户想做的可能是"把它变成真的信息主题"
        # (那就清掉),也可能只是点错了用途(那就改回去)。只骂一句"不能带工时",
        # 他得自己猜该改哪一边。
        assert "行动" in error["message"], error
        assert "清掉" in error["message"], error

    # 干净的建法当然要能过 —— 少了这一句,"所有带 purpose=information 的都拒掉"也全绿。
    clean = await _create_raw()
    assert clean.status_code == 201, clean.text
    info = clean.json()["node"]

    patch = f"/api/workspaces/{account.workspace_id}/nodes/{info['id']}"

    # 改:给一个信息主题补上工时 —— 同样拒。
    added = await app_client.patch(
        patch, json={"estimateMinutes": 90}, headers=account.headers
    )
    assert added.status_code == 400, added.text
    assert added.json()["error"]["code"] == "INVALID_INPUT"

    # 反过来:把一个**已经有工时**的任务改成信息主题。这一条连 `estimateMinutes`
    # 都没提,而结果一样不合法。
    task = await _create(app_client, account, title="写文献综述", node_type="task", estimate_minutes=90)
    flipped = await app_client.patch(
        f"/api/workspaces/{account.workspace_id}/nodes/{task['id']}",
        json={"purpose": "information"},
        headers=account.headers,
    )
    assert flipped.status_code == 400, (
        "把一个有工时的任务改成了信息主题 —— 库里于是留下一个『信息主题 + 90 分钟』,"
        f"实际返回 {flipped.status_code} {flipped.text}"
    )

    # 而**同一次请求里清掉工时再改用途**要能过:这才是那句话教的走法,
    # 只报错不给路的话用户只能自己试。
    together = await app_client.patch(
        f"/api/workspaces/{account.workspace_id}/nodes/{task['id']}",
        json={"purpose": "information", "estimateMinutes": None},
        headers=account.headers,
    )
    assert together.status_code == 200, together.text
    assert together.json()["node"]["purpose"] == "information"
    assert together.json()["node"]["estimateMinutes"] is None

    # 一步都不能落地:被拒的那两次,库里必须原样。
    stored = {node["id"]: node for node in (await _plan(app_client, account))["nodes"]}
    assert stored[info["id"]]["estimateMinutes"] is None, "被拒的写入还是落了一半"
    assert stored[task["id"]]["estimateMinutes"] is None
