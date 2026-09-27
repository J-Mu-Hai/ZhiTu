"""提案:从模型的变更到用户确认后真正落库。

## 三个阶段,以及它们之间那条不能省的线

    ① 生成   模型给出 actions → **只读校验** → 写一条 Proposal + N 条 ProposalItem
    ② 预览   用户看到"将要新增/修改/删除什么"
    ③ 确认   在锁里重新校验 → 一个事务写入 plan_nodes / dependencies / plan_revisions

**③ 里的"重新校验"不是多余的。** 提案生成之后,用户可能自己改过计划 —— 勾了一个任务
完成、编辑了节点、确认了另一份提案。那些改动都会让 `current_revision_version` 前进。
此时照单应用一份基于旧数据的提案,会静默回退掉用户刚做的改动,而界面上没有任何迹象。
所以确认路径上跑的是**和生成时完全相同的那个纯函数**,只是输入换成了此刻的数据。

## 确认事务里发生了什么,以及为什么是这些

    守卫写(锁住 workspace 行)
      → 版本号比对
      → 状态 CAS(WHERE status IN ('validated','pending_confirmation'))
      → 插幂等台账(唯一键 user_id + idempotency_key)
      → 插节点 / 改节点 / 软删除 / 增删依赖
      → 插 plan_revisions(snapshot + diff)
      → 更新 workspaces.current_revision_version
      → 插 domain_events
    一起提交;任何异常整体回滚

里面有两道**互相独立**的闸门,缺一不可:

- **守卫写 + 版本号**挡的是并发:两个请求同时读到 v3、都想写 v4。
- **唯一约束**挡的是重复执行:同一个幂等键被执行两次 —— 包括跨进程、跨重启的重复
  提交,那是内存里的锁永远挡不住的。

## 幂等为什么必须靠台账,而不能靠"看看状态是不是已经 applied"

那个做法有一个致命的窗口:第一个请求写完了节点、**还没提交**,第二个请求读到的状态
仍然是 validated,于是它也去写一遍。唯一约束把这个窗口关掉 —— 两个请求抢着插同一行
台账,数据库保证只有一个成功,输的那个回读赢家写好的结果原样返回。

## 用户双击"确认"会发生什么

第一次:正常写入,返回 `replayed=false`。
第二次:幂等键命中台账,返回**当时存下来的那份响应**,`replayed=true`,一行都没多写。

用户看到的是同一个结果,而不是一个"这份提案已经处理过了"的错误 —— 后者会让他怀疑
第一次到底成功了没有。
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.contracts.proposal import (
    ActionError,
    AppliedChangeView,
    ConfirmProposalResponse,
    ProposalItemView,
    ProposalView,
)
from backend.db.base import utcnow
from backend.db.locking import lock_workspace
from backend.db.models import (
    Dependency,
    DomainEvent,
    PlanNode,
    PlanRevision,
    Proposal,
    ProposalDecision,
    ProposalItem,
    Workspace,
)
from backend.db.models.enums import (
    DependencyType,
    NodeOrigin,
    NodeStatus,
    ProposalOp,
    ProposalStatus,
    RevisionActor,
    RevisionTrigger,
)
from backend.services import plan_service, turn_context
from backend.services.context import WorkspaceContext
from backend.services.node_service import touch_content_version
from backend.services.errors import (
    IdempotencyKeyReused,
    ProposalExpired,
    ProposalNoLongerValid,
    ProposalNotActionable,
    ProposalNotFound,
    StaleBaseRevision,
)
from backend.services.proposal_validation import (
    NodeSnapshot,
    ValidatedPlan,
    validate_actions,
)
from backend.services.timeutil import today_in

logger = logging.getLogger(__name__)

#: 提案多久之后作废。三天:足够用户想清楚,又不至于让一份基于"每周 6 小时"的计划
#: 在被搁置一周后还能照单应用 —— 那时他的时间预算很可能已经变了。
PROPOSAL_TTL_HOURS = 72

#: `reasoning` 落库前的截断长度。模型偶尔会把整段回复塞进来,而这一列是给人看的摘要,
#: 不是正文 —— 正文在 messages 里已经有了。
REASONING_MAX = 2000

#: 可以被确认的状态。`draft` 不在其中:它表示"还没校验完",而校验与建行是同一次
#: flush,所以正常情况下用户看不到这个状态。
ACTIONABLE_STATUSES = (ProposalStatus.VALIDATED, ProposalStatus.PENDING_CONFIRMATION)


@dataclass(frozen=True, slots=True)
class ProposalOutcome:
    """生成阶段的结果。

    `proposal=None` + `errors=()` 表示"模型没提任何变更",这是绝大多数轮次的情形。
    `proposal=None` + 有 `errors` 表示"提了,但没能通过校验" —— 这两件事对用户
    意味着完全不同的东西,所以调用方必须能分辨。
    """

    proposal: Proposal | None
    errors: tuple[ActionError, ...] = ()

    @property
    def rejected(self) -> bool:
        return bool(self.errors)


@dataclass(frozen=True, slots=True)
class ConfirmOutcome:
    response: ConfirmProposalResponse
    replayed: bool


# ---------------------------------------------------------------------------------
# 生成
# ---------------------------------------------------------------------------------
async def build_from_actions(
    db: AsyncSession,
    ctx: WorkspaceContext,
    *,
    conversation_id: uuid.UUID | None,
    actions: tuple[dict, ...],
    handles: tuple[tuple[str, str], ...],
    reasoning: str | None = None,
    assistant_message=None,
    trigger_type: RevisionTrigger | None = None,
    writable_handles: tuple[str, ...] | None = None,
) -> ProposalOutcome:
    """校验模型这一轮提的变更,通过就落一条提案。

    **校验失败时一行都不写。** 不是"小心翼翼地写一部分再回滚",而是这一层在失败
    路径上根本没有写的能力 —— 它拿到的 `plan` 是 None。

    `handles` 必须是**这一轮送给模型的那一份**记号表,不能在这里重新按当前库状态
    生成。重新生成的话,模型说的 `n3` 可能已经指向了另一个节点 —— 那正好是"用户
    改动被静默覆盖"的成因。

    `trigger_type` 让调用方能说清"这份提案是怎么来的"。复盘那条路径传
    `EXECUTION_DEVIATION`,于是它在 `plan_revisions` 里也分得出来 —— 而
    "这次调整是因为执行情况,不是我自己想改"正是复盘时最需要看得见的那件事。
    不给时按老规矩推:第一版计划是 `initial_plan`,其余是 `manual_replan`。

    `writable_handles` 是**这一轮模型能改的那些节点的记号**。传 `None` 表示不设范围
    限制(复盘那条工作区级的路径、以及直接调用的测试);对话那条路径一定传,
    因为它拿到的是一个有范围问题的上下文(用户在某一层子空间里)。范围之外的动作
    会被逐条拒成 `OUT_OF_SCOPE`,而不是静默丢掉 —— 用户要看到"AI 提了但我没执行",
    否则他会以为那句调整已经生效了。
    """
    if not actions:
        return ProposalOutcome(proposal=None)

    nodes = await turn_context.load_nodes(db, ctx.id)
    snapshots = {
        node.id: NodeSnapshot(
            id=node.id,
            parent_id=node.parent_id,
            title=node.title,
            depth=node.depth,
            order_index=node.order_index,
        )
        for node in nodes
    }
    handle_map = _handles_to_ids(handles)
    dependencies = await _load_dependency_pairs(db, ctx.id)
    # 记号表里认不出来的记号直接落空 —— 那一条会在校验里按悬空引用被拒,
    # 而不是变成"范围内没有这个节点"。
    writable = (
        None
        if writable_handles is None
        else {handle_map[handle] for handle in writable_handles if handle in handle_map}
    )

    result = validate_actions(
        actions,
        handles=handle_map,
        nodes=snapshots,
        dependencies=dependencies,
        today=today_in(ctx.timezone),
        writable=writable,
    )

    if result.plan is None:
        if result.errors:
            logger.info(
                "模型提的变更没有通过校验(共 %d 条问题),未产生任何写入: %s",
                len(result.errors),
                "; ".join(f"[{e.code}] {e.message}" for e in result.errors[:3]),
            )
        return ProposalOutcome(proposal=None, errors=result.errors)

    proposal = await _persist(
        db,
        ctx,
        conversation_id=conversation_id,
        plan=result.plan,
        reasoning=reasoning,
        trigger_type=trigger_type,
    )
    if assistant_message is not None:
        # 让界面能从这条回复直接跳到它带来的那份提案。关联由 messages 单侧持有,
        # proposals 表里没有回指的列(建表期的循环引用,见 models/proposal.py)。
        assistant_message.proposal_id = proposal.id
    await db.flush()
    return ProposalOutcome(proposal=proposal)


async def _persist(
    db: AsyncSession,
    ctx: WorkspaceContext,
    *,
    conversation_id: uuid.UUID | None,
    plan: ValidatedPlan,
    reasoning: str | None,
    trigger_type: RevisionTrigger | None = None,
) -> Proposal:
    version = await _current_revision_version(db, ctx.id)
    now = utcnow()

    proposal = Proposal(
        user_id=ctx.owner_id,
        workspace_id=ctx.id,
        conversation_id=conversation_id,
        status=ProposalStatus.VALIDATED,
        base_revision_version=version,
        # 版本号是 1 意味着"这个空间还没有任何一次计划变更记录"(见
        # workspace_service 里对这个计数器的说明)。所以这一份是第一版计划。
        trigger_type=trigger_type
        or (RevisionTrigger.INITIAL_PLAN if version == 1 else RevisionTrigger.MANUAL_REPLAN),
        reasoning=(reasoning or "").strip()[:REASONING_MAX] or None,
        change_summary=_change_summary(plan),
        content_hash=_content_hash(plan),
        expires_at=now + timedelta(hours=PROPOSAL_TTL_HOURS),
    )
    db.add(proposal)
    await db.flush()

    for item in plan.items:
        db.add(
            ProposalItem(
                proposal_id=proposal.id,
                ordinal=item.ordinal,
                op=ProposalOp(item.op),
                local_id=item.local_id,
                target_node_id=item.target_node_id,
                # **模型原样的那一段 JSON**,不是校验后重新 dump 出来的 ——
                # 确认时会拿它重新跑一遍校验,原样存下来才能保证"预览的"和
                # "执行的"是同一份输入(详见 proposal_validation 里那段注释)。
                payload=item.payload,
                created_at=now,
            )
        )
    await db.flush()
    return proposal


# ---------------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------------
async def load_proposal(db: AsyncSession, ctx: WorkspaceContext, proposal_id: uuid.UUID) -> Proposal:
    """按 id 取提案,**同时**校验它属于当前空间。

    归属条件写在 WHERE 里,与 `load_workspace_context` 同一条纪律:
    "取出来再比一次"意味着"忘记比"是一条可能的代码路径。
    """
    result = await db.execute(
        select(Proposal).where(Proposal.id == proposal_id, Proposal.workspace_id == ctx.id)
    )
    proposal = result.scalar_one_or_none()
    if proposal is None:
        raise ProposalNotFound("没有找到这份提案。")
    return proposal


async def load_items(db: AsyncSession, proposal_id: uuid.UUID) -> list[ProposalItem]:
    result = await db.execute(
        select(ProposalItem)
        .where(ProposalItem.proposal_id == proposal_id)
        .order_by(ProposalItem.ordinal.asc())
    )
    return list(result.scalars())


async def list_proposals(
    db: AsyncSession, ctx: WorkspaceContext, *, limit: int = 50
) -> list[Proposal]:
    """这个空间的提案,新的在前。"""
    result = await db.execute(
        select(Proposal)
        .where(Proposal.workspace_id == ctx.id)
        .order_by(Proposal.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars())


def to_view(proposal: Proposal, items: list[ProposalItem]) -> ProposalView:
    """提案 -> 接口视图。

    预览用的 `summary` / `targetTitle` 读的是 `change_summary` 里那份**生成时就写好**
    的展示信息,不是此刻重新算的。重新算会有两个问题:一是要重跑校验,而计划可能
    已经变了;二是那样显示的就不是"用户当初看到的那份提案"了 —— 一份提案的预览
    应该在它被决定之前保持不变。
    """
    stored = proposal.change_summary if isinstance(proposal.change_summary, dict) else {}
    display = {
        int(entry.get("ordinal", 0)): entry
        for entry in stored.get("items", [])
        if isinstance(entry, dict)
    }

    views: list[ProposalItemView] = []
    for item in items:
        meta = display.get(item.ordinal, {})
        views.append(
            ProposalItemView(
                ordinal=item.ordinal,
                op=item.op.value,
                summary=str(meta.get("summary") or item.op.value),
                local_id=item.local_id,
                target_node_id=item.target_node_id,
                target_title=meta.get("targetTitle"),
                payload=item.payload if isinstance(item.payload, dict) else {},
                affected_children=int(meta.get("affectedChildren") or 0),
            )
        )

    return ProposalView(
        id=proposal.id,
        status=proposal.status.value,
        trigger_type=proposal.trigger_type.value,
        base_revision_version=proposal.base_revision_version,
        reasoning=proposal.reasoning,
        change_summary=stored,
        item_count=len(views),
        items=views,
        created_at=proposal.created_at,
        expires_at=proposal.expires_at,
        decided_at=proposal.decided_at,
    )


async def view_of(db: AsyncSession, proposal: Proposal) -> ProposalView:
    return to_view(proposal, await load_items(db, proposal.id))


# ---------------------------------------------------------------------------------
# 确认
# ---------------------------------------------------------------------------------
async def confirm_proposal(
    db: AsyncSession,
    ctx: WorkspaceContext,
    proposal_id: uuid.UUID,
    *,
    idempotency_key: str,
) -> ConfirmOutcome:
    """把一份提案真正写进计划。"""
    # 下面几个值先取成**普通标量**再往下走。
    #
    # 因为失败路径上有 `db.rollback()`,而回滚会让会话里所有 ORM 对象过期。之后再读
    # `ctx.workspace.id` 或者 `proposal.base_revision_version` 会触发一次懒加载 ——
    # 在 async SQLAlchemy 里那会直接抛 `MissingGreenlet`,把一个本该是 409 的业务
    # 错误变成 500。这些值在函数入口处就是确定的,先取出来,失败分支就再也没有理由
    # 去碰 ORM 对象。
    workspace_id = ctx.id
    user_id = ctx.user.user_id
    request_hash = _hash({"proposalId": str(proposal_id)})

    # 快速路径:这个幂等键已经处理过。**先查台账再取提案** —— 顺序反过来的话,
    # 一份已经被处理、随后被别的提案取代的提案会先报"不可操作",而用户真正
    # 需要的是看到上次那次的成功结果。
    prior = await _find_decision(db, user_id, idempotency_key)
    if prior is not None:
        return _replay(prior, request_hash)

    proposal = await load_proposal(db, ctx, proposal_id)
    _assert_actionable(proposal)
    _assert_not_expired(proposal)
    base_version = proposal.base_revision_version

    try:
        # ---- 事务从这里开始 ------------------------------------------------
        await lock_workspace(db, workspace_id)

        # **版本号比对排在重新校验之前。** 两个都在挡"不要覆盖用户的改动",但顺序
        # 决定了用户看到哪句话。计划变了之后,重新校验往往报的是一条具体而令人困惑的
        # 错误(比如"n2 这个编号已经存在"),而真正的原因只是"你刚改过计划"。
        # 先报版本,用户拿到的才是能行动的那句话:重新生成。
        current = await _current_revision_version(db, workspace_id)
        if current != base_version:
            await db.rollback()
            await _mark_status(db, workspace_id, proposal_id, ProposalStatus.STALE)
            raise StaleBaseRevision(
                "这份提案生成之后计划又有过改动,直接应用会覆盖掉那次改动。请重新生成一份。",
                baseRevisionVersion=base_version,
                currentRevisionVersion=current,
            )

        # 拿**此刻**的数据重新跑一遍和生成时完全相同的那个纯函数。
        plan, errors = await _revalidate(db, ctx, proposal)
        if plan is None:
            first = errors[0] if errors else None
            await db.rollback()
            await _mark_status(
                db,
                workspace_id,
                proposal_id,
                ProposalStatus.FAILED,
                error_code=first.code if first else "PROPOSAL_NO_LONGER_VALID",
                error_detail=first.message if first else None,
            )
            raise ProposalNoLongerValid(
                "这份提案里有些变更放到现在的计划上已经不成立了,没有应用。请重新生成。",
                problems=[
                    {"ordinal": e.ordinal, "code": e.code, "message": e.message}
                    for e in errors
                ],
            )

        # 状态 CAS。两个请求都通过了上面的检查时,只有一个能改到这一行。
        decided = await db.execute(
            update(Proposal)
            .where(Proposal.id == proposal_id, Proposal.status.in_(ACTIONABLE_STATUSES))
            .values(status=ProposalStatus.APPLIED, decided_at=utcnow())
            .execution_options(synchronize_session=False)
        )
        if decided.rowcount == 0:
            await db.rollback()
            prior = await _find_decision(db, user_id, idempotency_key)
            if prior is not None:
                return _replay(prior, request_hash)
            raise ProposalNotActionable("这份提案已经处理过了。")

        # 幂等台账。**INSERT 放在写入之前** —— 它必须和业务写入在同一个事务里,
        # 这样"台账写了但节点没写"和"节点写了但台账没写"都不可能发生。
        decision = ProposalDecision(
            user_id=user_id,
            proposal_id=proposal_id,
            idempotency_key=idempotency_key,
            content_hash=request_hash,
            # 占位。真正要返回的响应体在写入完成后才构造得出来,下面同一个事务里
            # 把它 UPDATE 上去。
            result={},
            created_at=utcnow(),
        )
        db.add(decision)
        await db.flush()

        applied, revision_id = await _apply(db, ctx, proposal, plan)
        await db.flush()

        # 状态是 CAS 改的(那边用了 synchronize_session=False,内存里这份还停在
        # validated),所以响应里的状态要回读一次才准。
        await db.refresh(proposal)
        view = await view_of(db, proposal)
        response = ConfirmProposalResponse(proposal=view, applied=applied, replayed=False)
        decision.result = response.model_dump(mode="json", by_alias=True)

        db.add(
            DomainEvent(
                user_id=user_id,
                workspace_id=workspace_id,
                kind="proposal_applied",
                ref_type="proposal",
                ref_id=proposal_id,
                payload={
                    "revisionId": str(revision_id),
                    "revisionVersion": applied.revision_version,
                    "nodesCreated": applied.nodes_created,
                    "nodesUpdated": applied.nodes_updated,
                    "nodesDeleted": applied.nodes_deleted,
                },
                created_at=utcnow(),
            )
        )
        await db.commit()
        # ---- 事务到这里结束 ------------------------------------------------
        return ConfirmOutcome(response=response, replayed=False)

    except IntegrityError:
        # 台账的唯一键被撞了 —— 另一个请求用同一个幂等键赢了。这不是错误,
        # 是"你双击的第二下"。回读赢家写好的响应原样返回。
        await db.rollback()
        prior = await _find_decision(db, user_id, idempotency_key)
        if prior is None:
            # 撞的是别的唯一约束(比如 plan_revisions 的版本号)。那是真 bug,
            # 以原貌暴露,不伪装成幂等命中。
            raise
        return _replay(prior, request_hash)


async def reject_proposal(
    db: AsyncSession, ctx: WorkspaceContext, proposal_id: uuid.UUID, *, reason: str | None = None
) -> Proposal:
    """拒绝一份提案。**只是标记,不产生任何计划写入。**"""
    proposal = await load_proposal(db, ctx, proposal_id)
    if proposal.status is not ProposalStatus.VALIDATED and (
        proposal.status is not ProposalStatus.PENDING_CONFIRMATION
    ):
        raise ProposalNotActionable("这份提案已经处理过了。")

    proposal.status = ProposalStatus.REJECTED
    proposal.decided_at = utcnow()
    if reason:
        proposal.error_detail = reason.strip()[:500]
    await db.commit()
    return proposal


# ---------------------------------------------------------------------------------
# 确认事务的内部步骤
# ---------------------------------------------------------------------------------
async def _revalidate(
    db: AsyncSession, ctx: WorkspaceContext, proposal: Proposal
) -> tuple[ValidatedPlan | None, tuple[ActionError, ...]]:
    """拿**此刻**的数据把这份提案重新跑一遍校验。

    输入是当初存下来的那批 payload,记号表也要按此刻的节点顺序重建 —— 这里重建
    是**正确**的,而不是前面 `build_from_actions` 里被禁止的那件事。区别在于:
    生成时模型看到的是某一刻的记号表,必须用同一份;而确认时我们要问的是
    "这批变更放到现在的数据上还成立吗",用现在的记号表才问得对。
    """
    nodes = await turn_context.load_nodes(db, ctx.id)
    snapshots = {
        node.id: NodeSnapshot(
            id=node.id,
            parent_id=node.parent_id,
            title=node.title,
            depth=node.depth,
            order_index=node.order_index,
        )
        for node in nodes
    }
    handles = {f"n{index}": node.id for index, node in enumerate(nodes, start=1)}
    dependencies = await _load_dependency_pairs(db, ctx.id)

    items = await load_items(db, proposal.id)
    actions = tuple(item.payload for item in items if isinstance(item.payload, dict))

    result = validate_actions(
        actions,
        handles=handles,
        nodes=snapshots,
        dependencies=dependencies,
        today=today_in(ctx.timezone),
    )
    return result.plan, result.errors


async def _apply(
    db: AsyncSession, ctx: WorkspaceContext, proposal: Proposal, plan: ValidatedPlan
) -> tuple[AppliedChangeView, uuid.UUID]:
    """把校验过的变更集写进库里。**调用方必须已经持有空间上的锁。**"""
    revision_version = await _current_revision_version(db, ctx.id)
    now = utcnow()

    # -- 新建 ---------------------------------------------------------------
    created_nodes: list[PlanNode] = []
    for planned in plan.creates:
        action = planned.action
        node = PlanNode(
            id=planned.id,
            workspace_id=ctx.id,
            parent_id=planned.parent_id,
            title=action.title.strip(),
            description=_clean(action.description),
            acceptance_criteria=_clean(action.acceptance_criteria),
            node_type=action.node_type,
            status=NodeStatus.PENDING,
            priority=action.priority,
            estimate_minutes=action.estimate_minutes,
            deadline=action.deadline,
            order_index=planned.order_index,
            depth=planned.depth,
            # AI 提的节点标成 AI。界面上要能区分"我自己写的"和"AI 帮我定的" ——
            # 用户对这两类内容的信任程度不一样,混在一起会让他不知道自己该复核什么。
            origin=NodeOrigin.AI,
        )
        db.add(node)
        created_nodes.append(node)

    # **这一句不能省。** ORM 的 flush 顺序是按 mapper 之间的 `relationship()` 排的,
    # 不是按外键排的 —— 而 `Dependency` 与 `PlanNode` 之间刻意没有 relationship
    # (见 db/models/plan.py,两边只用裸外键)。所以不显式 flush 的话,依赖行会先于
    # 它引用的节点被插入,在开了外键约束的 SQLite 上直接 `FOREIGN KEY constraint failed`,
    # 用户看到的是"确认失败",日志里是一句和计划毫无关系的数据库报错。
    await db.flush()

    # -- 修改 ---------------------------------------------------------------
    for patch in plan.updates:
        node = await db.get(PlanNode, patch.node_id)
        if node is None or node.workspace_id != ctx.id:
            # 校验阶段已经确认过它在这个空间里,走到这里说明中途被别的东西删了。
            # 抛出去让整个事务回滚 —— 这正是"确认要么全成要么全不成"要的效果。
            raise ProposalNoLongerValid("提案里要修改的一个节点已经不存在了。")

        for field in patch.changed_fields:
            setattr(node, field, getattr(patch.action, field))
        # **正文的版本号必须跟着一起走。** 少这一句,"AI 改过的正文"在版本上等于没
        # 发生过:客户端手里那个号还停在原地,它下一次保存就会把这条**用户已经确认过**
        # 的改写静默盖掉 —— 而两边都不会看到冲突。规则与用户直接编辑共用同一个函数,
        # 免得两条路各写一遍、然后有一条忘了写。
        touch_content_version(node, patch.changed_fields)
        if patch.action.title is not None:
            node.title = patch.action.title.strip()
        # `completed_at` 与 `status` 必须一起改。留着一个"已完成但没有完成时间"的
        # 行,复盘时就算不出"这个阶段花了多久"。
        if "status" in patch.changed_fields:
            node.completed_at = now if node.status is NodeStatus.COMPLETED else None

    # -- 软删除 -------------------------------------------------------------
    if plan.deletes:
        await db.execute(
            update(PlanNode)
            .where(PlanNode.id.in_(plan.deletes), PlanNode.workspace_id == ctx.id)
            .values(deleted_at=now)
            .execution_options(synchronize_session=False)
        )

    # -- 依赖 ---------------------------------------------------------------
    for predecessor, successor in plan.dependencies_remove:
        await db.execute(
            delete(Dependency).where(
                Dependency.workspace_id == ctx.id,
                Dependency.predecessor_id == predecessor,
                Dependency.successor_id == successor,
            )
        )
    for predecessor, successor in plan.dependencies_add:
        db.add(
            Dependency(
                workspace_id=ctx.id,
                predecessor_id=predecessor,
                successor_id=successor,
                dep_type=DependencyType.FINISH_TO_START,
                lag_days=0,
            )
        )

    # 先 flush 再取快照:本会话是 autoflush=False(见 db/session.py),
    # 不显式 flush 的话快照里会缺掉刚刚新增的那些节点。
    await db.flush()

    # -- 计划版本 -----------------------------------------------------------
    snapshot = await plan_service.snapshot_payload(db, ctx.id)
    parent = await db.scalar(
        select(PlanRevision).where(
            PlanRevision.workspace_id == ctx.id,
            PlanRevision.version == revision_version - 1,
        )
    )
    revision = PlanRevision(
        workspace_id=ctx.id,
        version=revision_version,
        parent_revision_id=parent.id if parent else None,
        trigger_type=proposal.trigger_type,
        trigger_detail=f"确认了 AI 提出的 {len(plan.items)} 项变更",
        actor=RevisionActor.AI,
        proposal_id=proposal.id,
        snapshot=snapshot,
        diff={
            "createdNodeIds": [str(node.id) for node in created_nodes],
            "updatedNodeIds": [str(patch.node_id) for patch in plan.updates],
            "deletedNodeIds": [str(node_id) for node_id in plan.deletes],
            "addedDependencies": [
                {"predecessorId": str(a), "successorId": str(b)}
                for a, b in plan.dependencies_add
            ],
            "removedDependencies": [
                {"predecessorId": str(a), "successorId": str(b)}
                for a, b in plan.dependencies_remove
            ],
        },
        created_at=now,
    )
    db.add(revision)
    await db.flush()

    # 新节点属于这一次变更。放在这里而不是构造时,是因为那时 revision 还没有 id。
    for node in created_nodes:
        node.plan_revision_id = revision.id

    # -- 版本号前进 ---------------------------------------------------------
    # 用 UPDATE 而不是 `ctx.workspace.current_revision_version += 1`:后者读的是
    # 本会话开始时的那个值,而 CAS 之前可能有别的请求已经让它前进了。写 SQL 让数据库
    # 按此刻的真实值来算,顺带在 PostgreSQL 上给这一行留下一条写锁记录。
    await db.execute(
        update(Workspace)
        .where(Workspace.id == ctx.id)
        .values(current_revision_version=revision_version + 1)
        .execution_options(synchronize_session=False)
    )
    # 内存里的那份也要跟上 —— 同一个请求后面还会读到它(比如构造响应)。
    ctx.workspace.current_revision_version = revision_version + 1

    return (
        AppliedChangeView(
            nodes_created=len(created_nodes),
            nodes_updated=len(plan.updates),
            # 这里报的是**用户理解的"删掉了几个"**,也就是包含被连带删除的子节点。
            nodes_deleted=len(plan.deletes),
            dependencies_added=len(plan.dependencies_add),
            dependencies_removed=len(plan.dependencies_remove),
            revision_version=revision_version,
        ),
        revision.id,
    )


# ---------------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------------
def _handles_to_ids(handles: tuple[tuple[str, str], ...]) -> dict[str, uuid.UUID]:
    converted: dict[str, uuid.UUID] = {}
    for handle, raw in handles:
        try:
            converted[handle] = uuid.UUID(raw)
        except ValueError:
            # 记号表是服务端自己生成的,出现非法 uuid 说明上游写错了,不是
            # 模型的问题。跳过它比让它变成一个必然失败的查找要好 —— 至少
            # 剩下的记号还能正常解析。
            logger.warning("记号表里出现了不是 uuid 的值,已忽略: %s", handle)
    return converted


async def _load_dependency_pairs(
    db: AsyncSession, workspace_id: uuid.UUID
) -> set[tuple[uuid.UUID, uuid.UUID]]:
    """这个空间里**两端都还活着**的依赖边。校验环时用的就是它。

    "两端都活着"这一条是 2026-09-27 补的(规则见 `docs/10-NEXT-BATCH-SCOPE.md` 第 5 节)。
    补之前这里是"整个空间的依赖行",而删除节点**不再**物理删边(`node_service` 里那一段
    硬删已经去掉,理由见那里的注释)—— 于是归档节点留下的边会混进校验集合,让一份本来
    合法的提案撞上一条**穿过已归档节点**的环,报出来的链上还带着一个用户已经删掉的节点。

    这就是 `plan_service.load_all_dependencies` 那条纪律的同一份,写在提案这一侧。
    """
    live = select(PlanNode.id).where(
        PlanNode.workspace_id == workspace_id, PlanNode.deleted_at.is_(None)
    )
    result = await db.execute(
        select(Dependency.predecessor_id, Dependency.successor_id).where(
            Dependency.workspace_id == workspace_id,
            Dependency.predecessor_id.in_(live),
            Dependency.successor_id.in_(live),
        )
    )
    return {(row[0], row[1]) for row in result.all()}


async def _current_revision_version(db: AsyncSession, workspace_id: uuid.UUID) -> int:
    value = await db.scalar(
        select(Workspace.current_revision_version).where(Workspace.id == workspace_id)
    )
    return int(value or 1)


async def _find_decision(
    db: AsyncSession, user_id: uuid.UUID, idempotency_key: str
) -> ProposalDecision | None:
    result = await db.execute(
        select(ProposalDecision).where(
            ProposalDecision.user_id == user_id,
            ProposalDecision.idempotency_key == idempotency_key,
        )
    )
    return result.scalar_one_or_none()


def _replay(decision: ProposalDecision, request_hash: str) -> ConfirmOutcome:
    """返回上次存下来的那份响应。

    内容哈希对不上说明这个幂等键被用在了另一个请求上 —— 那时如果照样返回上次的
    结果,用户会看到**另一份提案**被"确认成功"。所以这里必须拦下来。
    """
    if decision.content_hash != request_hash:
        raise IdempotencyKeyReused(
            "这个幂等键已经用在另一次确认上了,请换一个。",
        )
    stored = decision.result if isinstance(decision.result, dict) else {}
    if not stored:
        # 台账写了但响应体还没写上 —— 只可能发生在赢家还在事务里没提交时,
        # 此时唯一约束还没生效,读到的会是一条空壳。让用户重试即可。
        raise ProposalNotActionable("这份提案正在处理中,请稍候刷新。")
    response = ConfirmProposalResponse.model_validate({**stored, "replayed": True})
    return ConfirmOutcome(response=response, replayed=True)


def _assert_actionable(proposal: Proposal) -> None:
    if proposal.status not in ACTIONABLE_STATUSES:
        raise ProposalNotActionable(
            "这份提案已经处理过了。",
            status=proposal.status.value,
        )


def _assert_not_expired(proposal: Proposal) -> None:
    if proposal.expires_at is not None and proposal.expires_at <= utcnow():
        raise ProposalExpired("这份提案放太久了,已经失效。请按现在的条件重新生成一份。")


async def _mark_status(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    proposal_id: uuid.UUID,
    status: ProposalStatus,
    *,
    error_code: str | None = None,
    error_detail: str | None = None,
) -> None:
    """单独提交一次状态变更。

    这里刻意**另起一个事务**而不是把状态改在失败的那个事务里:失败的事务会被回滚,
    状态改了也留不下。而这几个状态本身是有意义的 —— 一份"已经过期"的提案下次就不该
    再走一遍校验再失败一遍。

    只改状态这一列,不碰任何计划数据,所以它不违反"要么全成要么全不成"。

    参数收的是裸 `workspace_id` 而不是 `WorkspaceContext`:它**只在失败路径**上被调用,
    而那时会话刚刚回滚过,`ctx.workspace` 已经过期 —— 从它身上读任何字段都会触发一次
    异步上下文里做不到的懒加载。这个函数因此不接受任何 ORM 对象。
    """
    await db.execute(
        update(Proposal)
        .where(Proposal.id == proposal_id, Proposal.workspace_id == workspace_id)
        .values(
            status=status,
            decided_at=utcnow(),
            error_code=error_code,
            error_detail=error_detail,
        )
        .execution_options(synchronize_session=False)
    )
    await db.commit()


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None


def _content_hash(plan: ValidatedPlan) -> str:
    return _hash([item.payload for item in plan.items])


def _hash(value: object) -> str:
    """规范化 JSON 的 blake2b。键排序,保证"同样的内容"永远得到同一个哈希。"""
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.blake2b(encoded.encode("utf-8"), digest_size=32).hexdigest()


def _change_summary(plan: ValidatedPlan) -> dict:
    """给界面用的变更摘要。

    ## 只有一份表示,而不是 add/update/delete 三个数组

    计划里原先是 `{add, update, delete}`,但那样"每条变更"这个事实就同时存在于
    分组数组和它自己那一行里 —— 两处表示同一件事,迟早有一处会漏更新。这里改成
    **一个 items 数组 + 一组计数**:分组由界面按 `op` 过滤得到(一行 filter 的事),
    计数在这里算好(因为"这次总共改了几条"是要给用户看的一句话,不该由界面拼)。
    """
    items: list[dict] = []
    counts = {
        "createNode": 0,
        "updateNode": 0,
        "deleteNode": 0,
        "addDependency": 0,
        "removeDependency": 0,
    }
    bucket = {
        ProposalOp.CREATE_NODE.value: "createNode",
        ProposalOp.UPDATE_NODE.value: "updateNode",
        ProposalOp.DELETE_NODE.value: "deleteNode",
        ProposalOp.CREATE_DEPENDENCY.value: "addDependency",
        ProposalOp.DELETE_DEPENDENCY.value: "removeDependency",
    }

    for item in plan.items:
        key = bucket.get(item.op)
        if key:
            counts[key] += 1
        items.append(
            {
                "ordinal": item.ordinal,
                "op": item.op,
                "summary": item.summary,
                "localId": item.local_id,
                "targetNodeId": str(item.target_node_id) if item.target_node_id else None,
                "targetTitle": item.target_title,
                "affectedChildren": item.affected_children,
            }
        )

    return {"counts": counts, "total": len(items), "items": items}


__all__ = [
    "ACTIONABLE_STATUSES",
    "PROPOSAL_TTL_HOURS",
    "ConfirmOutcome",
    "ProposalOutcome",
    "build_from_actions",
    "confirm_proposal",
    "list_proposals",
    "load_items",
    "load_proposal",
    "reject_proposal",
    "to_view",
    "view_of",
]
