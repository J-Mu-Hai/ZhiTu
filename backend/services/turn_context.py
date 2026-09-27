"""把一次对话轮次需要的全部上下文组装出来。

## 这个文件修的是产品最核心的一个缺陷

原来的前端在 `send()` 里只发了四个字段(空间 id、当前节点 id、视图名、消息正文),
**既不带历史也不带计划**。所以后端那个 agent 看不到用户真实的空间里有什么,
只能看到它自己上一轮提议过的东西。用户说"把刚才那个阶段往后挪一周",
模型没有"刚才那个阶段"可指 —— 它只能猜。

现在这里把这几样东西一起送进去:

1. **今天是几号**(按用户时区算,不是 UTC)
2. **这个空间里真实存在的计划节点**,以及它们**之间的关系**
3. **这个空间已经确认的条件,以及还没问到的**
4. **作用范围**:用户在哪个子空间里、点着哪个节点、哪些是范围外只读的
5. **这次分析的输入快照**(见 `services/input_snapshot.py`),供"过期了吗"事后比较

## 第二版补的是"看不见正文"

第一版把节点的标题、类型、状态送进去了,但**没有正文**。于是用户在自己写的正文里
写下"只能周末做""没有设备""已经会 C 语言",模型一条都看不到 —— 它只会照着标题
猜,然后把用户刚刚写过的事情再问一遍。第二版按"离这一轮有多近"分层读:

    焦点            正文全文(用户正写着的那一页)
    祖先链          最近的几层 + 最上面那条(根目标是最硬的约束)给正文
    焦点的直属子节点 前若干个给正文
    范围内其它节点   只给标题
    范围外           只给标题,且**只读**

"读了多少"不是内部细节:`body_read` 一路带到渲染层,和"这个节点没有正文"分开呈现。

## "今天是几号"为什么必须在这里算

`UtcDateTime` 把所有时间戳统一成 UTC 存,那是为了存储正确。但"今天"不是时间戳,
是一个**时区相关的事实**:UTC 的 2026-09-25T22:00 在东八区已经是 09-26。
如果用 UTC 日期去算"还剩几周",东八区用户在晚上 8 点以后看到的每一份周计划都会
错一天 —— 而错一天在"这周还剩几天可以安排"这个问题上就是实打实的错误。
所以日期字符串按用户时区现算,`db/base.py` 里那条"哪一天绝不由时间戳推导"就是这条。
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import func, select

from backend.agent.prompts.planning import (
    ANCESTOR_BODIES_NEAREST,
    MAX_CHILD_BODIES,
)
from backend.agent.runtime.base import (
    HISTORY_TURNS,
    LAYER_ANCESTOR,
    LAYER_CHILD,
    LAYER_FOCUS,
    LAYER_OUTSIDE,
    LAYER_SCOPE,
    PlanNodeView,
    RelationView,
    TurnContext,
)
from backend.db.models import Dependency, Message, NodeRelation, PlanNode
from backend.services import input_snapshot
from backend.services.brief_service import load_brief, to_known_conditions
from backend.services.context import WorkspaceContext
from backend.services.errors import InvalidInput
from backend.services.timeutil import today_in

logger = logging.getLogger(__name__)

#: 送进模型的节点数上限。超过之后按"离根近的优先"截断 ——
#: 阶段和目标比第 40 个任务更能说明这个空间在干什么。
MAX_NODES = 80


def _weekday_cn(value) -> str:
    return "一二三四五六日"[value.weekday()]


async def load_nodes(db, workspace_id: uuid.UUID) -> list[PlanNode]:
    """取这个空间里没有被软删除的节点,按层级和排序号排列。

    **记号(`n1..nK`)的编号顺序就是这里的排序**,所以这个顺序不能改:提案确认那一步
    会拿同样的排序重新编号,两处不一致的话,模型说的 `n3` 在确认时会指向另一个节点。
    """
    result = await db.execute(
        select(PlanNode)
        .where(PlanNode.workspace_id == workspace_id, PlanNode.deleted_at.is_(None))
        .order_by(PlanNode.depth.asc(), PlanNode.order_index.asc(), PlanNode.created_at.asc())
        .limit(MAX_NODES)
    )
    return list(result.scalars())


async def count_live_nodes(db, workspace_id: uuid.UUID) -> int:
    """这个空间里存活节点的**总数**。只是为了让"我这次没读全"能被说出来。"""
    total = await db.scalar(
        select(func.count())
        .select_from(PlanNode)
        .where(PlanNode.workspace_id == workspace_id, PlanNode.deleted_at.is_(None))
    )
    return int(total or 0)


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


# ---------------------------------------------------------------------------------
# 范围
# ---------------------------------------------------------------------------------
class _Scope:
    """一次轮次的作用范围:**读什么、能改什么**。

    这不是一个可以省掉的中间层。范围要回答三个不同的问题(见 `TurnContext` 的注释):
    用户在哪个空间里、他在看哪个节点、模型能改哪些节点。三者混成一个的话,
    "用户点了一个节点"就会顺手把可改范围缩到他脚底下,而那是两件事。
    """

    __slots__ = ("scope_id", "focus_id", "in_scope", "ancestors", "children", "layer_of")

    def __init__(self) -> None:
        self.scope_id: uuid.UUID | None = None
        self.focus_id: uuid.UUID | None = None
        #: 范围内的节点 id(含范围起点自己)。
        self.in_scope: set[uuid.UUID] = set()
        #: 焦点的祖先链,从根往下。
        self.ancestors: list[uuid.UUID] = []
        #: 焦点的直属子节点 id。
        self.children: list[uuid.UUID] = []
        #: 节点 id -> 它属于哪一层。
        self.layer_of: dict[uuid.UUID, str] = {}


async def load_scope(
    db,
    ctx: WorkspaceContext,
    nodes: list[PlanNode],
    *,
    scope_root_id: uuid.UUID | None,
    focus_node_id: uuid.UUID | None,
) -> _Scope:
    """解出这次的作用范围。**只读。**

    ## 范围起点给错了怎么办

    给了 `scope_root_id` 但它不是这个空间里存活的节点 -> 直接拒绝(`InvalidInput`)。
    悄悄放宽成"整个空间"是最坏的处理:用户以为 AI 只在自己看的那一支里动,
    实际上它拿到了整棵树 —— 而这种错误不会有任何迹象,直到某天某个范围外的分支
    被改了。宁可让他看到一句"这个范围起点不对"。
    """
    scope = _Scope()
    live = {node.id: node for node in nodes}
    parent_of = {node.id: node.parent_id for node in nodes}

    if scope_root_id is not None:
        if scope_root_id not in live:
            # 少数情况下它确实存在、只是没落进这一批(节点超过 MAX_NODES 时)。
            # 这时也拒绝:基于"我没读到"去做范围判断,才是真正危险的那件事。
            exists = await db.scalar(
                select(PlanNode.id).where(
                    PlanNode.id == scope_root_id,
                    PlanNode.workspace_id == ctx.id,
                    PlanNode.deleted_at.is_(None),
                )
            )
            if exists is not None:
                raise InvalidInput(
                    "这一层节点太多,一次读不完,当前这一轮还进不了这个子空间。"
                    "可以先在它上层继续聊,或把它下面的分支分开建。"
                )
            raise InvalidInput("这个范围起点不在当前空间里。")
        scope.scope_id = scope_root_id
        scope.in_scope = _subtree(scope_root_id, nodes)
    else:
        scope.in_scope = set(live)

    # 焦点必须落在范围内。范围外的东西可以看得见,但不该是本轮讨论的对象:
    # 用户在别的分支上点过一个节点、又导航回了上层时,这个判断避免把"上一处的选择"
    # 当成"这一轮在聊什么"。
    if focus_node_id is not None and focus_node_id in scope.in_scope:
        scope.focus_id = focus_node_id
        chain: list[uuid.UUID] = []
        seen: set[uuid.UUID] = set()
        current = parent_of.get(focus_node_id)
        while current is not None and current not in seen:
            seen.add(current)
            chain.append(current)
            current = parent_of.get(current)
        chain.reverse()
        scope.ancestors = chain
        scope.children = [node.id for node in nodes if node.parent_id == focus_node_id]

    # 分层看的是**它在这一轮里扮演什么角色**,不是它在不在范围内 —— 范围外与否由
    # `in_scope` 单独表达(渲染成「只读」)。所以祖先优先于范围外:一个在范围之上的
    # 根目标,对这一轮的意义是"最硬的那条约束",把它归到"范围外"那一堆里,
    # 模型就看不到它为什么是约束了。
    ancestors = set(scope.ancestors)
    children = set(scope.children)
    for node in nodes:
        if node.id == scope.focus_id:
            scope.layer_of[node.id] = LAYER_FOCUS
        elif node.id in ancestors:
            scope.layer_of[node.id] = LAYER_ANCESTOR
        elif node.id in children:
            scope.layer_of[node.id] = LAYER_CHILD
        elif node.id in scope.in_scope:
            scope.layer_of[node.id] = LAYER_SCOPE
        else:
            scope.layer_of[node.id] = LAYER_OUTSIDE
    return scope


def _subtree(root_id: uuid.UUID, nodes: list[PlanNode]) -> set[uuid.UUID]:
    """一棵子树(含根自己)。显式栈,不递归 —— 深树会撞 Python 的递归深度,
    而那个报错和"收子节点"看起来毫无关系。"""
    children: dict[uuid.UUID, list[uuid.UUID]] = {}
    for node in nodes:
        if node.parent_id is not None:
            children.setdefault(node.parent_id, []).append(node.id)

    collected: set[uuid.UUID] = set()
    pending = [root_id]
    while pending:
        current = pending.pop()
        if current in collected:
            continue
        collected.add(current)
        pending.extend(children.get(current, ()))
    return collected


def _bodies_read(scope: _Scope, order: dict[uuid.UUID, int]) -> set[uuid.UUID]:
    """**哪些节点这次读到了正文。**

    这是一处产品判断,所以写在这里、写成一段能被读懂的规则,而不是散在查询条件里:

    - 焦点:全读。用户在写的那一页,优先。
    - 祖先链:最上面那条(空间的根目标,最硬的约束)+ 最近的 `ANCESTOR_BODIES_NEAREST`
      条。中间那些只给标题 —— 它们离这一轮太远,展开只会把焦点冲淡。
    - 直属子节点:按排序号取前 `MAX_CHILD_BODIES` 个。拆解要看的是这一层的构成,
      而不是把它整段抄一遍。

    没读到的不会被静默略过:渲染层会把这些节点一个个列出来说"本次没有读它的正文"。
    """
    read: set[uuid.UUID] = set()
    if scope.focus_id is not None:
        read.add(scope.focus_id)
    if scope.ancestors:
        read.update(scope.ancestors[:1])  # 最上面那一条:空间的根目标
        read.update(scope.ancestors[-ANCESTOR_BODIES_NEAREST:])
    children = sorted(scope.children, key=lambda node_id: order.get(node_id, 0))
    read.update(children[:MAX_CHILD_BODIES])
    return read


# ---------------------------------------------------------------------------------
# 关系
# ---------------------------------------------------------------------------------
async def load_edges(db, workspace_id: uuid.UUID) -> list[tuple[str, str, uuid.UUID, uuid.UUID, str | None]]:
    """这个空间里全部的关系边:`(kind, type, source, target, note)`。

    `dependencies`(前置)与 `node_relations`(关联/影响)是两张表,但对模型来说都是
    "这两个节点之间有一条线"。**合成一份读出来、但不合成一种语义** —— kind 一路带到
    渲染层,前置与关联在那里分开说。
    """
    edges: list[tuple[str, str, uuid.UUID, uuid.UUID, str | None]] = []

    dependencies = await db.execute(
        select(Dependency).where(Dependency.workspace_id == workspace_id)
    )
    for edge in dependencies.scalars():
        edges.append(
            ("dep", edge.dep_type.value, edge.predecessor_id, edge.successor_id, None)
        )

    relations = await db.execute(
        select(NodeRelation).where(NodeRelation.workspace_id == workspace_id)
    )
    for edge in relations.scalars():
        edges.append(
            ("rel", edge.relation_type.value, edge.source_node_id, edge.target_node_id, edge.note)
        )
    return edges


def _relation_views(
    edges: list[tuple[str, str, uuid.UUID, uuid.UUID, str | None]],
    handles: dict[uuid.UUID, str],
    visible: set[uuid.UUID],
) -> tuple[tuple[RelationView, ...], int]:
    """把边翻译成记号形式。**两端都要有记号才列得出来。**

    列不出来的那些如实计数返回:关系的缺席和数据的缺席在模型眼里长得一样,
    而"这两个节点没关系"和"我没看见它们的关系"会导致完全不同的计划。
    """
    views: list[RelationView] = []
    hidden = 0
    for kind, relation_type, source, target, note in edges:
        source_handle = handles.get(source)
        target_handle = handles.get(target)
        if (
            source_handle is None
            or target_handle is None
            or source not in visible
            or target not in visible
        ):
            hidden += 1
            continue
        views.append(
            RelationView(
                source=source_handle,
                target=target_handle,
                kind=kind,
                relation_type=relation_type,
                note=note,
            )
        )
    views.sort(key=lambda view: (view.kind, view.source, view.target))
    return tuple(views), hidden


# ---------------------------------------------------------------------------------
# 组装
# ---------------------------------------------------------------------------------
async def build_turn_context(
    db,
    ctx: WorkspaceContext,
    *,
    conversation_id: uuid.UUID,
    user_message: str,
    exclude_message_id: uuid.UUID | None = None,
    context_node_id: uuid.UUID | None = None,
    current_view: str | None = None,
    scope_root_id: uuid.UUID | None = None,
) -> TurnContext:
    """组装一次轮次的上下文。**只读,不写任何东西。**"""
    today = today_in(ctx.timezone)
    nodes = await load_nodes(db, ctx.id)
    live_total = await count_live_nodes(db, ctx.id)
    history = await load_history(db, conversation_id, exclude_message_id=exclude_message_id)
    brief = await load_brief(db, ctx.id)
    scope = await load_scope(
        db, ctx, nodes, scope_root_id=scope_root_id, focus_node_id=context_node_id
    )

    # 记号在这里、也只在这里分配。`load_nodes` 的排序是确定的(depth, order_index,
    # created_at),所以同一个库状态下 n1..nK 每次都对得上同一批节点 ——
    # 提案校验时用的是同一份映射,不需要把 id 序列化成字符串再反解。
    handles = tuple((f"n{index}", str(node.id)) for index, node in enumerate(nodes, start=1))
    handle_of = {uuid.UUID(node_id): handle for handle, node_id in handles}
    order = {node.id: node.order_index for node in nodes}
    read_bodies = _bodies_read(scope, order)
    scope_node = next((node for node in nodes if node.id == scope.scope_id), None)
    focus_node = next((node for node in nodes if node.id == scope.focus_id), None)

    views = tuple(
        PlanNodeView(
            handle=handle,
            title=node.title,
            node_type=node.node_type.value,
            status=node.status.value,
            depth=node.depth,
            deadline=node.deadline.isoformat() if node.deadline else None,
            estimate_minutes=node.estimate_minutes,
            parent_handle=handle_of.get(node.parent_id) if node.parent_id else None,
            description=node.description,
            acceptance_criteria=node.acceptance_criteria,
            body_read=node.id in read_bodies,
            layer=scope.layer_of.get(node.id, LAYER_SCOPE),
            in_scope=node.id in scope.in_scope,
        )
        for (handle, _), node in zip(handles, nodes, strict=True)
    )

    edges, hidden_edges = _relation_views(
        await load_edges(db, ctx.id), handle_of, {node.id for node in nodes}
    )
    # 可改集 = 范围内的那些。**服务端要按它拦越界动作**(见 proposal_validation),
    # 渲染出来的记号清单只是把同一件事告诉模型。
    writable = tuple(handle for handle, node_id in handles if uuid.UUID(node_id) in scope.in_scope)

    snapshot = await input_snapshot.capture(
        db,
        ctx,
        scope_root_id=scope.scope_id,
        focus_node_id=scope.focus_id,
        window=nodes,
        window_truncated=live_total > len(nodes),
    )

    return TurnContext(
        current_date=today.isoformat(),
        weekday=_weekday_cn(today),
        timezone=ctx.timezone,
        workspace_title=ctx.workspace.title,
        workspace_intent=ctx.workspace.intent or "",
        known=to_known_conditions(brief),
        nodes=views,
        history=tuple(history),
        user_message=user_message,
        current_view=current_view,
        context_node_title=focus_node.title if focus_node else None,
        scope_root_handle=handle_of.get(scope.scope_id) if scope.scope_id else None,
        scope_root_title=scope_node.title if scope_node else None,
        focus_handle=handle_of.get(scope.focus_id) if scope.focus_id else None,
        writable_handles=writable,
        edges=edges,
        edges_hidden=hidden_edges,
        live_node_count=live_total,
        window_truncated=live_total > len(nodes),
        input_snapshot=snapshot,
        node_handles=handles,
    )
