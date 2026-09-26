"""复盘:偏差事实,以及按执行情况重规划。

## 两个接口,可靠性完全不同

```
GET  /deviations   纯查询,确定性             —— 模型不在也有东西看
POST /replan       请模型提一份调整方案       —— 模型不在时如实说"这次没给出方案"
```

分开是有意的。用户打开复盘,他至少该知道"这周你有三场没记录、两场记成了没做完、
一个阶段过了截止日" —— 这些是库里的行,和模型在不在没有关系。把这份事实塞进
`POST /replan` 的响应里,就意味着"看事实"这件事也要等一次模型调用、也要花一次钱、
也会在模型超时时一起失败。

## 重规划走的是和阶段 4 完全相同的那条路

`services/review_service.propose_replan` 最后调的是
`proposal_service.build_from_actions` —— 与对话里生成提案是同一条路径、同一套校验、
同一张 `proposals` 表、同一个确认事务。**这里没有第二条写入计划的路。**

所以模型提出的调整**不会自动生效**:它落成一条待确认的提案,用户在界面上点了确认
才会写进 `plan_nodes`。区别只有一个:`trigger_type` 记成 `execution_deviation`,
于是版本历史里看得见"这次调整是因为执行情况,不是用户自己想改"。

## 为什么没有偏差时不调模型

"你这周排的 5 场里 4 场都记了,计划跟得上你"是一个完全正常的结论。为了这个结论去调
一次模型,既花钱,又会换回一个为了有话可说而硬凑出来的调整建议。这时返回的是
`consultedModel=false` 与一份空的 `deviations`。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agent.runtime.base import Reasoner
from backend.api.dependencies.agent import get_reasoner
from backend.api.dependencies.workspace import get_workspace_context
from backend.contracts.review import DeviationsResponse, ReplanResponse
from backend.db.session import get_db
from backend.services import review_service
from backend.services.context import WorkspaceContext

router = APIRouter()


@router.get(
    "/{workspace_id}/deviations",
    response_model=DeviationsResponse,
    summary="这个空间里需要看一眼的情况",
)
async def list_deviations(
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: AsyncSession = Depends(get_db),
) -> DeviationsResponse:
    """**只读,不调模型,永远可用。**

    返回三类事实:

    - `UNRECORDED_PAST_SESSION` —— 已经过去、但没有任何记录的场次。`isQuestion=true`:
      **没有记录不等于没完成**,它是一句提问,不是一条结论。
    - `RECORDED_SETBACK`   —— 用户自己记过"没做 / 只做了一部分 / 做了没成"的场次。
    - `OVERDUE_NODE`       —— 截止日已过、节点仍未完成。

    前两类之间的区别是整条链路的重点:把"未记录"当成"未完成",用户从第一天起就会被
    系统按一个他从没确认过的事实去重排计划,而他纠正不了。
    """
    return await review_service.list_deviations(db, ctx)


@router.post(
    "/{workspace_id}/replan",
    response_model=ReplanResponse,
    summary="按执行情况调整计划",
)
async def replan(
    ctx: WorkspaceContext = Depends(get_workspace_context),
    reasoner: Reasoner = Depends(get_reasoner),
    db: AsyncSession = Depends(get_db),
) -> ReplanResponse:
    """把偏差事实交给模型,换回一份**待确认**的调整方案。

    没有请求体:偏差是查出来的,不是客户端传上来的。让界面传一份"哪里没做到"进来,
    会出现两个真值来源,而它们不一致时没有任何东西会报错。

    **响应里的 `proposal` 还没有生效。** 用户确认之后才会写进计划;确认走的是
    `POST /proposals/{id}/confirm`,与对话里生成的提案是同一个接口。

    模型不可用时返回的是 `degraded=true` + `retryable` + 一份空提案与一句如实的
    说明,**不是**一份规则生成的调整方案 —— 一个凭空生成的调整方案会被用户当成系统的
    判断,而它其实什么依据都没有。
    """
    return await review_service.propose_replan(db, ctx, reasoner)
