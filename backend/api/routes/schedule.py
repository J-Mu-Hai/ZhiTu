"""排期:预览与应用。

## 路径挂在空间下面,但**效果**不止一个空间

路径是 `/api/workspaces/{id}/schedule/*`,因为"我要排期"这句话是从某个空间里说出来的,
而归属于空间的那一层校验(`get_workspace_context`)必须走一遍 —— 用户只能从自己的空间发起。

但**真正被排的和被写的,是这个账号的全部活动空间**。理由见 `services/schedule_service.py`:
时间池按人算,两个空间争的是同一个晚上。只排当前空间的话,另一个空间的安排会被当成不存在,
两个空间各自排满同一周,而界面上两边看上去都很正常。

这件事必须让用户看见,所以响应里带着 `scopeWorkspaceIds` 与 `applied.workspaces` ——
`applied.workspaces` 大于 1 时,界面的措辞是"这次调整同时更新了 N 个空间",不是"已保存"。

## 为什么预览是 POST 而不是 GET

它不产生任何写入,但它是一次**计算**:要读十几张表、跑一遍排期算法、再为每个缺口重跑三遍
来验证那三条出路。做成 GET 会让它落进浏览器与任何中间层的缓存里 —— 而它的结果依赖于
"此刻的今天"。缓存住一份昨天的排期给用户看,他会对着一个已经过去的日期点"应用"。

## 应用为什么还要再校验一次版本

预览和应用之间隔着一次界面渲染、一次点击和一次网络往返。`scheduleVersion` 是**输入**的
指纹,应用时服务端拿此刻的输入再算一遍,对不上就 409。用户点头的是他在屏幕上看到的那一份;
输入变了之后重排出来的可能是另一份(任务挪到了别的日子),而他没有看过它。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.dependencies.workspace import get_workspace_context
from backend.contracts.schedule import (
    ScheduleApplyRequest,
    ScheduleApplyResponse,
    SchedulePreviewResponse,
)
from backend.db.session import get_db
from backend.services import schedule_service
from backend.services.context import WorkspaceContext

router = APIRouter()


@router.post(
    "/{workspace_id}/schedule/preview",
    response_model=SchedulePreviewResponse,
    summary="算一份排期草案",
)
async def preview_schedule(
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: AsyncSession = Depends(get_db),
) -> SchedulePreviewResponse:
    """算一份排期,给用户看。**一行都不写。**

    没有请求体:排期的输入全部来自库里的事实 —— 用户的节点、依赖、既有场次、执行记录、
    时间预算与可用时段。让客户端传一份"计划"进来,会出现"界面上的版本"和"库里的版本"
    两个真值来源,而它们不一致时没有任何东西会报错。

    排不下的部分不会消失:`gaps` 里逐条给出是哪个约束卡的,`options` 里给出三条出路,
    每条都标着"它到底解不解决这个缺口"(`resolvesGap` 是**重跑一遍算出来的**,不是断言的)。
    """
    return await schedule_service.preview(db, ctx)


@router.post(
    "/{workspace_id}/schedule/apply",
    response_model=ScheduleApplyResponse,
    summary="应用这份排期",
)
async def apply_schedule(
    payload: ScheduleApplyRequest,
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: AsyncSession = Depends(get_db),
) -> ScheduleApplyResponse:
    """把预览过的那一份写进 `scheduled_sessions`。

    **幂等**:`idempotencyKey` 必填,双击"应用"只会写一次,第二次返回上次那个响应
    (`replayed=true`)。

    **不覆盖你看过的东西**:预览之后计划或时间预算变过 -> 409 `STALE_SCHEDULE_VERSION`,
    让你重新预览,而不是悄悄写下一份你没见过的安排。

    **一个事务**:任何一步失败整批回滚,不存在"排了一半"的日历。

    **已完成 / 已跳过 / 已取消 / 已锁定的场次不会被移动或取消。** 排期算法把它们当作
    既成事实,只把它们算进当天占用;写入路径上还有一道状态闸门兜底。
    """
    outcome = await schedule_service.apply(
        db,
        ctx,
        schedule_version=payload.schedule_version,
        idempotency_key=payload.idempotency_key,
    )
    return outcome.response
