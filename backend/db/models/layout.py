"""画布上的**用户偏好**:节点位置与每个层级的视口。

## 为什么单独一个文件,而不是塞进 plan.py

这两张表与计划的关系是"关于计划的偏好",不是计划本身。它们的区别不是理论上的:

- 写它们**不产生计划版本**。`plan_revisions` 记录的是"计划变了什么" —— 用户把
  一个节点往右拖了 40 像素,计划没变,排期没变,没有任何一个视图的含义变了。
  如果拖动也写版本,版本历史会被拖动淹没,"V7 改了什么"这个问题就再也答不上来。
- 它们**按用户分**。`node_positions` 的主键里有 `user_id`:位置是"我怎么看这张图",
  不是"这张图是什么"。同一个人换台机器、换台显示器,想要的也可能是另一套位置。

把这两条区别固化成文件边界,而不是留在注释里 —— 后来的人往 `plan_service` 里加
布局写入时,至少会撞见这个文件的名字。

## 为什么不合成一张 `(user, workspace, node, x, y, zoom, pan_x, pan_y)` 表

因为 `node_id` 和 `zoom/pan` 不能同时有值:视口属于**层级**(根画布、某个节点的子空间),
不属于某个节点。合成一张表就得让 `scope_node_id` 可空 —— 而**可空列不能做唯一键的一部分**:
SQLite 与 PostgreSQL 都认为 NULL 互不相等,于是唯一索引拦不住重复行,每次保存都插一条新的。
所以拆成两张,每张的键都全部非空。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Float, ForeignKey, Index, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.db.base import Base, UtcDateTime, UuidPk, utcnow


class NodePosition(UuidPk, Base):
    """某个用户在某张画布上把某个节点放在哪儿。

    ## 为什么 `x`/`y` 不放在 `plan_nodes` 上

    放在 `plan_nodes` 上意味着**位置是计划的一部分**:两个人看同一个空间会看到同一套
    位置,而且拖动会写进版本历史。位置是视图状态,不是计划状态 —— 它该按人存。

    ## 为什么没有 `created_at`

    这一行不是事件,是**被覆盖的状态**。唯一会被问到的问题是"它多久没动过了",
    所以只留 `updated_at`。加一个 `created_at` 只会让人误以为这里有历史可查 ——
    要历史就得每次写入插新行,那是另一个设计(而且那种设计下"当前位置是哪一行"
    需要额外的判据)。
    """

    __tablename__ = "node_positions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    node_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("plan_nodes.id", ondelete="CASCADE"), nullable=False
    )
    x: Mapped[float] = mapped_column(Float, nullable=False)
    y: Mapped[float] = mapped_column(Float, nullable=False)
    #: Python 侧生成,同 `TimestampMixin` 的理由(两端的 now() 语义不同)。
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=utcnow, onupdate=utcnow, nullable=False
    )

    __table_args__ = (
        # 一个人在一个空间里,一个节点只有一个位置。整 scope 覆盖式保存靠它幂等。
        UniqueConstraint(
            "user_id", "workspace_id", "node_id", name="uq_node_positions_user_id_workspace_id_node_id"
        ),
        # 上面那个唯一键以 `user_id` 打头,所以它服务不了任何"不知道用户是谁"的查询。
        # 而那种查询是存在的:级联清理、以及"这个空间里到底存过哪些位置"这种排查。
        # `scope_viewports` 不需要对应的索引 —— 它只会按层级被查,而那正是它的唯一键。
        Index("ix_node_positions_workspace_id_node_id", "workspace_id", "node_id"),
    )


class ScopeViewport(UuidPk, Base):
    """某个用户在某个**层级**上把画布平移缩放到哪儿。

    `scope_node_id` 是这一层的根节点(总空间的根目标、或某个子空间的中心节点)——
    与画布上的 `spaceId` 是同一个东西。每个层级各存一份,因为"我在总空间里看得比较远、
    在这个子空间里放大了看某一条"是很正常的两套视角。
    """

    __tablename__ = "scope_viewports"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    scope_node_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("plan_nodes.id", ondelete="CASCADE"), nullable=False
    )
    #: React Flow 的 zoom。留 Float 而不是整数:缩放是连续的,取整会让"回到上次那个
    #: 大小"差一点点,而用户对"我明明放大过"这件事是有记忆的。
    zoom: Mapped[float] = mapped_column(Float, nullable=False)
    pan_x: Mapped[float] = mapped_column(Float, nullable=False)
    pan_y: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=utcnow, onupdate=utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "workspace_id",
            "scope_node_id",
            name="uq_scope_viewports_user_id_workspace_id_scope_node_id",
        ),
    )


__all__ = ["NodePosition", "ScopeViewport"]
