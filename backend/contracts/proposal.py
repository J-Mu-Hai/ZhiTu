"""提案:模型动作的形状、校验错误的形状、给用户看的提案视图。

## 动作模型为什么住在契约层

它有两重身份,而且两重都需要同一份定义:

1. **模型输出的线格式。** 模型吐出来的 JSON 直接按这些模型校验。
2. **给用户看的预览。** 用户点确认之前要先看到"将要新增什么、删掉什么",那份预览
   就是这些模型 dump 出来的。

让它们共用一份定义,是为了不存在"校验用的规则"和"显示的规则"两套 —— 两套规则迟早
会不一致,而不一致的方向通常是把某个非法的东西显示成合法的。

## 为什么 op 用判别联合而不是一个大模型

`extra="forbid"` 配上判别联合,一个打错字的字段名(模型写 `deadLine` 而不是 `deadline`)
会当场变成 `PAYLOAD_SCHEMA_INVALID`,而不是被静默丢掉 —— 丢掉的话用户看到的是
"AI 说它排了截止时间,可计划里没有",而没有任何地方能解释为什么。

## `estimate_minutes` 的上界为什么是一个具体数字

不设上界的话,模型偶尔会把"每周 6 小时"错算成"每周可投入 6" 再乘来乘去,吐出一个
`999999`。这个数字会一路进到排期算法里,让整个可行性闸门报出一个荒谬的缺口。
`MAX_ESTIMATE_MINUTES` 是"单节点工时不可能超过这个数"的一句判断,写在契约里,
让它在最靠外的位置就被挡住。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import Field, TypeAdapter

from backend.contracts.common import ApiModel
from backend.db.models.enums import NodeStatus, NodeType, Priority, ProposalOp

#: 一次最多接受多少条变更。与 agent/runtime/direct_llm.py 的 MAX_ACTIONS 对齐 ——
#: 两处不一致的话,agent 层放行的条数会在这里被整批拒绝,而用户看到的是
#: "模型这次的回答没能解析出结果",一个和他实际遇到的问题毫无关系的文案。
MAX_ACTIONS = 80

#: 一次最多新建多少个节点。比 MAX_ACTIONS 小:一份提案里绝大多数条目应该是依赖与更新,
#: 一次冒出 60 个新节点的计划,人是不可能一次看完并判断的。
MAX_CREATED_NODES = 60

MAX_ESTIMATE_MINUTES = 100_000
MAX_TITLE_CHARS = 200
MAX_TEXT_CHARS = 2000
MAX_ACCEPTANCE_CHARS = 1000

#: 节点记号的形状。`n1`、`n12`;上界 5 位是给"句柄 + 新建"共用一个编号空间的余量。
#: 约束形状而不是任其自由发挥,是为了让 `n1 x`、`节点1` 这类写法变成一条明确的
#: 契约错误,而不是在校验器深处变成一个查不到的悬空引用。
HANDLE_PATTERN = r"^n[0-9]{1,5}$"

Handle = Annotated[str, Field(pattern=HANDLE_PATTERN)]


# ---------------------------------------------------------------------------------
# 动作
# ---------------------------------------------------------------------------------
class _ActionBase(ApiModel):
    """所有动作共有的字段。

    只有 `op`。放在基类里是为了让判别联合能统一取到它 —— 子类各自声明会更啰嗦,
    而漏声明一个就等于那个分支永远匹配不上。
    """

    op: str


class CreateNodeAction(_ActionBase):
    """新建一个节点。

    `parent_ref` 必须指向**已经存在**的节点,或在**本条之前**已经建立的本地编号。
    只允许向后引用这一条,让"父子关系成环"在结构上不可能发生 —— 不需要写一个
    环检测器去防它。
    """

    op: Literal["create_node"]
    local_id: Handle
    parent_ref: Handle
    title: str = Field(min_length=1, max_length=MAX_TITLE_CHARS)
    description: str | None = Field(default=None, max_length=MAX_TEXT_CHARS)
    acceptance_criteria: str | None = Field(default=None, max_length=MAX_ACCEPTANCE_CHARS)
    node_type: NodeType = NodeType.TASK
    estimate_minutes: int | None = Field(default=None, gt=0, le=MAX_ESTIMATE_MINUTES)
    deadline: date | None = None
    priority: Priority = Priority.MEDIUM


class UpdateNodeAction(_ActionBase):
    """修改一个已有节点。只写要改的字段。

    **刻意没有 `parent_ref`。** 重新挂父节点是最容易造出环的操作,而它在
    "AI 帮我排出阶段和任务"这件事里并不需要。不做它,`PARENT_CYCLE` 这个错误码
    在阶段 4 就不是"还没实现",而是"不可能发生" —— 后者是可以对用户讲的,
    前者只是又一个待办。
    """

    op: Literal["update_node"]
    target_ref: Handle
    title: str | None = Field(default=None, min_length=1, max_length=MAX_TITLE_CHARS)
    description: str | None = Field(default=None, max_length=MAX_TEXT_CHARS)
    acceptance_criteria: str | None = Field(default=None, max_length=MAX_ACCEPTANCE_CHARS)
    estimate_minutes: int | None = Field(default=None, gt=0, le=MAX_ESTIMATE_MINUTES)
    deadline: date | None = None
    priority: Priority | None = None
    status: NodeStatus | None = None


class DeleteNodeAction(_ActionBase):
    """删除一个节点**及其全部子节点**。

    子节点跟着一起走,不是"顺手"而是必须:计划里留一堆父节点已不存在的任务,
    它们会永远排在时间线上,而用户找不到任何入口去处理它们。
    """

    op: Literal["delete_node"]
    target_ref: Handle


class _DependencyActionBase(_ActionBase):
    predecessor_ref: Handle
    successor_ref: Handle


class CreateDependencyAction(_DependencyActionBase):
    op: Literal["create_dependency"]


class DeleteDependencyAction(_DependencyActionBase):
    op: Literal["delete_dependency"]


PlanAction = Annotated[
    CreateNodeAction
    | UpdateNodeAction
    | DeleteNodeAction
    | CreateDependencyAction
    | DeleteDependencyAction,
    Field(discriminator="op"),
]

#: 一次校验一个动作。判别联合必须走 TypeAdapter —— 直接 `PlanAction(...)` 拿到的是
#: `Annotated` 别名,不是可调用对象。
action_adapter: TypeAdapter[PlanAction] = TypeAdapter(PlanAction)

#: 阶段 4 能吃下的 op。**这是一个正面清单**:不在里面的 op 一律拒绝,
#: 包括枚举里存在但本阶段没实现的排期类操作(见 DEFERRED_OPS)。
ACCEPTED_OPS: frozenset[str] = frozenset(
    {
        ProposalOp.CREATE_NODE.value,
        ProposalOp.UPDATE_NODE.value,
        ProposalOp.DELETE_NODE.value,
        ProposalOp.CREATE_DEPENDENCY.value,
        ProposalOp.DELETE_DEPENDENCY.value,
    }
)

#: 枚举里有、但这个版本还做不到的 op。
#:
#: 它们依赖阶段 6 的排期算法(每日时间池、跨空间共享、长任务切分、最小改动重排)。
#: 把它们和"模型胡编的 op"分开报错,是因为两者对用户意味着完全不同的事:
#: 前者是"这个能力还没做",后者是"AI 这次输出了无效内容"。混成一个错误码,
#: 用户就永远无法判断该等一等还是该重试。
DEFERRED_OPS: frozenset[str] = frozenset(
    {
        ProposalOp.SCHEDULE_SESSIONS.value,
        ProposalOp.UNSCHEDULE_SESSIONS.value,
        ProposalOp.MOVE_SESSION.value,
        ProposalOp.LOCK_SESSION.value,
        ProposalOp.UPDATE_CAPACITY.value,
        ProposalOp.UPDATE_BRIEF.value,
    }
)


# ---------------------------------------------------------------------------------
# 校验错误与视图
# ---------------------------------------------------------------------------------
class ActionError(ApiModel):
    """一条变更被拒绝的原因。

    `ordinal` 指向提案里的第几条(从 1 开始),界面据此高亮到具体那一行。
    """

    ordinal: int
    code: str
    message: str


class ProposalItemView(ApiModel):
    """一条变更的展示形态。

    `summary` 是一句人能读的中文。**由服务端生成,不交给前端拼** —— 前端拼的时候
    必须自己判断"这条 create 的是什么类型、有没有截止时间",判断逻辑会分叉成好几处,
    而任何一处漏了都会让预览少显示一条变更。少显示一条变更的后果是:用户点确认时
    看到的是 5 条,实际写进去 6 条。
    """

    ordinal: int
    op: str
    summary: str
    local_id: str | None = None
    target_node_id: uuid.UUID | None = None
    target_title: str | None = None
    #: 将被写入的字段。界面可展开查看原始内容。
    payload: dict = Field(default_factory=dict)
    #: delete_node 会连带删掉的子节点数量。0 表示没有子树。
    affected_children: int = 0


class ProposalView(ApiModel):
    id: uuid.UUID
    status: str
    trigger_type: str
    base_revision_version: int
    reasoning: str | None = None
    change_summary: dict = Field(default_factory=dict)
    item_count: int = 0
    items: list[ProposalItemView] = Field(default_factory=list)
    created_at: datetime
    expires_at: datetime | None = None
    decided_at: datetime | None = None


class AppliedChangeView(ApiModel):
    """确认之后实际写进去了什么。**是查出来的,不是提案里声明的。**

    用户点确认之后看到"已应用 6 项变更",这个 6 如果来自提案本身,那它就只是一个
    复述 —— 万一写入中途只成功了一半,界面照样会说 6。所以这几个数字来自写入后
    重新查库的结果。
    """

    nodes_created: int = 0
    nodes_updated: int = 0
    nodes_deleted: int = 0
    dependencies_added: int = 0
    dependencies_removed: int = 0
    #: 这次写入**产生**的那一版(`plan_revisions` 里的那一行),不是写入之后空间的
    #: 当前版本号。两者恒差 1:空间起始于 1(还没有任何变更),第一次确认产生 V1,
    #: 之后 `/plan` 里的 `revisionVersion` 才是 2。
    #:
    #: **要"现在是多少版"就读 `/plan`**,不要读这个字段。写成注释而不是改名,是
    #: 因为线格式已经发出去了;但当初这里没有注释,验收脚本据此写出过一条错误的
    #: 断言(拿它和 `/plan` 的版本号比大小),所以把差异写在这里。
    revision_version: int = 0


class ConfirmProposalRequest(ApiModel):
    """确认请求。

    `idempotency_key` 由客户端生成,是"双击确认不会建两遍节点"的**唯一**依据。
    所以它必填 —— 让它是可选的、由服务端在缺失时补一个随机值,等于把这个保证
    悄悄关掉:用户双击两次会得到两个随机键,两条节点。
    """

    idempotency_key: str = Field(min_length=8, max_length=64)


class ConfirmProposalResponse(ApiModel):
    proposal: ProposalView
    applied: AppliedChangeView
    #: 这个幂等键之前已经处理过,返回的是当时存下来的结果,没有重新写入。
    replayed: bool = False


class RejectProposalRequest(ApiModel):
    """拒绝提案。理由是给人看的,不做校验之外的解释。"""

    reason: str | None = Field(default=None, max_length=500)


__all__ = [
    "ACCEPTED_OPS",
    "DEFERRED_OPS",
    "HANDLE_PATTERN",
    "MAX_ACTIONS",
    "MAX_CREATED_NODES",
    "MAX_ESTIMATE_MINUTES",
    "ActionError",
    "AppliedChangeView",
    "ConfirmProposalRequest",
    "ConfirmProposalResponse",
    "CreateDependencyAction",
    "CreateNodeAction",
    "DeleteDependencyAction",
    "DeleteNodeAction",
    "PlanAction",
    "ProposalItemView",
    "ProposalView",
    "RejectProposalRequest",
    "UpdateNodeAction",
    "action_adapter",
]
