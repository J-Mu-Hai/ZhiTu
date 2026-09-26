"""把一次对话轮次需要的全部上下文组装出来。

## 这个文件修的是产品最核心的一个缺陷

原来的前端在 `send()` 里只发了四个字段(空间 id、当前节点 id、视图名、消息正文),
**既不带历史也不带计划**。所以后端那个 agent 看不到用户真实的空间里有什么,
只能看到它自己上一轮提议过的东西。用户说"把刚才那个阶段往后挪一周",
模型没有"刚才那个阶段"可指 —— 它只能猜。

现在这里把三样东西一起送进去:

1. **今天是几号**(按用户时区算,不是 UTC)
2. **这个空间里真实存在的计划节点**
3. **这个空间已经确认的条件,以及还没问到的**

## "今天是几号"为什么必须在这里算

`UtcDateTime` 把所有时间戳统一成 UTC 存,那是为了存储正确。但"今天"不是时间戳,
是一个**时区相关的事实**:UTC 的 2026-09-25T22:00 在东八区已经是 09-26。
如果用 UTC 日期去算"还剩几周",东八区用户在晚上 8 点以后看到的每一份周计划都会
错一天 —— 而错一天在"这周还剩几天可以安排"这个问题上就是实打实的错误。
所以日期字符串按用户时区现算,`db/base.py` 里那条"哪一天绝不由时间戳推导"就是这条。
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from backend.agent.runtime.base import HISTORY_TURNS, PlanNodeView, TurnContext
from backend.db.models import Message, PlanNode
from backend.services.brief_service import load_brief, to_known_conditions
from backend.services.context import WorkspaceContext
from backend.services.timeutil import today_in

#: 送进模型的节点数上限。超过之后按"离根近的优先"截断 ——
#: 阶段和目标比第 40 个任务更能说明这个空间在干什么。
MAX_NODES = 80


def _weekday_cn(value) -> str:
    return "一二三四五六日"[value.weekday()]


async def load_nodes(db, workspace_id: uuid.UUID) -> list[PlanNode]:
    """取这个空间里没有被软删除的节点,按层级和排序号排列。"""
    result = await db.execute(
        select(PlanNode)
        .where(PlanNode.workspace_id == workspace_id, PlanNode.deleted_at.is_(None))
        .order_by(PlanNode.depth.asc(), PlanNode.order_index.asc(), PlanNode.created_at.asc())
        .limit(MAX_NODES)
    )
    return list(result.scalars())


async def load_history(
    db, conversation_id: uuid.UUID, *, exclude_message_id: uuid.UUID | None = None
) -> list[tuple[str, str]]:
    """最近若干轮对话,按时间正序。

    `exclude_message_id` 用来排除掉"刚刚这一轮的用户消息"—— 它会作为
    `user_message` 单独出现,同时又在历史里出现一次的话,模型会以为用户说了两遍。
    """
    stmt = select(Message).where(Message.conversation_id == conversation_id)
    if exclude_message_id is not None:
        stmt = stmt.where(Message.id != exclude_message_id)
    result = await db.execute(stmt.order_by(Message.seq.desc()).limit(HISTORY_TURNS))
    rows = list(result.scalars())
    rows.reverse()
    # 系统消息不进对话历史:它们是内部标记,模型看到会当成用户说的话。
    return [(m.role.value, m.content) for m in rows if m.role.value in ("user", "assistant")]


async def build_turn_context(
    db,
    ctx: WorkspaceContext,
    *,
    conversation_id: uuid.UUID,
    user_message: str,
    exclude_message_id: uuid.UUID | None = None,
    context_node_id: uuid.UUID | None = None,
    current_view: str | None = None,
) -> TurnContext:
    """组装一次轮次的上下文。**只读,不写任何东西。**"""
    today = today_in(ctx.timezone)
    nodes = await load_nodes(db, ctx.id)
    history = await load_history(db, conversation_id, exclude_message_id=exclude_message_id)
    brief = await load_brief(db, ctx.id)

    context_node_title = None
    if context_node_id is not None:
        for node in nodes:
            if node.id == context_node_id:
                context_node_title = node.title
                break

    # 记号在这里、也只在这里分配。`load_nodes` 的排序是确定的(depth, order_index,
    # created_at),所以同一个库状态下 n1..nK 每次都对得上同一批节点 ——
    # 提案校验时用的是同一份映射,不需要把 id 序列化成字符串再反解。
    handles = tuple((f"n{index}", str(node.id)) for index, node in enumerate(nodes, start=1))

    return TurnContext(
        current_date=today.isoformat(),
        weekday=_weekday_cn(today),
        timezone=ctx.timezone,
        workspace_title=ctx.workspace.title,
        workspace_intent=ctx.workspace.intent or "",
        known=to_known_conditions(brief),
        nodes=tuple(
            PlanNodeView(
                handle=handle,
                title=n.title,
                node_type=n.node_type.value,
                status=n.status.value,
                depth=n.depth,
                deadline=n.deadline.isoformat() if n.deadline else None,
                estimate_minutes=n.estimate_minutes,
            )
            for (handle, _), n in zip(handles, nodes, strict=True)
        ),
        history=tuple(history),
        user_message=user_message,
        current_view=current_view,
        context_node_title=context_node_title,
        node_handles=handles,
    )
