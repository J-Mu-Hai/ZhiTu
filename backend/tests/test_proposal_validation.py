"""非法模型输出不产生任何计划写入。

## 这个文件在防什么

模型吐出来的 JSON 是**外部输入**。它可以写出不存在的节点、写出 2026-13-45、写出
一次删掉整棵树的动作,也可以被用户粘贴进对话里的一段文字带偏。这一组测试逐条证明:
这些输入会被拒绝,而且**拒绝的时候一行计划都没写**。

## 为什么断言的是几张表,不是全部表

"零写入"在字面上是不成立的:用户那句话和模型的回复本来就该落库(那是阶段 3 的核心
保证,见 conversation_service 里"两次提交"的说明)。一口咬定 `snapshot(db) == before`
会因为消息表多了两行而失败 —— 那是个假失败,而且会诱使人把断言放松成"大概没写什么"。

真正要证明的是:**这次拒绝没有碰任何一份计划数据**。所以断言限定在这几张表上。
它们正好覆盖"一次成功的确认会动到的全部东西"(见 proposal_service 的 `_apply`),
所以它是一句能抓住所有部分写入回归的否定断言,而不是一句含糊的话。
"""

from __future__ import annotations

import httpx
import pytest

from backend.services import proposal_validation as codes
from backend.tests.conftest import FakeReasoner, snapshot

#: 一次成功的确认会动的全部表。拒绝路径上它们必须一行不变。
PLAN_TABLES = frozenset(
    {
        "plan_nodes",
        "dependencies",
        "plan_revisions",
        "proposals",
        "proposal_items",
        "proposal_decisions",
    }
)


async def _send(client: httpx.AsyncClient, account, content: str = "帮我排一下"):
    return await client.post(
        f"/api/workspaces/{account.workspace_id}/messages",
        json={"content": content},
        headers=account.headers,
    )


def _plan_only(snapshot: dict[str, int]) -> dict[str, int]:
    return {name: count for name, count in snapshot.items() if name in PLAN_TABLES}


#: (用例名, 模型提的 actions, 期望的错误码)
#
#: 每一条都是模型**真的可能吐出来**的东西,不是编出来凑数的:
#: `deadline` 写成 `2026-13-45` 是它把月份和日期记反了;`999999` 分钟是它把
#: "每周 6 小时"换算错单位;`parentRef` 指向 n9 是它在编一个不存在的阶段。
BAD_ACTIONS = [
    (
        "未知的操作类型",
        [{"op": "summon_dragon", "targetRef": "n1"}],
        codes.UNKNOWN_OP_TYPE,
    ),
    (
        "本阶段还没实现的排期操作",
        [{"op": "schedule_sessions", "nodeRef": "n1", "sessions": []}],
        codes.OP_NOT_YET_AVAILABLE,
    ),
    (
        "引用了一个不存在的记号",
        [{"op": "update_node", "targetRef": "n7", "title": "改个名字"}],
        codes.DANGLING_PROPOSAL_REF,
    ),
    (
        "引用了别的空间的节点 id",
        # 模型看不到真实 UUID,但提示注入可以让它试着写出一个。这里给一个格式合法
        # 的 id,证明它**不会**被"查一下数据库看看存不存在"这条路径放行。
        [{"op": "update_node", "targetRef": "0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b", "title": "x"}],
        codes.PAYLOAD_SCHEMA_INVALID,
    ),
    (
        "日期不存在",
        [
            {
                "op": "create_node",
                "localId": "n2",
                "parentRef": "n1",
                "title": "读文档",
                "deadline": "2026-13-45",
            }
        ],
        codes.PAYLOAD_SCHEMA_INVALID,
    ),
    (
        "标题超长",
        [
            {
                "op": "create_node",
                "localId": "n2",
                "parentRef": "n1",
                "title": "很长" * 200,
            }
        ],
        codes.PAYLOAD_SCHEMA_INVALID,
    ),
    (
        "工时大得不像话",
        [
            {
                "op": "create_node",
                "localId": "n2",
                "parentRef": "n1",
                "title": "学完 Python",
                "estimateMinutes": 999999,
            }
        ],
        codes.PAYLOAD_SCHEMA_INVALID,
    ),
    (
        "多了一个没定义的字段",
        [
            {
                "op": "create_node",
                "localId": "n2",
                "parentRef": "n1",
                "title": "读文档",
                "deadLine": "2026-10-01",
            }
        ],
        codes.PAYLOAD_SCHEMA_INVALID,
    ),
    (
        "截止日期在过去",
        [
            {
                "op": "create_node",
                "localId": "n2",
                "parentRef": "n1",
                "title": "读文档",
                "deadline": "2020-01-01",
            }
        ],
        codes.DEADLINE_IN_PAST,
    ),
    (
        "删除根目标",
        [{"op": "delete_node", "targetRef": "n1"}],
        codes.CANNOT_DELETE_ROOT,
    ),
    (
        "两个新节点用同一个记号",
        [
            {"op": "create_node", "localId": "n2", "parentRef": "n1", "title": "A"},
            {"op": "create_node", "localId": "n2", "parentRef": "n1", "title": "B"},
        ],
        codes.DUPLICATE_LOCAL_ID,
    ),
    (
        "父子关系成环(向前引用)",
        # 只允许向后引用这一条规则,让父环在结构上不可能出现 —— 这里证明它确实
        # 被挡住了,而不是"设想过它不会发生"。
        [
            {"op": "create_node", "localId": "n2", "parentRef": "n1", "title": "阶段"},
            {"op": "create_node", "localId": "n3", "parentRef": "n3", "title": "自己当自己的爹"},
        ],
        codes.DANGLING_PROPOSAL_REF,
    ),
    (
        "依赖自己",
        [{"op": "create_dependency", "predecessorRef": "n1", "successorRef": "n1"}],
        codes.SELF_DEPENDENCY,
    ),
    (
        "依赖成环",
        [
            {"op": "create_node", "localId": "n2", "parentRef": "n1", "title": "A"},
            {"op": "create_node", "localId": "n3", "parentRef": "n1", "title": "B"},
            {"op": "create_dependency", "predecessorRef": "n2", "successorRef": "n3"},
            {"op": "create_dependency", "predecessorRef": "n3", "successorRef": "n2"},
        ],
        codes.DEPENDENCY_CYCLE,
    ),
]


