"""执行反馈与「今天」。

## 写失败必须响亮地失败

`POST /sessions/{id}/executions` 是一条**用户以为自己在保存事实**的路径。他说"今天
做了 50 分钟",然后关掉界面。如果这一步悄悄失败了(或者被静默降级成内存保存),
那条记录就不存在了 —— 而复盘、排期、偏差检测从此都在一个错误的假设上往下算,用户
没有任何办法发现。

所以这条路径上**没有内存回退**。数据库写不进去时 `services/errors.py` 的
`DbUnavailable` 会让响应带上 503 与 `saved: false`(见 api/errors.py 里那个
OperationalError 处理器),成功时响应里也带着 `saved: true` —— 客户端只需要读一个
布尔值,不必靠状态码去猜。

## 幂等靠 `idempotencyKey`,不靠"猜"

用户在网络不稳时重发,不该产生第二条记录。键由客户端生成一次,重试复用同一个;
重复提交返回上一次那条(`replayed=true`),行数不变。

键相同但请求体不同会被**拒绝**(`IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_BODY`):
客户端若把所有请求共用一个固定字符串,不拦的话用户会看到"另一场安排的结果"覆盖了
自己刚才那次反馈。

## 没有记录 ≠ 没完成

`GET /today` 里每一场都有 `recorded` 与 `result`。`result` 为空表示**没有记录**,
不表示"没做"。已经过去、又没有记录的场次进 `checkInQuestions`,界面据此**问**,
而不是把它算成没完成 —— 把"未记录"当"未完成"会让复盘从第一天起就在算一个用户
从没确认过的偏差。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.dependencies.auth import AuthContext, get_auth_context
from backend.api.dependencies.session import SessionContext, get_session_context
from backend.contracts.execution import (
    ExecutionHistoryResponse,
    RecordExecutionRequest,
    RecordExecutionResponse,
    TodayResponse,
)
from backend.db.session import get_db
from backend.services import execution_service

router = APIRouter()


@router.post(
    "/sessions/{session_id}/executions",
    response_model=RecordExecutionResponse,
    summary="记录一场的执行结果",
)
async def record_execution(
    payload: RecordExecutionRequest,
    ctx: SessionContext = Depends(get_session_context),
    db: AsyncSession = Depends(get_db),
) -> RecordExecutionResponse:
    """把"这场实际发生了什么"写下来。**只追加,不修改已有记录。**

    `result` 是闭集:`completed` / `partial` / `skipped` / `failed`。

    四种结果对计划的含义**不一样**,这是这张表存在的理由:

    - `completed`  -> 这场完成
    - `partial`    -> 还在进行,做了一部分
    - `skipped`    -> 这场没做(冻结,排期不会再动它)
    - `failed`     -> 做了但没成 —— 这件事仍然欠着,所以这场**不会**被标记成完成

    写完之后响应里带着这一场的最新状态(`session`)与这个节点的剩余场次
    (`nodeRemainingSessions`),界面不必自己再算一遍。
    """
    outcome = await execution_service.record_execution(
        db,
        ctx.ctx,
        ctx.session.id,
        result=payload.result,
        idempotency_key=payload.idempotency_key,
        started_at=payload.started_at,
        ended_at=payload.ended_at,
        actual_minutes=payload.actual_minutes,
        completion_ratio=payload.completion_ratio,
        delay_reason=payload.delay_reason,
        user_feedback=payload.user_feedback,
    )
    return outcome.response


@router.get(
    "/sessions/{session_id}/executions",
    response_model=ExecutionHistoryResponse,
    summary="一场安排的全部反馈",
)
async def list_executions(
    ctx: SessionContext = Depends(get_session_context),
    db: AsyncSession = Depends(get_db),
) -> ExecutionHistoryResponse:
    """这一场报过的每一次结果,新的在前。

    分两次完成时这里会有两条(20 分钟 + 30 分钟),而场次行上的 `actual_minutes`
    是 50 —— **合计是可导出的,反过来不成立**:只留最后一次的话,一去不返的是过程。
    """
    records = await execution_service.list_records(db, ctx.ctx, ctx.session.id)
    return ExecutionHistoryResponse(
        session=await execution_service.session_payload(db, ctx.session),
        records=[execution_service.to_view(record) for record in records],
    )


@router.get("/today", response_model=TodayResponse, summary="今天要做的事")
async def today(
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
) -> TodayResponse:
    """跨**全部活动空间**的今天。

    路径上没有 `workspace_id`,因为用户打开界面时问的是"我今天要做什么",不是"我这个
    空间今天要做什么"。逐空间看的话,两个空间各排了一场 60 分钟,而他只有两个小时 ——
    那个冲突在任何一个单独的空间里都看不出来。

    归档的空间不参与 —— 一个归档之后仍然每天早上出现在「今天」里的空间,等于没归档。
    """
    return await execution_service.load_today(db, auth.user, auth.user.timezone)
