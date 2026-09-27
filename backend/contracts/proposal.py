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
from backend.contracts.plan import MAX_DESCRIPTION_CODEPOINTS, MAX_NOTE_CODEPOINTS
from backend.db.models.enums import NodePurpose, NodeStatus, NodeType, Priority, ProposalOp

#: 一次最多接受多少条变更。与 agent/runtime/direct_llm.py 的 MAX_ACTIONS 对齐 ——
#: 两处不一致的话,agent 层放行的条数会在这里被整批拒绝,而用户看到的是
#: "模型这次的回答没能解析出结果",一个和他实际遇到的问题毫无关系的文案。
MAX_ACTIONS = 80

#: 一次最多新建多少个节点。比 MAX_ACTIONS 小:一份提案里绝大多数条目应该是依赖与更新,
#: 一次冒出 60 个新节点的计划,人是不可能一次看完并判断的。
MAX_CREATED_NODES = 60

MAX_ESTIMATE_MINUTES = 100_000
MAX_TITLE_CHARS = 200
MAX_ACCEPTANCE_CHARS = 1000

# 说明这个字段的上限**不在这里**,在 `backend/contracts/plan.py` 的
# `MAX_DESCRIPTION_CODEPOINTS`(300 码点)。原来这里有一个 `MAX_TEXT_CHARS = 2000`:
# 它和手工编辑那条路的检查是**两个数**,而 AI 提的说明和用户自己写的说明进的是
# 同一列。同一个字段两个上限,结果就是"AI 能写的长度,我存不回去" —— 用户在编辑器里
# 打开一段 AI 刚写的说明,一保存就被 400 拒掉,而界面上没有任何东西提示过这一点。
#
# 现在两条路读同一个常量。手工路径**还有一条豁免规则**(存量已经超长的说明继续
# 想写多长写多长),那是给"上限之前就存在的文字"留的出路;AI 这边没有豁免:
# 它写的是新内容,而且它有别的落点 —— 长文本走 `UpdateNoteAction`。

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
    description: str | None = Field(default=None, max_length=MAX_DESCRIPTION_CODEPOINTS)
    acceptance_criteria: str | None = Field(default=None, max_length=MAX_ACCEPTANCE_CHARS)
    node_type: NodeType = NodeType.TASK
    #: 用途轴。§2.5 的访谈共建就是靠它:用户答了"我排名 38"、模型提一条
    #: `purpose=information` 的节点把这件事记下来 —— 而它**不能带工时、不能带截止**
    #: (见 `information_node_conflicts`),也不进排期。
    #:
    #: 默认 `planning`:模型没提这个字段时,它的行为和加这一列之前完全一样。
    purpose: NodePurpose = NodePurpose.PLANNING
    estimate_minutes: int | None = Field(default=None, gt=0, le=MAX_ESTIMATE_MINUTES)
    deadline: date | None = None
    priority: Priority = Priority.MEDIUM


class UpdateNodeAction(_ActionBase):
    """修改一个已有节点。只写要改的字段。

    **刻意没有 `parent_ref`。** 重新挂父节点是最容易造出环的操作,而它在
    "AI 帮我排出阶段和任务"这件事里并不需要。不做它,`PARENT_CYCLE` 这个错误码
    在阶段 4 就不是"还没实现",而是"不可能发生" —— 后者是可以对用户讲的,
    前者只是又一个待办。

    **也没有 `purpose`。** 把一个节点从"行动"改成"主题"会连带决定它的工时与截止
    怎么办(见 `information_node_conflicts`),而那是一个用户该做、也该看清楚的
    决定。让模型顺手改掉它,用户看到的是"有个任务不见了",而它其实还在画布上。
    用户自己在详情里改,走的是 `PATCH /nodes/{id}`。
    """

    op: Literal["update_node"]
    target_ref: Handle
    title: str | None = Field(default=None, min_length=1, max_length=MAX_TITLE_CHARS)
    description: str | None = Field(default=None, max_length=MAX_DESCRIPTION_CODEPOINTS)
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


class UpdateNoteAction(_ActionBase):
    """把一段长正文写进某个节点的**笔记**(`node_notes`)。§2.2 与 §7。

    ## 为什么不塞进 `UpdateNodeAction`

    `UpdateNodeAction` 的"改了哪些字段"是靠 `plan_nodes` 的列判定的,而笔记根本
    不在那张表上;两边还各有一个自己的乐观锁版本号。硬塞进去的后果是"改笔记"和
    "改说明"在预览里长得一模一样,而它们在确认时该比两个不同的号。

    ## `expected_note_version` 是**服务端填的**,不是模型填的

    与 `content_version` 的纪律一致("模型不能自己设置版本列"):模型写什么,
    校验器都会在 `_update_note` 里用**此刻库里的那个号**覆盖掉它,然后原样存进
    提案的 payload。到确认那一步,重校验拿库里最新的号再比一次 —— 于是
    "生成提案之后、用户确认之前,这段笔记被人改过"会变成一条明确的错误,
    而不是一次安静的文字替换。

    模型自己填的话,一个编出来的数字会让整份提案被拒;而这个字段真正要回答的
    问题是"我这一份是照哪一版写的",那个号只有服务端知道。所以 **`consume` 在
    任何校验之前就把模型写的这个键摘掉**,校验器看到的永远是 `None`;真正被比的
    那一份是服务端上一次校验写进 payload 的。声明这个字段而不是让它变成"未知键",
    是为了不让一个手滑写了它的模型撞上 `extra="forbid"` —— 那一撞会连带丢掉整轮
    用户看得见的更新,而它想做的事一点没错。
    """

    op: Literal["update_note"]
    target_ref: Handle
    #: 长正文。上限在 `_update_note` 里用中文消息执行(见 `MAX_NOTE_CODEPOINTS`):
    #: 这里**不写 `max_length`**,因为 pydantic 那句
    #: `String should have at most 20000 characters` 会经 `_describe` 变成
    #: "长正文的长度不合适" —— 没有数字的提示,用户只能猜。
    body: str
    #: 服务端记的前置条件。见上面那段:模型写的那一份会被摘掉,这一条只声明形状。
    expected_note_version: int | None = None


PlanAction = Annotated[
    CreateNodeAction
    | UpdateNodeAction
    | DeleteNodeAction
    | CreateDependencyAction
    | DeleteDependencyAction
    | UpdateNoteAction,
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
        ProposalOp.UPDATE_NOTE.value,
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
    #: 这一条本来是"新建",被**合并**成了"补充已有节点"。§2.5/§4.4。
    #:
    #: 值是那句"合并到哪里去了"的标题(被指向的那个节点的标题,或同批更早那条
    #: 新建的标题)。为 `None` 表示这一条没有被改写。
    #:
    #: **它必须存在。** 悄悄把一条"新建"改成"更新"是违反 §7.2 的:用户看到的是
    #: "AI 给我加了一个新节点",实际发生的是"它改了我一个旧节点" —— 两者在画布上
    #: 的样子完全不同,而确认框是用户唯一能拦住它的地方。
    coalesced_from: str | None = None


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
    #: 被写进长正文(笔记)的节点数。**不是节点数之外的另一样东西** —— 一个节点
    #: 可能既在 `nodes_updated` 里、又在 `notes_updated` 里(改说明的同时补了笔记),
    #: 所以这两个数字不能相加。
    notes_updated: int = 0
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
    "UpdateNoteAction",
    "action_adapter",
]
