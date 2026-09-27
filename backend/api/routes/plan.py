"""计划与提案。

## 为什么这四个接口挂在 `/api/workspaces/{id}` 下面

因为计划**属于空间**,不属于用户。用户的每个空间各有一份完整独立的计划树,而
"计划"这个词本身不携带"哪一个"的信息 —— 把 `GET /api/plan` 做成全局接口,迟早
要在查询参数里补一个 `workspaceId`,那时它和现在这个路径就是同一个东西,只是少了一层
归属校验的位置。

## 路径前缀与 workspaces.py 的关系

这个文件注册在 `/api/workspaces` 前缀下,和 `workspaces.py` 共用一个前缀。分成两个
文件是因为它们回答的是两类问题:那个文件管"空间是什么",这个文件管"空间里有什么计划、
有哪些等着你决定的提案"。混在一起会让 `workspaces.py` 变成什么都装的地方。

## 一个状态,多个视图

`GET /plan` 是**唯一**的计划读取接口。路径视图、时间线、任务列表、周计划、日计划
全都读这一份载荷,前端自己切。不做 `GET /plan/timeline` 这类接口 —— 那意味着同一份
计划有多个真值来源,而它们在不同时刻返回不一致的数据是迟早的事,且没有任何测试会发现。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.dependencies.workspace import get_workspace_context
from backend.contracts.analysis import MAX_ANALYSES, AnalysisListResponse
from backend.contracts.plan import (
    ArchiveImpactPayload,
    ArchivedNodePayload,
    CreateDependencyRequest,
    CreateNodeRequest,
    CreateRelationRequest,
    DependencyPayload,
    LayoutPayload,
    NodeEditResponse,
    OverbookedDayPayload,
    PlanNodePayload,
    PlanPayload,
    PutLayoutRequest,
    RelationPayload,
    RestoreResponse,
    UpdateNodeRequest,
    UpdateRelationRequest,
)
from backend.contracts.proposal import (
    ConfirmProposalRequest,
    ConfirmProposalResponse,
    ProposalView,
    RejectProposalRequest,
)
from backend.db.models.enums import AnalysisFreshness
from backend.db.session import get_db
from backend.services import (
    analysis_service,
    layout_service,
    node_service,
    plan_service,
    proposal_service,
)
from backend.services.context import WorkspaceContext

router = APIRouter()


@router.get("/{workspace_id}/plan", response_model=PlanPayload, summary="取计划")
async def read_plan(
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: AsyncSession = Depends(get_db),
) -> PlanPayload:
    """这个空间的完整计划。**只读,不产生任何副作用。**

    空空间返回的是 `nodes` 里只有一条根目标的正常载荷,不是 404 —— 新建的空间本来
    就该是可打开、可对话的。
    """
    return await plan_service.build_plan(db, ctx)


# ---------------------------------------------------------------------------------
# 直接编辑
#
# 这一组是**用户自己动手**的写路径,不经过提案。理由见 services/node_service.py:
# 勾掉一个任务已经是完整的意思表示,再问一次"确定吗"只会训练用户盲点确认。
#
# 它们和提案确认共用同一套版本记账(守卫锁 / plan_revisions / domain_events),
# 所以"AI 改的"和"我改的"在同一条版本线上,复盘时对得上。
# ---------------------------------------------------------------------------------
@router.post(
    "/{workspace_id}/nodes",
    response_model=NodeEditResponse,
    status_code=201,
    summary="新建节点",
)
async def create_node(
    payload: CreateNodeRequest,
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: AsyncSession = Depends(get_db),
) -> NodeEditResponse:
    """在自己已经有的节点下面挂一个新节点。**`origin` 记成 user,不是 ai。**"""
    result = await node_service.create_node(
        db,
        ctx,
        parent_id=payload.parent_id,
        title=payload.title,
        node_type=payload.node_type,
        purpose=payload.purpose,
        description=payload.description,
        acceptance_criteria=payload.acceptance_criteria,
        priority=payload.priority,
        estimate_minutes=payload.estimate_minutes,
        deadline=payload.deadline,
    )
    return _edit_response(result)


@router.patch(
    "/{workspace_id}/nodes/{node_id}", response_model=NodeEditResponse, summary="改节点"
)
async def update_node(
    node_id: uuid.UUID,
    payload: UpdateNodeRequest,
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: AsyncSession = Depends(get_db),
) -> NodeEditResponse:
    """改一个节点的字段。勾选完成就是这里,`status: "completed"`。

    `exclude_unset=True` 是关键:它让"没传这个字段"和"传了 null"分得开。前者是
    "不改",后者是"清空"(把截止时间去掉)。两者混起来的表现是"我只改了标题,
    说明被清空了"。

    `content_version` 走的是**另一条路**:它在那里被 `pop` 出来当成关键字参数,
    不进 `patch`。理由见 `UpdateNodeRequest.content_version` —— 它是前置条件,
    不是要写的列;混进 `patch` 会先撞上 `EDITABLE_FIELDS` 那道白名单,变成 400
    "这些字段不能直接改",而那会把"你手上那份旧了"报成"这个字段不能改"。
    """
    fields = payload.model_dump(exclude_unset=True)
    expected = fields.pop("content_version", None)
    result = await node_service.update_node(
        db, ctx, node_id, fields, expected_content_version=expected
    )
    return _edit_response(result)


@router.delete(
    "/{workspace_id}/nodes/{node_id}", response_model=NodeEditResponse, summary="删节点"
)
async def delete_node(
    node_id: uuid.UUID,
    mode: str = Query(
        default=node_service.ARCHIVE_MODE,
        pattern="^(archive|delete)$",
        description="archive(默认,可恢复)或 delete(彻底删除,不可恢复)",
    ),
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: AsyncSession = Depends(get_db),
) -> NodeEditResponse:
    """删掉一个节点**及其整棵子树**。根目标删不掉(409)。

    **默认是归档**(`mode=archive`):边的行一行不动,`POST /nodes/{id}/restore` 能把
    这一支原样拿回来。`mode=delete` 才是彻底删除 —— 它顺手物理删掉挂在上面的边,
    并且不再接受恢复。两者的区别在响应里的 `restorable` 上能直接读出来。

    `deletedCount` 会大于 1 —— 返回它是因为用户需要知道"我删的是这一条,
    还是它下面那一串也一起没了"。界面上要据此说清楚;而"这一下会带走什么"要在
    **点之前**从 `GET /nodes/{id}/archive-impact` 拿到。
    """
    result = await node_service.delete_node(db, ctx, node_id, mode=mode)
    return _edit_response(result)


@router.get(
    "/{workspace_id}/nodes/{node_id}/archive-impact",
    response_model=ArchiveImpactPayload,
    summary="归档前的影响范围",
)
async def read_archive_impact(
    node_id: uuid.UUID,
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: AsyncSession = Depends(get_db),
) -> ArchiveImpactPayload:
    """这一下会带走什么:后代、关系、依赖、场次。

    **只读,而且不加锁。** 它是给"你确定吗"那个对话框用的,而为了画一个对话框去抢
    工作区锁,会让"两个人在同一秒里删不同的东西"变成其中一个看到一句莫名其妙的等待
    超时。代价是这份数字可能比真正执行的那一刻早几秒 —— 界面上的措辞因此是
    "会带走",而不是"已经带走"。
    """
    impact = await node_service.archive_impact(db, ctx, node_id)
    return ArchiveImpactPayload(
        node_id=impact.node_id,
        title=impact.title,
        descendants=impact.descendants,
        relations=impact.relations,
        dependencies=impact.dependencies,
        sessions=impact.sessions,
        session_minutes=impact.session_minutes,
        overdue_sessions=impact.overdue_sessions,
    )


@router.post(
    "/{workspace_id}/nodes/{node_id}/restore",
    response_model=RestoreResponse,
    summary="恢复归档的节点",
)
async def restore_node(
    node_id: uuid.UUID,
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: AsyncSession = Depends(get_db),
) -> RestoreResponse:
    """把归档的节点**连同当时一起被归档的那一支**恢复回来。

    三种失败各自有明确的话,因为用户能做的事不一样:

    - `NODE_PURGED`(409):它是被彻底删除的,**没有**拿回来的办法。
    - `PARENT_ARCHIVED`(409):上层还在归档里,先恢复上层。
    - `DEPENDENCY_CYCLE`(409):恢复之后会出现环,所以整体没有恢复(什么都没变)。

    返回里带着排期报告(回来几场、过期几场、哪几天超了上限)。**恢复不重排、不丢弃
    任何场次** —— 静默重排等于替用户改计划。
    """
    result = await node_service.restore_node(db, ctx, node_id)
    return RestoreResponse(
        node=PlanNodePayload.model_validate(plan_service.node_to_dict(result.node)),
        revision_version=result.revision_version,
        restored_count=result.restored_count,
        restored_sessions=result.restored_sessions,
        restored_minutes=result.restored_minutes,
        overdue_sessions=result.overdue_sessions,
        overbooked_days=[
            OverbookedDayPayload(
                day=day.day,
                planned_minutes=day.planned_minutes,
                daily_cap=day.daily_cap,
                over_by=day.over_by,
            )
            for day in result.overbooked_days
        ],
        relations_visible=result.relations_visible,
    )


@router.get(
    "/{workspace_id}/archive",
    response_model=list[ArchivedNodePayload],
    summary="归档列表",
)
async def list_archive(
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: AsyncSession = Depends(get_db),
) -> list[ArchivedNodePayload]:
    """这个空间里归档过什么。新的在前。

    只列**每一次归档的根**:同一批被带走的子孙不单独出现,点那一行的"恢复"会把它们
    一起带回来。父节点在另一次归档里的那些仍然列出来,但标成 `restorable=false`
    并给出原因 —— 它们确实还在归档里,藏起来会让用户以为它被删了。
    """
    entries = await node_service.list_archive(db, ctx)
    return [
        ArchivedNodePayload(
            node=PlanNodePayload.model_validate(plan_service.node_to_dict(entry.node)),
            archived_at=entry.archived_at,
            descendants=entry.descendants,
            sessions=entry.sessions,
            restorable=entry.restorable,
            blocked_reason=entry.blocked_reason,
        )
        for entry in entries
    ]


@router.post(
    "/{workspace_id}/dependencies",
    response_model=DependencyPayload,
    status_code=201,
    summary="加依赖",
)
async def create_dependency(
    payload: CreateDependencyRequest,
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: AsyncSession = Depends(get_db),
) -> DependencyPayload:
    """加一条「前者完成后才能开始后者」。

    **幂等**:这条边本来就在的话返回它,不报 409 —— 用户点两下不该看到错误。
    会成环的话 409 `DEPENDENCY_CYCLE`,并且不写任何东西。
    """
    dependency = await node_service.add_dependency(
        db, ctx,
        predecessor_id=payload.predecessor_id,
        successor_id=payload.successor_id,
    )
    return DependencyPayload.model_validate(plan_service.dependency_to_dict(dependency))


@router.delete("/{workspace_id}/dependencies", status_code=204, summary="去掉依赖")
async def delete_dependency(
    predecessor_id: uuid.UUID,
    successor_id: uuid.UUID,
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """去掉一条依赖。**两条边都要给** —— 边的身份是这一对节点,不是单独一条。

    不存在的依赖返回 204 而不是 404:目标是"这条边没了",而它已经没了。
    """
    await node_service.remove_dependency(
        db, ctx, predecessor_id=predecessor_id, successor_id=successor_id
    )
    return Response(status_code=204)


# ---------------------------------------------------------------------------------
# 画布上的关系
#
# 三种类型(`depends_on` / `related_to` / `influences`)共用这一组路径和同一个响应
# 形状,因为对用户来说它们都是"一条边"。存在两张表里是存储层的选择,不该漏到接口上 ——
# 前端要是得先知道"这条边在哪个表",那它就得跟着这张表一起改。
# ---------------------------------------------------------------------------------
@router.post(
    "/{workspace_id}/relations",
    response_model=RelationPayload,
    status_code=201,
    summary="连一条关系",
)
async def create_relation(
    payload: CreateRelationRequest,
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: AsyncSession = Depends(get_db),
) -> RelationPayload:
    """连一条边。**默认由前端决定类型,服务端不猜。**

    `depends_on` 走的是排期那条路(有环检测),会成环就 409 `DEPENDENCY_CYCLE`;
    `related_to` / `influences` 允许成环。同一条边连两次是幂等的,不报错。
    """
    row = await node_service.add_relation(
        db,
        ctx,
        source_id=payload.source_id,
        target_id=payload.target_id,
        relation_type=payload.relation_type,
        note=payload.note,
    )
    return RelationPayload.model_validate(plan_service.relation_of(row))


@router.patch(
    "/{workspace_id}/relations/{relation_id}",
    response_model=RelationPayload,
    summary="改一条关系",
)
async def update_relation(
    relation_id: uuid.UUID,
    payload: UpdateRelationRequest,
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: AsyncSession = Depends(get_db),
) -> RelationPayload:
    """改类型或说明。`related_to` <-> `influences` 之间可以换。

    换成/换出 `depends_on` 返回 400 `RELATION_TYPE_CHANGE_UNSUPPORTED` —— 那会跨表,
    而 `dependencies` 没有说明列,搬过去用户写的解释就没了。理由写在
    `services/node_service.py::update_relation`。
    """
    relation = await node_service.update_relation(
        db, ctx, relation_id, payload.model_dump(exclude_unset=True)
    )
    return RelationPayload.model_validate(plan_service.relation_to_dict(relation))


@router.delete(
    "/{workspace_id}/relations/{relation_id}", status_code=204, summary="去掉一条关系"
)
async def delete_relation(
    relation_id: uuid.UUID,
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """去掉一条边。**不删节点** —— 边没了,两端的节点都还在。

    不存在的关系返回 404(而不是 204):这个接口收的是 id,不是"一对节点",
    所以"它已经没了"和"它从来没在过"是同一件事 —— 这里没有"目标已经达成"的语义。
    """
    await node_service.remove_relation(db, ctx, relation_id)
    return Response(status_code=204)


# ---------------------------------------------------------------------------------
# 布局
#
# 与上面两组写入的**根本区别**:它不写 `plan_revisions`、不发 `domain_events`。
# 位置和视口是用户偏好,不是计划的一部分 —— 理由写在 `services/layout_service.py`
# 的开头。谁要是往这里加一个版本记录,那个文件的第一段就是给他看的。
# ---------------------------------------------------------------------------------
@router.get("/{workspace_id}/layout", response_model=LayoutPayload, summary="取布局")
async def read_layout(
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: AsyncSession = Depends(get_db),
) -> LayoutPayload:
    """这个用户在这个空间里的全部位置与视口。**按用户取** —— 别人怎么摆不影响你。"""
    return await layout_service.load_layout(db, ctx)


@router.put("/{workspace_id}/layout", response_model=LayoutPayload, summary="存布局")
async def write_layout(
    payload: PutLayoutRequest,
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: AsyncSession = Depends(get_db),
) -> LayoutPayload:
    """整份提交布局。幂等,而且**不删除**没提交的行(见 `PutLayoutRequest` 的注释)。

    返回的是提交之后的完整布局,不是"成功"两个字 —— 客户端因此不必猜"到底存进去
    的是什么",尤其是在它同时提交了十几个节点的时候。
    """
    return await layout_service.put_layout(db, ctx, payload)


def _edit_response(result: node_service.EditResult) -> NodeEditResponse:
    return NodeEditResponse(
        node=PlanNodePayload.model_validate(plan_service.node_to_dict(result.node)),
        revision_version=result.revision_version,
        deleted_count=result.deleted_count,
        removed_dependencies=result.removed_dependencies,
        # 漏掉这一个字段的后果是**默认值静默生效**:`restorable` 的默认是 `True`,
        # 于是 `?mode=delete`(彻底删除)也会在响应里报"可以恢复",而客户端据此
        # 显示的是一句错误的承诺。它由 `test_archive_restore.py` 钉住。
        restorable=result.restorable,
    )


@router.get(
    "/{workspace_id}/proposals", response_model=list[ProposalView], summary="提案列表"
)
async def list_proposals(
    limit: int = Query(default=50, ge=1, le=200),
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: AsyncSession = Depends(get_db),
) -> list[ProposalView]:
    """这个空间的提案,新的在前。

    **不过滤状态。** 界面上"待确认"的那一份需要单独高亮,但"这份提案后来怎么样了"
    同样要能查 —— 只返回待确认的话,用户点完确认再刷新,那份提案就凭空消失了,
    他会怀疑刚才到底发生了什么。
    """
    proposals = await proposal_service.list_proposals(db, ctx, limit=limit)
    return [await proposal_service.view_of(db, proposal) for proposal in proposals]


@router.get(
    "/{workspace_id}/analyses",
    response_model=AnalysisListResponse,
    summary="AI 的分析记录",
)
async def list_analyses(
    focus_node_id: uuid.UUID | None = Query(default=None, alias="focusNodeId"),
    scope_root_id: uuid.UUID | None = Query(default=None, alias="scopeRootId"),
    limit: int = Query(default=analysis_service.DEFAULT_LIMIT, ge=1, le=MAX_ANALYSES),
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: AsyncSession = Depends(get_db),
) -> AnalysisListResponse:
    """AI 对某个节点(或某个范围)做过的分析,新的在前。

    **新鲜度是每次读现算的**(见 `analysis_service`):回去比一遍当时那份输入与现在
    库里的样子。所以同一个 id 隔一会儿再来读,`freshness` 可能已经变了 ——
    那是正确的,它描述的是"现在还成不成立",不是"它出生时成不成立"。

    `focusNodeId` 不给就是整个空间的分析。**不做"这个节点及其后代"的展开**:
    那会把一条关于祖辈的分析混进子节点的列表,而用户点开一个节点时想看的是
    关于**这个**节点的判断。

    **过期只标不改。** 这个接口不改任何东西:不重新生成分析、不动用户正文、
    不碰计划。要拿到基于最新内容的判断,得显式发起一次重新分析。
    """
    analyses = await analysis_service.list_for(
        db, ctx, focus_node_id=focus_node_id, scope_root_id=scope_root_id, limit=limit
    )
    views = [await analysis_service.view_of(db, ctx, analysis) for analysis in analyses]

    if not views:
        note = (
            "这个节点还没有被分析过。跟 AI 聊一次,它对你写的内容做出的判断会记在这里。"
            if focus_node_id is not None
            else "这个空间里还没有分析记录。"
        )
    else:
        stale = sum(1 for view in views if view.freshness is AnalysisFreshness.STALE)
        note = (
            f"共 {len(views)} 条,其中 {stale} 条基于已经变过的内容(标着原因的可以重新分析)。"
            if stale
            else f"共 {len(views)} 条,都是基于当前内容做出的。"
        )

    return AnalysisListResponse(
        analyses=views,
        focus_node_id=focus_node_id,
        scope_root_id=scope_root_id,
        note=note,
    )


@router.post(
    "/{workspace_id}/proposals/{proposal_id}/confirm",
    response_model=ConfirmProposalResponse,
    summary="确认提案",
)
async def confirm_proposal(
    proposal_id: uuid.UUID,
    payload: ConfirmProposalRequest,
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: AsyncSession = Depends(get_db),
) -> ConfirmProposalResponse:
    """把提案写进计划。

    **这个接口承担了产品里最重的一次写入**,所以它有三道保护,缺一不可:

    1. `idempotency_key` 必填 —— 用户双击"确认"只会写入一次,第二次返回的是
       上次那个响应(`replayed=true`),而不是一个"已经处理过了"的错误。
    2. 计划在提案生成后被改过 -> 409 `STALE_BASE_REVISION`。**不覆盖用户的改动**,
       让用户重新生成。
    3. 写入是一个事务。任何一步失败,整批回滚 —— 不存在"建了一半的阶段"。
    """
    outcome = await proposal_service.confirm_proposal(
        db, ctx, proposal_id, idempotency_key=payload.idempotency_key
    )
    return outcome.response


@router.post(
    "/{workspace_id}/proposals/{proposal_id}/reject",
    response_model=ProposalView,
    summary="拒绝提案",
)
async def reject_proposal(
    proposal_id: uuid.UUID,
    payload: RejectProposalRequest,
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: AsyncSession = Depends(get_db),
) -> ProposalView:
    """不采纳这份提案。**不产生任何计划写入** —— 只是把它的状态改成 rejected。

    拒绝后计划仍停在原来的版本上,`current_revision_version` 不动。这一点是有意的:
    "用户看过、决定不要"不该在版本历史上留下一个空版本,否则用户回头数"改过几次计划"
    会数出一次什么都没变的改变。
    """
    proposal = await proposal_service.reject_proposal(db, ctx, proposal_id, reason=payload.reason)
    return await proposal_service.view_of(db, proposal)
