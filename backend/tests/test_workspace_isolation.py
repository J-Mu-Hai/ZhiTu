"""新空间必须是**空的**。

## 这条测试针对的是一个真实发生过的行为

之前新建空间会把一整套保研 Demo(24 个节点)和一段示例对话克隆进去。用户的感受是:
我建了一个叫"Python 学习"的空间,打开它,里面是别人写的保研计划,还有一句我从没
说过的话。这是产品里最直接的"假装",而它同时污染了数据 —— 后面所有"AI 读不懂我的
计划"的问题都被这段凭空出现的计划搅在一起。

所以这里逐表断言:**新空间有且只有一个根目标节点,其余全是 0。**

## 为什么同时查接口和查库

接口返回 `counts` 是给用户看的,库里没有行是事实。两者都断言,是因为这两种不一致
各有各的失效方式 —— 接口算错了、或者写入路径漏了某张表。只查一边都会漏掉一半。
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models import (
    Conversation,
    Dependency,
    Message,
    PlanningBrief,
    PlanNode,
    PlanRevision,
    Proposal,
    ProposalItem,
    ScheduledSession,
    Workspace,
)


async def _count(db: AsyncSession, model) -> int:
    return int(await db.scalar(select(func.count()).select_from(model)) or 0)


async def test_create_workspace_returns_one_root_goal(
    app_client: httpx.AsyncClient, make_account
) -> None:
    """响应里就该看得出这个空间是空的。"""
    account = await make_account(create_workspace=False)

    response = await app_client.post(
        "/api/workspaces",
        json={
            "title": "Python 学习",
            "intent": "三个月内完成一个项目",
            "goal": "三个月内完成一个能跑起来的小项目",
        },
        headers=account.headers,
    )
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["workspace"]["title"] == "Python 学习"
    assert body["workspace"]["currentRevisionVersion"] == 1
    assert body["workspace"]["counts"] == {
        "nodes": 1,
        "conversations": 0,
        "proposals": 0,
        "scheduledSessions": 0,
    }

    root = body["rootNode"]
    assert root["nodeType"] == "goal"
    assert root["depth"] == 0
    assert root["title"] == "三个月内完成一个能跑起来的小项目"
    assert root["status"] == "pending"


async def test_create_workspace_writes_exactly_two_rows(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession
) -> None:
    """库里逐表核对。**除了 workspaces 与 plan_nodes,其余全是 0。**"""
    account = await make_account(create_workspace=False)
    created = await app_client.post(
        "/api/workspaces",
        json={"title": "英语提升"},
        headers=account.headers,
    )
    assert created.status_code == 201, created.text

    assert await _count(db, Workspace) == 1
    assert await _count(db, PlanNode) == 1

    # 下面每一张表都是"新空间里不该有的东西"。
    for model in (
        Conversation,
        Message,
        Proposal,
        ProposalItem,
        ScheduledSession,
        Dependency,
        PlanningBrief,
    ):
        assert await _count(db, model) == 0, f"{model.__tablename__} 里凭空多出了行"

    assert await _count(db, PlanRevision) == 0, (
        "新建空间不应产生计划版本:版本历史从第一次真正的变更开始,"
        "提前写一条意味着现在就定死了快照的 JSON 形状。"
    )


async def test_root_goal_falls_back_to_the_space_title(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession
) -> None:
    """用户没单独写目标时,根目标用空间名 —— 那也是用户自己输入的字。"""
    account = await make_account(create_workspace=False)
    created = await app_client.post(
        "/api/workspaces", json={"title": "考研准备"}, headers=account.headers
    )
    assert created.status_code == 201

    assert created.json()["rootNode"]["title"] == "考研准备"


async def test_root_goal_has_no_parent(make_account, db: AsyncSession) -> None:
    """根节点必须真的没有父节点,而不是"父节点恰好查不到"。"""
    account = await make_account()
    node = (
        await db.execute(
            select(PlanNode).where(PlanNode.workspace_id == uuid.UUID(account.workspace_id))
        )
    ).scalar_one()

    assert node.parent_id is None
    assert node.depth == 0
    assert node.deleted_at is None


@pytest.mark.parametrize("include_archived", [False, True])
async def test_new_space_is_visible_in_the_list(
    app_client: httpx.AsyncClient, make_account, include_archived: bool
) -> None:
    account = await make_account()
    response = await app_client.get(
        "/api/workspaces",
        params={"includeArchived": str(include_archived).lower()},
        headers=account.headers,
    )
    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()] == [account.workspace_id]


async def test_archiving_hides_from_the_default_list_and_can_be_undone(
    app_client: httpx.AsyncClient, make_account
) -> None:
    """归档是可逆的 —— 这是"没有 DELETE"能不能成立的前提。"""
    account = await make_account()

    archived = await app_client.patch(
        f"/api/workspaces/{account.workspace_id}",
        json={"status": "archived"},
        headers=account.headers,
    )
    assert archived.status_code == 200, archived.text
    assert archived.json()["status"] == "archived"
    assert archived.json()["archivedAt"] is not None

    default_list = await app_client.get("/api/workspaces", headers=account.headers)
    assert default_list.json() == []

    with_archived = await app_client.get(
        "/api/workspaces", params={"includeArchived": "true"}, headers=account.headers
    )
    assert [item["id"] for item in with_archived.json()] == [account.workspace_id]

    restored = await app_client.patch(
        f"/api/workspaces/{account.workspace_id}",
        json={"status": "active"},
        headers=account.headers,
    )
    assert restored.json()["status"] == "active"
    # status 与 archived_at 必须一起变,不能留下"已恢复但归档时间还在"的行。
    assert restored.json()["archivedAt"] is None


async def test_archived_space_can_still_be_read(
    app_client: httpx.AsyncClient, make_account
) -> None:
    """归档是用来隐藏的,不是用来锁死的。"""
    account = await make_account()
    await app_client.patch(
        f"/api/workspaces/{account.workspace_id}",
        json={"status": "archived"},
        headers=account.headers,
    )

    response = await app_client.get(
        f"/api/workspaces/{account.workspace_id}", headers=account.headers
    )
    assert response.status_code == 200
    assert response.json()["status"] == "archived"


async def test_unknown_fields_in_the_request_body_are_rejected(
    app_client: httpx.AsyncClient, make_account
) -> None:
    """打错字的字段名必须报错,而不是被静默丢掉。

    静默丢掉的表现是"我明明填了,它就是没保存" —— 用户没有任何线索可以追。
    """
    account = await make_account(create_workspace=False)
    response = await app_client.post(
        "/api/workspaces",
        json={"titel": "拼错了的字段名"},
        headers=account.headers,
    )
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["error"]["code"] == "REQUEST_INVALID"
    assert any("titel" in field["field"] for field in body["error"]["details"]["fields"])
