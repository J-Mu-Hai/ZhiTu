"""AI 分析:记下来、读出来、以及**判断它还算不算数**。

## 三件事,按重要性排

### ① 过期判断是现算的,不是存下来的

"这条分析过期了吗"没有存起来的答案。它是一句**比较的结论**:比的是这份分析当时
看到的输入,和此刻库里的样子。所以这一层只做一件事 —— 拿库里那行 `input_snapshot`
重新取一次快照(`current_snapshot_like`),两个比一比,把差异说成人话。

这样做还有一个不那么明显的好处:**理由本身是界面上的一句话**。"「阶段二」的正文
改过了"比一个"已过期"角标有用得多,而存一个 `stale` 布尔列是给不出理由的 ——
它只知道"有人翻过牌",不知道是谁翻的。

### ② 分析返回之后要立刻再比一次

模型调用可能要好几十秒。这期间用户完全可能已经改了正文、加了子节点、调了每周可投入
的时长。**那种情况下模型这段话说的就不再是现在的事**,不能直接变成一份可确认的提案
(规范 §2.3:"结果只能作为标有「基于旧版本」的历史分析")。

`input_changed_since` 就是那道闸。它必须**在生成提案之前**跑,而不是等用户点了确认
才拦 —— 等到那时候,用户已经在读一份基于旧数据的建议了。确认时的那道版本校验
(见 `proposal_service._revalidate`)仍然保留:它管的是另一段时间(从提案生成到用户
点确认),两者都要有,缺一段就有一段窗口是空的。

### ③ 只增不改

重新分析一次不抹掉上一次。用户常问的是"它上次为什么那么说",那需要上一次的原话还在。
所以这一层没有 UPDATE、没有 DELETE。

## 不在这里做的事

- **不写 `plan_nodes`。** 分析进的是自己的表。"模型的一句猜测悄悄变成计划的前提"
  这件事因此没有路径可走。
- **不改排期。** 分析里的 `risks` 可以是"会排不开",但那是判断,不是排期动作。
- **不判断"这条分析好不好"。** 可信度是模型自己写的一句话(`confidence_note`),
  不是这一层算出来的分数 —— 一个 0.8 会被当成可以比较的量,而它其实只是一句话。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import select

from backend.agent.runtime.base import AnalysisDraft
from backend.contracts.analysis import AnalysisView
from backend.db.base import utcnow
from backend.db.models import NodeAnalysis, PlanNode
from backend.db.models.enums import AnalysisFreshness, ModelSource
from backend.services import input_snapshot
from backend.services.context import WorkspaceContext
from backend.services.input_snapshot import InputSnapshot, SnapshotNode

logger = logging.getLogger(__name__)

#: 一次列多少条分析。与分析记录本身无关 —— 那是只增的,这个只是"一次读多少"。
DEFAULT_LIMIT = 5

#: 七栏的固定顺序。**与契约层和控制层共用一份** —— 三处各写一遍的话,某天加了
#: 一栏就总会漏掉一处,而漏掉的那处表现为"界面上少了半个分析",不报错。
SECTION_FIELDS = (
    "known",
    "unknowns",
    "evidence",
    "assumptions",
    "diagnosis",
    "strategy_options",
    "risks",
)


@dataclass(frozen=True, slots=True)
class Staleness:
    """这份分析还算不算数,以及为什么。"""

    freshness: AnalysisFreshness
    #: 具体变了什么。空表示没变。每一条都是给人看的一句话。
    reasons: tuple[str, ...] = ()
    #: 这次分析只读到了范围的一部分时的说明。**不影响新鲜度** ——
    #: 它说的是"这份判断能覆盖多大范围",不是"它过期了"。
    coverage_note: str | None = None

    @property
    def is_fresh(self) -> bool:
        return self.freshness is AnalysisFreshness.FRESH


# ---------------------------------------------------------------------------------
# 记
# ---------------------------------------------------------------------------------
async def record(
    db,
    ctx: WorkspaceContext,
    *,
    conversation_id: uuid.UUID | None,
    message_id: uuid.UUID | None,
    snapshot: InputSnapshot,
    draft: AnalysisDraft,
    prompt_version: str | None,
    model_source: ModelSource | None,
) -> NodeAnalysis:
    """把一次分析记下来。**只 INSERT。** 调用方负责提交。

    `scope_root_id` / `focus_node_id` 取自**快照**而不是参数:快照里那两个就是这一轮
    实际生效的范围,传两个来源进来迟早会出现"记的是这个、分析的是那个"。
    """
    analysis = NodeAnalysis(
        user_id=ctx.owner_id,
        workspace_id=ctx.id,
        conversation_id=conversation_id,
        message_id=message_id,
        scope_root_id=_as_uuid(snapshot.scope_root_id),
        focus_node_id=_as_uuid(snapshot.focus_node_id),
        prompt_version=prompt_version,
        model_source=model_source,
        input_snapshot=snapshot.to_payload(),
        confidence_note=draft.confidence_note,
        created_at=utcnow(),
        **{
            field: list(getattr(draft, field)) or None
            for field in SECTION_FIELDS
        },
    )
    db.add(analysis)
    await db.flush()
    return analysis


def _as_uuid(raw: str | None) -> uuid.UUID | None:
    return None if raw is None else uuid.UUID(raw)


# ---------------------------------------------------------------------------------
# 比
# ---------------------------------------------------------------------------------
async def current_snapshot_like(db, ctx: WorkspaceContext, stored: InputSnapshot) -> InputSnapshot:
    """用**当时那个读窗**重新取一次快照。

    "用当时的读窗"是这个函数存在的全部理由。直接 `capture(window=[])` 会让逐个记的
    节点变成"范围内按深度排序的前 200 个" —— 与原来那批不是同一组,于是每一行都会
    被比成"多出来的节点",理由全是噪音。逐行比较只有在**比的是同一批对象**时才有意义。
    """
    ids = [uuid.UUID(node.node_id) for node in stored.nodes]
    window = await _load_nodes(db, ctx.id, ids)
    return await input_snapshot.capture(
        db,
        ctx,
        scope_root_id=None if stored.scope_root_id is None else uuid.UUID(stored.scope_root_id),
        focus_node_id=None if stored.focus_node_id is None else uuid.UUID(stored.focus_node_id),
        window=window,
        window_truncated=stored.truncated,
    )


async def _load_nodes(db, workspace_id: uuid.UUID, ids: list[uuid.UUID]) -> list[PlanNode]:
    """按 id 取节点。**不筛 `deleted_at`** —— 快照里那批本来就含已删除的行,
    筛掉的话"被归档"会被读成"不见了",而后者是一句更重的判断。"""
    if not ids:
        return []
    result = await db.execute(
        select(PlanNode).where(PlanNode.workspace_id == workspace_id, PlanNode.id.in_(ids))
    )
    return list(result.scalars())


async def input_changed_since(db, ctx: WorkspaceContext, snapshot: InputSnapshot) -> bool:
    """这一轮分析开始之后,输入有没有变过。

    **这是"结果只能作为历史分析"那道闸。** 它必须在生成提案之前跑。判据是快照本身,
    不是时间戳:一次无关的写库(别的地方改了个名字)不该让这次分析作废,而一次
    相隔一毫秒的正文改动必须让它作废。
    """
    current = await current_snapshot_like(db, ctx, snapshot)
    return _diff(snapshot, current) != ()


def check(stored: InputSnapshot, current: InputSnapshot) -> Staleness:
    """两份快照的差异 -> 一句人话。

    分成两档的理由见 `_diff`:逐个记的那批能说出**具体是谁变了**,只有摘要不同的
    只能说**这一片的结构变了**。两者都不许留白 —— "看不出哪里变了"和"没变"在界面上
    长得一模一样,而前者会让一条其实已经失效的分析继续被当成最新。
    """
    reasons = _diff(stored, current)
    coverage_note = None
    if stored.truncated:
        coverage_note = (
            f"这次分析只读到了范围里的一部分节点({len(stored.nodes)} 个)。"
            "下面这些判断覆盖的是读过的那批,不是整个范围。"
        )
    return Staleness(
        freshness=AnalysisFreshness.STALE if reasons else AnalysisFreshness.FRESH,
        reasons=reasons,
        coverage_note=coverage_note,
    )


def _diff(stored: InputSnapshot, current: InputSnapshot) -> tuple[str, ...]:
    """两份快照的差异。**顺序固定**,免得同一次比较两次跑出不同的理由顺序。"""
    reasons: list[str] = []
    old = {node.node_id: node for node in stored.nodes}
    new = {node.node_id: node for node in current.nodes}

    for node_id in sorted(old.keys() - new.keys()):
        node = old[node_id]
        reasons.append(f"「{_label(node)}」已经不在了(被删除,或者被移出了这次读到的范围)")
    for node_id in sorted(new.keys() - old.keys()):
        node = new[node_id]
        reasons.append(f"这次读到的范围里多出了「{_label(node)}」")

    for node_id in sorted(old.keys() & new.keys()):
        before, after = old[node_id], new[node_id]
        label = _label(before)
        if before.parent_id != after.parent_id or before.depth != after.depth:
            reasons.append(f"「{label}」被移到别的地方了")
        if before.status != after.status:
            reasons.append(f"「{label}」的状态从 {before.status} 变成了 {after.status}")
        if before.deleted != after.deleted:
            reasons.append(f"「{label}」被归档了" if after.deleted else f"「{label}」被恢复了")
        if before.content_version != after.content_version:
            reasons.append(f"「{label}」的正文改过了")
        if before.title != after.title:
            reasons.append(f"「{label}」的标题从「{before.title}」改成了「{after.title}」")

    if stored.edges != current.edges:
        reasons.append("这一片的节点关系变了(加了或去掉了一条线)")
    if stored.brief_version != current.brief_version or (
        stored.weekly_available_minutes != current.weekly_available_minutes
    ):
        reasons.append("已知条件变了(截止时间、每周可投入这类)")
    if stored.capacity_digest != current.capacity_digest:
        reasons.append("你的整体时间预算变了")
    if stored.availability_digest != current.availability_digest:
        reasons.append("可用时段变了")
    if stored.schedule_digest != current.schedule_digest:
        reasons.append("这一片的排期变了")
    if stored.execution_digest != current.execution_digest:
        reasons.append("这一片的执行记录变了")

    # 逐行比不出来的那一类**必须留一句话**。范围里可能有节点在这个读窗之外 ——
    # 它们的新增、删除、移动只会改变结构摘要。那句"不知道是谁"的话不好听,
    # 但比"看起来一切正常"诚实:沉默在这里等于宣称一条已经作废的分析仍然成立。
    if not reasons and stored.structure_digest != current.structure_digest:
        reasons.append(
            "这一片的结构变了(有些变化发生在这次没逐个读到的节点上,说不清具体是哪一个)"
        )

    # **`live_node_count` 刻意不在上面这一串里。** 它是整个空间的计数,而用户在
    # 别的分支上加一个节点不该让这一支的分析全部作废 —— 那正是规范点名的
    # "没有读取也不影响本次决策的无关分支,不应使所有分析一起失效"。
    # 它还留在快照里,但只用来回答"这次读全了吗",不用来判过期。
    return tuple(reasons)


def _label(node: SnapshotNode) -> str:
    """一个节点在理由里怎么称呼。标题可能是空的 —— 那时退回 id 的前一段,
    而不是印一个空的引号。"""
    return node.title or f"节点 {node.node_id[:8]}"


# ---------------------------------------------------------------------------------
# 读
# ---------------------------------------------------------------------------------
async def list_for(
    db,
    ctx: WorkspaceContext,
    *,
    focus_node_id: uuid.UUID | None = None,
    scope_root_id: uuid.UUID | None = None,
    limit: int = DEFAULT_LIMIT,
) -> list[NodeAnalysis]:
    """某个节点(或某个范围)的分析,**新的在前**。

    `focus_node_id=None` 表示"这个空间的全部" —— 那是范围视图要的。**不做模糊匹配**:
    "这个节点以及它下面所有节点"是另一件事,而且它会把一条关于祖辈的分析混进
    子节点的列表里。
    """
    stmt = select(NodeAnalysis).where(NodeAnalysis.workspace_id == ctx.id)
    if focus_node_id is not None:
        stmt = stmt.where(NodeAnalysis.focus_node_id == focus_node_id)
    if scope_root_id is not None:
        stmt = stmt.where(NodeAnalysis.scope_root_id == scope_root_id)
    result = await db.execute(
        stmt.order_by(NodeAnalysis.created_at.desc(), NodeAnalysis.id.desc()).limit(limit)
    )
    return list(result.scalars())


async def view_of(db, ctx: WorkspaceContext, analysis: NodeAnalysis) -> AnalysisView:
    """一条分析 -> 界面要的形状。**新鲜度在这里现算。**"""
    stored = InputSnapshot.from_payload(analysis.input_snapshot)
    current = await current_snapshot_like(db, ctx, stored)
    staleness = check(stored, current)
    titles = await _titles(db, ctx, stored, analysis)

    return AnalysisView(
        id=analysis.id,
        workspace_id=analysis.workspace_id,
        scope_root_id=analysis.scope_root_id,
        focus_node_id=analysis.focus_node_id,
        scope_root_title=titles[0],
        focus_node_title=titles[1],
        prompt_version=analysis.prompt_version,
        model_source=analysis.model_source,
        created_at=analysis.created_at,
        freshness=staleness.freshness,
        stale_reasons=list(staleness.reasons),
        coverage_note=staleness.coverage_note,
        confidence_note=analysis.confidence_note,
        **{
            field: list(getattr(analysis, field) or ())
            for field in SECTION_FIELDS
        },
    )


async def _titles(
    db, ctx: WorkspaceContext, stored: InputSnapshot, analysis: NodeAnalysis
) -> tuple[str | None, str | None]:
    """范围和焦点的标题。

    **先看节点现在叫什么,节点没了再用快照里记的那个。** 顺序反过来会让用户看到
    一个他自己早就改掉的旧名字;而不留快照那一份,节点被删掉之后这条分析就变成
    "一条关于(已删除)的分析" —— 两个都不能接受,所以两份都用。
    """
    live = await _load_nodes(
        db,
        ctx.id,
        [node_id for node_id in (analysis.scope_root_id, analysis.focus_node_id) if node_id],
    )
    by_id = {node.id: node.title for node in live}
    recorded = {node.node_id: node.title for node in stored.nodes}
    return tuple(
        _title_for(node_id, by_id, recorded)
        for node_id in (analysis.scope_root_id, analysis.focus_node_id)
    )


def _title_for(node_id, by_id, recorded) -> str | None:
    if node_id is None:
        return None
    if node_id in by_id:
        return by_id[node_id]
    return recorded.get(str(node_id)) or None


__all__ = [
    "DEFAULT_LIMIT",
    "SECTION_FIELDS",
    "Staleness",
    "check",
    "current_snapshot_like",
    "input_changed_since",
    "list_for",
    "record",
    "view_of",
]
