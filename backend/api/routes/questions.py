"""问题节点:读取、回答、跳过、稍后。

路径都挂在 `{workspace_id}` 下面,所以每一个都先过 `get_workspace_context` ——
归属校验写在 SQL 的 WHERE 里,拿不到 `WorkspaceContext` 就调不到服务层。

## 回答接口为什么要一个模型依赖

回答问题**不是**只改一行状态:后端把它当成一轮普通对话跑(合成一条用户消息、调模型、
把模型提的变更按老规矩落成待确认提案)。所以它需要 reasoner,返回体里也带着那一轮的
完整结果(`turn`)。**但回答本身永远不直接改计划** —— 计划变更仍然要用户点确认。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agent.runtime import Reasoner
from backend.api.dependencies.agent import get_reasoner
from backend.api.dependencies.workspace import get_workspace_context
from backend.api.routes.workspaces import turn_response
from backend.contracts.question import (
    AnswerQuestionRequest,
    AnswerQuestionResponse,
    QuestionActionRequest,
    QuestionActionResponse,
    QuestionListResponse,
)
from backend.db.session import get_db
from backend.services import question_service
from backend.services.context import WorkspaceContext

router = APIRouter()


@router.get(
    "/{workspace_id}/questions",
    response_model=QuestionListResponse,
    summary="取这个空间的问题",
)
async def list_questions(
    include_decided: bool = Query(default=False, alias="includeDecided"),
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: AsyncSession = Depends(get_db),
) -> QuestionListResponse:
    """默认只返回还没结束的问题(pending / answered / investigating)。

    `includeDecided=true` 时把 resolved / archived 也带上 —— 审计与历史用。
    """
    rows = await question_service.list_questions(db, ctx, include_decided=include_decided)
    return QuestionListResponse(questions=[question_service.to_view(row) for row in rows])


@router.post(
    "/{workspace_id}/questions/{question_id}/answer",
    response_model=AnswerQuestionResponse,
    summary="回答一个问题",
)
async def answer_question(
    question_id: uuid.UUID,
    payload: AnswerQuestionRequest,
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: AsyncSession = Depends(get_db),
    reasoner: Reasoner = Depends(get_reasoner),
) -> AnswerQuestionResponse:
    """存答案 -> 标记处理中 -> 跑一轮后续对话。

    **答案先落库再调模型**:模型失败时答案与 `investigating` 状态都还在,刷新后读得回。
    重复提交同一个 `clientAnswerId` 不会重跑后续处理(幂等),返回体里 `replayed=true`。
    """
    outcome = await question_service.answer_question(
        db,
        ctx,
        reasoner,
        question_id,
        selected_option_ids=payload.selected_option_ids,
        custom_input=payload.custom_input,
        client_answer_id=payload.client_answer_id,
    )
    turn = (
        await turn_response(db, ctx, outcome.turn) if outcome.turn is not None else None
    )
    return AnswerQuestionResponse(
        question=question_service.to_view(outcome.question),
        turn=turn,
        replayed=outcome.replayed,
    )


@router.post(
    "/{workspace_id}/questions/{question_id}/skip",
    response_model=QuestionActionResponse,
    summary="跳过一个问题",
)
async def skip_question(
    question_id: uuid.UUID,
    payload: QuestionActionRequest,
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: AsyncSession = Depends(get_db),
) -> QuestionActionResponse:
    """跳过 -> `archived`。**只改状态,不碰计划。**"""
    question, replayed = await question_service.skip_question(
        db, ctx, question_id, client_action_id=payload.client_action_id, reason=payload.reason
    )
    return QuestionActionResponse(question=question_service.to_view(question), replayed=replayed)


@router.post(
    "/{workspace_id}/questions/{question_id}/later",
    response_model=QuestionActionResponse,
    summary="稍后回答一个问题",
)
async def defer_question(
    question_id: uuid.UUID,
    payload: QuestionActionRequest,
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: AsyncSession = Depends(get_db),
) -> QuestionActionResponse:
    """稍后回答 -> **保持 pending**,只记一条用户动作。"""
    question, replayed = await question_service.defer_question(
        db, ctx, question_id, client_action_id=payload.client_action_id, reason=payload.reason
    )
    return QuestionActionResponse(question=question_service.to_view(question), replayed=replayed)
