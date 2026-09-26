"""权限矩阵:每一个接口都被明确分类过。

## 这个测试真正防的是什么

不是"某个接口忘了鉴权"这一件事,而是**新加的接口没人去想它要不要鉴权**。

路由清单是**从 `app.openapi()` 现算出来的**,不是手写的。所以任何一个新路由,只要它的
(method, path) 没有出现在下面的分类表里,`test_every_route_is_classified` 就会失败,
并且直接告诉你该把它归到哪一类。忘了想 -> 测试红,而不是忘了想 -> 接口上线。

同一条纪律反过来也成立:分类表里有、而 openapi 里没有的条目同样让测试失败,否则这张表
会慢慢积累一堆描述着不存在接口的僵尸条目,而它的可信度就随之消失。

## 关于"B 拿 A 的 id"

`test_cross_account_access_is_404` 里每个用例都有一段"反向断言":**A 用同一个 id 必须
成功**。没有它,"B 得到 404"完全可能是因为那个 id 是假的 —— 那样测试是绿的,而权限
其实一点都没被验证。一个会假装通过的权限测试比没有权限测试更危险。
"""

from __future__ import annotations

import uuid

import httpx
import pytest

from backend.api.main import app
from backend.tests.conftest import Account

#: 不需要登录就能调用的接口。加进来就等于说"这个接口匿名可用",所以每加一个都该有理由。
PUBLIC_ROUTES = {
    ("POST", "/api/auth/register"): "注册本身当然不需要登录",
    ("POST", "/api/auth/login"): "登录就是拿令牌的过程",
    ("POST", "/api/auth/refresh"): "要轮换的令牌可能刚好过期,先过鉴权就轮换不了",
    ("POST", "/api/auth/logout"): (
        "必须幂等:第二次登出拿的是一张已作废的令牌。恒返回 204,没有令牌也无事可做"
    ),
    ("GET", "/health"): "存活探针",
    ("GET", "/ready"): "就绪探针",
}

#: 需要登录的接口。**没有任何一个接口是"看情况"的。**
AUTHENTICATED_ROUTES = {
    ("GET", "/api/auth/sessions"),
    ("DELETE", "/api/auth/sessions/{session_id}"),
    ("GET", "/api/users/me"),
    ("PATCH", "/api/users/me"),
    ("POST", "/api/workspaces"),
    ("GET", "/api/workspaces"),
    ("GET", "/api/workspaces/{workspace_id}"),
    ("PATCH", "/api/workspaces/{workspace_id}"),
    ("GET", "/api/workspaces/{workspace_id}/messages"),
    ("POST", "/api/workspaces/{workspace_id}/messages"),
    ("GET", "/api/workspaces/{workspace_id}/plan"),
    ("POST", "/api/workspaces/{workspace_id}/nodes"),
    ("PATCH", "/api/workspaces/{workspace_id}/nodes/{node_id}"),
    ("DELETE", "/api/workspaces/{workspace_id}/nodes/{node_id}"),
    ("POST", "/api/workspaces/{workspace_id}/dependencies"),
    ("DELETE", "/api/workspaces/{workspace_id}/dependencies"),
    ("POST", "/api/workspaces/{workspace_id}/relations"),
    ("PATCH", "/api/workspaces/{workspace_id}/relations/{relation_id}"),
    ("DELETE", "/api/workspaces/{workspace_id}/relations/{relation_id}"),
    ("GET", "/api/workspaces/{workspace_id}/layout"),
    ("PUT", "/api/workspaces/{workspace_id}/layout"),
    ("GET", "/api/workspaces/{workspace_id}/proposals"),
    ("POST", "/api/workspaces/{workspace_id}/proposals/{proposal_id}/confirm"),
    ("POST", "/api/workspaces/{workspace_id}/proposals/{proposal_id}/reject"),
    ("POST", "/api/workspaces/{workspace_id}/schedule/preview"),
    ("POST", "/api/workspaces/{workspace_id}/schedule/apply"),
    ("GET", "/api/workspaces/{workspace_id}/deviations"),
    ("POST", "/api/workspaces/{workspace_id}/replan"),
    # 执行反馈与「今天」。`{session_id}` 是**排期场次**,不是登录会话 ——
    # 归属校验见 `api/dependencies/session.py`。
    ("GET", "/api/sessions/{session_id}/executions"),
    ("POST", "/api/sessions/{session_id}/executions"),
    ("GET", "/api/today"),
    ("GET", "/api/reminders"),
    ("POST", "/api/reminders/dismiss"),
    ("POST", "/api/reminders/snooze"),
}

