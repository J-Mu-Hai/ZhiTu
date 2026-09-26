"""画布布局(节点位置与各层视口)的读写。

## 这个文件为什么**不写** `plan_revisions`,也不发 `domain_events`

产品上把两种改动分得很清楚:

> 布局与 viewport 是用户偏好,不影响任务完成状态。

把它固化成文件边界,而不是只写在注释里,是因为"顺手加一个版本记录"是一个看起来很
负责的动作 —— 而它的代价是**版本历史被拖动淹没**。用户复盘时会问"V7 改了什么把我
的排期挪走了",如果每拖动一次都记一版,这个问题就得从两百个版本里翻。版本号是给
"计划变成什么样了"用的,而位置变化不改变计划。

这条边界也解释了为什么它和 `node_service` 分成两个文件:那一个的每个写操作都必须
走 `_each_change`(锁 + 版本 + 事件),这一个一次都不能走。

## 事务边界

不加 `lock_workspace`:布局是**每人一份**的(键里有 `user_id`),两个人同时改各自的
位置不会互相破坏,不需要把一个空间的写入串起来。加锁的后果是"拖动一下画布"会和
"AI 确认提案"抢同一把锁。

整份提交(见 `contracts/plan.py::PutLayoutRequest`)在一个事务里完成:同一批位置
要么全进去,要么全不进去。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.contracts.plan import (
    LayoutPayload,
    LayoutPositionPayload,
    PutLayoutRequest,
    ScopeViewportPayload,
)
from backend.db.base import utcnow
from backend.db.models import NodePosition, PlanNode, ScopeViewport
from backend.services.context import WorkspaceContext
from backend.services.errors import NodeNotFound


def position_to_dict(position: NodePosition) -> dict[str, object]:
    return {"node_id": position.node_id, "x": position.x, "y": position.y}


def viewport_to_dict(viewport: ScopeViewport) -> dict[str, object]:
    return {
        "scope_node_id": viewport.scope_node_id,
        "zoom": viewport.zoom,
        "pan_x": viewport.pan_x,
        "pan_y": viewport.pan_y,
    }


async def load_layout(db: AsyncSession, ctx: WorkspaceContext) -> LayoutPayload:
    """这个用户在这个空间里的全部布局。**按用户取** —— 位置是"我怎么看这张图"。

    节点被软删除后,它的位置行还在(外键的 CASCADE 不会触发)。这里把它们过滤掉:
    返回一堆指向已删节点的位置,前端拿去摆会摆出一批看不见的节点,而它没有任何
    理由怀疑这份数据。
    """
    live = select(PlanNode.id).where(
        PlanNode.workspace_id == ctx.id, PlanNode.deleted_at.is_(None)
    )
    positions = await db.execute(
        select(NodePosition).where(
            NodePosition.user_id == ctx.user.user_id,
            NodePosition.workspace_id == ctx.id,
            NodePosition.node_id.in_(live),
        )
    )
    viewports = await db.execute(
        select(ScopeViewport).where(
            ScopeViewport.user_id == ctx.user.user_id,
            ScopeViewport.workspace_id == ctx.id,
            ScopeViewport.scope_node_id.in_(live),
        )
    )
    return LayoutPayload(
        positions=[LayoutPositionPayload.model_validate(position_to_dict(row)) for row in positions.scalars()],
        viewports=[ScopeViewportPayload.model_validate(viewport_to_dict(row)) for row in viewports.scalars()],
    )


async def put_layout(
    db: AsyncSession, ctx: WorkspaceContext, payload: PutLayoutRequest
) -> LayoutPayload:
    """整份提交布局。幂等 —— 同一份提交发两遍,结果一样。

    **不删除**没出现在这份请求里的行。前端在某个子空间里只看得到那一层的节点,
    做全量替换会删掉其他所有层级的位置,而它看起来完全正常,直到用户返回上一层
    发现节点全叠在一起了(见 `PutLayoutRequest` 的注释)。
    """
    node_ids = {item.node_id for item in payload.positions} | {
        item.scope_node_id for item in payload.viewports
    }
    if not node_ids:
        return await load_layout(db, ctx)

    # 归属校验做在 SQL 里:不属于这个空间、或已删除的 id 查不到。**整批拒绝**,
    # 不写一半 —— 一次提交里的位置是同一个瞬间的画面,写一半会让画布显示出
    # 一个从未存在过的布局。
    known = set(
        (
            await db.execute(
                select(PlanNode.id).where(
                    PlanNode.id.in_(node_ids),
                    PlanNode.workspace_id == ctx.id,
                    PlanNode.deleted_at.is_(None),
                )
            )
        ).scalars()
    )
    unknown = sorted(node_ids - known, key=str)
    if unknown:
        raise NodeNotFound(f"这些节点不在当前空间里:{', '.join(str(item) for item in unknown)}")

    now = utcnow()
    await _upsert_positions(db, ctx, payload.positions, now)
    await _upsert_viewports(db, ctx, payload.viewports, now)
    await db.commit()
    return await load_layout(db, ctx)


async def _upsert_positions(
    db: AsyncSession,
    ctx: WorkspaceContext,
    items: list[LayoutPositionPayload],
    now: datetime,
) -> None:
    """一次查、再逐条改或插。

    不用 `INSERT ... ON CONFLICT`:那个语句在 SQLite 与 PostgreSQL 上是两个不同的
    方言构造(`sqlite.insert` / `postgresql.insert`),而模型层的第一条纪律就是
    "两端同一套模型"。画布上几十个节点的读写本来就在一次请求里,一次 SELECT 加
    几十次 UPDATE 的代价远小于"两端各写一份 upsert"的风险。

    **先按节点去重,后面的赢。** 前端节流保存时会把同一个节点的两个中间位置打包发
    上来(拖动的第一个位置还没发出去,第二个就到了)。不去重的话这里会给同一个节点
    `db.add` 两行,而唯一约束要到 `flush()` 才炸 —— 用户看到的是一句和拖动毫无关系的
    数据库异常。保留最后一个也和整份提交的语义一致:"最后一次赢"是语义本身
    (见 `PutLayoutRequest` 的注释),不需要按时间戳仲裁。
    """
    wanted = {item.node_id: item for item in items}
    if not wanted:
        return
    existing = {
        row.node_id: row
        for row in (
            await db.execute(
                select(NodePosition).where(
                    NodePosition.user_id == ctx.user.user_id,
                    NodePosition.workspace_id == ctx.id,
                    NodePosition.node_id.in_(list(wanted)),
                )
            )
        ).scalars()
    }
    for node_id, item in wanted.items():
        row = existing.get(node_id)
        if row is None:
            db.add(
                NodePosition(
                    user_id=ctx.user.user_id,
                    workspace_id=ctx.id,
                    node_id=item.node_id,
                    x=item.x,
                    y=item.y,
                    updated_at=now,
                )
            )
        else:
            row.x = item.x
            row.y = item.y
            row.updated_at = now
    await db.flush()


async def _upsert_viewports(
    db: AsyncSession,
    ctx: WorkspaceContext,
    items: list[ScopeViewportPayload],
    now: datetime,
) -> None:
    """视口那一边与 `_upsert_positions` 同一个形状,包括"先按层级去重,后面的赢"。"""
    wanted = {item.scope_node_id: item for item in items}
    if not wanted:
        return
    existing = {
        row.scope_node_id: row
        for row in (
            await db.execute(
                select(ScopeViewport).where(
                    ScopeViewport.user_id == ctx.user.user_id,
                    ScopeViewport.workspace_id == ctx.id,
                    ScopeViewport.scope_node_id.in_(list(wanted)),
                )
            )
        ).scalars()
    }
    for scope_node_id, item in wanted.items():
        row = existing.get(scope_node_id)
        if row is None:
            db.add(
                ScopeViewport(
                    user_id=ctx.user.user_id,
                    workspace_id=ctx.id,
                    scope_node_id=item.scope_node_id,
                    zoom=item.zoom,
                    pan_x=item.pan_x,
                    pan_y=item.pan_y,
                    updated_at=now,
                )
            )
        else:
            row.zoom = item.zoom
            row.pan_x = item.pan_x
            row.pan_y = item.pan_y
            row.updated_at = now
    await db.flush()


__all__ = ["load_layout", "position_to_dict", "put_layout", "viewport_to_dict"]