@pytest.mark.parametrize(
    ("label", "actions", "expected_code"),
    BAD_ACTIONS,
    ids=[case[0] for case in BAD_ACTIONS],
)
async def test_bad_model_output_is_rejected_without_writing_plan_data(
    app_client: httpx.AsyncClient,
    make_account,
    use_reasoner,
    db,
    label: str,
    actions: list[dict],
    expected_code: str,
) -> None:
    """逐条证明:拒绝,给出具体原因,计划一行没动。"""
    account = await make_account()
    use_reasoner(FakeReasoner(actions=tuple(actions)))

    before = _plan_only(await snapshot(db))
    response = await _send(app_client, account)
    after = _plan_only(await snapshot(db))

    assert response.status_code == 200, f"{label}: {response.text}"
    body = response.json()

    # 提案没有被建立 —— 校验没通过的东西不该在库里留下一个"待确认"的壳。
    assert body["proposal"] is None, f"{label}: 非法的变更不应该产生提案"
    errors = body["proposalErrors"]
    assert errors, f"{label}: 拒绝了却没说为什么"
    assert expected_code in {e["code"] for e in errors}, (
        f"{label}: 期望 {expected_code},实际 {[e['code'] for e in errors]}"
    )
    assert after == before, f"{label}: 拒绝路径上写了计划数据"


async def test_a_rejected_proposal_leaves_no_stale_node_behind(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db
) -> None:
    """一半合法一半非法时,合法的那些也不写。

    `validate_actions` 是**纯函数**,失败时返回的 `plan` 是 None —— 不是"写了一半
    再回滚"。这条测试把那个结构性质变成一个能看见的断言:前两条完全合法、
    第三条引用不存在的节点,结果一条都不该落地。
    """
    account = await make_account()
    use_reasoner(
        FakeReasoner(
            actions=(
                {"op": "create_node", "localId": "n2", "parentRef": "n1", "title": "阶段一"},
                {"op": "create_node", "localId": "n3", "parentRef": "n2", "title": "任务一"},
                {"op": "update_node", "targetRef": "n99", "title": "改一个不存在的"},
            )
        )
    )

    before = _plan_only(await snapshot(db))
    response = await _send(app_client, account)
    after = _plan_only(await snapshot(db))

    assert response.status_code == 200, response.text
    assert response.json()["proposal"] is None
    assert after == before

    # 再加一句:确实没有节点被建出来,而不是"节点表恰好没变"。
    assert after["plan_nodes"] == 1, "空空间里应该只有根目标一个节点"


