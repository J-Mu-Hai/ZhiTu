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
    request_id: str = ""
    prompt_version: str = ""
    model_name: str | None = None
    latency_ms: int | None = None
    usage: dict | None = field(default=None)


@runtime_checkable
class Reasoner(Protocol):
    """调用模型的那件事。实现必须遵守上面那条"不抛异常"的契约。"""

    async def reason(self, turn: TurnContext) -> ReasoningResult: ...
