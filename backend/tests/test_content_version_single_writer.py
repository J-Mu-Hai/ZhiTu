"""正文版本号的**写侧**:规则只有一份,而且两条写入路径都真的用上了它。

## 这一组在防什么

`plan_nodes.content_version` 是客户端手里那个"我这份正文是第几版"的号。它只有
一个用处:把**丢失更新**变成一次看得见的 409,而不是一次谁都不知道的覆盖。

它要成立,前提是**每一次正文写入都推进它**。改一个已有节点的正文有两条路:

    用户直接编辑    PATCH /nodes/{id}             -> node_service.update_node
    AI 提案确认     POST /proposals/{id}/confirm  -> proposal_service._apply

原来只有第一条推进它。第二条(`UpdateNodeAction` 带 `description`)只 `setattr`,
于是存在过这样一条**两边都不会看到冲突**的路:

    用户开着编辑器(手上有第 1 版)
      → AI 提案改写正文,用户点了确认(库里已经是新文字)
      → 用户按保存,带着第 1 版
      → 通过 → **他刚刚亲手确认过的那段改写被静默盖掉**

所以这里钉三件事:推进这一列的地方全仓只有一处(**AST 扫出来的**,不是"我看过了");
提案改正文会 +1;提案确认之后旧编辑器的那一次保存会被 409 拦住、且一个字都不写。

## 为什么不去封闭"服务端内部调用不带版本号"

不带版本号 = 不检查,这是**有意**的(见 `UpdateNodeRequest.content_version`):内部
调用方(提案确认、排期、归档恢复)本来就持有工作区锁,不该被一个"你手上那份旧了"
挡住。本批要求的是"**写的时候版本必须动**",不是"写的时候必须报版本"——后者会把
内部调用全逼着去读一遍版本号,而它们真正需要的那个保护是锁,已经有了。

## 反过来,模型也不能自己设置这一列

版本由服务端维护。模型在动作里塞一个 `contentVersion` 进去,无论它是被拒掉还是被
忽略,这一列都不能变成它说的那个数(最后一条用例)。
"""

from __future__ import annotations

import ast
from pathlib import Path

import httpx

from backend.tests.conftest import FakeReasoner

#: 仓库根。`backend/tests/x.py` 往上数三层。
ROOT = Path(__file__).resolve().parents[2]

#: 扫这些目录找"谁在写这一列"。测试自己可以随便写(它们本来就是造场景的)。
SCANNED = ("backend/services", "backend/api", "backend/agent")

COLUMN = "content_version"

#: 允许写这一列的函数。**闭集**(见那条用例的 docstring):多一处写入点,就要在这里
#: 显式加一行并说明它管的是哪张表。
ALLOWED_WRITERS = {
    # 节点正文:`plan_nodes.content_version`。用户 PATCH 与提案确认共用。
    "backend/services/node_service.py:touch_content_version",
    # 笔记正文:`node_notes.content_version`。用户 PUT 笔记与提案里的
    # `update_note` 共用同一份理由,只是对象换成了长正文。
    "backend/services/note_service.py:touch_note_content_version",
}


def _sources() -> list[Path]:
    return sorted(path for part in SCANNED for path in (ROOT / part).rglob("*.py"))