#: 路径里带别人资源 id 的接口 -> 那个占位符指向什么资源。
#: 这些必须在"B 用 A 的 id"时返回 404(而不是 403 —— 403 等于确认那个 id 存在)。
CROSS_ACCOUNT_ROUTES = {
    ("GET", "/api/workspaces/{workspace_id}"): "workspace_id",
    ("PATCH", "/api/workspaces/{workspace_id}"): "workspace_id",
    ("GET", "/api/workspaces/{workspace_id}/messages"): "workspace_id",
    ("POST", "/api/workspaces/{workspace_id}/messages"): "workspace_id",
    ("GET", "/api/workspaces/{workspace_id}/plan"): "workspace_id",
    ("GET", "/api/workspaces/{workspace_id}/proposals"): "workspace_id",
    # 预览是只读的,所以 A 用自己那个空间一定拿得到 200 —— 反向断言成立。
    ("POST", "/api/workspaces/{workspace_id}/schedule/preview"): "workspace_id",
    # 偏差检测同样是只读的,而且**不调模型**:一个刚建好的空间没有偏差,直接返回空列表。
    # 这也让"重规划"能进这张表:A 对一个没有偏差的空间调 /replan 时,服务端在触到
    # 模型之前就返回了,所以反向断言拿到的是 200 而不是一次真实的模型调用。
    ("GET", "/api/workspaces/{workspace_id}/deviations"): "workspace_id",
    ("POST", "/api/workspaces/{workspace_id}/replan"): "workspace_id",
    # 布局两条都进得来,而且**两条都必须进**:位置是按用户存的,很容易顺手写成
    # "只按 user_id 查"—— 那样 B 拿 A 的空间 id 去读,会读到一份空布局并返回 200,
    # 看起来完全正常。这里要求 404,因为空间本身就不是 B 的。
    # 反向断言也成立:A 自己读自己的一定是 200,提交一份空布局同样 200。
    ("GET", "/api/workspaces/{workspace_id}/layout"): "workspace_id",
    ("PUT", "/api/workspaces/{workspace_id}/layout"): "workspace_id",
    ("DELETE", "/api/auth/sessions/{session_id}"): "session_id",
}

# 而 `POST/PATCH/DELETE /relations` 三条**不能**进这张表:前缀只有空间 id 的那条
# (POST)请求体里还需要两个**真实存在**的节点,反向断言才成立;另外两条收的是
# `{relation_id}`,拿一个假的 id 时 A 自己也是 404。它们的归属校验由
# `test_relations_are_scoped_to_the_workspace` 单独覆盖:A 先真的连出一条边,
# 再让 B 拿那个 id 去改去删。

# 执行反馈那两条(`/api/sessions/{session_id}/executions`)**不能进上面这张表**:
# 路径里的 `{session_id}` 在这里指的是**排期场次**,而 `_fill` 那个占位符已经被
# `/api/auth/sessions/{session_id}` 占用,填的是**登录会话**的 id。填错了的话 A 自己
# 也是 404,反向断言永远不成立,这条用例就只能写成假的 —— 那比没有更糟。
#
# 它的归属校验由 `test_execution_feedback.py::test_b_cannot_record_into_a_sessions`
# 单独覆盖:A 先真的排出一场,再让 B 拿那个场次 id 去写。