async def test_a_node_can_be_edited_and_have_children_added_at_once(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """**真实模型踩出来的一条。**

    它给根目标补了一句说明,再往根目标下面挂了四个阶段 —— 一个再合理不过的计划。
    而校验器把它整份拒了,连带给出一串误导性的后续错误("引用了不存在的 n2",
    因为那个 n2 本来该由被拒的那条建出来)。

    根因是把"这个节点被改过"和"这个节点被删了"当成了同一件事。`update_node` 根本
    没有 `parent_ref` —— 改标题、改说明、改截止时间都不会动到树的结构,所以在一个
    正在被改的节点下面新建子节点是完全自洽的。被删掉才是真的冲突。
    """
    account = await make_account()
    use_reasoner(
        FakeReasoner(
            actions=(
                {"op": "update_node", "targetRef": "n1", "description": "三个月从零到做出一个项目"},
                {"op": "create_node", "localId": "n2", "parentRef": "n1", "title": "阶段一"},
                {"op": "create_node", "localId": "n3", "parentRef": "n2", "title": "任务一"},
            )
        )
    )

    response = await _send(app_client, account)
    body = response.json()

    assert body["proposalErrors"] == [], f"这份计划被拒了: {body['proposalErrors']}"
    assert body["proposal"] is not None
    # 三条都要在:一次修改 + 两次新增。少一条就说明"接受"只是表面上的。
    assert body["proposal"]["itemCount"] == 3


async def test_a_deleted_node_cannot_gain_children_in_the_same_proposal(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """反过来那一半:往一个**正在被删**的节点下面加子节点,必须拒绝。

    否则会建出一个父节点已经被软删除的子节点 —— 它在界面上永远不可达,
    而用户找不到任何入口去处理它。

    这里的阶段是**上一轮真的建出来的**,因为删除只能作用于已存在的节点
    (删一个"这次提案里刚说要建"的东西是自相矛盾的,那会先被 DANGLING 挡掉)。
    """
    account = await make_account()

    use_reasoner(
        FakeReasoner(
            actions=(
                {"op": "create_node", "localId": "n2", "parentRef": "n1", "title": "阶段一"},
            )
        )
    )
    first = (await _send(app_client, account, "排一版")).json()
    assert first["proposal"] is not None

    confirmed = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/proposals/{first['proposal']['id']}/confirm",
        json={"idempotencyKey": "delete-guard-key"},
        headers=account.headers,
    )
    assert confirmed.status_code == 200, confirmed.text

    # 现在「阶段一」是真实存在的 n2。删掉它,同时又要往它下面挂一个任务。
    use_reasoner(
        FakeReasoner(
            actions=(
                {"op": "delete_node", "targetRef": "n2"},
                {"op": "create_node", "localId": "n3", "parentRef": "n2", "title": "孤儿任务"},
            )
        )
    )
    body = (await _send(app_client, account, "重排一下")).json()

    assert body["proposal"] is None
    assert codes.CONFLICTING_OPERATIONS in {e["code"] for e in body["proposalErrors"]}


async def test_actions_are_ignored_when_the_model_says_nothing(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db
) -> None:
    """模型没提变更 -> `proposal` 与 `proposalErrors` **都是空**。

    这一条与上面那组的分工是"沉默"和"说了但做不到"的区别。前端据此决定:前者
    什么都不显示,后者必须如实告诉用户"这次没能执行"。两者都用 `proposal is None`
    表示的话,界面只能二选一,总有一边是错的。
    """
    account = await make_account()
    use_reasoner(FakeReasoner())

    response = await _send(app_client, account)
    body = response.json()

    assert body["proposal"] is None
    assert body["proposalErrors"] == []
