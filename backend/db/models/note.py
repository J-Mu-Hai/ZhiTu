"""节点长正文(「笔记」)。§2.2。

## 为什么要单独一张表,而不是 `plan_nodes` 上加一列

三个理由,任何一个单独都够:

1. **版本账本的重量。** `plan_service.snapshot_payload` 每次改计划都把**每个节点**
   快照进 `plan_revisions.snapshot`(见 `_each_change`)。一列 20,000 码点的正文会让
   每一行版本都背着每个节点的全文 —— 用户复盘"V7 改了什么"时,拷出来的是几百份
   长正文的重复。
2. **`/plan` 的读放大。** 四个视图(路径 / 时间线 / 任务 / 周计划)每次都读整份计划,
   而它们**没有一个**需要长正文。挂在节点行上就等于每次渲染都拖着它走,哪怕界面上
   只有一个折叠的小角标。
3. **它是"另一份东西"**,不是节点的另一个属性。节点行是计划;笔记本记录的是
   思考过程。§10 那条"布局偏好绝不承载业务事实"有它的反面:**长正文也不该进
   版本账本** —— 版本号回答的是"计划变成什么样了"。

## 为什么有自己的 `content_version`

`plan_nodes.content_version` 的注释里有一句判据:**"冲突检测的范围要和冲突的范围
一样大。"** 共用一个号就会变成"有人改了那个节点 300 字的简述 → 你正在写的 20,000 字
笔记保存失败" —— 两件事根本不冲突,而用户看到的是"保存失败,请刷新重试",两次之后
他就不敢写长的了。

语义与节点正文那一列**完全复用**:同样的 `expected_content_version` 前置条件、同样的
409 `CONCURRENCY_CONFLICT`、消息里同样给出"库里是第几版、你手上是第几版"两个数。

## 它**不**写 `plan_revisions`、不发 `domain_events`、不推进 `revision_version`

与 `layout_service` 同一个边界(理由见那个文件的 docstring),但要多一条 —— 这一条
不显然:

> 推进 `revision_version` 会让**待确认的提案凭空失效**。

`current_revision_version` 是提案 `base_revision_version` 比对的那一头。用户一边读
AI 的分析、一边在笔记里补两句,然后点确认 —— 如果笔记写入推进了版本号,他会得到
一个 409「计划已经变了,请重新让 AI 看一遍」,而计划一个字都没变。这是**把一句假话说
得很确定**。

真正需要挡住的那种冲突("AI 说要改写这段笔记,而你同时也改了它")由笔记自己的
`content_version` 挡(见 `UpdateNoteAction`),不是由计划版本号挡。这也是为什么那个
版本号必须存在,而不是"抄一份"。

## 事务边界

要 `lock_workspace`(与 `layout_service` 不同:布局是每人一份、笔记是**每节点一份、
全空间共享**)。两个标签页同时 PUT `contentVersion: 0` 时,必须在锁内读到"已经有行了"
再返回 409 —— 否则两个 INSERT 撞唯一约束,抛 `IntegrityError`,而
`backend/api/errors.py` 刻意让它照常 500(见那几行注释)。
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, Index, Integer, Text, UniqueConstraint
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Mapped, mapped_column

from backend.db.base import Base, TimestampMixin, UuidPk, enum_type
from backend.db.models.enums import NodeOrigin


class NodeNote(UuidPk, TimestampMixin, Base):
    """一个节点的一份长正文。**每个节点至多一份。**

    `node_id` 上的唯一约束是这张表的形状本身,不是一道额外的保险:读的一方(编辑器、
    AI 的上下文)要问的问题是"这个节点的笔记是什么",而不是"有哪些版本"。留成一对多
    会让每一个读的人都得自己写一遍"取最新",而写错的那一次不会报错 —— 它只是显示了
    一段不是当前的文字。
    """

    __tablename__ = "node_notes"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    #: CASCADE(而不是 `analysis.py` 里那种 SET NULL):笔记**属于**节点,是它的正文;
    #: 节点被彻底删除后,一段没有主人的正文留下来只会被读成"别处的笔记"。
    #: 与分析的区别在于:分析是"某人当时怎么看",那是历史;笔记是节点自己的内容。
    node_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("plan_nodes.id", ondelete="CASCADE"), nullable=False
    )

    body: Mapped[str] = mapped_column(Text, nullable=False, default="")

    #: 笔记正文的乐观锁。语义与 `plan_nodes.content_version` 一字不差(见模块 docstring),
    #: 只是范围是这一份笔记。
    #:
    #: `default=1` 与 `server_default` 同时给,理由同 `plan_nodes.content_version`:
    #: 两个后端要生成同一条 DDL,而 `test_migration_matches_models.py` 会逐字比对。
    content_version: Mapped[int] = mapped_column(
        Integer, default=1, server_default=sql_text("1"), nullable=False
    )

    #: **最后一次写入是谁做的。** 复用 `NodeOrigin` 而不是新造一个枚举:它要回答的
    #: 就是那条 docstring 里的话("是用户自己写的还是 AI 提的"),而用户打开一段被
    #: AI 补充过的笔记时,需要知道哪部分不是自己写的。
    origin: Mapped[NodeOrigin] = mapped_column(
        enum_type(NodeOrigin, "node_origin"),
        default=NodeOrigin.USER,
        server_default=sql_text("'user'"),
        nullable=False,
    )

    __table_args__ = (
        # 一个节点一份。命名照 MetaData 的约定,否则 SQLite 的 batch 迁移会留下一个
        # 删不掉的匿名约束。
        UniqueConstraint("node_id", name="uq_node_notes_node_id"),
        # 这个索引服务的是一个具体的查询:`input_snapshot.capture` 取"这个空间里每个
        # 节点的笔记版本"(见 services/input_snapshot.py)。没有它,每次分析都要全表扫。
        Index("ix_node_notes_workspace_id", "workspace_id"),
    )


__all__ = ["NodeNote"]