# 确认、拒绝、应用排期、以及全部节点/依赖的写接口,**刻意不在上面这张表里**,尽管它们
# 的路径里也有别人的 id。
#
# 因为这张表的每个用例都要先做一次反向断言:"A 用同一个路径必须成功"。而确认一份
# 不存在的提案、改一个不存在的节点、应用一份必然是旧的排期版本,对 A 自己也是 404 / 409
# —— 反向断言永远不成立,这条用例就只能删掉或者写成假的。
#
# 它们需要的是另一套构造:A 先真的有一个节点、一份提案,再让 B 拿那个 id 去动它。
# 那是独立的测试 —— 提案那条在 test_proposal_confirm.py,**节点那条在下面**
# (`test_a_node_cannot_be_touched_through_someone_elses_workspace`)。
# 那一条不是"这里加一行"能覆盖的,但它的存在是必须的:少了它,节点写接口的归属
# 校验就完全没有测试。

#: 每个接口的最小合法请求体(需要请求体的才有)。
_BODIES: dict[tuple[str, str], dict] = {
    ("PATCH", "/api/users/me"): {},
    ("POST", "/api/workspaces"): {"title": "新空间"},
    ("PATCH", "/api/workspaces/{workspace_id}"): {},
    ("POST", "/api/workspaces/{workspace_id}/messages"): {"content": "你好"},
    ("POST", "/api/workspaces/{workspace_id}/nodes"): {
        "parentId": "00000000-0000-4000-8000-000000000002",
        "title": "探针",
    },
    ("PATCH", "/api/workspaces/{workspace_id}/nodes/{node_id}"): {},
    ("POST", "/api/workspaces/{workspace_id}/dependencies"): {
        "predecessorId": "00000000-0000-4000-8000-000000000002",
        "successorId": "00000000-0000-4000-8000-000000000003",
    },
    # `relationType` 是必填的(服务端不替用户猜类型),所以这里必须给一个合法值,
    # 否则匿名那条用例拿到的是 422 而不是 401 —— 那测的就是请求体校验了。
    ("POST", "/api/workspaces/{workspace_id}/relations"): {
        "sourceId": "00000000-0000-4000-8000-000000000002",
        "targetId": "00000000-0000-4000-8000-000000000003",
        "relationType": "related_to",
    },
    # 空数组是合法的:它表示"这一次没有要提交的位置"。跨账号那条用例因此拿得到
    # A 自己的 200(反向断言),而 B 仍然在触到布局之前就被空间归属挡下。
    ("PUT", "/api/workspaces/{workspace_id}/layout"): {},
    ("POST", "/api/workspaces/{workspace_id}/proposals/{proposal_id}/confirm"): {
        "idempotencyKey": "anon-probe-key"
    },
    ("POST", "/api/workspaces/{workspace_id}/proposals/{proposal_id}/reject"): {},
    # 形状合法但内容必然是旧的 —— 匿名那条用例在触到版本校验之前就该被 401 拦下,
    # 所以这里只要过得了 `ScheduleApplyRequest` 的字段约束即可。
    ("POST", "/api/workspaces/{workspace_id}/schedule/apply"): {
        "scheduleVersion": "0" * 32,
        "idempotencyKey": "anon-probe-key",
    },
    # 形状合法即可 —— 匿名那条用例在触到场次之前就被 401 拦下了。
    ("POST", "/api/sessions/{session_id}/executions"): {
        "result": "completed",
        "idempotencyKey": "anon-probe-key",
    },
    ("POST", "/api/reminders/dismiss"): {"key": "anon-probe"},
    ("POST", "/api/reminders/snooze"): {"key": "anon-probe", "hours": 24},
}

#: 路径里的 `{proposal_id}` / `{node_id}` / `{relation_id}` 用什么填。
#:
#: 匿名与跨账号两条用例都在**触到那个资源之前**就返回了(401 / 空间不属于你),所以
#: 这里只需要一个格式合法的 UUID —— 形状不对会在路径解析时变成 422,那测的就不是
#: 鉴权了。
_ANY_PROPOSAL_ID = "00000000-0000-4000-8000-000000000001"
_ANY_NODE_ID = "00000000-0000-4000-8000-000000000002"
_ANY_RELATION_ID = "00000000-0000-4000-8000-000000000004"


