"""计划节点、依赖关系与计划版本。

这里体现产品最重要的一条建模纪律:**"要做什么"与"哪天做"分开**。

- PlanNode 回答"要做什么",带截止日期(deadline),但**不带**开始/结束的排期。
- ScheduledSession 回答"哪天做",一个 PlanNode 可以对应多个 Session。

所以一个 8 小时的任务永远是**一行** PlanNode + 若干行 ScheduledSession,
不会为了填满日历被复制成 8 个同名任务。

层级只用 parent_id 这一种表示(邻接表)。docs/05-DATA-MODEL.md 里把 parent 也列为
一种 edge,那是两套真相 —— 图和计划视图会因此各说各话,所以不采用。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db.base import Base, JsonDict, TimestampMixin, UtcDateTime, UuidPk, enum_type
from backend.db.models.enums import (
    DependencyType,
    NodeOrigin,
    NodePurpose,
    NodeRelationType,
    NodeStatus,
    NodeType,
    PlanningLevel,
    Priority,
    RevisionActor,
    RevisionTrigger,
)


class PlanNode(UuidPk, TimestampMixin, Base):
    __tablename__ = "plan_nodes"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    # 自引用。刻意用 RESTRICT 而不是 CASCADE:级联删除会静默抹掉整棵子树,
    # 而删除必须是显式动作并写进计划版本。级联删除由服务层显式收集子树后逐个执行。
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("plan_nodes.id", ondelete="RESTRICT")
    )

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    # 验收标准。用户判断"这片树叶做完了没有"的依据,不是给 AI 看的。
    acceptance_criteria: Mapped[str | None] = mapped_column(Text)

    # 节点正文的乐观锁。**只被正文的保存路径使用。**
    #
    # 正文是长文本,而长文本的编辑是"打开 -> 写十分钟 -> 保存"—— 中间隔着足够长的时间,
    # 长到"另一个标签页也改了同一段"完全可能发生。没有这一列时,后保存的那次会静默
    # 覆盖前一次,而用户看到的是"保存成功"。
    #
    # 为什么不复用 `current_revision_version`:那是**整个空间**的版本号。拿它做正文的
    # 冲突检测,会让"我在另一个标签页勾掉了一个任务"变成"我的正文保存失败",而这两件事
    # 根本没有冲突。冲突检测的范围要和冲突的范围一样大,这一列就是那个范围。
    #
    # server_default 与 `User.token_version` 同理:SQLite 的 ADD COLUMN 不接受
    # "NOT NULL 且无默认值",给了常量默认值,两个后端才是同一条 ALTER。
    content_version: Mapped[int] = mapped_column(
        Integer, default=1, server_default=sql_text("1"), nullable=False
    )

    node_type: Mapped[NodeType] = mapped_column(
        enum_type(NodeType, "node_type"), default=NodeType.TASK, nullable=False
    )
    # 用途轴。与 `node_type` **正交** —— 见 `NodePurpose` 的 docstring。
    #
    # server_default 是**必需**的(与上面 `content_version` 同一个理由,不是抄来的
    # 习惯):SQLite 的 ADD COLUMN 不接受"NOT NULL 且无默认值"。少了它,迁移根本
    # 加不上这一列;而只在迁移里写、模型里不写,`create_all` 与迁移出来的 DDL 就会
    # 分叉,`test_migration_matches_models.py` 逐字比对会红。两处必须同时有。
    purpose: Mapped[NodePurpose] = mapped_column(
        enum_type(NodePurpose, "node_purpose"),
        default=NodePurpose.PLANNING,
        server_default=sql_text("'planning'"),
        nullable=False,
    )
    # 规划层级(战略 / 阶段 / 月 / 周 / 日)。**可空** —— 这一列是纯增量的:
    # 旧节点全部是 NULL(unspecified),行为与加这一列之前一模一样,不回填、不重写。
    #
    # 为什么可空而不给默认值:"默认 strategy"或"默认 task 层"都会把存量节点
    # 静默翻译成一个它们从未表达过的语义。NULL 就是"还没说"。
    planning_level: Mapped[PlanningLevel | None] = mapped_column(
        enum_type(PlanningLevel, "planning_level")
    )
    status: Mapped[NodeStatus] = mapped_column(
        enum_type(NodeStatus, "node_status"), default=NodeStatus.PENDING, nullable=False
    )
    priority: Mapped[Priority] = mapped_column(
        enum_type(Priority, "node_priority"), default=Priority.MEDIUM, nullable=False
    )

    # 预计工时(分钟)。是排期算法的输入,不是排期结果本身。
    estimate_minutes: Mapped[int | None] = mapped_column(Integer)
    deadline: Mapped[date | None] = mapped_column(Date)

    order_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 从父链算出的层级,冗余存储以便一次查询取出某一层的全部节点。
    depth: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    origin: Mapped[NodeOrigin] = mapped_column(
        enum_type(NodeOrigin, "node_origin"), default=NodeOrigin.USER, nullable=False
    )
    # 引入这个节点的计划版本。用于"AI 生成的阶段/任务在哪个版本出现"。
    plan_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("plan_revisions.id", ondelete="SET NULL")
    )

    completed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    # 软删除。历史版本里的快照仍要能引用到它。
    deleted_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    # "这一次删除不可恢复"的那一笔。**归档与彻底删除都打 `deleted_at`**,两者的区别
    # 只在这里:归档留空(可恢复),彻底删除打上(恢复入口据此拒绝)。
    #
    # 为什么不是再加一个布尔列:时间戳顺手回答了"什么时候彻底删的",而布尔列会在
    # 某次排查里被发现"没有时间,只剩一个 true"。
    #
    # 为什么彻底删除也要留行:`plan_revisions.snapshot` 里引用着这些 id,物理删除会让
    # 历史版本指向空气。"彻底"指的是不可恢复,不是从历史里抹掉。
    purged_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    # 这一次删除的批次号。**同一个删除动作给整支子树打的是同一个 UUID。**
    #
    # 恢复要回答的问题是"当时是哪一下把它带走的",而不是"它下面现在有哪些节点" ——
    # 后者会把用户**更早单独收起来**的子项一起复活(见 `restore_node`)。这个列就是
    # 那个"哪一下"。
    #
    # 为什么不用 `deleted_at` 当批次标识(它曾经就是):那要靠"两次归档不会落在同一个
    # 微秒上"成立。现在确实不会 —— 但那是一条**关于精度的默认性质**,不是写下来的
    # 约束:列类型、驱动、某一处 `replace(microsecond=0)` 都能让它不成立,而失效时的
    # 症状是"我恢复了一项,他当时特意收起来的另一项也跟着回来了" —— 恰好是这个功能
    # 最不该有的错,而且不会有任何提示。显式给号,这条约束就不再依赖时间精度。
    #
    # 可空:活着的节点没有批次可言(`deleted_at` 为空的行这一列也是空)。
    archive_batch_id: Mapped[uuid.UUID | None] = mapped_column()

    workspace: Mapped[Workspace] = relationship()  # noqa: F821
    parent: Mapped[PlanNode | None] = relationship(
        remote_side="PlanNode.id", back_populates="children"
    )
    children: Mapped[list[PlanNode]] = relationship(back_populates="parent")

    __table_args__ = (
        CheckConstraint(
            "estimate_minutes IS NULL OR estimate_minutes > 0", name="estimate_minutes_positive"
        ),
        CheckConstraint("depth >= 0", name="depth_non_negative"),
        Index("ix_plan_nodes_workspace_id_parent_id", "workspace_id", "parent_id"),
        Index("ix_plan_nodes_workspace_id_status", "workspace_id", "status"),
        Index("ix_plan_nodes_deadline", "deadline"),
    )


class Dependency(UuidPk, TimestampMixin, Base):
    """finish-to-start 前置关系。环由服务层用递归可达性探测拒绝,不靠数据库。"""

    __tablename__ = "dependencies"

    # 冗余 workspace_id:跨空间依赖校验和按空间取图都靠它,避免每次 JOIN plan_nodes。
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    predecessor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("plan_nodes.id", ondelete="CASCADE"), nullable=False
    )
    successor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("plan_nodes.id", ondelete="CASCADE"), nullable=False
    )
    dep_type: Mapped[DependencyType] = mapped_column(
        enum_type(DependencyType, "dependency_type"),
        default=DependencyType.FINISH_TO_START,
        nullable=False,
    )
    lag_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "predecessor_id", "successor_id", name="uq_dependencies_predecessor_id_successor_id"
        ),
        CheckConstraint("predecessor_id <> successor_id", name="no_self_dependency"),
        Index("ix_dependencies_workspace_id_successor_id", "workspace_id", "successor_id"),
    )


class NodeRelation(UuidPk, TimestampMixin, Base):
    """用户自己画的、除"前置"之外的关系。

    **为什么另起一张表,而不是往 `dependencies` 里加类型。** `dependencies` 是排期的
    输入:排期器读它算最早能排到哪天。往里加一种"不参与排期"的类型,就得让排期器
    开始挑类型 —— 而那正是"排期结果取决于一条不该影响它的边"的来源。分开存之后,
    排期链路一行代码都不用改,这是这个决定的全部价值。

    三种关系的统一视图在接口层(`contracts/plan.py` 的 `RelationType`),它把这张表
    的行和 `dependencies` 的行投影成同一个形状给画布。**投影不是第二份存储。**

    ## 无向的 `related_to` 怎么去重

    唯一约束是那张表上的 `(source, target, type)`。`related_to` 是无向的,如果按用户
    画的顺序存,A→B 和 B→A 会是两行,同一条边在画布上画两遍。所以写入前把两端按
    UUID 排成固定顺序(见 `node_service.add_relation`),让"A 关联 B"和"B 关联 A"
    落到同一行上。约束只守最后一道,规范化在服务层。
    """

    __tablename__ = "node_relations"

    #: 冗余 workspace_id,同 `dependencies` 的理由:按空间取整张图时不用 JOIN plan_nodes。
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    source_node_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("plan_nodes.id", ondelete="CASCADE"), nullable=False
    )
    target_node_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("plan_nodes.id", ondelete="CASCADE"), nullable=False
    )
    relation_type: Mapped[NodeRelationType] = mapped_column(
        enum_type(NodeRelationType, "node_relation_type"), nullable=False
    )
    #: 这条边是什么意思。用户自己写的说明 —— 与节点正文一样,是给人看的,不是排期输入。
    note: Mapped[str | None] = mapped_column(Text)
    #: 谁画的。复用 `NodeOrigin`(`user` / `ai`):边和节点一样,要能分辨
    #: "这是我连的"还是"AI 建议的"。`dependencies` 没有这一列 —— 它的历史在
    #: `plan_revisions.diff` 里,不是新加的,这里不去动它。
    origin: Mapped[NodeOrigin] = mapped_column(
        enum_type(NodeOrigin, "node_origin"), default=NodeOrigin.USER, nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "source_node_id",
            "target_node_id",
            "relation_type",
            name="uq_node_relations_source_node_id_target_node_id_relation_type",
        ),
        CheckConstraint("source_node_id <> target_node_id", name="no_self_relation"),
        Index("ix_node_relations_workspace_id", "workspace_id"),
        Index("ix_node_relations_source_node_id", "source_node_id"),
        Index("ix_node_relations_target_node_id", "target_node_id"),
    )


class PlanRevision(UuidPk, Base):
    """计划的版本记录。每次变更都产生一条,不覆盖历史。

    snapshot 与 diff 都存:只存 snapshot 的话,"V4 改了什么"每次都要重算;
    只存 diff 的话,回滚与对比需要从头重放。
    """

    __tablename__ = "plan_revisions"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)

    parent_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("plan_revisions.id", ondelete="SET NULL")
    )

    trigger_type: Mapped[RevisionTrigger] = mapped_column(
        enum_type(RevisionTrigger, "revision_trigger"), nullable=False
    )
    trigger_detail: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[RevisionActor] = mapped_column(
        enum_type(RevisionActor, "revision_actor"), nullable=False
    )
    proposal_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("proposals.id", ondelete="SET NULL")
    )

    snapshot: Mapped[dict] = mapped_column(JsonDict, nullable=False)
    diff: Mapped[dict] = mapped_column(JsonDict, nullable=False)

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)

    workspace: Mapped[Workspace] = relationship()  # noqa: F821

    __table_args__ = (
        UniqueConstraint("workspace_id", "version", name="uq_plan_revisions_workspace_id_version"),
        Index("ix_plan_revisions_workspace_id_version", "workspace_id", "version"),
    )
