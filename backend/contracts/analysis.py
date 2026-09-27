"""AI 分析的读接口形状。

## 为什么新鲜度是算出来的,而不是一个列

"这条分析过期了吗"没有存起来的答案。它是一句比较的结论 —— 比的是**这份分析当时
看到的输入**和**此刻库里的样子**。存一个布尔列就得有人负责在节点被改、关系被改、
排期被改的每一个地方把它翻过来,而那必然漏;漏掉的那一次不会报错,只会让一条早就
作废的分析继续显示成「最新」。

所以 `freshness` 与 `stale_reasons` 每次读都现算。好处不只是不会漏:
**理由本身成了界面上的一句话** —— "「阶段二」的正文改过了"比一个"已过期"角标有用得多。

## 为什么三档新鲜度这里只有两档

规范写的是「最新 / 过期 / 需要重新分析」。第三档不是另一种数据处境,它是**过期之后
该做什么**,由界面按 `STALE` 呈现成一句邀请和一个入口(见 `AnalysisFreshness` 的注释)。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import Field

from backend.contracts.common import ApiModel
from backend.db.models.enums import AnalysisFreshness, ModelSource

#: 一次最多返回多少条分析。分析是只增的,一个被反复讨论的节点会有很多条 ——
#: 而界面只显示最近的一条加上"更早的那些"。给到 20 是留出翻看的余地,
#: 不是让调用方一次拉全(要全的话得做分页,那是另一个决定)。
MAX_ANALYSES = 20


class AnalysisView(ApiModel):
    """一条分析:模型当时看到了什么、判断了什么、现在还成不成立。"""

    id: uuid.UUID
    workspace_id: uuid.UUID
    #: 这次分析的范围起点 / 讨论对象。节点被彻底删除后为 None。
    scope_root_id: uuid.UUID | None = None
    focus_node_id: uuid.UUID | None = None
    #: 当时的标题。**节点后来改名或被删掉时,这两个字段是这条分析还读得懂的原因**
    #: —— 只留一个 id 的话,用户看到的是"一条关于(已删除)的分析"。
    scope_root_title: str | None = None
    focus_node_title: str | None = None

    prompt_version: str | None = None
    model_source: ModelSource | None = None
    created_at: datetime

    #: 现算的新鲜度。`fresh` = 输入没变过;`stale` = 变过了,理由在下面。
    freshness: AnalysisFreshness
    #: 具体变了什么。每一条都是给人看的一句话。
    stale_reasons: list[str] = Field(default_factory=list)
    #: 这次分析**只读到了范围的一部分**时的说明。它不影响新鲜度,但影响"这份判断
    #: 能覆盖多大范围" —— 不说的话,一份只读了前 80 个节点的分析看起来像读全了。
    coverage_note: str | None = None

    #: 七栏判断。**分开成列是这份契约的重点**:它们的可靠程度不同,
    #: 混成一段自由文本之后读的人分不出哪句该信。
    known: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    diagnosis: list[str] = Field(default_factory=list)
    strategy_options: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    #: 可信度说明。自由文本,**不是分数** —— 一个 0.8 会被当成可以比较的量。
    confidence_note: str | None = None


class AnalysisListResponse(ApiModel):
    """某个节点(或某个范围)的分析,新的在前。"""

    analyses: list[AnalysisView]
    #: 这批分析对应的节点。`focus_node_id` 为空表示"这个空间的全部分析"。
    focus_node_id: uuid.UUID | None = None
    scope_root_id: uuid.UUID | None = None
    #: 一句话说明这批数据现在是什么状态。**空列表时这句话必须说清"是没有,还是没查到"**
    #: —— 一个空数组在界面上长得和"加载失败"一模一样。
    note: str = ""


__all__ = ["MAX_ANALYSES", "AnalysisListResponse", "AnalysisView"]
