"""成长空间。

**没有 DELETE。** 空间级联删除会连整棵计划树、全部历史版本与执行记录一起抹掉,而且
`plan_nodes.parent_id` 是 RESTRICT,数据库本身也会在删到一半时报错。归档
(`PATCH {"status": "archived"}`)是可逆的、用户能理解的那条路。真要删除,必须先有导出
和二次确认,那是独立能力,不是顺手加的一个路由。

每个 `{workspace_id}` 路径都必须经过 `get_workspace_context` —— 它在 SQL 的 WHERE 里
就把归属写死了,拿不到 `WorkspaceContext` 就没法调到服务层(见 services/context.py)。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agent.runtime import Reasoner
from backend.api.dependencies.agent import get_reasoner
from backend.api.dependencies.auth import AuthContext, get_auth_context
from backend.api.dependencies.workspace import get_workspace_context
from backend.contracts.conversation import (
    BriefView,
    ConversationView,
    MessageView,
    SendMessageRequest,
    SendMessageResponse,
)
from backend.contracts.workspace import (
    CreateWorkspaceRequest,
    RootNode,
    UpdateWorkspaceRequest,
    WorkspaceCreated,
    WorkspaceDetail,
    WorkspaceSummary,
)
from backend.db.models import Message, PlanningBrief
from backend.db.session import get_db
from backend.services import (
    analysis_service,
    brief_service,
    conversation_service,
    proposal_service,
    workspace_service,
)
from backend.services.context import WorkspaceContext

router = APIRouter()


@router.post(
    "",
    response_model=WorkspaceCreated,
    status_code=status.HTTP_201_CREATED,
    summary="新建成长空间",
)
async def create_workspace(
    payload: CreateWorkspaceRequest,
    ctx: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
) -> WorkspaceCreated:
    """建一个**空**空间:一条空间记录 + 一个根目标节点,别无其他。

    返回 `RootNode` 是为了让前端立刻拿到根节点的 id 去关联后续对话;
    返回 `counts` 是为了让"这个空间里到底有什么"在响应里就能核对 —— 现在应该是
    `{"nodes": 1, "conversations": 0, "proposals": 0, "scheduledSessions": 0}`。
    """
    workspace, root = await workspace_service.create_workspace(
        db,
        user=ctx.user,
        title=payload.title,
        intent=payload.intent,
        goal=payload.goal,
    )
    return WorkspaceCreated(
        workspace=await workspace_service.build_detail(db, workspace),
        root_node=RootNode(
            id=root.id,
            title=root.title,
            node_type=root.node_type,
            status=root.status,
            depth=root.depth,
            deadline=root.deadline,
        ),
    )


@router.get("", response_model=list[WorkspaceSummary], summary="空间列表")
async def list_workspaces(
    include_archived: bool = Query(default=False, alias="includeArchived"),
    ctx: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
) -> list[WorkspaceSummary]:
    """默认只列活动空间。查询参数用 camelCase 别名,与响应体保持一致。"""
    workspaces = await workspace_service.list_workspaces(
        db, ctx.user, include_archived=include_archived
    )
    return [workspace_service.build_summary(workspace) for workspace in workspaces]


@router.get("/{workspace_id}", response_model=WorkspaceDetail, summary="空间详情")
async def read_workspace(
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: AsyncSession = Depends(get_db),
) -> WorkspaceDetail:
    return await workspace_service.build_detail(db, ctx.workspace)


@router.patch("/{workspace_id}", response_model=WorkspaceDetail, summary="改标题 / 意向 / 归档")
async def update_workspace(
    payload: UpdateWorkspaceRequest,
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: AsyncSession = Depends(get_db),
) -> WorkspaceDetail:
    """只改传了的字段。归档与取消归档都走这里(改 `status`)。"""
    changes = {
        field: getattr(payload, field) for field in payload.model_fields_set
    }
    workspace = await workspace_service.update_workspace(db, ctx, **changes)
    return await workspace_service.build_detail(db, workspace)


# ---------------------------------------------------------------------------------
# 对话
# ---------------------------------------------------------------------------------
def _message_view(message: Message) -> MessageView:
    return MessageView(
        id=message.id,
        role=message.role.value,
        content=message.content,
        seq=message.seq,
        created_at=message.created_at,
        context_node_id=message.context_node_id,
        proposal_id=message.proposal_id,
        model_source=message.model_source.value if message.model_source else None,
        degraded=message.degraded,
        degraded_reason=message.degraded_reason.value if message.degraded_reason else None,
    )


def _brief_view(brief: PlanningBrief | None) -> BriefView:
    return BriefView.model_validate(brief_service.known_summary(brief))


@router.get("/{workspace_id}/messages", response_model=ConversationView, summary="取对话")
async def read_messages(
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: AsyncSession = Depends(get_db),
) -> ConversationView:
    """这个空间的完整对话。

    **新空间返回的是一个空列表,不是 404。** "还没有聊过"和"这个空间不存在"是两回事,
    前端在空状态下要显示的是引导语而不是错误页。

    这是个**只读**接口,不会顺手建一条空对话 —— 否则用户打开一次工作台,库里就多出
    一行他从没说过话的对话。
    """
    conversation = await conversation_service.find_primary_conversation(db, ctx)
    page = await conversation_service.list_messages(db, ctx)
    brief = await brief_service.load_brief(db, ctx.id)
    return ConversationView(
        id=conversation.id if conversation else None,
        messages=[_message_view(m) for m in page.messages],
        brief=_brief_view(brief),
        truncated=page.truncated,
    )


@router.post("/{workspace_id}/messages", response_model=SendMessageResponse, summary="发一条消息")
async def send_message(
    payload: SendMessageRequest,
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: AsyncSession = Depends(get_db),
    reasoner: Reasoner = Depends(get_reasoner),
) -> SendMessageResponse:
    """把用户这一轮交给模型,返回模型的回复。

    **状态码恒为 200,即使是新消息。** 不用 201 是为了让客户端不为"重试"写第二套
    分支:`replayed` 已经如实写在响应体里,而 200/201 的区别对调用方没有任何额外信息,
    却会让一部分 HTTP 客户端把 201 当成"资源被创建"而触发一次多余的列表刷新。

    **模型失败不是 5xx。** 用户的那句话已经落库了(见 conversation_service 的两次提交),
    接口返回的是一个 `degraded=true` 的正常响应 —— 用户看到的是"这次没生成成功,可以重试",
    而不是一个错误页。真正的 5xx 只留给"我们这边出问题了"。
    """
    outcome = await conversation_service.submit_turn(
        db,
        ctx,
        reasoner,
        content=payload.content,
        client_message_id=payload.client_message_id,
        context_node_id=payload.context_node_id,
        current_view=payload.current_view,
        scope_root_id=payload.scope_root_id,
    )
    return await _turn_response(db, ctx, outcome)


@router.post(
    "/{workspace_id}/nodes/{node_id}/analysis/refresh",
    response_model=SendMessageResponse,
    summary="让 AI 根据最新内容重新分析这个节点",
)
async def refresh_analysis(
    node_id: uuid.UUID,
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: AsyncSession = Depends(get_db),
    reasoner: Reasoner = Depends(get_reasoner),
) -> SendMessageResponse:
    """「根据最新内容重新分析」按钮。**它走的是同一条对话工作流,不是另一条。**

    内部就是 `submit_turn`,只不过那句"根据最新内容重新分析一下这个节点"由服务端替
    用户说了。理由是这个按钮**必须**在对话里留下痕迹:否则用户点完之后,对话停在他
    上一次说话的地方,而画布上多了一条分析 —— 两处记录对不上,下次他自己也说不清
    那条分析是哪来的。所以这一次调用**真的会追加一条用户消息和一条助手消息**,
    和手打那句话完全一样。

    **那句话写在服务层而不是前端**(见 `analysis_service.REANALYZE_MESSAGE`):
    按钮与对话不能有两个真相,换个客户端说的也得是同一句。

    路径挂在节点下面是因为它分析的是**这个节点**(`context_node_id`,它决定这条分析
    记录的 `focusNodeId`);但这一轮的范围仍然是整个空间 —— 重新分析要的是"把最新的
    前因后果都看一遍",把范围缩到某个子空间只会让它少读一半。

    **失败与降级的表现与发消息完全相同**:模型不可用时返回 `degraded=true` 的正常
    响应,那一轮不会留下分析记录。界面上要如实显示成"这次没分析成",不能拿上一次的
    旧分析顶替 —— 那正是这一批要拦的那种假象。

    **`contextNodeId` 的归属校验在 `submit_turn` 里,两条路径共用同一处。**
    曾经这条路径单独先查一次,理由是"发消息那条里节点只是提示,节点没了也该把话答完"。
    那个区别站不住:同一个非法 id 在一条路上 404、在另一条路上被**悄悄忽略**,
    等于同一份输入有两套真相,而"忽略"那一套没有任何迹象。现在两条路都拒绝,
    校验因此在服务层做一次就够 —— 校验和执行挨在一起,谁调用都绕不过去。
    查不到与不属于当前空间返回同一个错误(理由见 `NodeNotFound` 的注释)。
    """
    outcome = await conversation_service.submit_turn(
        db,
        ctx,
        reasoner,
        content=analysis_service.REANALYZE_MESSAGE,
        context_node_id=node_id,
    )
    return await _turn_response(db, ctx, outcome)


async def _turn_response(
    db: AsyncSession, ctx: WorkspaceContext, outcome: conversation_service.TurnOutcome
) -> SendMessageResponse:
    """把一次回合的结果拼成响应体。**发消息与重新分析共用这一个。**

    两条路径的响应形状必须逐字段一致,不然前端就得为"按钮那次"写第二套读取逻辑,
    而两套逻辑的差异会体现在最不起眼的地方:`inputChanged` 少读一次,用户在按钮那条
    路径上永远看不到"这次是基于旧输入"的提示。
    """
    # 简报在这里重新取一次,而不是用 outcome.brief:重复提交时服务层不会去解析条件,
    # 那条路径上 outcome.brief 是 None,直接用它会让重试的响应里简报突然消失。
    brief = await brief_service.load_brief(db, ctx.id)

    return SendMessageResponse(
        user_message=_message_view(outcome.user_message),
        assistant_message=_message_view(outcome.assistant_message),
        reply=outcome.result.reply,
        source=outcome.result.source.value,
        degraded=outcome.result.degraded,
        degraded_reason=(
            outcome.result.degraded_reason.value if outcome.result.degraded_reason else None
        ),
        retryable=outcome.result.retryable,
        prompt_version=outcome.result.prompt_version,
        model_name=outcome.result.model_name,
        latency_ms=outcome.result.latency_ms,
        brief=_brief_view(brief),
        changed_fields=list(outcome.changed_fields),
        # 提案在这里转成视图而不是直接把 ORM 对象交给 FastAPI 序列化:两者字段名
        # 几乎一样,但那点差别(下划线 vs 驼峰、要带 item 明细)恰好是最容易
        # 悄悄错掉的地方,而错了以后前端拿到的是 undefined 而不是报错。
        proposal=(
            await proposal_service.view_of(db, outcome.proposal)
            if outcome.proposal is not None
            else None
        ),
        proposal_errors=list(outcome.proposal_errors),
        input_changed=outcome.input_changed,
        replayed=outcome.replayed,
    )