def _routes_from_openapi() -> set[tuple[str, str]]:
    """从 OpenAPI 文档里现算路由清单。

    **不能用 `app.routes`。** FastAPI 0.141 把 `include_router` 进来的路由包在
    `_IncludedRouter` 里,`app.routes` 根本看不到它们 —— 阶段 1 的验收脚本就因此
    报过一次"没有任何 /api 路由"的假通过。`app.openapi()` 才反映真实表面。
    """
    return {
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        for method in operations
    }


def test_every_route_is_classified() -> None:
    """每个路由要么在 PUBLIC 里,要么在 AUTHENTICATED 里,没有第三种。"""
    actual = _routes_from_openapi()
    classified = set(PUBLIC_ROUTES) | set(AUTHENTICATED_ROUTES)

    unclassified = sorted(actual - classified)
    assert not unclassified, (
        "有路由没有被分类。新接口必须显式决定它要不要登录:\n"
        f"  {unclassified}\n"
        "  需要登录 -> 加进 AUTHENTICATED_ROUTES,并在签名里加 Depends(get_current_user)\n"
        "  匿名可用 -> 加进 PUBLIC_ROUTES,并写清为什么可以匿名"
    )

    stale = sorted(classified - actual)
    assert not stale, f"分类表里这些路由已经不存在了,请删掉: {stale}"


@pytest.mark.parametrize(("method", "template"), sorted(AUTHENTICATED_ROUTES))
async def test_anonymous_is_rejected(
    app_client: httpx.AsyncClient, make_account, method: str, template: str
) -> None:
    """不带 Authorization 头访问,一律 401。

    路径里的 id 用**真实存在**的资源 A 的 id。用假 id 的话,401 可能只是"没找到"的
    副作用,而不是"没登录"的结果 —— 那样测的是别的东西。
    """
    account = await make_account()
    request = app_client.build_request(
        method, _fill(template, account), json=_BODIES.get((method, template))
    )
    response = await app_client.send(request)

    assert response.status_code == 401, f"{method} {template} -> {response.status_code}"
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"
    assert response.headers.get("WWW-Authenticate") == "Bearer"


@pytest.mark.parametrize("token", ["", "not-a-token", "Basic abc", "Bearer", "bearer  "])
async def test_malformed_authorization_is_rejected(
    app_client: httpx.AsyncClient, token: str
) -> None:
    """畸形的 Authorization 头 -> 401,不是 500,也不是放行。"""
    headers = {"Authorization": token} if token else {}
    response = await app_client.get("/api/users/me", headers=headers)

    assert response.status_code == 401, response.text
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


@pytest.mark.parametrize(("method", "template"), sorted(CROSS_ACCOUNT_ROUTES))
async def test_cross_account_access_is_404(
    app_client: httpx.AsyncClient, make_account, method: str, template: str
) -> None:
    """B 拿着 A 的资源 id 访问 -> 404,并且 A 自己访问同一路径是成功的。

    第二句才是这条测试的重点:它排除掉"因为 id 是假的所以 404"这个解释。
    """
    account_a = await make_account(email="a@example.com")
    account_b = await make_account(email="b@example.com", workspace_title="英语")

    path = _fill(template, account_a)
    body = _BODIES.get((method, template))

    # 先证明这个路径对 A 自己是通的。
    mine = await app_client.request(method, path, json=body, headers=account_a.headers)
    assert mine.status_code in (200, 204), f"A 自己都访问不了,后面的断言没有意义: {mine.text}"

    theirs = await app_client.request(method, path, json=body, headers=account_b.headers)
    assert theirs.status_code == 404, (
        f"B 用 A 的 id 访问 {method} {template} 得到 {theirs.status_code},期望 404"
    )
    # 403 会确认"这个 id 存在但不属于你",等于把系统里有哪些空间告诉了任何人。
    assert theirs.status_code != 403
    assert theirs.json()["error"]["code"] in {"WORKSPACE_NOT_FOUND", "NOT_FOUND"}


