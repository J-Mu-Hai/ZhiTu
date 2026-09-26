"""复盘:偏差事实与"按执行情况调整计划"。

## 偏差检测是确定性的,重规划提案不是

`DeviationView` 里每一条都来自数据库里的事实(没记录的场次、记录成跳过/失败的场次、
过了截止日还没完成的节点),算法完全确定,不需要模型。**这一层永远可用** ——
模型挂了、key 没了,用户照样能看到"这周你有三场没记录、两场记成了没做完"。

`POST /replan` 在这批事实之上再往前走一步:把它们交给模型,请它提一份调整方案。
那份方案走的是**和阶段 4 完全相同的路径** —— 校验、预览、确认事务一个都不少,
`triggerType` 是 `execution_deviation`,所以它在版本历史里也分得出来是"因为执行情况
而调整的"。

**模型不可用时这里的表现是"给出事实、不给方案",不是"编一份方案"。** 这与
`rule_fallback` 那条"禁止编造计划内容"是同一条纪律:一个凭空生成的调整方案会被用户
当成系统的判断,而它其实什么依据都没有。

## 没有偏差时不调模型

`consultedModel=false` + `deviations=[]` 是一个合法的、常见的回答。为了"一切正常"
去调一次模型,既花钱,又会得到一个为了有话可说而硬凑出来的调整建议。
"""

from __future__ import annotations

import uuid
from datetime import date

from pydantic import Field

from backend.contracts.common import ApiModel
from backend.contracts.proposal import ActionError, ProposalView


class DeviationView(ApiModel):
    """一条偏差事实。

    `isQuestion` 是这一层最重要的一个字段。同一批数据里有两类完全不同的东西:

    - `true`  —— 我们**不知道**发生了什么(那场没有记录)。它可以被问,不能被算作
      偏差去驱动调整。把它当成"没完成",用户从第一天起就会被系统按一个他从没确认过
      的事实去重排计划。
    - `false` —— 用户**说了**发生了什么(记录成跳过/失败/部分完成),或者一件事
      客观上过期了(截止日已过而节点未完成)。这些可以驱动调整。
    """

    code: str
    workspace_id: uuid.UUID | None = None
    node_id: uuid.UUID | None = None
    node_title: str = ""
    session_id: uuid.UUID | None = None
    #: 服务端拼好的一句话,例如「周三那场 60 分钟的「变量与类型」还没有记录」。
    detail: str = ""
    #: 这是一句提问(`true`)还是一句结论(`false`)。见类文档。
    is_question: bool = False
    days_ago: int | None = None
    #: 结构化的事实,给界面按需展示,不必去解析上面那句话。
    facts: dict = Field(default_factory=dict)


class DeviationsResponse(ApiModel):
    """只是偏差事实 —— 不请模型,不花钱,永远可用。

    单独一个接口而不是"POST /replan 里顺便返回",是因为这两件事的可靠性完全不同:
    这一份是纯查询,模型挂了、key 没了、额度用光了,它照样正确。界面要先能显示它,
    再考虑要不要问模型。
    """

    deviations: list[DeviationView] = Field(default_factory=list)
    analyzed_for: date | None = None
    #: 其中有多少条是**提问**(没有记录的场次),而不是结论。界面上这两类的措辞
    #: 必须不同:一句"你还没做吗"和一句"你报告了没做"不是一回事。
    question_count: int = 0
    note: str = ""


class ReplanResponse(ApiModel):
    """一次"按执行情况调整"的结果。

    三样东西刻意分开返回:用户**总能**拿到偏差事实;模型**可能**给出提案;模型不可用时
    事实照样在,只是 `proposal` 为空而 `message` 说明原因。
    """

    deviations: list[DeviationView] = Field(default_factory=list)
    #: 这一轮有没有真的请模型看过。没有偏差时是 false。
    consulted_model: bool = False
    proposal: ProposalView | None = None
    #: 模型提了变更但没通过校验时,逐条的问题。与 `proposal is None` 一起读 ——
    #: 前者是"提了但不行",后者是"没提"。这两件事对用户意味着不同的东西。
    proposal_errors: list[ActionError] = Field(default_factory=list)

    source: str | None = None
    degraded: bool = False
    degraded_reason: str | None = None
    retryable: bool = False

    #: 服务端拼好的一句话。**降级时它必须如实说明"这次没能给出调整方案"** ——
    #: 悄悄返回空提案,用户会以为系统看过之后认为不需要调整。
    message: str = ""
    #: 这份提案基于哪一天的分析。日期是"哪一天"这件事在复盘里必须明确:
    #: 同一批场次,昨天看和今天看,"过去几天"是不同的。
    analyzed_for: date | None = None


__all__ = ["DeviationView", "DeviationsResponse", "ReplanResponse"]
