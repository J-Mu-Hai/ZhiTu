"""AI 分析:它对一块内容的**理解与诊断**,以及它是**基于什么**做出的。

## 为什么单独一张表,而不是写进 `plan_nodes`

因为这两样东西的归属完全不同。`plan_nodes.description` 是**用户写的原文**,
它是事实的来源;这张表里的是**模型的判断**,它是可以被推翻、被更新的。写在一起的
后果不是审美问题:模型某次委婉的"我理解你想说的是……"会覆盖掉用户的原话,而用户
从此再也找不回自己写的那句 —— 这正是规范里"AI 不能静默改写描述"要防的事。
放在两张表里,这条边界是结构性的,不依赖谁记得。

同一层理由还有一条:分析**不是**排期与提案的输入。它有它自己的读法(见
`services/analysis_service.py`),而排期只读用户确认过的列。分开存之后,
"模型的一个猜测悄悄变成了计划的前提"这件事没有路径可走。

## 为什么每行记一份输入快照

判断"这条分析过期了吗"没有别的办法。时间不行 —— 昨天的分析和今天的数据完全可能
一致,一分钟前的分析也可能已经作废。只能把**当时那份输入**记下来,和现在比一比。
`input_snapshot` 存的就是那个(形状见 `services/input_snapshot.py` 的 `to_payload`)。

**所以 `stale` 不在这里。** 它由读的时候现算(见 `analysis_service.check`),因为
"过期"不是这条分析的一个属性 —— 它是一句比较的结论,而比较的另一头随时在变。
存一个布尔列,就得有人负责在节点被改、关系被改、排期被改的每一个地方去把它翻过来,
而那必然漏 —— 漏掉的那一次不会报错,只是让一条早就作废的分析继续显示成最新。

## 为什么是只增不改(append-only)

重新分析一次不应该抹掉上一次。用户要问的问题常常是"它上次为什么那么说" —— 那需要
上一次的原话还在。所以这一张表只有 INSERT 和 SELECT,没有 UPDATE、没有 DELETE:
`created_at` 之外不加 `TimestampMixin`,因为没有"更新"可言。

顺带解决的是"哪一份是当前的":同一个(范围, 焦点)可以有多行,最新的一行就是当前那份。
"最新"按 `created_at` 排序取,不额外维护一个 `is_current` 列 —— 那个列同样需要一个
在每次插入时把旧行翻下去的写入者。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.db.base import Base, JsonDict, UtcDateTime, UuidPk, enum_type
from backend.db.models.enums import ModelSource


class NodeAnalysis(UuidPk, Base):
    """一次分析:输入是什么、模型看懂了什么。

    `scope_root_id` / `focus_node_id` 与 `Proposal` 里那几个关系列一样,用
    `ondelete="SET NULL"` 而不是 CASCADE:节点被彻底删除时,这条分析**不该跟着消失**。
    它记录的是"当时模型是这么理解的",那是历史事实;让它随节点一起被抹掉,等于
    "把东西删掉之后,连它曾经被这样看待过都不存在了"。SET NULL 之后这一行仍然
    读得出来(会显示成"当时的节点已经不在了"),而那恰好是事实。
    """

    __tablename__ = "node_analyses"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL")
    )
    #: 哪一条助手回复说出了这段分析。用户从分析区点回对话时靠它定位。
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL")
    )

    #: 这次分析的作用范围起点。NULL = 整个空间(那时候没有更窄的范围可说)。
    scope_root_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("plan_nodes.id", ondelete="SET NULL")
    )
    #: 这一轮讨论的那个节点。NULL = 用户没有指定。
    focus_node_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("plan_nodes.id", ondelete="SET NULL")
    )

    #: 是哪一版提示词、哪个模型来源产出的。事后排查"这段分析为什么这么怪"要靠它。
    prompt_version: Mapped[str | None] = mapped_column(String(32))
    model_source: Mapped[ModelSource | None] = mapped_column(
        enum_type(ModelSource, "model_source")
    )

    #: 当时的输入(`InputSnapshot.to_payload()`)。**这一列是"过期了吗"的唯一依据。**
    input_snapshot: Mapped[dict] = mapped_column(JsonDict, nullable=False)

    #: 分析内容。**每一项都是一个列表,因为规范要求它必须能区分来源** ——
    #: "已知的事"和"模型假设"混在一段自由文本里,读的人分不出哪句该信。
    #: 分开成列之后,这个区分是结构性的,不依赖模型老实不老实。
    known: Mapped[dict | None] = mapped_column(JsonDict)
    unknowns: Mapped[dict | None] = mapped_column(JsonDict)
    evidence: Mapped[dict | None] = mapped_column(JsonDict)
    assumptions: Mapped[dict | None] = mapped_column(JsonDict)
    diagnosis: Mapped[dict | None] = mapped_column(JsonDict)
    strategy_options: Mapped[dict | None] = mapped_column(JsonDict)
    risks: Mapped[dict | None] = mapped_column(JsonDict)
    #: 可信度说明。**自由文本,不是分数** —— 一个 0.8 会被当成可以比较的量,
    #: 而它其实只是一句话。
    confidence_note: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)

    __table_args__ = (
        # "这个节点的最新分析是哪一条" —— 分析区每次打开都要问这个问题。
        Index(
            "ix_node_analyses_workspace_id_focus_node_id_created_at",
            "workspace_id",
            "focus_node_id",
            "created_at",
        ),
        Index("ix_node_analyses_workspace_id_created_at", "workspace_id", "created_at"),
        Index("ix_node_analyses_message_id", "message_id"),
    )


__all__ = ["NodeAnalysis"]