async def test_workspace_list_only_contains_own_spaces(
    app_client: httpx.AsyncClient, make_account
) -> None:
    """列表接口只能看到自己的空间 —— 授权不是一个接口一个接口地漏,是一整类。"""
    account_a = await make_account(email="a@example.com", workspace_title="A 的空间")
    account_b = await make_account(email="b@example.com", workspace_title="B 的空间")

    listing = await app_client.get("/api/workspaces", headers=account_b.headers)
    assert listing.status_code == 200
    ids = {item["id"] for item in listing.json()}

    assert ids == {account_b.workspace_id}
    assert account_a.workspace_id not in ids


async def test_public_routes_work_without_a_token(app_client: httpx.AsyncClient) -> None:
    """分类成 PUBLIC 的接口,匿名真的能用 —— 否则那张表在自欺。"""
    health = await app_client.get("/health")
    assert health.status_code == 200

    ready = await app_client.get("/ready")
    assert ready.status_code == 200


def _fill(template: str, account: Account) -> str:
    """把路径模板里的占位符换成该账号的真实资源 id。"""
    return (
        template.replace("{workspace_id}", account.workspace_id)
        .replace("{session_id}", account.session_id)
        .replace("{proposal_id}", _ANY_PROPOSAL_ID)
        .replace("{node_id}", _ANY_NODE_ID)
        .replace("{relation_id}", _ANY_RELATION_ID)
    )


@pytest.mark.parametrize("method", ["PATCH", "DELETE"])
async def test_a_node_cannot_be_touched_through_someone_elses_workspace(
    app_client: httpx.AsyncClient, make_account, method: str
) -> None:
    """B 拿 A 的 workspace id + A 的 node id -> 404,而 A 自己动得了。

    单独写一条,是因为上面那张 `CROSS_ACCOUNT_ROUTES` 表覆盖不到它:那张表要求
    "A 用同一个路径必须成功",而拿一个不存在的 node id 时 A 自己也是 404。

    **反向断言在这里格外重要。** 少了"先证明 A 能动它",这个测试完全可能是因为
    节点 id 写错了才 404 —— 那样它是绿的,而节点写接口的归属校验一行都没被验证。
    B 的 404 理由也必须排除 403:403 等于告诉任何人"这个 id 存在,只是不是你的"。
    """
    account_a = await make_account(email="a@example.com")
    account_b = await make_account(email="b@example.com", workspace_title="英语")

    # A 的根目标:建空间时就存在的那一个,也是这里唯一现成的真实节点。
    plan = await app_client.get(
        f"/api/workspaces/{account_a.workspace_id}/plan", headers=account_a.headers
    )
    node_id = next(node["id"] for node in plan.json()["nodes"] if node["parentId"] is None)

    theirs = await app_client.request(
        method,
        f"/api/workspaces/{account_a.workspace_id}/nodes/{node_id}",
        json={"description": "偷偷改一下"} if method == "PATCH" else None,
        headers=account_b.headers,
    )
    assert theirs.status_code == 404, theirs.text
    assert theirs.json()["error"]["code"] == "WORKSPACE_NOT_FOUND"

    # 反向断言:**A 自己用同一个 id 是通的**。PATCH 根目标改说明是允许的
    # (只有删除会被 `RootNodeProtected` 挡住),所以这条路径确实可达。
    if method == "PATCH":
        mine = await app_client.patch(
            f"/api/workspaces/{account_a.workspace_id}/nodes/{node_id}",
            json={"description": "我自己改的"},
            headers=account_a.headers,
        )
        assert mine.status_code == 200, mine.text
        assert mine.json()["node"]["description"] == "我自己改的"


async def test_malformed_uuid_path_is_a_contract_error(
    app_client: httpx.AsyncClient, make_account
) -> None:
    account = await make_account()
    response = await app_client.get(
        f"/api/workspaces/{uuid.uuid4().hex[:8]}", headers=account.headers
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "REQUEST_INVALID"


async def test_unknown_but_valid_uuid_is_404(
    app_client: httpx.AsyncClient, make_account
) -> None:
    """合法但没人用过的 UUID -> 404。与"格式不对"是两回事,状态码也不同。"""
    account = await make_account()
    response = await app_client.get(f"/api/workspaces/{uuid.uuid4()}", headers=account.headers)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "WORKSPACE_NOT_FOUND"