def _writes_to_the_column(path: Path) -> list[tuple[str, int]]:
    """这份文件里,哪些函数给 `.content_version` 赋过值。

    返回 `(函数名, 行号)`。用 AST 而不是 grep:注释、文档字符串、`==` 比较、
    读属性全都会让 grep 命中,而"到底有没有写"必须问语法树。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[str, int]] = []
    stack: list[str] = []

    def visit(node: ast.AST) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            stack.append(node.name)
            for child in ast.iter_child_nodes(node):
                visit(child)
            stack.pop()
            return
        if isinstance(node, (ast.Assign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Attribute) and target.attr == COLUMN:
                    found.append((".".join(stack) or "<module>", node.lineno))
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(tree)
    return found


def test_the_body_version_is_written_in_exactly_one_function() -> None:
    """全仓只有那两个函数能改**各自的** `content_version`,一个函数管一张表。

    这条断言不是洁癖:两条路径各写一遍 `+ 1`,迟早有一条被漏掉 —— 而漏掉的那条
    不会报错、不会变慢、也不会让任何测试红,它只会让某一次写入在版本上**不存在**。
    提案路径就是这么漏的。所以这里钉死"只有一个写入点",让它以后想漏都没地方漏。

    ## 为什么是两张表、两条记录,而不是一条

    `plan_nodes.content_version` 与 `node_notes.content_version` 是两列**同名**的
    版本号,而且是刻意分开的:冲突检测的范围要和冲突的范围一样大,共用会让
    "有人改了那个节点 300 字的简述"变成"你正在写的 20,000 字笔记保存失败"。

    所以这里比的是一个**闭集**:以后每多一个版本列,就必须在这里显式加一行。
    这行加得出来,但加不出来的是"顺手在某个服务里写一句 `x.content_version += 1`"
    —— 那正是这条用例要挡的动作。
    """
    found = [
        (path.relative_to(ROOT).as_posix(), where, line)
        for path in _sources()
        for where, line in _writes_to_the_column(path)
    ]
    writers = {f"{path}:{where}" for path, where, _line in found}
    assert writers == ALLOWED_WRITERS, (
        "推进版本号的地方和这张名单不一致(多了、少了,或者挪了位置)。"
        f"现在扫到:{[f'{p}:{w}:{n}' for p, w, n in found] or '一处都没有'}\n"
        "每一列版本号只允许一个推进函数,而且必须是两条写入路径共用的那一个"
        "——否则总会有一条忘了写。加一个新版本列是允许的:在 ALLOWED_WRITERS 里"
        "显式加一行,并写清楚它管的是哪张表。"
    )


# ---------------------------------------------------------------------------------
# 辅助:走接口,不直接写库
# ---------------------------------------------------------------------------------
async def _plan(client: httpx.AsyncClient, account) -> dict:
    response = await client.get(
        f"/api/workspaces/{account.workspace_id}/plan", headers=account.headers
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _root_id(client: httpx.AsyncClient, account) -> str:
    plan = await _plan(client, account)
    return next(node["id"] for node in plan["nodes"] if node["parentId"] is None)


async def _node(client: httpx.AsyncClient, account, node_id: str) -> dict:
    plan = await _plan(client, account)
    return next(node for node in plan["nodes"] if node["id"] == node_id)


def _asks_the_model_to_write(description: str) -> FakeReasoner:
    """让假模型提一条"改写根目标正文"的提案。

    `n1` 是建空间时那条根目标 —— 记号的编号从它开始(见 `services/turn_context.py`)。
    """
    return FakeReasoner(
        reply="我把这条的正文改写了一下。",
        actions=({"op": "update_node", "targetRef": "n1", "description": description},),
    )


async def _propose(client: httpx.AsyncClient, account) -> dict | None:
    response = await client.post(
        f"/api/workspaces/{account.workspace_id}/messages",
        json={"content": "帮我把这条的正文写清楚"},
        headers=account.headers,
    )
    assert response.status_code == 200, response.text
    return response.json()["proposal"]


async def _confirm(
    client: httpx.AsyncClient, account, proposal_id: str, key: str = "single-writer-key"
) -> httpx.Response:
    return await client.post(
        f"/api/workspaces/{account.workspace_id}/proposals/{proposal_id}/confirm",
        json={"idempotencyKey": key},
        headers=account.headers,
    )


async def _save_body(
    client: httpx.AsyncClient, account, node_id: str, text: str, version: int
) -> httpx.Response:
    return await client.patch(
        f"/api/workspaces/{account.workspace_id}/nodes/{node_id}",
        json={"description": text, "contentVersion": version},
        headers=account.headers,
    )


# ---------------------------------------------------------------------------------
# 提案改正文:版本要动
# ---------------------------------------------------------------------------------
async def test_an_ai_proposal_that_rewrites_the_body_bumps_the_version(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    account = await make_account()
    root = await _root_id(app_client, account)
    assert (await _node(app_client, account, root))["contentVersion"] == 1

    use_reasoner(_asks_the_model_to_write("AI 改写过的正文:先做实验,再补对照。"))
    proposal = await _propose(app_client, account)
    assert proposal is not None, "提案没有被建立"

    confirmed = await _confirm(app_client, account, proposal["id"])
    assert confirmed.status_code == 200, confirmed.text

    after = await _node(app_client, account, root)
    assert after["description"] == "AI 改写过的正文:先做实验,再补对照。"
    assert after["contentVersion"] == 2, (
        "AI 改过正文,版本号却没动 —— 那么客户端手里那个旧号看起来仍然有效,"
        "它下一次保存就会把这条用户刚确认过的改写静默盖掉。"
    )


async def test_an_editor_holding_the_old_version_cannot_overwrite_the_confirmed_rewrite(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """这一条就是上面那段注释里那个场景,一步一步走一遍。

    用户开着编辑器(手上有第 1 版)→ AI 提案改写正文、用户点确认 → 用户按保存。
    保存**必须**被拦住:界面会告诉他"库里已经是第 2 版",而不是让他把那段改写吃掉
    之后两边都显示成功。
    """
    account = await make_account()
    root = await _root_id(app_client, account)

    use_reasoner(_asks_the_model_to_write("AI 改写过的正文。"))
    proposal = await _propose(app_client, account)
    assert proposal is not None
    assert (await _confirm(app_client, account, proposal["id"])).status_code == 200

    refused = await _save_body(app_client, account, root, "我手上这一份旧文字。", version=1)
    assert refused.status_code == 409, (
        f"旧编辑器把 AI 改写过的正文盖掉了(返回 {refused.status_code})—— 两边都不会看到冲突。"
    )
    details = refused.json()["error"]["details"]
    assert details["content_version"] == 2
    assert details["expected_content_version"] == 1

    still = await _node(app_client, account, root)
    assert still["description"] == "AI 改写过的正文。", "被拒绝的那一次写入必须一个字都不写"
    assert still["contentVersion"] == 2, "被拒绝的写入不能推进版本号"


async def test_an_ai_proposal_that_only_changes_other_fields_does_not_bump_the_version(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """非正文字段仍然不推进版本号 —— 两条路径的判据必须是同一个。

    否则会出现一种很别扭的交互:AI 只是提了一句"这条估时改成 90 分钟",用户在另一个
    标签页里写的正文就被判成了过期。
    """
    account = await make_account()
    root = await _root_id(app_client, account)

    use_reasoner(
        FakeReasoner(actions=({"op": "update_node", "targetRef": "n1", "estimateMinutes": 90},))
    )
    proposal = await _propose(app_client, account)
    assert proposal is not None
    assert (await _confirm(app_client, account, proposal["id"])).status_code == 200

    after = await _node(app_client, account, root)
    assert after["estimateMinutes"] == 90
    assert after["contentVersion"] == 1, "改工时不该推进正文版本号"


# ---------------------------------------------------------------------------------
# 模型也不能自己设置这一列
# ---------------------------------------------------------------------------------
async def test_the_model_cannot_write_the_version_itself(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """模型在动作里塞一个版本号:被拒掉也好、被忽略也好,这一列都不能听它的。

    真正要钉的不变式是这一条:**版本号前进,当且仅当正文真的变了。** 它比"某个字段
    被丢弃"更结实 —— 以后谁调整动作契约的形状,这条仍然成立。
    """
    account = await make_account()
    root = await _root_id(app_client, account)
    before = await _node(app_client, account, root)

    use_reasoner(
        FakeReasoner(
            actions=(
                {
                    "op": "update_node",
                    "targetRef": "n1",
                    "description": "正文改了,但版本号是模型自己填的",
                    "contentVersion": 99,
                },
            )
        )
    )
    proposal = await _propose(app_client, account)
    if proposal is not None:
        assert (await _confirm(app_client, account, proposal["id"])).status_code == 200

    after = await _node(app_client, account, root)
    assert after["contentVersion"] != 99, "模型自己填的版本号被写进库了"
    assert after["contentVersion"] == before["contentVersion"] + (
        1 if after["description"] != before["description"] else 0
    ), "版本号前进必须当且仅当正文变了"
