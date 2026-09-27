"""节点长正文(「笔记」)的读写。§2.2。

## 与 `node_service` 的分界

那个文件的每个写操作都必须走 `_each_change`(守卫锁 + 新版本号 + `plan_revisions`
一行 + `domain_events` 一行),这个文件**一次都不走**。理由在那个上下文管理器的
docstring 里,以及 `layout_service` 模块头的同一段话:**版本号回答的是"计划变成
什么样了"**,而一段笔记改写不改变计划 —— 让每一次自动保存都记一版,用户复盘时
要问的"V7 把我哪个排期挪走了"就得从几百版里翻。

还有一条更硬的:推进 `revision_version` 会让**待确认的提案凭空失效**(见
`db/models/note.py` 的模块 docstring)。用户一边看 AI 的分析一边补两句笔记,
然后点确认 —— 他得到的会是一句"计划已经变了",而计划一个字都没变。

## 但它**要**那把空间锁(与 `layout_service` 不同)

布局是每人一份,笔记是**每个节点一份、整个空间共享**。两个标签页同时给同一个
节点发第一次保存(都带 `contentVersion: 0`),没有锁的话两边都会读到"还没有行",
两边都 INSERT,第二个撞唯一约束 —— 而 `backend/api/errors.py` 刻意让
`IntegrityError` 照常 500(那是真的不该发生的错误)。锁内读则第二个请求看到行、
返回 409,和节点正文那条路的行为一模一样。

顺序必须是**先加锁、再读、再比、再写**(`save` 就是这么写的):反过来,"读到 v1
→ 别人写成 v2 → 我按 v1 写下去"这三步里的中间那一步谁都拦不住。

## 上限

`MAX_NOTE_CODEPOINTS`(20,000 码点)在**写入时**执行,**先拒不写**:截断一段
用户打的字比拒绝它糟得多 —— 他会以为自己的东西存下来了。这不是"前端也拦一道"
就够的事,界面上的计数只是提前告知。
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.contracts.plan import MAX_NOTE_CODEPOINTS, NotePayload, PutNoteRequest
from backend.db.base import utcnow
from backend.db.locking import lock_workspace
from backend.db.models import NodeNote
from backend.db.models.enums import NodeOrigin
from backend.services import node_service
from backend.services.context import WorkspaceContext
from backend.services.errors import ConcurrencyConflict, InvalidInput


def payload_of(note: NodeNote | None, node_id: uuid.UUID) -> NotePayload:
    """一行笔记 -> 接口载荷。`None`(从来没写过)映射成空正文 + 第 0 版。"""
    if note is None:
        return NotePayload(node_id=node_id)
    return NotePayload(
        node_id=node_id,
        body=note.body,
        content_version=note.content_version,
        updated_at=note.updated_at,
    )


def touch_note_content_version(note: NodeNote, body: str) -> bool:
    """把新正文写进去,并**在真的变了的时候**推进版本号;返回是否推进了。

    **全仓唯一推进 `node_notes.content_version` 的地方。** 与
    `node_service.touch_content_version` 是同一条纪律,理由也一样:改一份已有笔记
    有两条路(用户在编辑器里自动保存、AI 提案里的 `update_note`),各写一遍 `+ 1`
    的后果是迟早有一条忘了写 —— 而忘写的那条不会报错,它只是让某一次写入在版本上
    **不存在**,于是另一个标签页手里的旧版本号看起来仍然有效,下一次保存就安静地
    把别人刚写的东西盖掉。`test_content_version_single_writer.py` 里那条 AST 用例
    把这两处一起钉住。

    "真的变了才 +1" 与节点正文那条路一致:`content_version` 的前进当且仅当正文变了。
    用户打开编辑器、什么都没改就自动保存一次,不该让别人手里的版本号作废。
    """
    if body == note.body:
        return False
    note.body = body
    note.content_version = note.content_version + 1
    return True


async def load_for(db: AsyncSession, ctx: WorkspaceContext, node_id: uuid.UUID) -> NotePayload:
    """读一个节点的笔记。

    **还没有写过不是错误**:返回空正文 + `contentVersion: 0`,不是 404。编辑器打开时
    的第一件事就是这个 GET,404 会把"这个节点还没有笔记"变成一个错误路径 —— 而它
    是完全正常的状态。

    节点本身走 `node_service.load_node`:不在这个空间里、或者已归档的节点,这里同样是
    404(与其它节点路由一致,否则拿别人的节点 id 就能读到别人的笔记)。
    """
    node = await node_service.load_node(db, ctx, node_id)
    return payload_of(await _row(db, ctx, node.id), node.id)


async def save(
    db: AsyncSession, ctx: WorkspaceContext, node_id: uuid.UUID, payload: PutNoteRequest
) -> NotePayload:
    """用户在编辑器里保存这一份笔记。"""
    body = _checked(payload.body)

    # 先加锁,再读。见模块 docstring 里"两个标签页同时发第一次保存"那段。
    await lock_workspace(db, ctx.id)
    node = await node_service.load_node(db, ctx, node_id)
    note = await _row(db, ctx, node.id)

    current = note.content_version if note is not None else 0
    expected = payload.expected_content_version
    if expected is not None and expected != current:
        raise ConcurrencyConflict(
            f"这份笔记在别处被改过了(你手上是第 {expected} 版,库里已经是第 {current} 版),"
            "所以这次没有写进去。",
            content_version=current,
            expected_content_version=expected,
        )

    if note is None and not body:
        # "没有笔记"与"笔记是空的"不给用户两件看不出区别的事:一个节点从没写过笔记,
        # 而这一次要写的也是空的 —— 那就什么都不必存,也不必让它从此有第 1 版。
        # 事务随请求一起结束,那把守卫写(见 `lock_workspace`)跟着一起丢掉。
        return payload_of(None, node.id)

    written = await _write(db, ctx, node.id, note, body, origin=NodeOrigin.USER)
    await db.commit()
    return payload_of(written, node.id)


async def apply_body(
    db: AsyncSession,
    ctx: WorkspaceContext,
    node_id: uuid.UUID,
    *,
    body: str,
    expected_version: int | None = None,
    origin: NodeOrigin = NodeOrigin.AI,
) -> NodeNote | None:
    """提案确认那条路写笔记。**调用方必须已经持有空间上的锁。**

    (那个锁由 `proposal_service.confirm_proposal` 在重校验之前拿下,覆盖到写入,
    所以这里不再加一道 —— 同一把锁加两次会自己等自己。)

    返回写进去的那一行;`None` 表示"没有东西可写"(节点从来没有笔记,而这次要写的
    也是空的)。调用方据此决定要不要把它算进"实际写了几项"。

    **调用方还必须已经确认这个节点属于本空间、且未被删除** —— 与 `_apply` 对修改
    路径的要求一致(那里是 `db.get` + `workspace_id` 比对)。这里刻意不重复查一次:
    多一次查询换不来更多保证,而"两处都查、只改一处"的分叉才是风险。
    """
    cleaned = _checked(body)
    note = await _row(db, ctx, node_id)
    current = note.content_version if note is not None else 0
    if expected_version is not None and expected_version != current:
        # 正常情况下到不了这里:`_revalidate` 刚刚拿同一个号比过一次(同一个锁里)。
        # 留着它是因为"校验与写入之间没有窗口"是一条**依赖调用顺序**的性质,
        # 而顺序会被人改。
        raise ConcurrencyConflict(
            f"这份笔记在确认之前被改过了(提案基于第 {expected_version} 版,"
            f"库里已经是第 {current} 版),所以这次没有写进去。",
            content_version=current,
            expected_content_version=expected_version,
        )
    if note is None and not cleaned:
        return None

    return await _write(db, ctx, node_id, note, cleaned, origin=origin)


async def _write(
    db: AsyncSession,
    ctx: WorkspaceContext,
    node_id: uuid.UUID,
    note: NodeNote | None,
    body: str,
    *,
    origin: NodeOrigin,
) -> NodeNote:
    """真正落那一行。**调用方必须已经持有锁、并比过版本。**

    正文**原样存**:不去首尾空白、不做归一化。长文本里缩进和空行是内容的一部分 ——
    `node_service._clean` 那套(去首尾空白、空串归 `None`)是给 300 字的简述用的,
    用在 20,000 字的正文上会把用户粘贴进来的排版吃掉。
    """
    if note is None:
        note = NodeNote(
            workspace_id=ctx.id,
            node_id=node_id,
            body="",
            # 从第 0 版开始,再由 `touch_note_content_version` 推到第 1 版。
            # 不直接写成 1:那样"版本号前进当且仅当正文变了"就会有一条例外,
            # 而列上的 `default=1` 是给"迁移之前就存在的行"用的,不是给这条路的。
            content_version=0,
            origin=origin,
        )
        db.add(note)
        await db.flush()

    touch_note_content_version(note, body)
    note.origin = origin
    note.updated_at = utcnow()
    await db.flush()
    return note


async def _row(db: AsyncSession, ctx: WorkspaceContext, node_id: uuid.UUID) -> NodeNote | None:
    return await db.scalar(
        select(NodeNote).where(NodeNote.node_id == node_id, NodeNote.workspace_id == ctx.id)
    )


def _checked(body: str) -> str:
    """太长就**拒绝**,不截断。返回原样的正文。"""
    if len(body) > MAX_NOTE_CODEPOINTS:
        raise InvalidInput(
            f"长正文最多 {MAX_NOTE_CODEPOINTS} 个字(按 Unicode 码点计),"
            f"这一份有 {len(body)} 个。"
        )
    return body


__all__ = [
    "apply_body",
    "load_for",
    "payload_of",
    "save",
    "touch_note_content_version",
]
