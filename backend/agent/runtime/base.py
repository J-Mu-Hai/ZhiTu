"""Reasoner 接缝 —— 模型调用的**唯一**入口。

## 为什么先有接缝、后有实现

这个文件里没有一行代码会去连网。它定义的是"调用模型"这件事的形状:输入什么、
输出什么、失败了怎么办。具体是谁来回答(openJiuwen / 直连 DeepSeek / 规则),
由 `build_reasoner()` 在运行时决定。

先有接缝的实际收益,是"没有 API key 也能跑全部测试"从一句承诺变成了结构性事实:
测试把 `get_reasoner` 这个依赖覆盖掉即可,不需要 monkeypatch httpx,也不需要
"记得把 key 清空"。key 在不在,和被测试的那段逻辑没有关系。

## 一条硬契约:`reason()` 对上游失败永不抛异常

网络超时、401、返回的不是 JSON、JSON 里少了字段——这些全是**可预期的上游失败**,
一律返回 `degraded=True` 的结果。理由是调用方需要据此给用户一个明确的交代
("模型暂时不可用,点这里重试"),而异常在 FastAPI 里会变成 500 页面,
用户拿到的是"服务器错误",不是"这次没排上,重试一下"。

真正会抛的只有结构性 bug(比如传进来的 TurnContext 自相矛盾)。
`degraded` 这个字段因此永远有值,调用方永远不需要处理 `None`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from backend.db.models.enums import DegradedReason, ModelSource

if TYPE_CHECKING:  # pragma: no cover - 只为类型检查,运行时不导入
    # **只做类型引用,不在运行时导入。** 快照的存储形状住在 services 里(它同时管
    # 落库契约),而 `agent/` 这一层不该在运行时依赖 services —— 依赖方向是
    # services -> agent。写成字符串注解 + TYPE_CHECKING 之后,两边都成立。
    from backend.services.input_snapshot import InputSnapshot

#: 送进模型的上下文最多带多少轮历史。太长的历史既费钱又会让模型忽略最近几轮,
#: 而"最近几轮"才是用户此刻在说的事。
HISTORY_TURNS = 12

# ---------------------------------------------------------------------------------
# 节点在本次上下文里的位置。**一个节点只属于一层**,渲染时按层分段。
# ---------------------------------------------------------------------------------
#: 本轮讨论的对象(用户正看着它)。正文优先给全。
LAYER_FOCUS = "focus"
#: 焦点的祖先链 —— 目标与约束所在。**只读**的规则不在这里:祖先在范围内一样可写,
#: 可不可写看的是范围,不是辈分。
LAYER_ANCESTOR = "ancestor"
#: 焦点的直属子节点。渐进拆解要看的正是这一层。
LAYER_CHILD = "child"
#: 范围内、既不是焦点也不是它的直属子节点的那部分。
LAYER_SCOPE = "scope"
#: 范围之外。**默认只读** —— 模型可以看见它(看得见才知道整棵树长什么样),
#: 但对它提的任何变更都会被服务端拒绝。
LAYER_OUTSIDE = "outside"


@dataclass(frozen=True, slots=True)
class KnownConditions:
    """当前已经确认的规划条件。

    `weekly_available_minutes` 为 None 表示**用户还没说过**——不是"零"。
    这个区分是整条链路的重点:排期算法必须能分辨"每周投入 0 分钟"和"不知道投入多少"。
    """

    goal: str | None = None
    deadline: str | None = None  # ISO 日期字符串,不用 date 是为了让这里零依赖
    weekly_available_minutes: int | None = None
    current_level: str | None = None
    success_criteria: str | None = None
    constraints: tuple[str, ...] = ()

    @property
    def missing(self) -> tuple[str, ...]:
        """还缺哪些真正会改变排法的条件。规则化在这里,不在提示词里 ——
        提示词可以改措辞,但"缺什么"必须是可断言的。"""
        gaps: list[str] = []
        if self.deadline is None:
            gaps.append("deadline")
        if self.weekly_available_minutes is None:
            gaps.append("weekly_available_minutes")
        if self.current_level is None:
            gaps.append("current_level")
        return tuple(gaps)


@dataclass(frozen=True, slots=True)
class PlanNodeView:
    """给模型看的节点。**不含 UUID** —— 见下。

    `handle` 是这一轮里这个节点的本地记号(`n1`、`n2`…)。模型只看得到记号;它要修改
    某个已有节点时,也只能写记号,由服务端翻译回真实行。所以模型**在物理上无法指涉
    一个它没见过的 id** —— 这是提示注入的着力点被拿掉的地方,不是靠提示词请求它自觉。

    ## `body_read` 为什么必须与 `description` 分开

    `description` 为空有两种完全不同的原因:这个节点没有正文,或者**本次没读它的
    正文**(范围外、层级太远、超出预算)。合成一种的话,模型看到一片空白,会以为
    "这些节点都是空的",然后凭想象补内容 —— 而它其实什么都没看到。分开之后,
    渲染层能如实印出"本次没有读它的正文,需要就先问",而"没读到"不会变成"没有"。

    正文的截断不在这里做:`description` 是原样的事实,截到多少字由渲染层决定,
    因此"模型实际看到多少"只有一处答案(见 `agent/prompts/planning.py`)。
    """

    handle: str
    title: str
    node_type: str
    status: str
    depth: int
    deadline: str | None = None
    estimate_minutes: int | None = None
    #: `planning`(要排期)还是 `information`(只记事)。**与 `node_type` 正交** ——
    #: 模型需要它才能看懂"这个节点不该有工时、也不该被排进日历"。
    #: 让模型看见只是告知;拦它的是服务端(`proposal_validation` 与 `node_service`)。
    #:
    #: 排在这里是为遵守 dataclass 的字段顺序:带默认值的字段必须在所有不带默认值的
    #: 字段之后,否则 `@dataclass` 在**导入时**就抛 `TypeError` —— 症状是整个测试
    #: 收集阶段直接中断,而不是某一条用例变红。
    purpose: str = "planning"
    #: 父节点的记号。根目标为 None。用来让模型看清自己拿到的是一棵树,而不是一张表。
    parent_handle: str | None = None
    description: str | None = None
    acceptance_criteria: str | None = None
    #: 本次读了它的正文吗?**与"有没有正文"不是一回事**。
    body_read: bool = False
    #: 见本文件顶部那组 `LAYER_*`。
    layer: str = LAYER_SCOPE
    #: 是否在本次的作用范围之内。范围外的默认只读(服务端强制,不是靠提示词)。
    in_scope: bool = True

    @property
    def read_only(self) -> bool:
        return not self.in_scope


@dataclass(frozen=True, slots=True)
class RelationView:
    """给模型看的一条关系边。**两端也是记号**,不是 id —— 同 `PlanNodeView`。

    「前置」与「关联」分成两个字段表达,而不是合成一个 `label`:前者是排期算法的输入
    (后者不能早于前者完成),后者只是说明。混成一个,模型下一轮就会拿一条关联去推排期。
    """

    source: str  # 记号
    target: str  # 记号
    #: 'dep' = 前置(`dependencies` 表);'rel' = 用户自己画的关联/影响。
    kind: str
    #: `finish_to_start` / `related_to` / `influences`。
    relation_type: str
    note: str | None = None


# ---------------------------------------------------------------------------------
# 时间底盘。**这一组全是只读的事实,没有一条是"我会替你安排"**。
#
# 为什么单开一组而不是几个散字段:它们一起回答的是同一个问题 ——
# 「这个人还剩多少时间、已经被占掉多少」。模型拿它做的判断(排不排得开、要不要砍)
# 全都建立在这几个数字上,而它们全部来自排期器自己用的那几处(见 services/turn_context
# 的 `load_time_view`)。散着塞进 `KnownConditions` 的话,"哪些是用户说的条件、
# 哪些是系统算出来的事实"这条界线就没了。
# ---------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class AvailableWindowView:
    """一条周期性可用时段。`weekday` 0 = 周一,与 `date.weekday()` 一致。"""

    weekday: int
    start_minute: int
    end_minute: int


@dataclass(frozen=True, slots=True)
class SessionFactView:
    """已经排进日历的一场。**已经排进去的,不是"建议排的"。**"""

    handle: str
    day: str  # YYYY-MM-DD
    minutes: int
    status: str
    #: 用户锁定的场次不能被自动挪走。要说"我把这场往后挪了"之前必须先看这个。
    locked: bool = False


@dataclass(frozen=True, slots=True)
class ExecutionFactView:
    """做过的记录。`actual_minutes` 为空表示用户没报实际用时。"""

    handle: str
    result: str
    actual_minutes: int | None = None
    completion_ratio: str | None = None


@dataclass(frozen=True, slots=True)
class TimeView:
    """这个人的时间底盘。**只读**。

    ## 为什么这里必须有"不知道"

    `weekly_total_minutes` 为 None 表示**个人容量表里没有这一行** —— 而不是"每周零分钟"。
    这个区分是整条时间链路的重点,与 `KnownConditions.weekly_available_minutes` 为 None
    是同一个道理:分不清的话,模型会把"不知道"当成"没有限制",然后给出一个自己
    都没底的可行性判断。所以除了数字,这里还要回答**这些数字是从哪来的**
    (`capacity_configured`),渲染层才有话可说。

    ## `capacity_minutes` 是什么、不是什么

    它是**视界内最多能拿出的分钟数**(以今天到最远截止日为准,与排期预览同一个视界、
    同一个 `assess_feasibility` 口径)。它**不是**"这份计划排不排得开"的结论 ——
    结论要把各任务的 `estimateMinutes` 加起来才谈得上,而那是模型自己算的,服务端
    一个字都不替它说。两个数字混为一谈的后果,是模型把"总容量"读成"已经排好了"。
    """

    # --- 视界 ---
    horizon_days: int
    #: 最远的截止日。视界就是按它推出来的;为空表示所有节点都没写截止时间。
    horizon_last_day: str | None
    #: 视界已经到达上限(`MAX_HORIZON_DAYS`)。到了就**必须说出来** ——
    #: 否则"这期间最多能拿出多少"会被读成"总共能拿出多少"。
    horizon_at_limit: bool
    capacity_minutes: int

    # --- 预算 ---
    #: 个人容量表里的每周总量。None = 表里没有这一行(注册时刻意不建),**不是零**。
    weekly_total_minutes: int | None
    #: 真正生效的每周预算 = 总量 × 安全系数。权威算法是 `scheduler.calendar.weekly_budget`。
    weekly_budget_minutes: int
    safety_factor: str | None
    daily_cap_minutes: int
    min_session_minutes: int
    max_session_minutes: int
    default_buffer_minutes: int
    #: 个人容量表里到底有没有一行。False 时上面几个数字来自**默认值**或用户说过的那句,
    #: 必须如实说明"这不是你设的"。
    capacity_configured: bool

    # --- 什么时候有空 ---
    windows: tuple[AvailableWindowView, ...] = ()
    windows_total: int = 0
    #: 视界内的逐日例外(请假/临时有空)。列出来的有上限,总数如实给。
    exceptions: tuple[tuple[str, int | None, bool], ...] = ()
    exceptions_total: int = 0

    # --- 已经排进去的 ---
    sessions: tuple[SessionFactView, ...] = ()
    sessions_total: int = 0
    #: **别的空间**还排着多少场(仍然占着时间的那几种)。时间池是按人算的,
    #: 子空间不各自拥有一份额度 —— 不写出这个数,模型会以为每个子空间都能占满整周。
    sessions_other_workspaces: int = 0

    # --- 做过的 ---
    executions: tuple[ExecutionFactView, ...] = ()
    executions_total: int = 0

    # --- 计划这一侧要多少 ---
    #: 这个空间里**还没做完的任务**的预计工时合计。**只是一个加法**,不是结论 ——
    #: 它和 `capacity_minutes` 一起给,是为了让"够不够"这一步建立在两个可核对的数字上,
    #: 而不是让模型自己去加几十个节点的 `estimateMinutes`(它会加错,而且错得看不出来)。
    open_task_minutes: int = 0
    #: 其中**没填预计工时**的任务数。大于 0 时上面那个合计数只是**下限** —— 必须说出来,
    #: 否则模型会把"我看到的加起来"当成"总共要多少"。这正是规范里那条
    #: "不知道工时时不要声称日程已经合理安排"的落点。
    open_tasks_without_estimate: int = 0
    #: 这个人还有几个别的活动空间。大于 0 时上面那个合计数**不包含它们的任务**。
    other_active_workspaces: int = 0


@dataclass(frozen=True, slots=True)
class TurnContext:
    """一次对话轮次里,模型能看到的全部东西。

    ## 这里没有 UUID,是刻意的

    节点用标题指代,不用 id。模型看不到真实主键,也就不可能产出一个指向别的空间
    节点的 id —— 提示注入在这里失去着力点:被污染的模型只能在**它看得见的标题集合**
    里说话,而那个集合是服务端按当前空间查出来的。阶段 4 生成提案时同理:模型只见
    `n1..nK`,映射回真实行由服务端做。

    ## 为什么 current_date 是一个字符串而不是 datetime

    "今天是哪一天"必须由**用户所在时区**决定,绝不由 UTC 时间戳推导。UTC 的 09-25
    在东八区可能已经是 09-26,而这个日期会被用来算"还剩几周",错一天就会让周计划
    整体错位。所以调用方算好日期再传进来,这里只负责把它放进提示词。
    """

    current_date: str  # YYYY-MM-DD,已按用户时区算好
    weekday: str
    timezone: str
    workspace_title: str
    workspace_intent: str
    known: KnownConditions
    nodes: tuple[PlanNodeView, ...] = ()
    history: tuple[tuple[str, str], ...] = ()  # (role, content)
    user_message: str = ""
    #: 用户此刻在界面上看着哪个视图/哪个节点。用于"把这个阶段展开讲讲"这类指代消解。
    current_view: str | None = None
    context_node_title: str | None = None

    # ---------------------------------------------------------------------------
    # 作用范围。**三个概念必须分开,混成一个就会写坏东西**
    #
    #   scope(范围起点)  用户此刻在哪个空间里 —— 导航出来的,通常是某一层子空间
    #   focus(讨论对象)  用户点着哪个节点 —— 这一轮在聊的是它
    #   writable(可改集) 模型这一轮能改哪些节点 —— 目前等于"范围内的那些"
    #
    # 分开的价值在"越界请求能被拒绝"这句话上:模型可以**看见**范围外的东西
    # (不看见就不知道整棵树),但它对范围外提的任何变更都会被服务端拒掉,
    # 而不是靠提示词请它自觉。
    # ---------------------------------------------------------------------------
    #: 范围起点的记号与标题。None 表示这一轮没有更窄的范围可说(即整个空间)。
    scope_root_handle: str | None = None
    scope_root_title: str | None = None
    #: 讨论对象的记号。`context_node_title` 是它的标题。
    focus_handle: str | None = None
    #: 这一轮可以改的节点记号。空元组表示"没有范围限制"由调用方决定,见
    #: `proposal_service.build_from_actions` 的 `writable_handles`。
    writable_handles: tuple[str, ...] = ()
    #: 这个空间里现存的节点之间的关系。两端都是记号。
    edges: tuple[RelationView, ...] = ()
    #: 有多少条关系因为两端之一不在本次读到的节点里而没能列出来。**如实计数**,
    #: 不列出来又不说明的话,模型会把"我没看到"当成"没有关系"。
    edges_hidden: int = 0
    #: 这个空间里当时存活的节点总数,以及"分批读"有没有发生。
    #:
    #: 存在的理由只有一条:让"我这次没有读全"成为一句**能被说出口的事实**。
    #: 漏读又不说,模型就会拿半份上下文当全份用。
    live_node_count: int = 0
    window_truncated: bool = False

    #: 时间底盘(见 `TimeView`)。**只读** —— 这一轮里没有任何一个字段是模型能改的。
    #:
    #: 为 None 表示这一次没有读时间信息(手工构造 TurnContext 的那些调用方)。
    #: 渲染层据此印出"本次没有读时间信息",而不是印一份空表 —— 空表看起来像
    #: "这个人没有时间预算",那正是这一批要修的那种"把没读到当成没有"。
    time: TimeView | None = None

    #: 这次分析的输入快照(见 services/input_snapshot.py)。**服务端专用,不渲染。**
    input_snapshot: InputSnapshot | None = None

    #: 记号 -> 真实节点 id 的映射,**服务端专用,绝不渲染进提示词**。
    #:
    #: 模型这一轮产出的每一个 ref 都要靠它翻译回真实行;翻译不出来的一律按悬空引用
    #: 拒绝(见 services/proposal_validation.py 的 DANGLING_PROPOSAL_REF)。做成 tuple
    #: 而不是 dict,是为了和这个 dataclass 的"不可变"约定一致,也让它可哈希。
    node_handles: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class BriefClaim:
    """模型从这一轮对话里读出的一个条件值。

    `source` 是模型对自己这句话来源的判断。服务端**不信任这个标签,但执行它的后果**:
    `model_assumed` 的值永远不会写进 weekly_available_minutes / deadline 这些
    真正驱动排期的列,只会进 assumptions 审计表。模型就算撒谎,也骗不进列。
    """

    field: str
    value: object
    source: str  # 'user_stated' | 'model_assumed'

    @property
    def is_user_stated(self) -> bool:
        return self.source == "user_stated"


@dataclass(frozen=True, slots=True)
class AnalysisDraft:
    """模型对一块内容的判断。**和 `reply` 是两样东西。**

    `reply` 是给用户看的一段话;这里是**结构化的判断**,存进 AI 分析层,下一次看
    同一个节点时读得到。分成七栏不是分类癖 —— 它们的可靠程度完全不同:
    `known` 是"我读到了",`evidence` 是"用户给的、带来源",`assumptions` 是
    "我替用户假设的",`strategy_options` 是"可以怎么走"。混成一段自由文本之后,
    读的人分不出哪句该信 —— 而他会拿它当事实用。

    **每一项都是纯文本,不带 `source` 标签**:分栏本身已经表达了来源,再加一个标签
    会出现"标签说 user_stated、内容读起来像猜测"这种自相矛盾的行。
    """

    known: tuple[str, ...] = ()
    unknowns: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    diagnosis: tuple[str, ...] = ()
    strategy_options: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    confidence_note: str | None = None

    def is_empty(self) -> bool:
        """七栏全空、也没有可信度说明 —— 那这一轮其实什么都没判断。

        单独存在是因为"模型给了 `analysis` 键但里面是空的"必须被识别出来:照收的话,
        分析区会多出一条"什么也没说"的记录,而它会挤掉上一条真正有内容的分析。
        """
        return not any(
            (
                self.known,
                self.unknowns,
                self.evidence,
                self.assumptions,
                self.diagnosis,
                self.strategy_options,
                self.risks,
                self.confidence_note,
            )
        )


@dataclass(frozen=True, slots=True)
class ReasoningResult:
    """一次模型调用的结果。**永远是可用的**,即使内容为空。"""

    reply: str
    source: ModelSource
    degraded: bool = False
    degraded_reason: DegradedReason | None = None
    #: 这次失败重试一下有没有意义。超时/限流 -> True;没配 key -> False。
    #: 界面据此决定"重试"按钮是显示还是灰掉 —— 让用户点一个注定失败的按钮是折磨。
    retryable: bool = False
    brief_claims: tuple[BriefClaim, ...] = ()
    actions: tuple[dict, ...] = ()
    #: 模型这一轮形成的判断。None = 它这轮没给(纯聊天、纯提问)。
    analysis: AnalysisDraft | None = None
    request_id: str = ""
    prompt_version: str = ""
    model_name: str | None = None
    latency_ms: int | None = None
    usage: dict | None = field(default=None)


@runtime_checkable
class Reasoner(Protocol):
    """调用模型的那件事。实现必须遵守上面那条"不抛异常"的契约。"""

    async def reason(self, turn: TurnContext) -> ReasoningResult: ...
